#!/usr/bin/env python3
"""孤儿 PR 自动收口 —— PR 开着却没有任何未结卡认领时，自动建一张收口卡交给 pm。

背景（2026-10-31）：11 张子卡全部 done，但 #50/#52/#49/#51 四个 PR 全开着、main 一行没动。
原因是「实现卡交付 PR」与「合并动作」之间没有归属：卡面写着「本卡不合并」，于是没人接。
旧的 pr_orphan_check.sh 只报告，报了三轮没人看 —— 报告不是机制。

本脚本把「报告」升级成「建卡」：
  · 只建卡，不合并：合并仍走 pm + merge_guard + R7 判档，不绕过任何规则。
  · 跳过证据/不合并类 PR（head 前缀 evidence/ 或标题含「证据 PR / 不合并」）。
  · 同一 PR 12 小时内只自动建一次（状态文件），避免合并失败后无限重开。
  · 每轮最多建 MAX_PER_RUN 张，避免刷板。

用法：pr_orphan_autoclose.py [--apply]   默认 dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys

DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
BOARD = "devteam"
REPO = "/home/liu/project/team-board"
HERMES = "/home/liu/.local/bin/hermes"
GH = "/home/liu/.local/bin/gh"
WS = "/home/liu/.hermes/profiles/paimeng/workspace"
LOG = f"{WS}/pr-orphan-autoclose.log"
STATE = f"{WS}/pr-orphan-autoclose-state.json"
DIGEST = f"{WS}/pr-orphan-autoclose-digest.md"
MAX_PER_RUN = 2
DEDUPE_HOURS = 12
SKIP_TITLE = ("证据 PR", "不合并")

BODY = """PR-CLAIM: #{n}

## 目标
合并 PR #{n}（{title}）进 main。唯一入口 `merge_guard --method merge --delete-branch`，合并后确认源分支已删。

## 为什么有这张卡
自动收口：PR 已开但没有任何未结卡认领它（{files} 个文件，head `{head}`）。建卡由 paimeng 的 `pr_orphan_autoclose.py` 完成，合并仍由 pm 判档执行。

## 要求
- 开工先 `gh pr checks {n}`；不是全绿就先补，别合
- 合并前重数 diff（`gh pr view --json files`）：涨过 10 文件或碰 CI/依赖/迁移/凭据 → R7 C 档，停下上报老板
- 分支落后就 rebase 到最新 main（冲突优先改 cli.py / README.md，改完 `ast.parse` 自检）
- 合并提交用 `merge-record` 生成三行 trailer（Merge-By / Merge-Card / Merge-PR）
- 合并后在 main 上跑 `python -m pytest -q`，贴 rc 与末行

## 边界
- 只合这一个 PR，不合别的
- 不动 .github/ 与依赖文件
"""


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{dt.datetime.now():%F %T} {msg}\n")


def run(cmd, timeout=60, cwd=REPO):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"exec failed: {exc}"


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_state(st: dict) -> None:
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = args.apply

    rc, out = run([GH, "pr", "list", "--state", "open", "--json",
                   "number,title,headRefName,headRefOid,changedFiles,isDraft"])
    if rc != 0:
        log(f"gh pr list failed rc={rc} {out.strip()[:150]}")
        print("gh failed:", out.strip()[:200])
        return 1
    prs = json.loads(out or "[]")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    st = load_state()
    now = dt.datetime.now()
    acted, skipped = [], []

    for pr in prs:
        n, title = pr["number"], pr.get("title") or ""
        head_ref = pr.get("headRefName") or ""
        if pr.get("isDraft"):
            skipped.append((n, "draft"))
            continue
        if head_ref.startswith("evidence/") or any(k in title for k in SKIP_TITLE):
            skipped.append((n, "证据/不合并类"))
            continue
        hit = con.execute(
            """select count(*) from tasks t where t.status not in ('done','archived')
                 and (t.body like ? or exists (select 1 from task_comments c
                      where c.task_id=t.id and c.body like ?))""",
            (f"%#{n}%", f"%#{n}%")).fetchone()[0]
        if hit:
            skipped.append((n, "已有未结卡认领"))
            continue
        prev = st.get(str(n))
        if prev:
            age_h = (now - dt.datetime.fromisoformat(prev["at"])).total_seconds() / 3600
            if age_h < DEDUPE_HOURS:
                skipped.append((n, f"{age_h:.1f}h 前已自动建过 {prev['task']}"))
                continue
        if len(acted) >= MAX_PER_RUN:
            skipped.append((n, "本轮配额已满"))
            continue
        body = BODY.format(n=n, title=title[:60], files=pr.get("changedFiles"), head=head_ref and pr.get("headRefOid", "")[:8])
        card_title = f"合并 PR #{n}（自动收口卡）"
        acted.append((n, card_title))
        if apply:
            rc2, out2 = run([HERMES, "kanban", "--board", BOARD, "create", card_title,
                             "--assignee", "pm", "--body", body], cwd="/home/liu")
            card_id = ""
            for tok in (out2 or "").split():
                if tok.startswith("t_"):
                    card_id = tok
            log(f"create for PR #{n} rc={rc2} card={card_id} {out2.strip()[:120]}")
            if rc2 == 0 and card_id:
                st[str(n)] = {"task": card_id, "at": now.isoformat(timespec="seconds")}
                save_state(st)
    con.close()

    lines = [f"# 孤儿 PR 自动收口 {now:%F %H:%M} ({'APPLY' if apply else 'DRY-RUN'})", ""]
    if acted:
        lines += ["## 建卡"] + [f"- PR #{n} → {t}" for n, t in acted] + [""]
    if skipped:
        lines += ["## 跳过"] + [f"- PR #{n}：{why}" for n, why in skipped] + [""]
    with open(DIGEST, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    for n, t in acted:
        print(("APPLY " if apply else "DRY   ") + f"PR #{n} -> {t}")
    for n, why in skipped:
        print(f"skip  PR #{n}: {why}")
    if not acted and not skipped:
        print("no open PRs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
