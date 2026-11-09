#!/usr/bin/env python3
"""patrol_quick — 零 token 巡检快检：6 条 SQL，任一非空才输出（沉默即正常）。

定位于 2026-10-31 老板拍板 C2：ops 的 07:10 / 18:10 两次 agent 巡检降频后，
「能在 SQL 里判真假」的那部分由本件每小时 :40 顶上；判断类活（证据是否成立、
合并身份是否 repo、拆卡质量）仍归 01:10 / 12:10 的 agent 巡检与 merge-audit，不进本件。

六条（判据全部取自板上真实字段，不引用任何 CI）：
  P1 卡死  status='running' 且 last_heartbeat_at 距今 > 20 分钟
  P2 超时  status='running' 且 started_at 距今 > max_runtime_seconds（缺省 5400 秒）
  P3 跑挂  consecutive_failures >= 2，或该卡最近一条 run 的 outcome ∈
           crashed / timed_out / spawn_failed / failed / gave_up
  P4 卡住  status='blocked' 且最近一条 kind='blocked' 事件距今 > 2 小时
  P5 父卡  有子卡、子卡全部 done，父卡仍未结（not in done/archived）
  P6 盲跑  status='running' 且 15 分钟内没有 `[hb]` 评论（B1 心跳口径）

去重：同一张卡的同一类命中，6 小时内只报一次（状态文件 workspace/.patrol-alerts.json），
理由同 stall_alert.py —— 一张卡会连续几小时停在同一状态，不去重等于每小时刷屏。

用法：
  patrol_quick.py                     # 有命中打一段；无命中零输出（cron 沉默）
  patrol_quick.py --hits-out <文件>    # 命中文本另存一份（guard 包装投递用）
  patrol_quick.py --db <看板副本>      # 指向副本，本地验证命中用（缺省真板）
  patrol_quick.py --state <状态文件>    # 去重状态换路径（验证时不污染真状态）
  patrol_quick.py --no-dedupe         # 不看状态文件、不写状态文件（验证命中用）

退出码：0 = 跑完（有命中/无命中都是 0）；2 = 自身出错（写 stderr）。
出错必须非 0：guard_run.sh 会把 rc 记进 guard/patrol_quick.ok，guard_health.py 据此报红。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

BOARD_DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
STATE = "/home/liu/.hermes/profiles/ops/workspace/.patrol-alerts.json"
DEFAULT_MAX_RUNTIME_S = 5400
STUCK_MIN = 20
BLIND_MIN = 15
BLOCKED_STUCK_MIN = 120
DEDUPE_H = 6
BAD_OUTCOMES = ("crashed", "timed_out", "spawn_failed", "failed", "gave_up")

CHECKS = (
    ("P1 卡死", f"running 且心跳静默 > {STUCK_MIN} 分钟"),
    ("P2 超时", "running 且超过 max_runtime_seconds"),
    ("P3 跑挂", "consecutive_failures>=2 或最近一次 run 非正常结束"),
    ("P4 卡住", f"blocked 且 > {BLOCKED_STUCK_MIN} 分钟无人处置"),
    ("P5 父卡", "子卡全 done 而父卡未结"),
    ("P6 盲跑", f"running 且 {BLIND_MIN} 分钟内无 [hb]"),
)


def q_p1(con, now):
    rows = con.execute(
        "select id, assignee, title, "
        "       cast(? - coalesce(last_heartbeat_at, started_at, created_at) as integer) age "
        "from tasks where status='running' "
        "  and cast(? - coalesce(last_heartbeat_at, started_at, created_at) as integer) > ? "
        "order by age desc",
        (now, now, STUCK_MIN * 60),
    ).fetchall()
    return [(r[0], r[1], f"心跳静默 {r[3] // 60} 分钟 — {short(r[2])}") for r in rows]


def q_p2(con, now):
    rows = con.execute(
        "select id, assignee, title, started_at, "
        "       coalesce(max_runtime_seconds, ?) cap "
        "from tasks where status='running' and started_at is not null "
        "  and cast(? - started_at as integer) > coalesce(max_runtime_seconds, ?)",
        (DEFAULT_MAX_RUNTIME_S, now, DEFAULT_MAX_RUNTIME_S),
    ).fetchall()
    out = []
    for tid, who, title, started, cap in rows:
        over_min = (now - started - cap) // 60
        out.append((tid, who, f"已跑 {(now - started) // 60} 分钟，超上限 {cap // 60} 分钟 {over_min} — {short(title)}"))
    return out


def q_p3(con, now):
    rows = con.execute(
        "select t.id, t.assignee, t.title, t.consecutive_failures, "
        "       (select r.outcome from task_runs r where r.task_id=t.id "
        "          order by r.started_at desc, r.id desc limit 1) last_outcome "
        "from tasks t "
        "where t.status not in ('done','archived') "
        "  and (t.consecutive_failures >= 2 "
        "       or (select r.outcome from task_runs r where r.task_id=t.id "
        "             order by r.started_at desc, r.id desc limit 1) "
        "          in ('crashed','timed_out','spawn_failed','failed','gave_up'))"
    ).fetchall()
    return [
        (r[0], r[1], f"连败 {r[3]} / 最近 run outcome={r[4]} — {short(r[2])}")
        for r in rows
    ]


def q_p4(con, now):
    rows = con.execute(
        "select t.id, t.assignee, t.title, "
        "       cast(? - (select max(e.created_at) from task_events e "
        "                   where e.task_id=t.id and e.kind='blocked') as integer) age "
        "from tasks t where t.status='blocked'",
        (now,),
    ).fetchall()
    out = []
    for tid, who, title, age in rows:
        age = age or 0
        if age // 60 > BLOCKED_STUCK_MIN:
            out.append((tid, who, f"blocked {age // 60} 分钟无人处置 — {short(title)}"))
    return out


def q_p5(con, now):
    rows = con.execute(
        "select p.id, p.assignee, p.status, p.title, count(c.child_id) n "
        "from tasks p join task_links c on c.parent_id = p.id "
        "where p.status not in ('done','archived','triage') "
        "group by p.id having sum(case when (select status from tasks x "
        "   where x.id=c.child_id) in ('done','archived') then 1 else 0 end) = count(c.child_id) "
        "order by p.id",
    ).fetchall()
    return [
        (r[0], r[1], f"子卡 {r[4]} 张全 done，父卡仍 {r[2]} — {short(r[3])}")
        for r in rows
    ]


def q_p6(con, now):
    rows = con.execute(
        "select t.id, t.assignee, t.title, "
        "       (select max(c.created_at) from task_comments c "
        "          where c.task_id=t.id and c.body like '[hb]%') last_hb, "
        "       t.started_at "
        "from tasks t where t.status='running'",
    ).fetchall()
    out = []
    for tid, who, title, last_hb, started in rows:
        base = last_hb or started
        if base is None:
            out.append((tid, who, f"running 但没开始时间也没 [hb] — {short(title)}"))
            continue
        age = now - base
        if age > BLIND_MIN * 60:
            tag = "从未落 [hb]" if last_hb is None else f"最后一条 [hb] 在 {age // 60} 分钟前"
            out.append((tid, who, f"{tag} — {short(title)}"))
    return out


QUERIES = {"P1 卡死": q_p1, "P2 超时": q_p2, "P3 跑挂": q_p3,
           "P4 卡住": q_p4, "P5 父卡": q_p5, "P6 盲跑": q_p6}


def short(s: str | None, n: int = 40) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_state(path: str, st: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, path)


def guard_reds() -> list[str]:
    """A1 自检结果带进收件箱：guard_health.check() 判红的看护件，逐条返回。

    2026-11-01 加：guard_health 2026-10-31 19:10 起自锁连红 12 小时，收件箱里一行都没有，
    01:10 巡检从收件箱读不到 → 这里补一条通路（不重复 guard_health 的判据，只搬结论）。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import guard_health  # noqa: PLC0415
        return guard_health.check()
    except Exception as exc:  # noqa: BLE001
        return [f"guard_health 判不了: {type(exc).__name__}: {exc}"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=BOARD_DB)
    ap.add_argument("--hits-out", default=None)
    ap.add_argument("--state", default=STATE, help="去重状态文件（验证时指到临时路径）")
    ap.add_argument("--no-dedupe", action="store_true")
    args = ap.parse_args()

    now = int(dt.datetime.now().timestamp())
    try:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        st = {} if args.no_dedupe else load_state(args.state)
        fresh, repeated = [], 0
        for name, _desc in CHECKS:
            for tid, who, detail in QUERIES[name](con, now):
                key = f"{name}|{tid}"
                prev = st.get(key, 0)
                if not args.no_dedupe and prev and (now - prev) / 3600 < DEDUPE_H:
                    repeated += 1
                    continue
                st[key] = now
                fresh.append(f"[{name}] {tid} {(who or '-'):8} {detail}")
        for red in guard_reds():
            key = f"自检|{red.split(':')[0]}"
            prev = st.get(key, 0)
            if not args.no_dedupe and prev and (now - prev) / 3600 < DEDUPE_H:
                repeated += 1
                continue
            st[key] = now
            fresh.append(f"[自检红 A1] {red}")
        con.close()
        if not args.no_dedupe:
            stale = [k for k, v in st.items() if (now - v) / 3600 > 24 * 7]
            for k in stale:
                st.pop(k, None)
            save_state(args.state, st)
    except Exception as exc:  # noqa: BLE001
        print(f"patrol_quick 自身出错: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if not fresh:
        return 0

    ts = dt.datetime.now().strftime("%F %H:%M")
    head = f"巡检快检 {ts} 命中 {len(fresh)} 条"
    if repeated:
        head += f"（另有 {repeated} 条 {DEDUPE_H} 小时内已报过，略）"
    text = "\n".join(
        [head + "："]
        + [f"  {f}" for f in fresh]
        + ["处置：`python3 /home/liu/.hermes/profiles/ops/scripts/kanban_status.py` 看板情；"
           "能机器解的已由 unstick / selfheal 自动处理；判断类活留 01:10 / 12:10 巡检。"]
    )
    print(text)
    if args.hits_out:
        with open(args.hits_out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
