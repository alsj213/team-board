"""``merge-train``：把并行开发后的 PR 汇入改成**单条串行队列**。

设计边界（与 ``merge_guard`` 的分工）：

- train 只负责“谁先合、什么时候 update-branch、更新后内容有没有变、CI 是否全绿”；
- **不绕过** ``merge_guard``：认领唯一性、证据四槽、双问绑定、rebase 闸门、合并与删分支
  仍然全部由 ``merge_guard`` 判；
- train 不持有合并 token：``--by`` 必须是 ``repo/run<N>``，由 ``merge_guard`` 再校验；
- 失败只影响当前 PR（``bounced``），队列继续；只有 main 本身不健康 / 锁冲突才停整条。

每个 PR 的步骤：

1. **预检**：OPEN、非 draft、MERGEABLE、head/base 与队列一致、CI 全绿；
2. **证据**：按当前 head 算内容指纹（``reviewhead.fingerprint``），四槽绑 patch 判；
3. **更新**（可选）：base 不是 head 祖先时调用 GitHub ``update-branch``（不 force-push），
   轮询新 head；内容指纹必须不变，否则 bounce 回作者；
4. **合并**：调用 ``python -m team_board.merge_guard``，带 ``--delete-branch`` 和认账参数；
5. **记录**：``merged`` / ``bounced``，下一条继续。

``--apply`` 才写远端；缺省是 dry-run（只读预检 + 证据 + 打印计划）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import db, evidence, reviewhead
from .merges import MERGE_BY_RE
from .model import DEFAULT_DB

#: PR 号上限（与 ``evidence.PR_MAX`` / ``prclaim.MAX_PR`` 同口径）
PR_MAX = evidence.PR_MAX

#: 卡号形态（与 ``evidence.CARD_RE`` 同口径）
CARD_RE = evidence.CARD_RE

#: ``gh pr view`` 要的字段
PR_VIEW_FIELDS = (
    "number,state,isDraft,headRefName,baseRefName,headRefOid,"
    "mergeable,statusCheckRollup"
)

#: GitHub 检查项的“绿”结论；其它结论一律按红/未知处理，不猜
GREEN_CONCLUSIONS: frozenset[str] = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})

#: 未完成状态
PENDING_STATES: frozenset[str] = frozenset(
    {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
)

#: 明确失败结论
RED_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
)

#: 队列项允许的键（多键/缺键一律拒，避免拼错字段被静默忽略）
QUEUE_KEYS: frozenset[str] = frozenset(
    {"pr", "card", "branch", "by", "subject", "body_file", "base"}
)


class TrainError(Exception):
    """参数 / 环境错误（CLI 转 rc2；单个 PR 的业务失败用 ``bounced``，不抛这个）。"""


@dataclass(frozen=True)
class CommandResult:
    """一次外部命令的结果。"""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def command(self) -> str:
        return " ".join(self.argv)


@dataclass(frozen=True)
class QueueItem:
    """一条待汇入的 PR。"""

    pr: int
    card: str
    branch: str
    by: str
    base: str = "main"
    subject: str | None = None
    body_file: str | None = None

    @property
    def merge_subject(self) -> str:
        return self.subject or f"Merge PR #{self.pr}（{self.card}）"


@dataclass(frozen=True)
class TrainOptions:
    """一次 train 的全局参数。"""

    repo: str
    db_path: str = DEFAULT_DB
    remote: str = "origin"
    max_items: int = 5
    dry_run: bool = True
    update_branch: bool = False
    timeout: float = 900.0
    interval: float = 5.0
    body_dir: str | None = None


@dataclass(frozen=True)
class PrView:
    """``gh pr view`` 里本模块关心的字段。"""

    number: int
    state: str
    is_draft: bool
    head_ref: str
    base_ref: str
    head_oid: str
    mergeable: str
    checks: str


@dataclass(frozen=True)
class StepResult:
    """一条 PR 的处理结果。"""

    pr: int
    status: str  # merged | bounced | dry-run
    detail: str
    head: str = ""
    patch: str = ""

    def render(self) -> str:
        parts = [f"PR #{self.pr}", self.status]
        if self.head:
            parts.append(f"head={self.head[:12]}")
        if self.patch:
            parts.append(f"patch={self.patch[:12]}")
        return " | ".join(parts) + (f" | {self.detail}" if self.detail else "")


def load_queue(path: str | Path) -> list[QueueItem]:
    """读队列 JSON；格式错 / 字段多缺 / 重复 PR 一律 :class:`TrainError`。"""

    raw = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrainError(f"队列不是合法 JSON：{exc}") from exc
    if isinstance(payload, Mapping) and "queue" in payload:
        payload = payload["queue"]
    if not isinstance(payload, list) or not payload:
        raise TrainError("队列必须是非空 JSON 数组（或 {\"queue\": [...]}）")
    items = [_parse_item(row, index) for index, row in enumerate(payload)]
    seen_pr: set[int] = set()
    seen_card: set[str] = set()
    for item in items:
        if item.pr in seen_pr:
            raise TrainError(f"队列里 PR #{item.pr} 重复")
        if item.card in seen_card:
            raise TrainError(f"队列里卡 {item.card} 重复")
        seen_pr.add(item.pr)
        seen_card.add(item.card)
    return items


def _parse_item(row: object, index: int) -> QueueItem:
    if not isinstance(row, Mapping):
        raise TrainError(f"队列第 {index + 1} 项不是对象")
    keys = set(row)
    extra = keys - QUEUE_KEYS
    if extra:
        raise TrainError(f"队列第 {index + 1} 项有未知键：{sorted(extra)}")
    required = {"pr", "card", "branch", "by"}
    absent = required - keys
    if absent:
        raise TrainError(f"队列第 {index + 1} 项缺键：{sorted(absent)}")
    pr = row["pr"]
    if not isinstance(pr, int) or isinstance(pr, bool) or not (1 <= pr <= PR_MAX):
        raise TrainError(f"队列第 {index + 1} 项 pr 非法：{pr!r}")
    card = row["card"]
    if not isinstance(card, str) or CARD_RE.fullmatch(card) is None:
        raise TrainError(f"队列第 {index + 1} 项 card 非法：{card!r}")
    branch = row["branch"]
    if not isinstance(branch, str) or evidence.card_from_branch(branch) != card:
        raise TrainError(
            f"队列第 {index + 1} 项 branch 非法：{branch!r}"
            f"（必须是 wt/{card}）"
        )
    by = row["by"]
    if not isinstance(by, str) or MERGE_BY_RE.fullmatch(by) is None:
        raise TrainError(f"队列第 {index + 1} 项 by 非法：{by!r}（形如 repo/run200）")
    if not by.startswith("repo/"):
        raise TrainError(
            f"队列第 {index + 1} 项 by={by!r}：train 只接受 repo/run<N>，"
            "合并权归 repo（R15）"
        )
    base = row.get("base", "main")
    if not isinstance(base, str) or not base.strip() or base != base.strip():
        raise TrainError(f"队列第 {index + 1} 项 base 非法：{base!r}")
    subject = row.get("subject")
    if subject is not None and (not isinstance(subject, str) or not subject.strip()):
        raise TrainError(f"队列第 {index + 1} 项 subject 非法：{subject!r}")
    body_file = row.get("body_file")
    if body_file is not None and not isinstance(body_file, str):
        raise TrainError(f"队列第 {index + 1} 项 body_file 非法：{body_file!r}")
    return QueueItem(
        pr=pr,
        card=card,
        branch=branch,
        by=by,
        base=base,
        subject=subject,
        body_file=body_file,
    )


def checks_state(rollup: object) -> str:
    """把 ``statusCheckRollup`` 归一成 ``green`` / ``red`` / ``pending`` / ``none``。

    没有检查项不等于绿：``none`` 让 train bounce，交给人决定（与 ``gh pr checks`` 的
    ``NO_CHECKS`` 口径一致）。
    """

    if not isinstance(rollup, list) or not rollup:
        return "none"
    states: list[str] = []
    for item in rollup:
        if not isinstance(item, Mapping):
            return "red"
        conclusion = item.get("conclusion")
        state = item.get("state")
        status = item.get("status")
        value = conclusion or state or status
        if not isinstance(value, str):
            return "pending"
        value = value.upper()
        if value in RED_CONCLUSIONS:
            states.append("red")
        elif value in GREEN_CONCLUSIONS:
            states.append("green")
        elif value in PENDING_STATES or value in {"", "NONE"}:
            states.append("pending")
        else:
            # 未知结论（例如未来新增的 neutral 变体）一律 pending，不猜绿
            states.append("pending")
    if "red" in states:
        return "red"
    if "pending" in states:
        return "pending"
    return "green"


def parse_pr_view(payload: object, expected_pr: int) -> PrView:
    """校验 ``gh pr view`` 的 JSON；字段缺失 / 类型错一律 :class:`TrainError`。"""

    if not isinstance(payload, Mapping):
        raise TrainError(f"PR #{expected_pr} 的 gh 输出不是 JSON 对象")
    number = payload.get("number")
    if number != expected_pr:
        raise TrainError(f"gh 返回的 PR 号 {number!r} != 期望 {expected_pr}")
    state = payload.get("state")
    head_ref = payload.get("headRefName")
    base_ref = payload.get("baseRefName")
    head_oid = payload.get("headRefOid")
    mergeable = payload.get("mergeable")
    is_draft = payload.get("isDraft")
    if not all(isinstance(value, str) for value in (state, head_ref, base_ref, head_oid)):
        raise TrainError(f"PR #{expected_pr} 的 gh 输出缺字符串字段")
    if not isinstance(mergeable, str) or not isinstance(is_draft, bool):
        raise TrainError(f"PR #{expected_pr} 的 gh 输出缺 mergeable/isDraft")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head_oid) is None:
        raise TrainError(f"PR #{expected_pr} 的 headRefOid 非法：{head_oid!r}")
    return PrView(
        number=number,
        state=state,
        is_draft=is_draft,
        head_ref=head_ref,
        base_ref=base_ref,
        head_oid=head_oid,
        mergeable=mergeable.upper(),
        checks=checks_state(payload.get("statusCheckRollup")),
    )


# ---------------------------------------------------------------- 外部命令


def default_run(argv: Sequence[str], cwd: str) -> CommandResult:
    """默认执行器：argv 列表、shell=False、捕获 stdout/stderr。"""

    proc = subprocess.run(  # nosec B603 - argv 列表、shell=False
        list(argv), cwd=cwd, capture_output=True, text=True, check=False
    )
    return CommandResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def default_merge_run(
    argv: Sequence[str], cwd: str, env: Mapping[str, str]
) -> CommandResult:
    """``merge_guard`` 的默认执行器（需要注入 ``PYTHONPATH``）。"""

    proc = subprocess.run(  # nosec B603 - argv 列表、shell=False
        list(argv), cwd=cwd, capture_output=True, text=True, check=False, env=dict(env)
    )
    return CommandResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def repo_slug(repo: str, remote: str, run: Callable[[Sequence[str], str], CommandResult]) -> str:
    """从 ``git remote get-url <remote>`` 取 ``owner/repo``。"""

    proc = run(("git", "remote", "get-url", remote), repo)
    if proc.returncode != 0:
        raise TrainError(f"取 remote {remote} 失败：{proc.command}: {proc.stderr.strip()}")
    url = proc.stdout.strip()
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if match is None:
        raise TrainError(f"无法从 remote URL 解析 owner/repo：{url!r}")
    return f"{match.group(1)}/{match.group(2)}"


# ---------------------------------------------------------------- Trainer


class Trainer:
    """一次队列的执行器。外部命令 / 指纹 / 证据判定都可注入，便于单测。"""

    def __init__(
        self,
        opts: TrainOptions,
        *,
        run: Callable[[Sequence[str], str], CommandResult] = default_run,
        fingerprint: Callable[[str, str, str], reviewhead.Fingerprint] = reviewhead.fingerprint,
        judge: Callable[[str, int, str, str], evidence.Judgment] | None = None,
        merge_run: Callable[
            [Sequence[str], str, Mapping[str, str]], CommandResult
        ] = default_merge_run,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.opts = opts
        self._run = run
        self._fingerprint = fingerprint
        self._judge = judge or self._judge_from_db
        self._merge_run = merge_run
        self._sleep = sleep
        self._clock = clock
        self._slug: str | None = None

    # -- 基础 ---------------------------------------------------------

    def _run_ok(self, argv: Sequence[str], what: str) -> CommandResult:
        proc = self._run(argv, self.opts.repo)
        if proc.returncode != 0:
            raise TrainError(f"{what}失败（rc={proc.returncode}）：{proc.command}：{proc.stderr.strip()}")
        return proc

    def fetch(self) -> None:
        self._run_ok(("git", "fetch", self.opts.remote, "--prune"), "git fetch")

    def slug(self) -> str:
        if self._slug is None:
            self._slug = repo_slug(self.opts.repo, self.opts.remote, self._run)
        return self._slug

    def pr_view(self, pr: int) -> PrView:
        proc = self._run(
            ("gh", "pr", "view", str(pr), "-R", self.slug(), "--json", PR_VIEW_FIELDS),
            self.opts.repo,
        )
        if proc.returncode != 0:
            raise TrainError(f"读 PR #{pr} 失败：{proc.command}: {proc.stderr.strip()}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise TrainError(f"PR #{pr} 的 gh 输出不是 JSON：{exc}") from exc
        return parse_pr_view(payload, pr)

    def is_ancestor(self, base_ref: str, head: str) -> bool:
        proc = self._run(
            ("git", "merge-base", "--is-ancestor", base_ref, head), self.opts.repo
        )
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise TrainError(
            f"判定 {base_ref} 是否为 {head} 祖先失败：{proc.stderr.strip()}"
        )

    def patch(self, base: str, head: str) -> str:
        try:
            fp = self._fingerprint(self.opts.repo, f"{self.opts.remote}/{base}", head)
        except reviewhead.PatchIdError as exc:
            raise TrainError(f"计算内容指纹失败：{exc}") from exc
        if fp.kind != "patch" or fp.value is None:
            raise TrainError(f"内容指纹不可复用（kind={fp.kind}），不送进合并")
        return fp.value

    def _judge_from_db(
        self, card: str, pr: int, head: str, patch: str
    ) -> evidence.Judgment:
        conn = None
        try:
            conn = db.connect(self.opts.db_path)
            events = evidence.collect(conn, card)
            return evidence.judge(events, head, pr, patch=patch)
        except (db.BoardError, evidence.EvidenceReadError, OSError) as exc:
            raise TrainError(f"读证据失败（{self.opts.db_path}）：{exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    # -- 步骤 ---------------------------------------------------------

    def preflight(self, item: QueueItem) -> PrView:
        view = self.pr_view(item.pr)
        if view.state != "OPEN":
            raise TrainError(f"PR 状态是 {view.state}，只有 OPEN 能合")
        if view.is_draft:
            raise TrainError("PR 是 draft")
        if view.mergeable != "MERGEABLE":
            raise TrainError(f"mergeable={view.mergeable}，不送进合并")
        if view.head_ref != item.branch:
            raise TrainError(f"headRefName {view.head_ref!r} != 队列 branch {item.branch!r}")
        if view.base_ref != item.base:
            raise TrainError(f"baseRefName {view.base_ref!r} != 队列 base {item.base!r}")
        if view.checks == "red":
            raise TrainError("CI 有失败检查")
        if view.checks == "pending":
            raise TrainError("CI 仍在运行")
        if view.checks == "none":
            raise TrainError("PR 没有检查项（NO_CHECKS 不算绿）")
        return view

    def update(self, item: QueueItem, head: str) -> str:
        """base 不是 head 祖先 → GitHub update-branch，返回新 head。"""

        base_ref = f"{self.opts.remote}/{item.base}"
        if self.is_ancestor(base_ref, head):
            return head
        proc = self._run(
            (
                "gh",
                "api",
                "-X",
                "PUT",
                f"repos/{self.slug()}/pulls/{item.pr}/update-branch",
                "-f",
                f"expected_head_sha={head}",
            ),
            self.opts.repo,
        )
        if proc.returncode != 0:
            raise TrainError(
                f"update-branch 失败（rc={proc.returncode}）：{proc.stderr.strip()}；"
                "退回作者 rebase（train 不 force-push）"
            )
        deadline = self._clock() + self.opts.timeout
        while self._clock() < deadline:
            view = self.pr_view(item.pr)
            if view.head_oid != head:
                return view.head_oid
            self._sleep(self.opts.interval)
        raise TrainError("update-branch 后等待新 head 超时")

    def wait_checks(self, pr: int) -> PrView:
        deadline = self._clock() + self.opts.timeout
        while self._clock() < deadline:
            view = self.pr_view(pr)
            if view.checks == "green":
                return view
            if view.checks in ("red", "none"):
                raise TrainError(f"CI 状态 {view.checks}，不合并")
            self._sleep(self.opts.interval)
        raise TrainError("等待 CI 全绿超时")

    def body_file(self, item: QueueItem) -> str:
        if item.body_file is not None:
            path = Path(item.body_file)
            if not path.is_file():
                raise TrainError(f"body_file 不存在：{path}")
            return str(path)
        root = Path(self.opts.body_dir or (Path.home() / ".cache" / "team-board" / "merge-train"))
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"pr-{item.pr}.body"
        proc = self._run(
            (
                sys.executable,
                "-m",
                "team_board.cli",
                "merge-record",
                "--card",
                item.card,
                "--by",
                item.by,
                "--pr",
                str(item.pr),
                "--branch",
                item.branch,
                "--subject",
                item.merge_subject,
                "--body-out",
                str(path),
                "--repo",
                self.opts.repo,
            ),
            self.opts.repo,
        )
        if proc.returncode != 0:
            raise TrainError(
                f"生成认账 body 失败（rc={proc.returncode}）：{proc.stderr.strip()}"
            )
        return str(path)

    def merge(self, item: QueueItem, body_file: str) -> None:
        env = dict(os.environ)
        src = str(Path(self.opts.repo) / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        argv = (
            sys.executable,
            "-m",
            "team_board.merge_guard",
            "--pr",
            str(item.pr),
            "--branch",
            item.branch,
            "--delete-branch",
            "--repo",
            self.opts.repo,
            "--remote",
            self.opts.remote,
            "--db",
            self.opts.db_path,
            "--card",
            item.card,
            "--by",
            item.by,
            "--subject",
            item.merge_subject,
            "--body-file",
            body_file,
        )
        proc = self._merge_run(argv, self.opts.repo, env)
        if proc.returncode != 0:
            raise TrainError(
                f"merge_guard rc={proc.returncode}：{(proc.stderr or proc.stdout).strip()}"
            )

    def one(self, item: QueueItem) -> StepResult:
        view = self.preflight(item)
        head = view.head_oid
        patch = self.patch(item.base, head)
        judgment = self._judge(item.card, item.pr, head, patch)
        if not judgment.ok:
            raise TrainError(f"证据不通过：{judgment.verdict}；{judgment.detail}")

        needs_update = not self.is_ancestor(f"{self.opts.remote}/{item.base}", head)
        if self.opts.dry_run:
            action = "would update-branch + merge" if needs_update else "would merge"
            return StepResult(
                pr=item.pr, status="dry-run", detail=action, head=head, patch=patch
            )

        if needs_update:
            if not self.opts.update_branch:
                raise TrainError(
                    f"分支落后 {self.opts.remote}/{item.base}；加 --update-branch 或退回作者 rebase"
                )
            new_head = self.update(item, head)
            new_patch = self.patch(item.base, new_head)
            if new_patch != patch:
                raise TrainError(
                    f"update-branch 后内容变了（patch {patch[:12]} → {new_patch[:12]}），"
                    "退回作者重跑双问/证据"
                )
            head, patch = new_head, new_patch
            view = self.wait_checks(item.pr)
            head = view.head_oid

        body = self.body_file(item)
        self.merge(item, body)
        return StepResult(pr=item.pr, status="merged", detail="", head=head, patch=patch)


def run_train(
    items: Iterable[QueueItem],
    opts: TrainOptions,
    *,
    trainer: Trainer | None = None,
) -> list[StepResult]:
    """串行跑队列；单条失败记 ``bounced``，继续下一条。"""

    train = trainer or Trainer(opts)
    train.fetch()
    results: list[StepResult] = []
    for item in list(items)[: opts.max_items]:
        try:
            results.append(train.one(item))
        except TrainError as exc:
            results.append(StepResult(pr=item.pr, status="bounced", detail=str(exc)))
    return results
