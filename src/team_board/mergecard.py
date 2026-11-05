"""合并卡生命周期：**实现卡 done 之后**才允许建收口/合并卡。

背景（2026-11-01/02 实测）：worker 在实现卡还 running 时就建合并卡，``kanban_unstick``
看到同一 PR 的认领仍归实现卡，把新合并卡判成“重复认领”归档，导致 PR 合不了、
还要人工重建。根因不是认领规则太严，而是**合并卡建得太早**。

本模块把建卡前置条件做成一个纯计划函数：

- 从 ``--impl-card``（或 ``wt/t_<id>`` 分支）取实现卡；
- 实现卡状态必须是 ``done``（:func:`check_impl_done`，CLI 在调 ``gh`` **之前**先跑它，
  否则卡没 done 时用户看到的是 gh 的网络报错而不是这条前置条件）；
- PR 必须 OPEN；已有开放合并卡则幂等返回它，不重复建；
- 计划通过后才由适配层起 ``hermes kanban create`` 子进程。

卡正文里的合并命令必须是**可照抄执行**的形态：``cd <repo> && PYTHONPATH=<repo>/src
python3 -m team_board.merge_guard …``（与 ``merge-record`` 打印的那一行同形）。裸
``merge_guard …`` 在本仓不存在这个可执行文件，repo 照抄只会得到 rc=127。

``merge_guard`` / ``kanban_unstick`` 的判定不改；本模块只在“建卡”这一步把关。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess  # nosec B404 - 只为起 hermes kanban create 子进程
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import prclaim

__all__ = [
    "BOARD",
    "IMPL_DONE",
    "CreateResult",
    "MergeCardError",
    "MergeCardPlan",
    "Runner",
    "check_impl_done",
    "create_argv",
    "create_merge_card",
    "impl_from_branch",
    "parse_created_id",
    "plan",
]

#: 建卡落哪个板（与 evidence_cards 同一契约）
BOARD = "devteam"

#: ``wt/t_<8hex>`` 或裸卡号
_BRANCH_RE = re.compile(r"^(?:wt/)?(t_[0-9a-f]{8})$", re.ASCII)
_CREATED_RE = re.compile(r"\bt_[0-9a-f]{8}\b")

#: 实现卡必须处于的状态（``check_impl_done`` 与 ``plan`` 的唯一判据）
IMPL_DONE = "done"
_IMPL_DONE = IMPL_DONE


class MergeCardError(Exception):
    """建卡前置条件不满足（CLI 渲染成 rc1，不冒 traceback）。"""


class Runner(Protocol):
    """子进程适配点（默认 :func:`subprocess.run`；单测注入假 runner）。"""

    def __call__(self, argv: Sequence[str]) -> Any: ...


@dataclass(frozen=True)
class MergeCardPlan:
    """一张待建合并卡的完整计划；``existing`` 非空表示库里已有开放合并卡。"""

    impl_card: str
    pr: int
    title: str
    body: str
    idempotency_key: str
    existing: str | None = None

    @property
    def should_create(self) -> bool:
        return self.existing is None


@dataclass(frozen=True)
class CreateResult:
    """建卡子进程的结果。"""

    plan: MergeCardPlan
    rc: int
    card_id: str | None
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0 and self.card_id is not None


def impl_from_branch(branch: str) -> str:
    """``wt/t_abcd1234`` / ``t_abcd1234`` → ``t_abcd1234``；其它形态抛错。"""

    match = _BRANCH_RE.fullmatch(branch.strip())
    if match is None:
        raise MergeCardError(
            f"分支 {branch!r} 不是卡分支（应为 wt/t_<8hex> 或 t_<8hex>）"
        )
    return match.group(1)


def check_impl_done(impl_card: str, impl_status: str) -> None:
    """实现卡没 done 就抛 :class:`MergeCardError`；CLI 在调 ``gh`` 之前先跑这一条。"""

    if impl_status != _IMPL_DONE:
        raise MergeCardError(
            f"实现卡 {impl_card} 当前状态是 {impl_status or '空'}；"
            "只有 done 之后才能建合并卡（避免 kanban_unstick 判重复认领）"
        )


def _title_suffix(impl_title: str) -> str:
    cleaned = " ".join(impl_title.split())
    return cleaned[:60] if cleaned else "实现卡"


def plan(
    *,
    impl_card: str,
    impl_status: str,
    impl_title: str,
    pr: int,
    pr_state: str,
    head_oid: str,
    slug: str,
    pending_rows: Sequence[tuple[str, str, str, str, str]],
    branch: str | None = None,
    title: str | None = None,
    repo: str | None = None,
) -> MergeCardPlan:
    """校验并构造合并卡计划；不满足前置条件抛 :class:`MergeCardError`。

    ``pending_rows`` 是 ``db.iter_pending_cards`` 的 ``(id,status,assignee,title,body)``；
    只用来发现“同一 PR 已有开放合并卡”（幂等）或“别的未结卡仍认领该 PR”（拒绝）。

    ``repo`` 是 ``--repo`` 的路径，只用来把正文里的合并命令拼成**可执行**的绝对路径
    （``cd <repo> && PYTHONPATH=<repo>/src python3 -m team_board.merge_guard …``）；
    缺省取当前目录的 realpath。
    """

    check_impl_done(impl_card, impl_status)
    if pr <= 0:
        raise MergeCardError(f"PR 号非法: {pr}")
    state = (pr_state or "").strip().upper()
    if state != "OPEN":
        raise MergeCardError(f"PR #{pr} 状态是 {state or '空'}；只有 OPEN 才能建合并卡")
    if re.fullmatch(r"[0-9a-f]{40}", head_oid or "", re.ASCII) is None:
        raise MergeCardError(f"PR #{pr} 的 head 不是 40 位小写十六进制: {head_oid!r}")

    existing: str | None = None
    for card_id, _status, _assignee, card_title, body in pending_rows:
        if card_id == impl_card:
            continue
        if not prclaim.claims(body, pr):
            continue
        if prclaim.needs_claim(card_title):
            existing = card_id
            break
        raise MergeCardError(
            f"PR #{pr} 仍被未结卡 {card_id}（{card_title or '无标题'}）认领；"
            "先让该卡 done 或归档，再建合并卡"
        )

    if existing is not None:
        return MergeCardPlan(
            impl_card=impl_card,
            pr=pr,
            title=title or "",
            body="",
            idempotency_key=f"merge-card-{impl_card}-{pr}",
            existing=existing,
        )

    branch_name = branch or f"wt/{impl_card}"
    if _BRANCH_RE.fullmatch(branch_name.strip()) is None:
        raise MergeCardError(f"分支 {branch_name!r} 不是卡分支")
    card_title = title or f"收口：合并 PR #{pr}（{impl_card} {_title_suffix(impl_title)}）"
    body = _render_body(
        impl_card=impl_card,
        impl_title=impl_title,
        pr=pr,
        head_oid=head_oid,
        slug=slug,
        branch=branch_name,
        repo=repo,
    )
    return MergeCardPlan(
        impl_card=impl_card,
        pr=pr,
        title=card_title,
        body=body,
        idempotency_key=f"merge-card-{impl_card}-{pr}",
    )


def _render_body(
    *,
    impl_card: str,
    impl_title: str,
    pr: int,
    head_oid: str,
    slug: str,
    branch: str,
    repo: str | None = None,
) -> str:
    """合并卡正文（首行必须顶格 PR-CLAIM，供认领闸门读）。

    两条命令都必须**照抄即能跑**：本仓没有 ``merge_guard`` 这个可执行文件，裸写
    ``merge_guard …`` 是 rc=127。形态与 ``merge-record`` 打印的那一行完全一致 ——
    ``cd <repo> && PYTHONPATH=<repo>/src python3 -m team_board.merge_guard …``，
    ``--body-file`` 恒为绝对路径（``merge_guard`` 拒相对路径）。路径与标题一律过
    :func:`shlex.quote`（仓库路径含空格、标题含 ``'`` ``$`` 时照抄仍是一条命令）。

    合并命令前面串一步 ``reviewmarkers``：``review-markers`` required check 在 PR
    首次 push 时必然失败（qa 的双问评论不会触发 workflow），qa PASS 后需要定点重跑
    那个 job 才会变绿；否则 ``merge_guard`` 的 CI 全绿闸门会死锁（2026-11-06 实测）。
    """

    subject = f"Merge PR #{pr}（{impl_card}）：{_title_suffix(impl_title)}"
    repo_q = shlex.quote(os.path.realpath(repo if repo else "."))
    src_q = shlex.quote(os.path.realpath(repo if repo else ".") + "/src")
    body_file = os.path.join(tempfile.gettempdir(), f"{impl_card}-merge-{pr}.body")
    record = (
        f"cd {repo_q} && PYTHONPATH={src_q} python3 -m team_board.cli merge-record"
        f" --card $HERMES_KANBAN_TASK --by repo/run$HERMES_KANBAN_RUN_ID"
        f" --pr {pr} --branch {branch} --body-out {body_file}"
    )
    merge = (
        f"cd {repo_q} && PYTHONPATH={src_q} python3 -m team_board.reviewmarkers"
        f" --pr {pr} --wait 240"
        f" && PYTHONPATH={src_q} python3 -m team_board.merge_guard"
        f" --pr {pr} --branch {branch} --method merge --delete-branch"
        " --card $HERMES_KANBAN_TASK --by repo/run$HERMES_KANBAN_RUN_ID"
        f" --subject {shlex.quote(subject)} --body-file {body_file} --repo {repo_q}"
        " --broadcast-tip"
    )
    return f"""PR-CLAIM: #{pr}
