#!/usr/bin/env python3
"""daily_metrics — 每天记一行「花了多少 / 卡正文多长 / 盲跑率」，用来验证降本与心跳规则有没有效果。

输出三段：
1. 窗口内各 profile 的估算花费 + 调用次数（来自各 profile 的 session_model_usage）
2. 窗口内新建卡的 body 字节数分布（来自看板 tasks 表）+ 超标卡清单
3. 盲跑率（B1 口径：running 且 15 分钟内没有 `[hb]` 评论的卡 ÷ running 卡数，目标 <10%）
   + 窗口内「有 run 的卡落过 [hb] 的比例」（心跳覆盖率）

只读，不改任何东西。每天 05:05 跑一次（system crontab，guard_run 包装），
结果追加到 workspace/daily-metrics.log；`--weekly` 走 7 日窗口并覆盖写
workspace/weekly-report.md，供 ops 周一的周报取数。
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import sqlite3
import statistics
from collections import defaultdict

PROFILE_GLOB = "/home/liu/.hermes/profiles/*/state.db"
BOARD_DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
BODY_CAP = 2048
WORKSPACE = "/home/liu/.hermes/profiles/ops/workspace"
BLIND_MIN = 15          # B1 口径：running 且 15 分钟内没有 [hb] = 盲跑
BLIND_TARGET = 10.0     # 盲跑率目标 <10%


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
    lines = [f"卡正文（窗口内新建 {len(rows)} 张，上限 {BODY_CAP}B）:"]
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


def blind_section(t0: float, t1: float) -> list[str]:
    """盲跑率（B1 口径，2026-10-31 老板拍板）：running 且 15 分钟内没有 [hb] 评论。

    另报一个窗口内的「心跳覆盖率」（有 run 的卡里落过 [hb] 的比例），
    用来判断这条规则到底有没有被执行，而不只是看一个瞬时快照。
    """
    con = sqlite3.connect(f"file:{BOARD_DB}?mode=ro", uri=True)
    now = dt.datetime.now().timestamp()
    rows = con.execute("select id, assignee, started_at from tasks where status='running'").fetchall()
    blind = []
    for tid, who, started in rows:
        last = con.execute(
            "select max(created_at) from task_comments where task_id=? and body like '[hb]%'",
            (tid,),
        ).fetchone()[0]
        base = last or started
        if base is None or (now - base) > BLIND_MIN * 60:
            blind.append((tid, who))
    n = len(rows)
    rate = (len(blind) / n * 100) if n else 0.0
    week = con.execute(
        "select count(distinct task_id) from task_runs where started_at >= ? and started_at < ?",
        (int(t0), int(t1)),
    ).fetchone()[0]
    covered = con.execute(
        "select count(distinct r.task_id) from task_runs r where r.started_at >= ? and r.started_at < ? "
        "and exists(select 1 from task_comments c where c.task_id=r.task_id and c.body like '[hb]%')",
        (int(t0), int(t1)),
    ).fetchone()[0]
    con.close()
    lines = [f"盲跑率（B1 口径，目标 <{BLIND_TARGET:.0f}%）："]
    if n:
        verdict = "合格" if rate < BLIND_TARGET else "不合格"
        lines.append(f"  running {n} 张，其中 {BLIND_MIN} 分钟内无 [hb] {len(blind)} 张 → 盲跑率 {rate:.1f}%（{verdict}）")
        for tid, who in blind[:10]:
            lines.append(f"    {tid} {who or '-'}")
    else:
        lines.append(f"  当前没有 running 卡 → 盲跑率 0.0%（合格）")
    cov = (covered / week * 100) if week else 0.0
    lines.append(f"  窗口内有 run 的卡 {week} 张，落过 [hb] 的 {covered} 张 → 心跳覆盖率 {cov:.1f}%")
    return lines


def flow_section(t0: float, t1: float) -> list[str]:
    """窗口内的流程健康：开卡分布、blocked 年龄、run/guard/evidence 次数。

    只读；用来回答“bot 轮次是不是太多、阻塞有没有积压、证据有没有写”。
    """
    con = sqlite3.connect(f"file:{BOARD_DB}?mode=ro", uri=True)
    try:
        open_rows = con.execute(
            "select status, count(*) from tasks "
            "where status not in ('done','archived') group by status"
        ).fetchall()
        blocked = con.execute(
            "select t.id, t.assignee, coalesce(t.block_kind,'-'), "
            "       max(e.created_at) last_blocked, count(e.id) n "
            "  from tasks t left join task_events e "
            "    on e.task_id=t.id and e.kind='blocked' "
            " where t.status='blocked' "
            " group by t.id order by last_blocked asc"
        ).fetchall()
        runs_total, runs_cards = con.execute(
            "select count(*), count(distinct task_id) from task_runs "
            "where started_at >= ? and started_at < ?", (int(t0), int(t1))
        ).fetchone()
        top_runs = con.execute(
            "select task_id, count(*) n from task_runs "
            "where started_at >= ? and started_at < ? "
            "group by task_id order by n desc, task_id limit 5", (int(t0), int(t1))
        ).fetchall()
        now_ts = dt.datetime.now().timestamp()
        run_rows = con.execute(
            "select task_id, started_at, ended_at from task_runs "
            "where started_at >= ? and started_at < ?", (int(t0), int(t1))
        ).fetchall()
        card_seconds = defaultdict(int)
        card_runs = defaultdict(int)
        for tid, started, ended in run_rows:
            card_runs[tid] += 1
            end = now_ts if ended is None else ended
            card_seconds[tid] += max(0, end - started)
        top_cost = sorted(card_seconds, key=lambda k: (-card_seconds[k], k))[:5]
        guards = con.execute(
            "select count(*) from task_events where kind='respawn_guarded' "
            "and created_at >= ? and created_at < ?", (int(t0), int(t1))
        ).fetchone()[0]
        top_guards = con.execute(
            "select task_id, count(*) n from task_events where kind='respawn_guarded' "
            "and created_at >= ? and created_at < ? "
            "group by task_id order by n desc, task_id limit 5", (int(t0), int(t1))
        ).fetchall()
        evidence = con.execute(
            "select count(*) from task_events where kind in "
            "('spec_sign','impl_done','tier','qa_pass','review','boss_approve') "
            "and created_at >= ? and created_at < ?", (int(t0), int(t1))
        ).fetchone()[0]
        done = con.execute(
            "select count(*) from tasks where completed_at >= ? and completed_at < ?",
            (int(t0), int(t1)),
        ).fetchone()[0]
    finally:
        con.close()

    lines = ["流程（窗口内）:"]
    if open_rows:
        lines.append(
            "  开卡：" + " / ".join(f"{status} {n}" for status, n in open_rows)
        )
    else:
        lines.append("  开卡：无（全部 done/archived）")
    if blocked:
        now = dt.datetime.now().timestamp()
        oldest = blocked[0]
        age_h = (
            (now - oldest[3]) / 3600 if oldest[3] else float("nan")
        )
        kinds = {}
        for _tid, _who, kind, _last, _n in blocked:
            kinds[kind] = kinds.get(kind, 0) + 1
        lines.append(
            f"  blocked {len(blocked)} 张（"
            + " / ".join(f"{k} {v}" for k, v in sorted(kinds.items()))
            + f"）；最久 {age_h:.1f}h（{oldest[0]} {oldest[1] or '-'}）"
        )
    else:
        lines.append("  blocked 0 张")
    lines.append(f"  run {runs_total} 次 / {runs_cards} 张卡；窗口 done {done} 张")
    if top_runs:
        lines.append(
            "  单卡 run 最多："
            + " / ".join(f"{tid} {n}" for tid, n in top_runs)
        )
    if top_cost:
        lines.append(
            "  单卡成本最多："
            + " / ".join(
                f"{tid} {card_seconds[tid] // 60}m/{card_runs[tid]}次"
                for tid in top_cost
            )
        )
    lines.append(f"  guard（respawn_guarded）{guards} 次")
    if top_guards:
        lines.append(
            "  guard 最多："
            + " / ".join(f"{tid} {n}" for tid, n in top_guards)
        )
    lines.append(f"  evidence 事件 {evidence} 条")
    return lines


def soul_section() -> list[str]:
    """SOUL 体积：每个 worker run 都会加载，>20KB 视为描述膨胀。"""
    rows = []
    for path in glob.glob("/home/liu/.hermes/profiles/*/SOUL.md"):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        prof = path.split("/profiles/")[1].split("/")[0]
        rows.append((size, prof))
    rows.sort(reverse=True)
    lines = ["SOUL 体积（每 run 加载，>20KB 视为膨胀）:"]
    for size, prof in rows:
        flag = "  **超 20KB**" if size > 20 * 1024 else ""
        lines.append(f"  {prof:16} {size/1024:6.1f} KB{flag}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--weekly", action="store_true",
                    help="7 日窗口 + 盲跑率，写 workspace/weekly-report.md（ops 周报用）")
    args = ap.parse_args()

    if args.weekly:
        t1 = dt.datetime.now().timestamp()
        t0 = t1 - 7 * 86400
        day = (f"近 7 天（{dt.datetime.fromtimestamp(t0):%F} ~ "
               f"{dt.datetime.fromtimestamp(t1):%F}）")
        out_path = args.out or f"{WORKSPACE}/weekly-report.md"
    else:
        t0, t1, day = last_day_window()
        out_path = args.out or f"{WORKSPACE}/daily-metrics.log"

    out = [f"===== {day}"]
    out += cost_section(t0, t1)
    out += [""] + body_section(t0, t1)
    out += [""] + blind_section(t0, t1)
    out += [""] + flow_section(t0, t1)
    out += [""] + soul_section()
    text = "\n".join(out)
    print(text)
    try:
        with open(out_path, "a" if not args.weekly else "w", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
    except OSError as exc:
        print(f"(写日志失败: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
