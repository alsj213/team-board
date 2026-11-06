"""把「依赖 PR 合并」型 blocked 卡自动解封。

背景（2026-11-06 端到端验证）：E2 `t_75ed5294` 因「E1 未合 main：PR #108 仍 OPEN」
blocked；E1 合并后它仍停在 blocked，需要 ops 手动 unblock。本工具扫描 blocked 卡，
从最近一条 ``blocked`` 事件的 ``reason`` 里提取 ``PR #N``，只有**所有** cited PR
都已是 ``MERGED`` 才解封。

安全边界（宁可不做，不可错放）：

- reason 没有 ``PR #N`` / 没有合并依赖语义 / 含凭据、权限、批准、人工等词 → 跳过；
- 任一 cited PR 不是 ``MERGED``（OPEN / CLOSED / 取不到）→ 跳过；
- 缺省 dry-run，``--apply`` 才调 ``hermes kanban unblock``。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 - 只起常量 argv 的 gh/hermes 子进程
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import db, model

__all__ = [
    "BOARD",
    "Candidate",
    "UnblockMergedError",
    "collect",
    "extract_prs",
    "is_merge_dependency",
    "main",
]

#: 默认板与仓库（与其它 team-board 工具同口径）
BOARD = "devteam"
SLUG = "alsj213/team-board"

#: ``PR #108`` / ``PR#108`` / ``pr # 108``
PR_RE = re.compile(r"\bPR\s*#\s*([0-9]{1,6})\b", re.IGNORECASE)

#: 「等某个 PR 合入」的语义。没有这类词就不是依赖型 block，不碰。
MERGE_HINT = re.compile(
    r"(未合|未合并|仍\s*OPEN|等.{0,16}合|合入|合并后|merge[ds]?|MERGED|OPEN)",
    re.IGNORECASE,
)

#: 这些词说明卡在别的东西（凭据/权限/人工）上，即使提到 PR 也不自动解。
DENY = ("凭据", "密钥", "权限", "账号", "批准", "审批", "老板", "人工")


class UnblockMergedError(Exception):
    """执行失败（CLI 渲染成 rc1，不冒 traceback）。"""


class Runner(Protocol):
    """gh / hermes 子进程适配点（单测注入假 runner）。"""

    def __call__(self, argv: Sequence[str], cwd: str) -> Any: ...


@dataclass(frozen=True)
class Candidate:
    """一张 blocked 卡与它 cited 的 PR。"""

    card: str
    reason: str
    prs: tuple[int, ...]


def _default_runner(argv: Sequence[str], cwd: str) -> Any:
    return subprocess.run(  # nosec B603 - argv 是常量元组，不经 shell
        tuple(argv), cwd=cwd, capture_output=True, text=True, check=False
    )


def _call(runner: Runner | None, argv: Sequence[str], cwd: str = ".") -> tuple[int, str, str]:
    proc = (runner or _default_runner)(argv, cwd)
    return (
        int(getattr(proc, "returncode", 1)),
        str(getattr(proc, "stdout", "") or ""),
        str(getattr(proc, "stderr", "") or ""),
    )


def extract_prs(reason: str) -> tuple[int, ...]:
    """从 block reason 里提取 ``PR #N``；返回去重升序元组。"""

    return tuple(sorted({int(match.group(1)) for match in PR_RE.finditer(reason)}))


def is_merge_dependency(reason: str) -> bool:
    """reason 是否明确是「等 PR 合并」型 block。"""

    if not PR_RE.search(reason):
        return False
    if any(word in reason for word in DENY):
        return False
    return MERGE_HINT.search(reason) is not None


def _latest_block_reason(payload: str) -> str | None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    return reason if isinstance(reason, str) and reason.strip() else None


def collect(conn: Any) -> tuple[Candidate, ...]:
    """扫描 blocked 卡，返回候选（只读，不改库）。"""

    out: list[Candidate] = []
    for task in db.load_tasks(conn):
        if task.status != "blocked":
            continue
        events = db.iter_card_events(conn, task.id, ("blocked",))
        if not events:
            continue
        reason = _latest_block_reason(events[-1][1])
        if reason is None or not is_merge_dependency(reason):
            continue
        out.append(Candidate(card=task.id, reason=reason, prs=extract_prs(reason)))
    return tuple(out)


def pr_states(
    prs: Sequence[int], *, slug: str = SLUG, runner: Runner | None = None
) -> dict[int, str]:
    """逐个查 ``gh pr view <n> --json state``；取不到记 ``UNKNOWN``。"""

    states: dict[int, str] = {}
    for pr in prs:
        rc, out, _err = _call(
            runner, ("gh", "pr", "view", str(pr), "-R", slug, "--json", "state")
        )
        if rc != 0:
            states[pr] = "UNKNOWN"
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            states[pr] = "UNKNOWN"
            continue
        state = data.get("state") if isinstance(data, dict) else None
        states[pr] = state if isinstance(state, str) and state else "UNKNOWN"
    return states


def unblock(
    card: str,
    *,
    reason: str,
    board: str = BOARD,
    runner: Runner | None = None,
) -> None:
    """调 ``hermes kanban unblock``；rc≠0 抛错。"""

    argv = (
        "hermes",
        "kanban",
        "--board",
        board,
        "unblock",
        card,
        "--reason",
        reason,
    )
    rc, out, err = _call(runner, argv)
    if rc != 0:
        raise UnblockMergedError(
            f"hermes kanban unblock {card} 失败（rc={rc}）：{(err or out).strip()}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m team_board.unblockmerged",
        description="解封「cited PR 已全部 MERGED」的 blocked 卡（缺省 dry-run）。",
    )
    parser.add_argument("--db", default=model.DEFAULT_DB, help="看板库路径")
    parser.add_argument("--board", default=BOARD, help=f"板名（默认 {BOARD}）")
    parser.add_argument("--slug", default=SLUG, help=f"仓库（默认 {SLUG}）")
    parser.add_argument("--apply", action="store_true", help="真解封；缺省只 dry-run")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几张（0=全部）")
    args = parser.parse_args(argv)

    if args.limit < 0:
        print("unblock-merged: FAIL | --limit 不能是负数", file=sys.stderr)
        return 2

    conn = None
    try:
        conn = db.connect(args.db)
        candidates = collect(conn)
    except db.BoardError as exc:
        print(f"unblock-merged: FAIL | {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()

    if not candidates:
        print("unblock-merged: 0 张候选")
        return 0

    selected = candidates if args.limit == 0 else candidates[: args.limit]
    print(f"unblock-merged: 扫描 {len(candidates)} 张候选")
    for candidate in selected:
        states = pr_states(candidate.prs, slug=args.slug)
        if not all(state == "MERGED" for state in states.values()):
            detail = ", ".join(f"#{pr}={state}" for pr, state in states.items())
            print(f"{candidate.card} | skip（{detail}）")
            continue
        prs = ", ".join(f"#{pr}" for pr in candidate.prs)
        if not args.apply:
            print(f"{candidate.card} | {prs} MERGED → would unblock（dry-run）")
            continue
        try:
            unblock(
                candidate.card,
                reason=f"unblock-merged: {prs} 已 MERGED，自动解除依赖等待",
                board=args.board,
            )
        except UnblockMergedError as exc:
            print(f"unblock-merged: FAIL | {exc}", file=sys.stderr)
            return 1
        print(f"{candidate.card} | {prs} MERGED → unblocked")
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
