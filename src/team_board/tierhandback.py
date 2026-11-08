"""PM 写完 tier 后把卡交回 qa 收口的 sweep。

背景：qa 收口现在有内核硬前置——卡上必须有合法的 pm ``tier`` 证据。qa 的出路是
``request_review(pm)``；pm 写完 tier 后如果只结束自己的 run、没有把卡交回 qa，
卡会停在 ``review / assignee=pm``。本 sweep 只处理这一种确定状态：

- ``status=review``、``assignee=pm``、``current_run_id IS NULL``；
- 正文有 ``QA-SPEC: qa``；
- 最近一条转换事件恰为 ``tier``（不是 tier 之后又被 qa 交回 pm 的 review_requested）；
- tier 事件的 run profile 是 ``pm``，payload 满足三键与取值域。

命中后只做一件事：``hermes kanban assign <card> qa``。不自动改状态、不自动写
tier、不碰 merge card；dry-run 是缺省。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess  # nosec B404 - 只起常量 argv 的 hermes 子进程
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import db, model

__all__ = [
    "BOARD",
    "Candidate",
    "TierHandbackError",
    "collect",
    "handoff",
    "main",
]

#: 默认板（与其它 team-board 工具同口径）
BOARD = "devteam"

#: qa 签署行（与内核 _QA_SPEC_SIGN_RE 同口径：只认整行）
QA_SPEC = "QA-SPEC: qa"

#: 判断「tier 之后有没有新的交接」只看这些事件；comment 等不参与。
TRANSITION_KINDS: tuple[str, ...] = (
    "tier",
    "review_requested",
    "changes_requested",
    "completed",
    "reopened",
)

#: tier evidence 的三键（与 team_board.evidence.REQUIRED_KEYS['tier'] 同口径）
TIER_KEYS = ("tier", "reason", "head_sha")
TIERS = frozenset({"A", "B", "C"})
HEAD_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$", re.ASCII)
REASON_MAX = 200


class TierHandbackError(Exception):
    """执行失败（CLI 渲染成 rc1，不冒 traceback）。"""


class Runner(Protocol):
    """hermes 子进程适配点（单测注入假 runner）。"""

    def __call__(self, argv: Sequence[str], cwd: str) -> Any: ...


@dataclass(frozen=True)
class Candidate:
    """一张停在 review/pm、tier 已写但没交回 qa 的卡。"""

    card: str
    tier: str


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


def _valid_tier_payload(raw: str | None) -> str | None:
    """合法 tier 事件返回档位；否则 ``None``（sweep 不猜）。"""

    try:
        payload = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if any(key not in payload for key in TIER_KEYS):
        return None
    if payload.get("tier") not in TIERS:
        return None
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX:
        return None
    if not isinstance(payload.get("head_sha"), str):
        return None
    if HEAD_RE.fullmatch(payload["head_sha"]) is None:
        return None
    return payload["tier"]


def collect(conn: sqlite3.Connection) -> tuple[Candidate, ...]:
    """扫描「review/pm + tier 已写 + 无 active run」的卡（只读）。"""

    placeholders = ",".join("?" for _ in TRANSITION_KINDS)
    rows = conn.execute(
        "SELECT id, body FROM tasks "
        " WHERE status = 'review' AND assignee = 'pm' AND current_run_id IS NULL"
    ).fetchall()
    out: list[Candidate] = []
    for card, body in rows:
        if QA_SPEC not in (body or ""):
            continue
        latest = conn.execute(
            "SELECT e.kind, e.payload, e.run_id, r.profile "
            "  FROM task_events e LEFT JOIN task_runs r ON r.id = e.run_id "
            f" WHERE e.task_id = ? AND e.kind IN ({placeholders}) "
            " ORDER BY e.id DESC LIMIT 1",
            (card, *TRANSITION_KINDS),
        ).fetchone()
        if latest is None or latest[0] != "tier":
            continue
        if latest[3] != "pm":
            continue
        tier = _valid_tier_payload(latest[1])
        if tier is None:
            continue
        out.append(Candidate(card=card, tier=tier))
    return tuple(out)


def handoff(
    card: str,
    *,
    board: str = BOARD,
    runner: Runner | None = None,
) -> None:
    """把卡改派给 qa；rc≠0 抛错。"""

    argv = ("hermes", "kanban", "--board", board, "assign", card, "qa")
    rc, out, err = _call(runner, argv)
    if rc != 0:
        raise TierHandbackError(
            f"hermes kanban assign {card} qa 失败（rc={rc}）：{(err or out).strip()}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m team_board.tierhandback",
        description="把「tier 已写但停在 review/pm」的卡交回 qa（缺省 dry-run）。",
    )
    parser.add_argument("--db", default=model.DEFAULT_DB, help="看板库路径")
    parser.add_argument("--board", default=BOARD, help=f"板名（默认 {BOARD}）")
    parser.add_argument("--apply", action="store_true", help="真改派；缺省只 dry-run")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几张（0=全部）")
    args = parser.parse_args(argv)

    if args.limit < 0:
        print("tier-handback: FAIL | --limit 不能是负数", file=sys.stderr)
        return 2

    conn = None
    try:
        conn = db.connect(args.db)
        candidates = collect(conn)
    except db.BoardError as exc:
        print(f"tier-handback: FAIL | {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"tier-handback: FAIL | {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()

    if not candidates:
        print("tier-handback: 0 张候选")
        return 0

    selected = candidates if args.limit == 0 else candidates[: args.limit]
    print(f"tier-handback: 扫描 {len(candidates)} 张候选")
    for candidate in selected:
        if not args.apply:
            print(f"{candidate.card} | tier={candidate.tier} → would assign qa（dry-run）")
            continue
        try:
            handoff(candidate.card, board=args.board)
        except TierHandbackError as exc:
            print(f"tier-handback: FAIL | {exc}", file=sys.stderr)
            return 1
        print(f"{candidate.card} | tier={candidate.tier} → assigned qa")
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
