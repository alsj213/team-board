#!/usr/bin/env python3
"""卡死告警：只在「同一张卡已经停滞超过 2 小时」时输出一行文字（由 cron 直接投递）。

沉默即正常：没有新告警就不打印任何东西，cron 不会发消息。
去重：同一张卡 6 小时内只告警一次（状态文件）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys

DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
STATE = "/home/liu/.hermes/profiles/paimeng/workspace/.stall-alerts.json"
SEVERE_MIN = 120
DEDUPE_H = 6


def load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save(st: dict) -> None:
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, STATE)


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    now = dt.datetime.now()
    rows = con.execute(
        "select id,title,status,assignee,started_at,created_at from tasks "
        "where status in ('running','blocked')").fetchall()
    st = load()
    alerts = []
    for r in rows:
        last = con.execute(
            "select max(created_at) from task_events where task_id=? and kind in "
            "('heartbeat','commented','blocked','spawned','claimed')", (r["id"],)).fetchone()[0]
        base = last or r["started_at"] or r["created_at"] or now.timestamp()
        age_min = (now.timestamp() - base) / 60
        if age_min < SEVERE_MIN:
            continue
        prev = st.get(r["id"], 0)
        if prev and (now.timestamp() - prev) / 3600 < DEDUPE_H:
            continue
        st[r["id"]] = now.timestamp()
        who = r["assignee"] or "-"
        alerts.append(f"⏳ {r['id']} [{r['status']}] {who} 已静 {age_min/60:.1f} 小时：{(r['title'] or '')[:44]}")
    con.close()
    save(st)
    if alerts:
        print("卡死告警（超 2 小时无心跳/无动作，已去重）：")
        for a in alerts:
            print(a)
        print("处理：`python3 ~/.hermes/profiles/paimeng/scripts/kanban_status.py` 看板情；能机器解的由 kanban_unstick 已自动处理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
