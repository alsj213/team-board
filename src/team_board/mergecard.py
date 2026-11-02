"""合并卡生命周期：**实现卡 done 之后**才允许建收口/合并卡。

背景（2026-11-01/02 实测）：worker 在实现卡还 running 时就建合并卡，``kanban_unstick``
看到同一 PR 的认领仍归实现卡，把新合并卡判成“重复认领”归档，导致 PR 合不了、
还要人工重建。根因不是认领规则太严，而是**合并卡建得太早**。

本模块把建卡前置条件做成一个纯计划函数：

- 从 ``--impl-card``（或 ``wt/t_<id>`` 分支）取实现卡；
- 实现卡状态必须是 ``done``；
- PR 必须 OPEN；已有开放合并卡则幂等返回它，不重复建；
- 计划通过后才由适配层起 ``hermes kanban create`` 子进程。

``merge_guard`` / ``kanban_unstick`` 的判定不改；本模块只在“建卡”这一步把关。
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 - 只为起 hermes kanban create 子进程
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import prclaim

__all__ = [
    "BOARD",
    "CreateResult",
    "MergeCardError",
    "MergeCardPlan",
    "Runner",
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

_IMPL_DONE = "done"


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
) -> MergeCardPlan:
    """校验并构造合并卡计划；不满足前置条件抛 :class:`MergeCardError`。

    ``pending_rows`` 是 ``db.iter_pending_cards`` 的 ``(id,status,assignee,title,body)``；
    只用来发现“同一 PR 已有开放合并卡”（幂等）或“别的未结卡仍认领该 PR”（拒绝）。
    """

    if impl_status != _IMPL_DONE:
        raise MergeCardError(
            f"实现卡 {impl_card} 当前状态是 {impl_status or '空'}；"
            "只有 done 之后才能建合并卡（避免 kanban_unstick 判重复认领）"
        )
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
) -> str:
    """合并卡正文（首行必须顶格 PR-CLAIM，供认领闸门读）。"""

    subject = f"Merge PR #{pr}（{impl_card}）：{_title_suffix(impl_title)}"
    return f"""PR-CLAIM: #{pr}
TEMPLATE: card-template.md#v3

## 收口：合并 PR #{pr}（{impl_card}）

- PR https://github.com/{slug}/pull/{pr}（base=main，head={branch} @ {head_oid}）
- 实现卡 {impl_card} 已 done（{_title_suffix(impl_title)}）
- 合并：`team-board merge-record --card $HERMES_KANBAN_TASK --by repo/run$HERMES_KANBAN_RUN_ID --pr {pr} --branch {branch} --body-out <绝对路径>`，再 `merge_guard --pr {pr} --branch {branch} --method merge --delete-branch --card $HERMES_KANBAN_TASK --by repo/run$HERMES_KANBAN_RUN_ID --subject '{subject}' --body-file <绝对路径> --repo <repo>`
- 合并后 `git ls-remote --exit-code --refs origin refs/heads/{branch}` rc=2；`merge-audit --repo . --card $HERMES_KANBAN_TASK --require --rev origin/main` rc=0

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


def create_merge_card(
    plan: MergeCardPlan,
    *,
    body_path: str,
    runner: Runner = subprocess.run,  # type: ignore[assignment]
) -> CreateResult:
    """起 ``hermes kanban create``；不做重试，rc 交给调用方。"""

    argv = create_argv(plan, body_path)
    proc = runner(argv)
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
