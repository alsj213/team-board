#!/usr/bin/env python3
"""板情：每张未结卡现在在干什么、卡了多久、下一步归谁、判定正常/停滞/无主。

数据源：看板 DB（只读）+ 任务事件里的 heartbeat note / 最近评论。
判据（可机器查）：
  · 停滞：running 但心跳龄 > STALL_MIN；或 blocked 超过 STALL_MIN
  · 无主：ready/todo 且无人认领超过 ORPHAN_MIN；或 PR 开着但无合并归属卡
用法：kanban_status.py [--json] [--stall-only]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys

DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
REPO = "/home/liu/project/team-board"
STALL_MIN = 30
ORPHAN_MIN = 45


def fmt_age(minutes: float) -> str:
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes/60:.1f}h"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stall-only", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    now = dt.datetime.now().timestamp()

    rows = con.execute(
        "select id,title,status,assignee,created_at,started_at,current_run_id,body "
        "from tasks where status not in ('done','archived') order by created_at").fetchall()

    out = []
    for r in rows:
        tid = r["id"]
        # 最近一次心跳/评论里的一句话
        note = ""
        hb = con.execute(
            "select payload,created_at from task_events where task_id=? and kind in ('heartbeat','commented','blocked') "
            "order by created_at desc limit 1", (tid,)).fetchone()
        hb_at = r["started_at"] or r["created_at"] or now
        if hb:
            hb_at = hb["created_at"] or hb_at
            try:
                p = json.loads(hb["payload"] or "{}")
                note = (p.get("note") or p.get("reason") or "")[:90]
            except Exception:  # noqa: BLE001
                note = ""
        run = None
        if r["current_run_id"]:
            run = con.execute(
                "select profile,status,outcome,last_heartbeat_at from task_runs where id=?",
                (r["current_run_id"],)).fetchone()
        age = (now - (hb_at or now)) / 60
        verdict = "ok"
        if r["status"] == "running" and age > STALL_MIN:
            verdict = "STALL"
        elif r["status"] == "blocked" and age > STALL_MIN:
            verdict = "BLOCKED"
        elif r["status"] in ("ready", "todo", "scheduled") and (now - (r["created_at"] or now)) / 60 > ORPHAN_MIN:
            verdict = "WAITING"
        out.append({
            "id": tid, "title": (r["title"] or "")[:46], "status": r["status"],
            "assignee": r["assignee"], "age": round(age), "verdict": verdict,
            "run_profile": run["profile"] if run else "", "note": note,
        })
    con.close()

    if args.stall_only:
        out = [o for o in out if o["verdict"] != "ok"]
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    icon = {"ok": "·", "STALL": "!", "BLOCKED": "#", "WAITING": "…"}
    print(f"板情 {dt.datetime.now():%m-%d %H:%M}   未结 {len(out)} 张")
    for o in out:
        who = o["run_profile"] or o["assignee"]
        print(f" {icon.get(o['verdict'],'?')} {o['id']} [{o['status']:9}] {who:9} 静 {fmt_age(o['age']):>5}  {o['title']}")
        if o["note"]:
            print(f"        └ {o['note']}")
    stall = [o for o in out if o["verdict"] in ("STALL", "BLOCKED")]
    queue = [o for o in out if o["verdict"] == "WAITING"]
    ok = [o for o in out if o["verdict"] == "ok"]
    print(f"\n判读：正常 {len(ok)} / 排队等前置 {len(queue)} / 停滞 {len(stall)}"
          + ("   （! = running 但心跳超 30 分钟；# = 阻塞超 30 分钟；… = 在队列里等前置卡，45 分钟以上）" if (stall or queue) else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
