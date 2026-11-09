#!/usr/bin/env python3
"""回收「PR 已合并/关闭，但自动收口卡还开着」的卡，避免合并后白跑 bot。

只处理标题形如 ``复审/补证据/rebase/合并 PR #N（自动收口卡）`` 且状态为
ready/todo/scheduled/blocked 的卡；running 一律不碰（可能正在合并）。blocked 的
自动卡在 PR 已合并/关闭后同样过期，应回收；PR 状态取不到就跳过（fail-closed）。

用法：pr_merged_reaper.py [--apply] [--db PATH] [--gh PATH] [--hermes PATH]
默认 dry-run。cron 用 --apply，每 10 分钟一次。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import subprocess
import sys

DB = os.environ.get("REAPER_DB") or "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
GH = os.environ.get("REAPER_GH") or "/home/liu/.local/bin/gh"
HERMES = os.environ.get("REAPER_HERMES") or "/home/liu/.local/bin/hermes"
BOARD = "devteam"
LOG = "/home/liu/.hermes/profiles/ops/workspace/pr-merged-reaper.log"
TITLE_RE = re.compile(
    r"^(?:复审|补证据|rebase|合并)\s+PR\s+#([1-9][0-9]*).*（自动收口卡）$"
)
OPEN_STATUSES = ("ready", "todo", "scheduled", "blocked")


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%F %T} {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def run(argv: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"


def pr_state(pr: int) -> str | None:
    rc, out, _ = run([GH, "pr", "view", str(pr), "-R", "alsj213/team-board", "--json", "state", "--jq", ".state"])
    state = (out or "").strip().upper()
    if rc != 0 or state not in ("OPEN", "MERGED", "CLOSED"):
        return None
    return state


def main() -> int:
    global GH, HERMES
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--gh", default=GH)
    ap.add_argument("--hermes", default=HERMES)
    args = ap.parse_args()
    GH, HERMES = args.gh, args.hermes

    con = None
    try:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT id, status, title FROM tasks WHERE status IN (?, ?, ?, ?) "
            "AND title LIKE '%（自动收口卡）' ORDER BY id",
            OPEN_STATUSES,
        ).fetchall()
    except sqlite3.Error as exc:
        log(f"环境错误: 读库失败 {args.db}: {exc}")
        return 2
    finally:
        if con is not None:
            con.close()

    candidates = []
    for tid, status, title in rows:
        match = TITLE_RE.match(str(title or ""))
        if match:
            candidates.append((str(tid), str(status), int(match.group(1)), str(title)))

    archived, kept, unknown = [], [], []
    for tid, status, pr, title in candidates:
        state = pr_state(pr)
        if state is None:
            unknown.append((tid, pr))
            continue
        if state == "OPEN":
            kept.append((tid, pr))
            continue
        if not args.apply:
            archived.append((tid, pr, state))
            continue
        rc, out, err = run([HERMES, "kanban", "--board", BOARD, "archive", tid])
        if rc == 0:
            archived.append((tid, pr, state))
            log(f"archive {tid} PR #{pr} state={state}")
        else:
            unknown.append((tid, pr))
            log(f"archive FAILED {tid} PR #{pr} rc={rc} {err.strip()[:200]}")

    log(
        f"{'APPLY' if args.apply else 'DRY'} candidates={len(candidates)} "
        f"archived={len(archived)} kept={len(kept)} unknown={len(unknown)}"
    )
    for tid, pr, state in archived:
        print(f"{'APPLY' if args.apply else 'DRY'} {tid} <- PR #{pr} {state}")
    for tid, pr in unknown:
        print(f"SKIP {tid} <- PR #{pr} 状态判不了")
    return 1 if unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
