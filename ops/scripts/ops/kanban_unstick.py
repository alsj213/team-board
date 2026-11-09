#!/usr/bin/env python3
"""看板解卡（unstick）—— 机器能判的卡住，机器自己解。

背景：2026-10-30 一晚上两次因为「收口卡缺 PR-CLAIM」和「等已合并的 PR」把链冻住 2 小时，
都只能等编排方（人）在普通 shell 补。这个脚本把那两类判断做成无人值守动作。

规则（只做机器能判的，判不了只记账不动手）：
  R1 解封：blocked 卡的最近 blocked 理由里出现「等 PR #N / PR #N 合并」，且 gh 报 N 已 MERGED → unblock。
  R2 补行：ready/todo/scheduled 的收口卡（标题含 合并/收口），正文含唯一一个 PR #N 但无 PR-CLAIM → 顶格补 PR-CLAIM: #N。
  R3 重复认领：同一 PR 被 ≥2 张**合并语义**卡（标题命中 prclaim.needs_claim：合并卡/收口卡/合并 PR #n/收口：）
          顶格 PR-CLAIM 时，只写 digest「需人判」，**不 archive**。实现卡/验证卡正文首行的 PR-CLAIM
          是「我开的 PR」自认行，按 mergeowner 第 5 条「本卡自己认领不算」不构成归属（2026-11-03 t_d356f6fe：曾据此误归档合法收口卡 t_ec78eebc）。
  R4 解泊：scheduled 卡最近一条 scheduled 事件的 reason 里点到的卡号**全部 done** → 停泊前提已满足。
          正文首行是 PR-CLAIM: #n（合并/收口类）→ 不 unblock，digest 记 `R4 hold <卡号> 认领 #n` 交人处置
          （R15-B 合并权归 repo，unblock 会派 pm 去干它不该干的合并）；其余 → unblock，本轮最多 5 张。

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
LOG = _os.environ.get("KANBAN_UNSTICK_LOG") or "/home/liu/.hermes/profiles/ops/workspace/kanban-unstick.log"
DIGEST = _os.environ.get("KANBAN_UNSTICK_DIGEST") or "/home/liu/.hermes/profiles/ops/workspace/kanban-unstick-digest.md"

PR_WAIT = re.compile(r"(?:等|wait(?:ing)? (?:for|on))\s*PR\s*#?(\d+)|PR\s*#(\d+)\s*(?:合并|merged)", re.I)
PR_ANY = re.compile(r"PR\s*#(\d+)", re.I)
MERGE_CARD = re.compile(r"合并|收口|merge", re.I)
#: team_board 源码树：合并语义判据（prclaim.needs_claim）从这里 import —— 唯一判据，本地不抄第二份正则
SRC = _os.environ.get("KANBAN_UNSTICK_TB_SRC") or "/home/liu/project/team-board/src"
TASK_ID = re.compile(r"\bt_[0-9a-f]{8}\b")
PR_CLAIM_LINE = re.compile(r"^PR-CLAIM:\s*#(\d+)")
STALE_MIN = 60
MAX_R4_UNBLOCK = 5


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


def last_scheduled_reason(con: sqlite3.Connection, tid: str) -> str:
    row = con.execute(
        "select payload from task_events where task_id=? and kind='scheduled' "
        "order by created_at desc, id desc limit 1",
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


def load_needs_claim():
    """取团队唯一判据 ``team_board.prclaim.needs_claim``；取不到返回 ``None``（R3 整段跳过，绝不 archive）。

    不在本地抄一份「合并卡/收口卡/合并 PR #n/收口：」正则：抄一份就是第二份判据，两边必然漂。
    """
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    try:
        from team_board.prclaim import needs_claim  # noqa: PLC0415 —— 故意延后
    except Exception as exc:  # noqa: BLE001
        print(f"环境错误: 无法从 {SRC} 导入 team_board.prclaim.needs_claim：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None
    # 行为自检（不比对正则文本，只钉判据方向）：合并语义命中、实现卡不命中
    try:
        ok = (needs_claim("合并卡（pm）：PR #1")
              and needs_claim("收口：合并 PR #94（t_26084082）")
              and not needs_claim("脚本化流程预检：card-audit + handoff-check"))
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        print(f"环境错误: prclaim.needs_claim 行为自检不通过（{SRC}）", file=sys.stderr)
        return None
    return needs_claim


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的动手（默认 dry-run）")
    args = ap.parse_args()
    apply = args.apply
    needs_claim = load_needs_claim()
    con = con_ro()
    now = dt.datetime.now()
    holders = claim_holders(con)
    actions: list[str] = []
    holds: list[str] = []
    stale: list[tuple[str, str, int]] = []
    r4_unblocked = 0

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

        # --- R4：scheduled 卡的停泊前提已满足却无人解泊 ---
        if status == "scheduled":
            reason = last_scheduled_reason(con, tid)
            dep_ids = sorted(set(TASK_ID.findall(reason)))
            if dep_ids:
                marks = ",".join("?" * len(dep_ids))
                got = {r["id"]: r["status"] for r in con.execute(
                    f"select id, status from tasks where id in ({marks})", dep_ids)}
                # 全部 done 才算前提满足；板上不存在的卡号按未 done 处理（不猜）
                if all(got.get(i) == "done" for i in dep_ids):
                    first = (body.strip().splitlines() or [""])[0]
                    m_claim = PR_CLAIM_LINE.search(first)
                    if m_claim:
                        # 合并/收口类：R15-B 合并权归 repo，脚本不 unblock，交人处置
                        holds.append(f"R4 hold {tid} 认领 #{m_claim.group(1)}")
                        if apply:
                            log(f"R4 hold {tid} PR-CLAIM #{m_claim.group(1)} deps={dep_ids}")
                    elif r4_unblocked >= MAX_R4_UNBLOCK:
                        stale.append((tid, f"R4 前提已满足但本轮解泊已达 {MAX_R4_UNBLOCK} 张上限", age_min))
                    else:
                        why = f"R4: {', '.join(dep_ids)} 全 done"
                        actions.append(f"R4 unblock {tid} ({why}) {title[:40]}")
                        if apply:
                            cur = con.execute("select status from tasks where id=?", (tid,)).fetchone()
                            if not cur or cur["status"] != "scheduled":
                                log(f"R4 skip {tid} 重读 status={(cur['status'] if cur else 'MISSING')}，不动卡")
                                continue
                            rc, out = run([HERMES, "kanban", "--board", BOARD,
                                           "unblock", tid, "--reason", why])
                            log(f"R4 unblock {tid} rc={rc} {out.strip()[:120]}")
                        r4_unblocked += 1
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

    # --- R3：同一 PR 多张认领卡 —— 只有「合并语义」卡的认领才算归属 ---
    # 本条不 archive。若将来加回 archive：只能对「归属唯一」的卡动手，且必须先 comment 写清依据再 archive
    # （t_d356f6fe 卡面第 3 条保留要求；R24 ② 禁止对歧义动手）。
    if needs_claim is not None:
        by_pr: dict[str, list[tuple[str, str]]] = {}
        for row in con.execute(
                "select id, title, status, body from tasks where status not in ('done','archived')"):
            m = re.search(r"^\s*PR-CLAIM:\s*#(\d+)", row["body"] or "", re.M)
            if not m:
                continue
            if not needs_claim(row["title"] or ""):
                # 实现卡/验证卡正文首行的 PR-CLAIM 是「我开的 PR」自认行，按 mergeowner 第 5 条「本卡自己认领不算」不构成归属
                continue
            by_pr.setdefault(m.group(1), []).append((row["id"], row["status"]))
        for pr, cards in sorted(by_pr.items(), key=lambda kv: int(kv[0])):
            if len(cards) < 2:
                continue
            who = "、".join(f"{tid}（{st}）" for tid, st in cards)
            stale.append((cards[0][0],
                          f"PR #{pr} 有 {len(cards)} 张合并/收口类卡同时认领：{who}"
                          f"（R3 不再自动 archive，需人判）", 0))

    # --- R3：digest ---
    if stale or actions or holds:
        lines = [f"# unstick digest {now:%F %H:%M}  ({'APPLY' if apply else 'DRY-RUN'})", ""]
        if actions:
            lines += ["## 已解（机器判定可自动处理）"] + [f"- {a}" for a in actions] + [""]
        if holds:
            lines += ["## R4 hold（合并/收口类，R15-B 合并权归 repo，脚本不 unblock，交人处置）"] + [
                f"- {h}" for h in holds] + [""]
        if stale:
            lines += ["## 需人判（脚本不动）"] + [
                f"- `{t}` {why}（建卡 {age} 分钟）" for t, why, age in stale] + [""]
        with open(DIGEST, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    for a in actions:
        print(("APPLY " if apply else "DRY   ") + a)
    for h in holds:
        print(("APPLY " if apply else "DRY   ") + h)
    for t, why, age in stale:
        print(f"DRY   stale {t}: {why} ({age}m)")
    if not actions and not holds and not stale:
        print("nothing to do")
    if needs_claim is None:
        print("环境错误: R3 判据（team_board.prclaim.needs_claim）不可用，本轮未做重复认领判定、未 archive 任何卡",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
