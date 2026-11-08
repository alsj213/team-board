"""把「合并卡因证据缺失被 block、底层证据已补齐」的卡自动解封。

E1 实测：qa 在 pm 写 tier 前 complete，合并卡的 evidence 闸门把卡 block；人工补
tier/qa 收口后，合并卡仍停在 blocked，需要再手工 unblock。本工具只处理这一种：

- blocked 卡的最近一条 ``blocked`` reason 明确是证据缺失（含 ``证据`` / ``tier`` /
  ``qa_pass`` / 四槽字样），且不含凭据/权限/人工；
- 卡正文能定位 ``PR-CLAIM: #N``、实现卡号 ``t_xxxxxxxx``、head（``@ <40hex>``）；
- 对实现卡跑 ``evidence.collect`` + ``evidence.judge``，四槽齐且绑当前 head → 才解封。

安全边界与 ``unblockmerged`` 一致：缺省 dry-run，``--apply`` 才调 hermes；
任一条件不满足就跳过，宁可不做。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 - 只起常量 argv 的 hermes 子进程
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import db, evidence, model, prclaim

__all__ = [
    "BOARD",
    "Candidate",
    "UnblockEvidenceError",
    "collect",
    "main",
    "unblock",
]

BOARD = "devteam"

#: 证据缺失型 block 的关键词；同时出现 PR 依赖语义时交给 unblockmerged，不抢。
EVIDENCE_HINT = re.compile(
    r"(证据|tier|qa_pass|review:defect|review:regression|四槽|evidence)",
    re.IGNORECASE,
)
PR_DEPENDENCY = re.compile(
    r"(未合|未合并|仍\s*OPEN|等.{0,16}合|合并后|PR\s*#\s*\d+.*(OPEN|MERGED))",
    re.IGNORECASE,
)
DENY = ("凭据", "密钥", "权限", "账号", "批准", "审批", "老板", "人工")

CARD_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
HEAD_RE = re.compile(r"@\s*([0-9a-f]{40})\b")
BLOCKED_KEYS = ("blocked",)


class UnblockEvidenceError(Exception):
    """执行失败（CLI 渲染成 rc1，不冒 traceback）。"""


class Runner(Protocol):
    """hermes 子进程适配点（单测注入假 runner）。"""

    def __call__(self, argv: Sequence[str], cwd: str) -> Any: ...


@dataclass(frozen=True)
class Candidate:
    """一张证据已补齐、可以解封的合并卡。"""

    card: str
    impl_card: str
    pr: int
    head: str
    verdict: str


def _default_runner(argv: Sequence[str], cwd: str) -> Any:
    return subprocess.run(  # nosec B603 - argv 是常量元组，不经 shell
        tuple(argv), cwd=cwd, capture_output=True, text=True, check=False
    )


def _call(
    runner: Runner | None, argv: Sequence[str], cwd: str = "."
) -> tuple[int, str, str]:
    proc = (runner or _default_runner)(argv, cwd)
    return (
        int(getattr(proc, "returncode", 1)),
        str(getattr(proc, "stdout", "") or ""),
        str(getattr(proc, "stderr", "") or ""),
    )


def _latest_block_reason(payload: str | None) -> str | None:
    try:
        data = json.loads(payload or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    return reason if isinstance(reason, str) and reason.strip() else None


def _is_evidence_block(reason: str) -> bool:
    if any(word in reason for word in DENY):
        return False
    if PR_DEPENDENCY.search(reason):
        return False
    return EVIDENCE_HINT.search(reason) is not None


def _impl_from_body(body: str) -> str | None:
    # 合并卡正文固定有「实现卡 t_xxxxxxxx」；标题也可能带（t_xxxxxxxx）。
    match = re.search(r"实现卡\s+(t_[0-9a-f]{8})", body)
    if match is not None:
        return match.group(1)
    match = CARD_RE.search(body)
    return match.group(0) if match else None


def _pr_from_body(body: str) -> int | None:
    try:
        return prclaim.first_claim_pr(body)
    except (TypeError, ValueError):
        return None


def _head_from_body(body: str) -> str | None:
    match = HEAD_RE.search(body)
    return match.group(1) if match else None


def collect(conn: Any) -> tuple[Candidate, ...]:
    """扫描 blocked 合并卡；只读，不改库。"""

    placeholders = ",".join("?" for _ in BLOCKED_KEYS)
    rows = conn.execute(
        "SELECT id, title, body FROM tasks WHERE status = 'blocked'"
    ).fetchall()
    out: list[Candidate] = []
    for card, title, body in rows:
        body = body or ""
        # 只处理合并卡；实现卡即使带 PR-CLAIM 也不在本工具的职责内。
        if not prclaim.needs_claim(title or ""):
            continue
        if _pr_from_body(body) is None:
            continue
        events = conn.execute(
            "SELECT payload FROM task_events "
            f"WHERE task_id = ? AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (card, *BLOCKED_KEYS),
        ).fetchall()
        reason = _latest_block_reason(events[0][0]) if events else None
        if reason is None or not _is_evidence_block(reason):
            continue
        impl = _impl_from_body(body)
        pr = _pr_from_body(body)
        head = _head_from_body(body)
        if impl is None or pr is None or head is None:
            continue
        # 实现卡必须仍然 done：reopen 后的 review 期间不能解封合并卡。
        impl_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (impl,)
        ).fetchone()
        if impl_row is None or impl_row[0] != "done":
            continue
        try:
            evs = evidence.collect(conn, impl)
            judgment = evidence.judge(evs, head, pr)
        except evidence.EvidenceReadError:
            continue
        if not judgment.ok:
            continue
        out.append(Candidate(
            card=card, impl_card=impl, pr=pr, head=head, verdict=judgment.verdict,
        ))
    return tuple(out)


def unblock(
    card: str,
    *,
    reason: str,
    board: str = BOARD,
    runner: Runner | None = None,
) -> None:
    argv = ("hermes", "kanban", "--board", board, "unblock", card, "--reason", reason)
    rc, out, err = _call(runner, argv)
    if rc != 0:
        raise UnblockEvidenceError(
            f"hermes kanban unblock {card} 失败（rc={rc}）：{(err or out).strip()}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m team_board.unblockevidence",
        description="解封「证据缺失已补齐」的 blocked 合并卡（缺省 dry-run）。",
    )
    parser.add_argument("--db", default=model.DEFAULT_DB, help="看板库路径")
    parser.add_argument("--board", default=BOARD, help=f"板名（默认 {BOARD}）")
    parser.add_argument("--apply", action="store_true", help="真解封；缺省只 dry-run")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几张（0=全部）")
    args = parser.parse_args(argv)

    if args.limit < 0:
        print("unblock-evidence: FAIL | --limit 不能是负数", file=sys.stderr)
        return 2

    conn = None
    try:
        conn = db.connect(args.db)
        candidates = collect(conn)
    except db.BoardError as exc:
        print(f"unblock-evidence: FAIL | {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()

    if not candidates:
        print("unblock-evidence: 0 张候选")
        return 0

    selected = candidates if args.limit == 0 else candidates[: args.limit]
    print(f"unblock-evidence: 扫描 {len(candidates)} 张候选")
    for candidate in selected:
        if not args.apply:
            print(
                f"{candidate.card} | {candidate.impl_card} 证据已齐"
                f"（{candidate.verdict}）→ would unblock（dry-run）"
            )
            continue
        try:
            unblock(
                candidate.card,
                reason=(
                    f"unblock-evidence: {candidate.impl_card} 的 {candidate.verdict} "
                    f"已补齐（PR #{candidate.pr}），自动解除证据等待"
                ),
                board=args.board,
            )
        except UnblockEvidenceError as exc:
            print(f"unblock-evidence: FAIL | {exc}", file=sys.stderr)
            return 1
        print(f"{candidate.card} | 证据已齐 → unblocked")
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
