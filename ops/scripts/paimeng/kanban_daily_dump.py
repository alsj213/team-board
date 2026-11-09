#!/usr/bin/env python3
"""Dump devteam kanban board facts for the morning briefing cron.

Read-only. Prints compact markdown the cron agent turns into a digest.
Deterministic ordering; no wall-clock noise beyond derived ages.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_KANBAN_ROOT", "/home/liu/.hermes/kanban"))
NOW = time.time()

# Resolve the active board the same way the CLI does.
slug = os.environ.get("HERMES_KANBAN_BOARD") or ""
cur = ROOT / "current"
if not slug and cur.exists():
    slug = cur.read_text().strip()
slug = slug or "devteam"
db = ROOT / "boards" / slug / "kanban.db"


def age(ts):
    if not ts:
        return "—"
    d = NOW - float(ts)
    if d < 3600:
        return f"{int(d // 60)}分钟前"
    if d < 86400:
        return f"{d / 3600:.1f}小时前"
    return f"{d / 86400:.1f}天前"


def hfmt(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts))) if ts else "—"


def main():
    if not db.exists():
        print(f"BOARD_MISSING {db}")
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print(f"# board={slug} dump_at={hfmt(NOW)}")
    counts = con.execute(
        "select status, count(*) n from tasks group by status order by n desc"
    ).fetchall()
    print("counts: " + ", ".join(f"{r['status']}={r['n']}" for r in counts))

    open_rows = con.execute(
        """select id,title,assignee,status,priority,created_by,created_at,started_at,result
           from tasks where status not in ('done','archived')
           order by case status when 'blocked' then 0 when 'running' then 1
                     when 'review' then 2 when 'ready' then 3 else 4 end,
                    coalesce(priority,3), created_at"""
    ).fetchall()

    print(f"\n## OPEN ({len(open_rows)})")
    for r in open_rows:
        extra = ""
        if r["status"] == "running":
            hb = con.execute(
                """select last_heartbeat_at from task_runs where task_id=?
                   order by started_at desc limit 1""",
                (r["id"],),
            ).fetchone()
            hb_at = hb["last_heartbeat_at"] if hb else None
            stale = (NOW - float(hb_at)) / 60 if hb_at else None
            extra = f" last_heartbeat={age(hb_at)}" + (" ⚠️STALE" if stale and stale > 60 else "")
        run = con.execute(
            """select profile,status,outcome,started_at,ended_at,error,summary
               from task_runs where task_id=? order by started_at desc limit 1""",
            (r["id"],),
        ).fetchone()
        if run:
            extra += f" last_run={run['profile']}/{run['status']}/{run['outcome'] or '-'}"
            if run["error"]:
                extra += f" error={str(run['error'])[:160]}"
        print(
            f"- {r['id']} [{r['status']}] P{r['priority']} assignee={r['assignee'] or '-'} "
            f"created_by={r['created_by'] or '-'} age={age(r['created_at'])}{extra}\n  {r['title']}"
        )
        if r["status"] == "blocked" and r["result"]:
            print(f"  blocked_reason: {str(r['result'])[:300]}")
        if r["status"] == "blocked":
            # A blocked card's decision text usually lives in body, not result.
            # Keep it on one physical line so the OPEN block stays one entry per card.
            brow = con.execute(
                "select body from tasks where id=?", (r["id"],)
            ).fetchone()
            body = str(brow["body"] or "") if brow else ""
            body = " ⏎ ".join(body.splitlines())
            print(
                "  blocked_body: "
                + body[:2000]
                + ("…[truncated]" if len(body) > 2000 else "")
            )

    done24 = con.execute(
        """select id,title,assignee,completed_at from tasks
           where status='done' and completed_at is not null and completed_at > ?
           order by completed_at desc""",
        (NOW - 86400,),
    ).fetchall()
    print(f"\n## DONE_LAST_24H ({len(done24)})")
    for r in done24:
        print(f"- {r['id']} {r['assignee'] or '-'} @{hfmt(r['completed_at'])} {r['title']}")

    fails = con.execute(
        """select task_id,profile,status,outcome,error,ended_at from task_runs
           where ended_at > ?
             and (status in ('failed','crashed','error','aborted','timed_out','gave_up')
                  or outcome in ('crashed','timed_out','gave_up','failed','error'))
           order by ended_at desc limit 10""",
        (NOW - 86400,),
    ).fetchall()
    print(f"\n## FAILED_RUNS_LAST_24H ({len(fails)})")
    for r in fails:
        print(
            f"- {r['task_id']} {r['profile']} {r['status']}/{r['outcome'] or '-'} "
            f"@{hfmt(r['ended_at'])} {str(r['error'] or '')[:160]}"
        )

    # A worker that blocked itself is not a crash, but it IS a human action item.
    blocks = con.execute(
        """select task_id,profile,ended_at,summary from task_runs
           where ended_at > ? and outcome='blocked'
           order by ended_at desc limit 10""",
        (NOW - 86400,),
    ).fetchall()
    print(f"\n## BLOCKED_RUNS_LAST_24H ({len(blocks)})")
    for r in blocks:
        print(
            f"- {r['task_id']} {r['profile']} @{hfmt(r['ended_at'])} "
            f"{str(r['summary'] or '')[:1200]}"
        )

    subs = con.execute("select count(*) n from kanban_notify_subs").fetchone()["n"]
    unsub = con.execute(
        """select count(*) n from tasks t where t.status not in ('done','archived')
           and not exists (select 1 from kanban_notify_subs s where s.task_id=t.id)"""
    ).fetchone()["n"]
    print(f"\n## NOTIFY subs_total={subs} open_tasks_without_sub={unsub}")


if __name__ == "__main__":
    main()
