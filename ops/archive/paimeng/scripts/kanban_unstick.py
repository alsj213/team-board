#!/usr/bin/env python3
"""看板解卡（unstick）—— 机器能判的卡住，机器自己解。

背景：2026-10-30 一晚上两次因为「收口卡缺 PR-CLAIM」和「等已合并的 PR」把链冻住 2 小时，
都只能等编排方（人）在普通 shell 补。这个脚本把那两类判断做成无人值守动作。

规则（只做机器能判的，判不了只记账不动手）：
  R1 解封：blocked 卡的最近 blocked 理由里出现「等 PR #N / PR #N 合并」，且 gh 报 N 已 MERGED → unblock。
  R2 补行：ready/todo/scheduled 的收口卡（标题含 合并/收口），正文含唯一一个 PR #N 但无 PR-CLAIM → 顶格补 PR-CLAIM: #N。
  R3 记账：blocked 超过 60 分钟且不属于 R1 的卡，写进 digest 文件（不推送、不动卡）。

安全：只读打开 DB；从不碰 running 卡；多 PR 号的卡一律不动；每次动作留 comment；全部写日志。
用法：kanban_unstick.py [--apply]   （默认 dry-run）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys

import os as _os
DB = _os.environ.get("KANBAN_UNSTICK_DB") or "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
BOARD = "devteam"
REPO = "/home/liu/project/team-board"
HERMES = "/home/liu/.local/bin/hermes"
GH = "/home/liu/.local/bin/gh"
LOG = _os.environ.get("KANBAN_UNSTICK_LOG") or "/home/liu/.hermes/profiles/paimeng/workspace/kanban-unstick.log"
DIGEST = _os.environ.get("KANBAN_UNSTICK_DIGEST") or "/home/liu/.hermes/profiles/paimeng/workspace/kanban-unstick-digest.md"

PR_WAIT = re.compile(r"(?:等|wait(?:ing)? (?:for|on))\s*PR\s*#?(\d+)|PR\s*#(\d+)\s*(?:合并|merged)", re.I)
PR_ANY = re.compile(r"PR\s*#(\d+)", re.I)
MERGE_CARD = re.compile(r"合并|收口|merge", re.I)
STALE_MIN = 60


def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%F %T")
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{ts} {msg}\n")


def con_ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def run(cmd: list[str], timeout: int = 60, cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"exec failed: {exc}"


def pr_state(num: str) -> str:
    rc, out = run([GH, "pr", "view", num, "--json", "state"], timeout=45, cwd=REPO)
    if rc != 0:
        return f"ERR({out.strip()[:80]})"
    try:
        return json.loads(out).get("state", "UNKNOWN")
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def last_blocked_reason(con: sqlite3.Connection, tid: str) -> str:
    row = con.execute(
        "select payload from task_events where task_id=? and kind='blocked' order by created_at desc limit 1",
        (tid,)).fetchone()
    if not row or not row["payload"]:
        return ""
    try:
        return str(json.loads(row["payload"]).get("reason") or "")
    except Exception:  # noqa: BLE001
        return str(row["payload"])


def claim_holders(con: sqlite3.Connection) -> dict[str, str]:
    """未结卡的 PR-CLAIM 行：{pr 号: 卡号}"""
    holders: dict[str, str] = {}
    for row in con.execute(
            "select id, body from tasks where status not in ('done','archived')"):
        m = re.search(r"^\s*PR-CLAIM:\s*#(\d+)", row["body"] or "", re.M)
        if m:
            holders[m.group(1)] = row["id"]
    return holders


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的动手（默认 dry-run）")
    args = ap.parse_args()
    apply = args.apply
    con = con_ro()
    now = dt.datetime.now()
    holders = claim_holders(con)
    actions: list[str] = []
    stale: list[tuple[str, str, int]] = []

    rows = con.execute(
        "select id, title, status, body, created_at from tasks where status in ('blocked','ready','todo','scheduled')"
    ).fetchall()

    for row in rows:
        tid, title, status, body = row["id"], row["title"] or "", row["status"], row["body"] or ""
        age_min = int((now.timestamp() - (row["created_at"] or now.timestamp())) / 60)

        # --- R1：等已合并的 PR ---
        if status == "blocked":
            reason = last_blocked_reason(con, tid)
            m = PR_WAIT.search(reason)
            if m:
                num = m.group(1) or m.group(2)
                state = pr_state(num)
                if state == "MERGED":
                    msg = f"自动解封（kanban_unstick）：PR #{num} 已 MERGED，依赖已满足，继续本卡。"
                    actions.append(f"R1 unblock {tid} (PR #{num} MERGED) {title[:40]}")
                    if apply:
                        run([HERMES, "kanban", "--board", BOARD, "comment", tid, msg])
                        rc, out = run([HERMES, "kanban", "--board", BOARD, "unblock", tid, "--reason", msg])
                        log(f"R1 unblock {tid} rc={rc} {out.strip()[:120]}")
                else:
                    stale.append((tid, f"等 PR #{num}（{state}）", age_min))
            else:
                stale.append((tid, reason[:120] or "(无理由)", age_min))
            continue

        # --- R2：收口卡缺 PR-CLAIM ---
        if not MERGE_CARD.search(title):
            continue
        if re.search(r"^\s*PR-CLAIM:", body, re.M):
            continue
        prs = PR_ANY.findall(title + "\n" + body)
        nums = sorted(set(prs))
        if len(nums) != 1:
            if nums:
                stale.append((tid, f"收口卡含多个 PR 号 {nums}，需人判", age_min))
            continue
        num = nums[0]
        if num in holders:
            stale.append((tid, f"PR #{num} 已被 {holders[num]} 认领，需人判", age_min))
            continue
        new_body = f"PR-CLAIM: #{num}\n" + body
        if len(new_body.encode("utf-8")) > 2048:
            stale.append((tid, f"补 PR-CLAIM 后正文超 2048B（{len(new_body.encode())}B）", age_min))
            continue
        actions.append(f"R2 add-claim {tid} (PR #{num}) {title[:40]}")
        if apply:
            tmp = f"/tmp/unstick-{tid}.txt"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(new_body)
            rc, out = run([HERMES, "kanban", "--board", BOARD, "edit", tid, "--body", new_body])
            log(f"R2 claim {tid} rc={rc} {out.strip()[:120]}")
            run([HERMES, "kanban", "--board", BOARD, "comment", tid,
                 f"自动补写 PR-CLAIM: #{num}（kanban_unstick：收口卡正文缺认领行，merge_guard 会拦）"])
            holders[num] = tid

    # --- R3：digest ---
    if stale or actions:
        lines = [f"# unstick digest {now:%F %H:%M}  ({'APPLY' if apply else 'DRY-RUN'})", ""]
        if actions:
            lines += ["## 已解（机器判定可自动处理）"] + [f"- {a}" for a in actions] + [""]
        if stale:
            lines += ["## 需人判（脚本不动）"] + [
                f"- `{t}` {why}（建卡 {age} 分钟）" for t, why, age in stale] + [""]
        with open(DIGEST, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    for a in actions:
        print(("APPLY " if apply else "DRY   ") + a)
    for t, why, age in stale:
        print(f"DRY   stale {t}: {why} ({age}m)")
    if not actions and not stale:
        print("nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
