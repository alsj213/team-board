#!/usr/bin/env python3
"""daily_metrics — 每天记一行「花了多少 / 卡正文多长」，用来验证降本有没有效果。

输出两段：
1. 昨天各 profile 的估算花费 + 调用次数（来自各 profile 的 session_model_usage）
2. 昨天新建卡的 body 字节数分布（来自看板 tasks 表）+ 超标卡清单

只读，不改任何东西。建议每天低峰跑一次，结果追加到 --out 指定的文件。
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import sqlite3
import statistics
from collections import defaultdict

PROFILE_GLOB = "/home/liu/.hermes/profiles/*/state.db"
BOARD_DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
BODY_CAP = 2048


def last_day_window() -> tuple[float, float, str]:
    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - dt.timedelta(days=1)
    return start.timestamp(), today.timestamp(), start.strftime("%Y-%m-%d")


def cost_section(t0: float, t1: float) -> list[str]:
    by = defaultdict(lambda: [0, 0.0])
    for db in glob.glob(PROFILE_GLOB):
        prof = db.split("/profiles/")[1].split("/")[0]
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = con.execute(
                "select coalesce(sum(api_call_count),0), coalesce(sum(estimated_cost_usd),0) "
                "from session_model_usage where last_seen >= ? and last_seen < ?",
                (t0, t1),
            ).fetchone()
        except sqlite3.Error:
            continue
        by[prof][0] += int(row[0] or 0)
        by[prof][1] += float(row[1] or 0)
    lines = ["花费（估算 USD / 调用次数）:"]
    total = 0.0
    for prof, (calls, cost) in sorted(by.items(), key=lambda kv: -kv[1][1]):
        if cost <= 0 and calls == 0:
            continue
        lines.append(f"  {prof:16} {cost:8.3f}  {calls:>6} 次")
        total += cost
    lines.append(f"  {'合计':16} {total:8.3f}")
    lines.append("  注：wsl-gp-agent / wsl-uumit-0706 是其他项目，仅列数字。")
    return lines


def body_section(t0: float, t1: float) -> list[str]:
    con = sqlite3.connect(f"file:{BOARD_DB}?mode=ro", uri=True)
    rows = con.execute(
        "select id, assignee, length(cast(body as blob)) b, substr(title,1,34) "
        "from tasks where created_at >= ? and created_at < ?",
        (int(t0), int(t1)),
    ).fetchall()
    lines = [f"卡正文（昨日新建 {len(rows)} 张，上限 {BODY_CAP}B）:"]
    if not rows:
        lines.append("  （昨天没有新建卡）")
        return lines
    sizes = [r[2] or 0 for r in rows]
    over = [r for r in rows if (r[2] or 0) > BODY_CAP]
    lines.append(
        f"  中位 {int(statistics.median(sizes))}B / 平均 {int(sum(sizes)/len(sizes))}B / 最大 {max(sizes)}B"
    )
    if over:
        lines.append(f"  超限 {len(over)} 张:")
        for r in over[:10]:
            lines.append(f"    {r[0]} {r[1]:8} {r[2]:>6}B  {r[3]}")
    else:
        lines.append("  无超限")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/home/liu/.hermes/profiles/paimeng/workspace/daily-metrics.log")
    args = ap.parse_args()

    t0, t1, day = last_day_window()
    out = [f"===== {day}"]
    out += cost_section(t0, t1)
    out += [""] + body_section(t0, t1)
    text = "\n".join(out)
    print(text)
    try:
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
    except OSError as exc:
        print(f"(写日志失败: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
