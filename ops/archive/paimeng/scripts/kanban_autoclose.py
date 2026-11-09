#!/usr/bin/env python3
"""kanban_autoclose — 补齐 dispatcher 的两个缺口。

1) 父卡自动关：父卡停在 scheduled/ready，而它的全部子卡都已 done（且父卡没有活跃 worker）
   → unblock + complete 关掉，并在卡上留一行说明。
   为什么需要：worker 被 scope 在"自己那张卡"，delegated child 又禁止 mutate 别人卡，
   所以没有任何 worker 能关自己的父卡（案例 t_e32041c1 / t_385db65f / t_6a97c910，三次）。

2) 滞留卡检测：ready/scheduled 超过阈值仍无人认领 → 在卡上留一条带 [stale-flag] 标记的
   评论（同一条只留一次），并把情况打到 stdout 供 cron 投递。
   为什么需要：dispatcher 的 respawn_guarded(active_pr) 会让卡静默卡住 8 小时，
   事件史里只有 guard 记录，没有任何告警（案例 t_385db65f）。

只做状态流转，不改验收语义、不改 verdict。默认 dry-run，加 --apply 才动手。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BOARD_ROOT = Path(os.environ.get("KANBAN_AUTOCLOSE_BOARD_ROOT") or
                  (Path.home() / ".hermes" / "kanban" / "boards"))
STALE_MARKER = "[stale-flag]"


def db_path(board: str) -> Path:
    return BOARD_ROOT / board / "kanban.db"


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def find_closable_parents(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """父卡在 scheduled/ready、无活跃 worker、且所有子卡都已 done。

    两种子卡来源都要认：
    1) task_links 里真的有 parent→child 链接（pm 用 parents=[...] 拆的卡）；
    2) 没有链接，只有 scheduled 事件的 reason 文本（形如
       "R2-WAIT-CHILDREN: t_xxx(说明) → t_yyy(说明)"）—— pm 实际经常这么干，
       关系只活在文字里，链接表是空的。这种情况按文本里出现的卡号判定，
       并且要求这些卡都在父卡之后创建、且都 done。
    """
    linked = con.execute(
        """
        SELECT p.id, p.status, p.assignee, p.title,
               COUNT(c.child_id)                        AS children,
               SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS done_children
        FROM tasks p
        JOIN task_links c        ON c.parent_id = p.id
        JOIN tasks t             ON t.id = c.child_id
        WHERE p.status IN ('scheduled', 'ready')
          AND (p.current_run_id IS NULL OR p.current_run_id = '')
        GROUP BY p.id
        HAVING done_children = children AND children > 0
        """
    ).fetchall()
    found = {r["id"]: r for r in linked}

    orphans = con.execute(
        """
        SELECT p.id, p.status, p.assignee, p.title, p.created_at
        FROM tasks p
        WHERE p.status IN ('scheduled', 'ready')
          AND (p.current_run_id IS NULL OR p.current_run_id = '')
          AND NOT EXISTS (SELECT 1 FROM task_links l WHERE l.parent_id = p.id)
        """
    ).fetchall()
    for p in orphans:
        row = con.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'scheduled' "
            "ORDER BY created_at DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        if not row or not row["payload"]:
            continue
        ids = sorted(set(re.findall(r"t_[0-9a-f]{8}", row["payload"])))
        ids = [i for i in ids if i != p["id"]]
        if not ids:
            continue
        rows = con.execute(
            "SELECT id, status, created_at FROM tasks WHERE id IN (%s)"
            % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
        if len(rows) != len(ids):          # 有卡号在执行库里找不到
            continue
        if any(r["status"] not in ("done", "archived") for r in rows):
            continue
        if any(r["created_at"] <= p["created_at"] for r in rows):  # 早于父卡的卡不算子卡
            continue
        found[p["id"]] = {
            "id": p["id"], "status": p["status"], "assignee": p["assignee"],
            "title": p["title"], "children": len(ids), "done_children": len(ids),
        }
    return list(found.values())


def find_stale(con: sqlite3.Connection, stale_seconds: int) -> list[sqlite3.Row]:
    cutoff = int(time.time()) - stale_seconds
    return con.execute(
        """
        SELECT id, status, assignee, title, created_at
        FROM tasks
        WHERE status IN ('ready', 'scheduled')
          AND created_at < ?
          AND (current_run_id IS NULL OR current_run_id = '')
        ORDER BY created_at
        """,
        (cutoff,),
    ).fetchall()


def already_flagged(con: sqlite3.Connection, task_id: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ? LIMIT 1",
        (task_id, f"%{STALE_MARKER}%"),
    ).fetchone()
    return row is not None


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="devteam")
    ap.add_argument("--apply", action="store_true", help="真的动手（默认只打印计划）")
    ap.add_argument("--stale-minutes", type=int, default=30)
    ap.add_argument("--max-per-run", type=int, default=5)
    args = ap.parse_args()

    path = db_path(args.board)
    if not path.exists():
        print(f"board 不存在: {path}")
        return 0

    con = connect(path)
    parents = find_closable_parents(con)[: args.max_per_run]
    stale = find_stale(con, args.stale_minutes * 60)

    lines: list[str] = []
    for p in parents:
        lines.append(f"AUTOCLOSE {p['id']} [{p['status']}] 子卡 {p['done_children']}/{p['children']} done :: {p['title'][:40]}")
    for s in stale:
        if already_flagged(con, s["id"]):
            continue
        age_min = int((time.time() - s["created_at"]) / 60)
        lines.append(f"STALE {s['id']} [{s['status']}] 已 {age_min} 分钟无人认领 :: {s['title'][:40]}")

    if not lines:
        return 0

    if not args.apply:
        print("[dry-run]")
        print("\n".join(lines))
        return 0

    for p in parents:
        rc1, out1 = run(["hermes", "kanban", "--board", args.board,
                         "unblock", p["id"], "--reason",
                         f"autoclose: 子卡 {p['done_children']}/{p['children']} 全 done"])
        rc2, out2 = run(["hermes", "kanban", "--board", args.board,
                         "complete", p["id"], "--result",
                         f"由 kanban_autoclose 代关：全部 {p['children']} 张子卡已 done。"
                         f"原因：worker 作用域限制，任何 worker 都无法关闭父卡。"])
        status = "OK" if rc1 == 0 and rc2 == 0 else f"FAILED(unblock={rc1},complete={rc2})"
        print(f"AUTOCLOSE {p['id']} {status}")

    for s in stale:
        if already_flagged(con, s["id"]):
            continue
        age_min = int((time.time() - s["created_at"]) / 60)
        run(["hermes", "kanban", "--board", args.board, "comment", s["id"],
             f"{STALE_MARKER} 已在 {s['status']} 停留 {age_min} 分钟无人认领，"
             f"可能是 respawn_guarded 静默挡派；请 ops 或老板确认是否人工处置。",
             "--author", "kanban_autoclose"])
        print(f"FLAGGED {s['id']} (stale {age_min}m)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