TEMPLATE: card-template.md#v3

## 收口：合并 PR #{pr}（{impl_card}）

- PR https://github.com/{slug}/pull/{pr}（base=main，head={branch} @ {head_oid}）
- 实现卡 {impl_card} 已 done（{_title_suffix(impl_title)}）
- 第 1 步 生成认账 body（同时打印第 2 步的命令）：

```
{record}
```

- 第 2 步 刷新 required check 并合并（先定点重跑 review-markers，再走 R12 唯一入口；缺 --delete-branch 会被拒）：

```
{merge}
```

- 合并后 `git ls-remote --exit-code --refs origin refs/heads/{branch}` rc=2

不做：不改代码、不改验收。
"""


def create_argv(
    plan: MergeCardPlan, body_path: str, *, board: str = BOARD
) -> tuple[str, ...]:
    """建合并卡的 argv（唯一形态；``--json`` 便于解析新卡号）。"""

    return (
        "hermes",
        "kanban",
        "--board",
        board,
        "create",
        plan.title,
        "--assignee",
        "repo",
        "--parent",
        plan.impl_card,
        "--body-file",
        body_path,
        "--idempotency-key",
        plan.idempotency_key,
        "--json",
    )


def parse_created_id(stdout: str) -> str | None:
    """从 ``hermes create --json`` 输出解析新卡号；解析不了回退正则。"""

    text = stdout.strip()
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            card_id = payload.get("id") or payload.get("task_id")
            if isinstance(card_id, str) and _CREATED_RE.fullmatch(card_id):
                return card_id
    match = _CREATED_RE.search(text)
    return match.group(0) if match else None


def _default_runner(argv: Sequence[str]) -> Any:
    """默认执行器：捕获 stdout/stderr，``--json`` 才能被解析（2026-11-03 dogfood）。"""

    return subprocess.run(  # nosec B603 - argv 是常量元组，不经 shell
        tuple(argv), capture_output=True, text=True, check=False
    )


def create_merge_card(
    plan: MergeCardPlan,
    *,
    body_path: str,
    runner: Runner | None = None,
) -> CreateResult:
    """起 ``hermes kanban create``；不做重试，rc 交给调用方。"""

    argv = create_argv(plan, body_path)
    proc = (runner or _default_runner)(argv)
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    rc = int(getattr(proc, "returncode", 1))
    return CreateResult(
        plan=plan,
        rc=rc,
        card_id=parse_created_id(stdout) if rc == 0 else None,
        stdout=stdout,
        stderr=stderr,
    )
