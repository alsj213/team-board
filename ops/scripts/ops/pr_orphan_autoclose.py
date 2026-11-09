#!/usr/bin/env python3
"""孤儿 PR 自动收口 —— PR 开着却没有任何未结卡认领时，按「三项前置」决定建哪一种卡。

背景（2026-10-31，卡 t_b79ce810 / t_290ef106）：
  · 旧版发现孤儿 PR 一律建「合并 PR #n（自动收口卡）」，卡面只有 PR-CLAIM 与通用要求。
    repo 门房的三条判据（卡面有 `[QA] PASS` + pm 的 R7 档位 / 双问绑定当前 head /
    分支不落后 main）一条都不满足 → 一次造出 6 张注定 blocked 的合并卡
    （#56/#57/#58/#59/#60/#61），「合并无主」被换成了「合并排队等人工搬证据」。
  · 现在：建卡前先判三项，依次「双问 → 落后 → 证据」，命中哪一项就建**那一种**动作卡
    （复审卡 pm / rebase 卡 engineer / 补证据卡 pm），三项齐全才建合并卡（repo）。
    「判不了」一律当不满足：不建卡，digest 报给人（fail-closed）。

判据只从团队自己的程序取，不复制第二份逻辑（R11 ③ 同口径）：
  · 认领唯一性  → ``team_board.prclaim.audit``（未结卡正文顶格 `PR-CLAIM: #<n>`）
  · 双问新鲜度  → ``team_board.reviewhead.analyze``（REVIEW-HEAD 是否绑定当前 headRefOid）
  · 分支落后    → ``team_board.merge_guard.rebase_fetch_argv`` / ``rebase_merge_base_argv``
                  / ``classify_merge_base_rc``（与 merge_guard 合并前那道闸门同一份判定）
  · 验收证据    → 板上「认领该 PR 的 done 卡」里的 qa PASS run 与 pm 的 R7 档位结论

建出来的每张卡都带顶格 `PR-CLAIM: #<n>`：既占住认领（下一轮不会再给同一 PR 重复开卡），
也让合并卡只在所有动作卡关闭后才会被建出来。

用法：pr_orphan_autoclose.py [--apply] [--pr N ...] [--db PATH] [--max-cards N]
      默认 dry-run；--db/--pr 只用于在板库副本上做验收演练。
退出码：0 = 正常（含本轮无事可做 / 只写 digest）；1 = gh 或建卡失败；
        2 = 环境错误（import / 版本钉 / 读库），digest 首行 `环境错误: …`，
        此时一张卡都不建 —— fail-closed，绝不回退子串判据。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
BOARD = "devteam"
#: 主 checkout：判「分支落后」时在这里 fetch。演练可覆盖（cron 不设 = 用真仓库）
REPO = os.environ.get("PR_ORPHAN_REPO") or "/home/liu/project/team-board"
HERMES = "/home/liu/.local/bin/hermes"
#: gh 可执行文件：演练可覆盖成假 gh（cron 不设 = 用真 gh）
GH = os.environ.get("PR_ORPHAN_GH") or "/home/liu/.local/bin/gh"
REMOTE = "origin"
WS = "/home/liu/.hermes/profiles/ops/workspace"
LOG = f"{WS}/pr-orphan-autoclose.log"
STATE = f"{WS}/pr-orphan-autoclose-state.json"
DIGEST = f"{WS}/pr-orphan-autoclose-digest.md"
#: dry-run 专用：把「本轮会建哪些卡、卡面长什么样」原样落盘，--apply 前可人工过目
DRY_BODIES = f"{WS}/pr-orphan-autoclose-dryrun-bodies.md"
MAX_PER_RUN = 2
DEDUPE_HOURS = 12
#: PR 最近更新后的静默窗口：合并/推送/评论进行中时不要抢着建动作卡。
#: 2026-11-04 t_360a813f：PR #97 合并前 36 秒被建复审卡，白跑一个 pm run。
QUIET_MINUTES = 15
SKIP_TITLE = ("证据 PR", "不合并")
BODY_LIMIT = 2048  # R16：tasks.body 的 UTF-8 字节数硬上限

#: team_board 源码树：唯一判据（prclaim / reviewhead / merge_guard）从这里 import
SRC = os.environ.get("PR_ORPHAN_TB_SRC") or "/home/liu/project/team-board/src"

#: 版本钉 1：认领行形态一旦变化，本脚本必须重验而不是静默继续（R17）
CLAIM_PATTERN = r"^PR-CLAIM: #([1-9][0-9]*)$"
#: 版本钉 2：merge_guard 的私有 rebase ref 前缀（判分支落后时抓的就是它）
REBASE_REF_PREFIX = "refs/merge-guard"

#: qa PASS run 的 summary 形态：首词 PASS / `QA PASS:` / `[QA] PASS`
QA_PASS_RE = re.compile(r"^\s*(?:\[QA\]\s*)?(?:QA\s*)?PASS\b", re.I)
QA_NOT_PASS_RE = re.compile(r"^\s*(?:\[QA\]\s*)?(?:ISSUES|BLOCKED|FAIL|CHANGES)", re.I)
#: 卡上写的证据行：`[QA] PASS: run 464（qa，…）`（补证据卡的产物；严格按 run 号取）
QA_PASS_LINE_RE = re.compile(r"^\[QA\]\s*PASS:\s*run\s+([0-9]+)", re.M)
#: pm 的 R7 档位结论（「R7 **B 档**」「档位：B·night」「档位 = B（不升级）」三形态都收）
GRADE_RE = re.compile(
    r"(?:(?:R7)\s*\**\s*)?(?:档位\s*[:：=＝]?\s*\**|R7\s*\**)\s*([ABC])\s*\**\s*(?:档)?"
)
#: 合并类卡标题：内核 `_PR_CLAIM_TRIGGER_RE` 只认首行这几种开头（assignee 必须是 repo）
MERGE_TITLE_TRIGGER = re.compile(r"^(?:合并卡|收口卡|合并\s*PR\s*#\s*\d+|收口[：:（(])")

KIND_MERGE = "merge"
KIND_REVIEW = "review"
KIND_REBASE = "rebase"
KIND_EVIDENCE = "evidence"
KIND_ORDER = (KIND_REVIEW, KIND_REBASE, KIND_EVIDENCE, KIND_MERGE)


class EnvError(Exception):
    """环境错误：import / 版本钉 / 读库任一失败 → digest 首行 `环境错误:` + rc2。"""


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{dt.datetime.now():%F %T} {msg}\n")


def run(cmd, timeout=180, cwd=REPO):
    """返回 (rc, stdout, stderr)。stdout 与 stderr 分开 —— 建卡失败时 stderr 原文要进 digest。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"exec failed: {exc}"


def load_tb():
    """按 SRC 取团队唯一的判据模块；任何一步不符即 EnvError（不降级、不兜底）。"""
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    try:
        from team_board import db, merge_guard, prclaim, reviewhead  # noqa: PLC0415 —— 故意延后
    except Exception as exc:  # noqa: BLE001
        raise EnvError(
            f"无法从 {SRC} 导入 team_board.db/prclaim/reviewhead/merge_guard："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    pattern = getattr(getattr(prclaim, "CLAIM_RE", None), "pattern", None)
    if pattern != CLAIM_PATTERN:
        raise EnvError(f"版本钉不符：prclaim.CLAIM_RE.pattern={pattern!r}，期望 {CLAIM_PATTERN!r}")
    prefix = getattr(merge_guard, "MERGE_GUARD_REF_PREFIX", None)
    if prefix != REBASE_REF_PREFIX:
        raise EnvError(f"版本钉不符：merge_guard.MERGE_GUARD_REF_PREFIX={prefix!r}，期望 {REBASE_REF_PREFIX!r}")
    for mod, attr in (
        (db, "iter_claim_rows"),
        (prclaim, "audit"),
        (reviewhead, "analyze"),
        (reviewhead, "fetch_bodies"),
        (reviewhead, "ReviewHeadError"),
        (merge_guard, "rebase_fetch_argv"),
        (merge_guard, "rebase_merge_base_argv"),
        (merge_guard, "classify_merge_base_rc"),
    ):
        if not hasattr(mod, attr):
            raise EnvError(f"版本钉不符：team_board 里没有 {mod.__name__}.{attr}")
    return db, prclaim, reviewhead, merge_guard


def write_digest(lines: list[str]) -> None:
    with open(DIGEST, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def env_fail(msg: str) -> int:
    """fail-closed：digest 首行 `环境错误: …`，不建任何卡，rc=2。"""
    log(f"环境错误: {msg}")
    write_digest([
        f"环境错误: {msg}",
        "",
        f"- 时间：{dt.datetime.now():%F %H:%M}",
        f"- PR_ORPHAN_TB_SRC={SRC}",
        "- 处置：本轮未建任何卡（不回退子串判据）。修好环境后下一轮自动恢复。",
    ])
    print(f"环境错误: {msg}")
    return 2


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


# ---------------------------------------------------------------- 三项前置判定


def done_claim_cards(con, n: int) -> list[tuple[str, int]]:
    """认领该 PR 且已经 done 的实现卡：[(task_id, completed_at)]，最近的在前。

    认领行形态与 R17 同一份正则（CLAIM_PATTERN）；只收 status='done' —— 未结卡由
    prclaim.audit 负责（那是「还要不要建新卡」的判据，不是证据来源）。
    """
    rx = re.compile(CLAIM_PATTERN, re.M)
    out: list[tuple[str, int]] = []
    try:
        rows = con.execute(
            "SELECT id, body, completed_at, created_at FROM tasks WHERE status = 'done'"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise EnvError(f"读 done 卡失败：{type(exc).__name__}: {exc}") from exc
    for tid, body, done_at, created_at in rows:
        if str(n) in rx.findall(body or ""):
            out.append((tid, done_at or created_at or 0))
    out.sort(key=lambda kv: kv[1], reverse=True)
    return out


def _short(text: str, limit: int = 80) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def collect_evidence(con, n: int) -> dict:
    """从认领该 PR 的 done 卡里取 ① qa PASS run ② pm 的 R7 档位结论（都取最近一条）。

    返回 {"cards": [...], "qa": {...}|None, "grade": {...}|None}；
    取数本身失败 → EnvError（判不了 ≠ 缺证据，前者不许建卡）。
    """
    cards = done_claim_cards(con, n)
    qa = None
    grade = None
    # 第一轮：优先认「真跑过的 qa run」（最强证据）
    for tid, _ in cards:
        for rid, profile, outcome, summary, ended_at in con.execute(
            "SELECT id, profile, outcome, summary, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC",
            (tid,),
        ).fetchall():
            if profile != "qa" or outcome != "completed":
                continue
            text = summary or ""
            if QA_PASS_RE.match(text) and not QA_NOT_PASS_RE.match(text):
                qa = {
                    "run": rid,
                    "task": tid,
                    "at": (dt.datetime.fromtimestamp(ended_at) if ended_at else None),
                    "text": _short(text.splitlines()[0] if text else "", 70),
                }
                break
        if qa:
            break
    # 第二轮：没有 qa run 时，认「卡上写的 [QA] PASS: run <id>」那一行（补证据卡的产物）
    if qa is None:
        for tid, _ in cards:
            for author, body, created_at in con.execute(
                "SELECT author, body, created_at FROM task_comments WHERE task_id = ? ORDER BY id DESC",
                (tid,),
            ).fetchall():
                if author not in ("qa", "pm"):
                    continue
                m = QA_PASS_LINE_RE.search(body or "")
                if m:
                    qa = {
                        "run": int(m.group(1)),
                        "task": tid,
                        "at": (dt.datetime.fromtimestamp(created_at) if created_at else None),
                        "text": _short([ln for ln in (body or "").splitlines() if QA_PASS_LINE_RE.search(ln)][0], 70),
                        "by": author,
                    }
                    break
            if qa:
                break
    for tid, _ in cards:
        for author, body, created_at in con.execute(
            "SELECT author, body, created_at FROM task_comments WHERE task_id = ? ORDER BY id DESC",
            (tid,),
        ).fetchall():
            if author != "pm":
                continue
            hit = _grade_from(body or "")
            if hit:
                grade = {"letter": hit[0], "reason": hit[1], "task": tid, "at": created_at}
                break
        if grade is None:
            for rid, summary, ended_at in con.execute(
                "SELECT id, summary, ended_at FROM task_runs WHERE task_id = ? AND profile = 'pm' "
                "AND outcome = 'completed' ORDER BY id DESC",
                (tid,),
            ).fetchall():
                hit = _grade_from(summary or "")
                if hit:
                    grade = {"letter": hit[0], "reason": hit[1], "task": tid, "at": ended_at, "run": rid}
                    break
        if grade is None:
            row = con.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()
            hit = _grade_from((row[0] if row else "") or "")
            if hit:
                grade = {"letter": hit[0], "reason": hit[1], "task": tid, "at": None}
        if qa and grade:
            break
    return {"cards": [tid for tid, _ in cards], "qa": qa, "grade": grade}


def _grade_from(text: str) -> tuple[str, str] | None:
    """从一段正文里抽 R7 档位：返回 (A|B|C, 同行理由)。"""
    for line in text.splitlines():
        m = GRADE_RE.search(line)
        if not m:
            continue
        rest = line[m.end():].strip()
        rest = rest.lstrip("-—|｜:：（(").strip()
        # 卡面常写成「档位: B｜理由: …」：模板自己会补「理由:」，这里去掉重复的那份
        rest = re.sub(r"^理由\s*[:：]\s*", "", rest)
        if not rest:
            prev = line[: m.start()].strip().strip("-—|｜:：").strip()
            rest = prev or "卡面原文仅写档位，未写理由"
        return m.group(1), _short(rest, 70)
    return None


def review_state(reviewhead, n: int, head: str) -> dict:
    """双问是否绑定当前 head：ok=True 绑定成立；False 需要复审；异常 → 判不了。"""
    try:
        report = reviewhead.analyze(head, reviewhead.fetch_bodies(n))
    except Exception as exc:  # noqa: BLE001 —— ReviewHeadError / 取数失败都算「判不了」
        return {"state": "unknown", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "state": "ok" if report.ok else "stale",
        "missing": list(report.missing),
        "marks": [("OK" if m.sha == head else "STALE", m.short, m.kind) for m in report.marks],
        "scanned": report.bodies_scanned,
    }


def behind_state(mg, n: int, base: str, branch: str) -> dict:
    """分支是否落后 base：用 merge_guard 自己那两道命令，判据与合并前闸门同一份。"""
    argv = mg.rebase_fetch_argv(REPO, REMOTE, n, base, branch)
    rc, out, err = run(list(argv), timeout=180)
    if rc != 0:
        return {"state": "unknown", "detail": f"fetch rc={rc}: {_short(err or out, 120)}"}
    rc2, out2, err2 = run(list(mg.rebase_merge_base_argv(REPO, n)), timeout=60)
    verdict = mg.classify_merge_base_rc(rc2)
    if verdict == mg.ANCESTOR_YES:
        return {"state": "ok"}
    if verdict == mg.ANCESTOR_NO:
        rc3, out3, _ = run(["git", "-C", REPO, "rev-list", "--count",
                            f"{mg.rebase_head_ref(n)}..{mg.rebase_base_ref(n)}"], timeout=60)
        count = (out3 or "").strip() if rc3 == 0 else "?"
        return {"state": "behind", "count": count}
    return {"state": "unknown", "detail": f"merge-base rc={rc2}: {_short(err2 or out2, 120)}"}


def decide(review: dict, behind: dict, evidence: dict) -> tuple[str, str]:
    """返回 (kind, 一句话结论)。顺序钉死：双问 → 落后 → 证据 → 合并。"""
    if review["state"] == "unknown":
        return "none", f"双问判不了（{review['detail']}）→ 不建卡"
    if review["state"] == "stale":
        miss = ",".join(review.get("missing") or []) or "无结构化标记"
        return KIND_REVIEW, f"双问未绑定当前 head（缺 {miss}）→ 建复审卡"
    if behind["state"] == "unknown":
        return "none", f"落后判定不了（{behind['detail']}）→ 不建卡"
    if behind["state"] == "behind":
        return KIND_REBASE, f"分支落后 main {behind['count']} 个提交 → 建 rebase 卡"
    if not evidence["cards"]:
        return KIND_EVIDENCE, "找不到认领该 PR 的 done 实现卡 → 建补证据卡"
    if not evidence["qa"]:
        return KIND_EVIDENCE, "缺 qa 的 PASS run → 建补证据卡"
    if not evidence["grade"]:
        return KIND_EVIDENCE, "缺 pm 的 R7 档位结论 → 建补证据卡"
    return KIND_MERGE, "三项齐全 → 建合并卡"


# ---------------------------------------------------------------- 卡面模板

REVIEW_BODY = """PR-CLAIM: #{n}

## 目标
在 PR #{n}（`{branch}`）当前 head `{head8}` 上重跑第二视角双问（缺陷 + 回归），两条结论按结构化标记贴 PR 评论。只评审，不改代码、不合并。

## 为什么有这张卡
自动收口脚本判定双问不成立：{review_why}。命令可复跑：
`PYTHONPATH=src python -m team_board.cli pr-review-head-audit --pr {n} --head {head}`

## 要求
- 每条结论一条 PR 评论，正文**顶格**两行：`REVIEW-HEAD: <40 位小写 sha>` + `REVIEW-KIND: defect|regression`
- 跑完贴上面那条命令的 rc（须 0）+ 结论末行；rc=0 后本卡 done
- 本卡只解决双问；其余缺口（落后 / 证据）脚本下一轮会自动开卡
{extra}
## 边界
不改代码、不 rebase 别人分支、不动 `.github/` 与依赖文件。
"""

REBASE_BODY = """PR-CLAIM: #{n}

## 目标
把 `{branch}` rebase 到最新 `origin/main` 并推回，让 base 重新成为 head `{head8}` 的祖先。不合并。

## 为什么有这张卡 / 为什么 assignee 是 pm 不是 engineer
`git merge-base --is-ancestor` 判 rc=1：main 不是 head 的祖先（落后 {count} 个提交），merge_guard 的合并前闸门会拒。本卡**不直接派 engineer**：内核建卡闸门要求 assignee=engineer 的卡正文带 qa 签署的「## 验收标准」（`QA-SPEC: qa`），脚本无权代签 —— 请 pm 按常规划卡流程签好验收标准后交 engineer 执行。

## 要求
- engineer 执行：rebase 后推 `git push origin HEAD:refs/heads/{branch}`（R10：SSH over 443），贴 rc
- 贴 `git merge-base --is-ancestor origin/main origin/{branch}; echo rc=$?` → 须 0
- head 变了 → 双问必须在新 head 上重跑；脚本下一轮会开复审卡，**别在本卡做双问**
- 冲突 / 身份问题按 R14 标因（env / spec），不许强推、不许改判据

## 边界
只 rebase 这一条分支；不写功能、不合并、不碰 `.github/` 与依赖文件。
"""

EVIDENCE_BODY = """PR-CLAIM: #{n}

## 目标
给 PR #{n}（`{branch}`，head `{head8}`）补齐合并前必须的验收证据，写进本卡评论：① qa 的 PASS run；② pm 的 R7 档位结论。只补证据，不改代码、不合并。

## 为什么有这张卡
自动收口脚本在认领该 PR 的 done 卡里没取到全部证据：{evidence_why}。repo 门房要求合并卡卡面必须有 `[QA] PASS: <run>` 与 `档位: <A|B|C>｜理由: …`，缺一条就必然被拦 —— 所以这里不建合并卡。

## 要求
- 在本卡评论里贴两行，逐字采用下面形态（脚本会照抄进合并卡）：
  `[QA] PASS: run <id>（<profile>，<时间>）`
  `档位: <A|B|C>｜理由: <一行>`
- qa 从没跑过该 PR 时：派 qa 卡复跑并签署；确实该由 pm 验收的，写 `[QA] PASS: run <id>（pm，<时间>，本 PR 验收归 pm）` —— 不许把没跑过的写成 PASS
- 证据齐了本卡 done；脚本下一轮会建合并卡（合并归 repo，R15）

## 边界
不改代码、不改判定、不合并。
"""

MERGE_BODY = """PR-CLAIM: #{n}
[QA] PASS: {qa_line}
档位: {grade_line}

## 目标
合并 PR #{n}（{title}）进 main；合并后确认源分支已删（R12/R13）。

## 三项前置（脚本实测，命令可复跑）
- 双问：`PYTHONPATH=src python -m team_board.cli pr-review-head-audit --pr {n} --head {head}` → rc=0（{review_why}）
- 落后：`git merge-base --is-ancestor origin/main origin/{branch}` → rc=0
- 证据：done 实现卡 {cards}；qa run {qa_run}；档位 {grade_letter}（{grade_task}）

## 合并命令（R12①/R13② 原样抄；缺 --delete-branch → rc2、不发起合并）
先用 `merge-record --card <本卡> --by repo/run<NNN> --pr {n} --branch {branch} --subject "<单行标题>" --body-out <绝对路径>` 生成 body，再执行：
`cd /home/liu/project/team-board && PYTHONPATH=src python3 -m team_board.merge_guard --pr {n} --branch {branch} --method merge --delete-branch --card <本卡> --by repo/run<NNN> --subject "<单行标题>" --body-file <绝对路径> --repo /home/liu/project/team-board`

## 要求
- 开工先 `gh pr checks {n}`；不是全绿就先补，别合
- 合并前重数 diff（`gh pr view --json files`）：涨过 10 文件或碰 CI/依赖/迁移/凭据 → R7 C 档，停下上报
- main 又前进导致 rebase 闸门 rc=1 → `kanban_block(kind="dependency")` 写明需重排 rebase（作者身份 engineer）；**不许自己 rebase**、不许改身份
- 完成判据：`git ls-remote --exit-code --refs origin refs/heads/{branch}` → rc=2（源分支已删）

## 边界
只合这一个 PR，不合别的；不动 `.github/` 与依赖文件
"""


def build_body(kind: str, ctx: dict) -> str:
    if kind == KIND_REVIEW:
        extra = ""
        if ctx.get("behind_note"):
            extra = f"\n- 本 PR 还落后 main {ctx['behind_count']} 个提交：rebase 会换 head，双问须在 rebase 后那一次跑\n"
        return REVIEW_BODY.format(extra=extra, **ctx)
    if kind == KIND_REBASE:
        return REBASE_BODY.format(**ctx)
    if kind == KIND_EVIDENCE:
        return EVIDENCE_BODY.format(**ctx)
    return MERGE_BODY.format(**ctx)


def card_title(kind: str, n: int, ctx: dict) -> str:
    if kind == KIND_MERGE:
        return f"合并 PR #{n}（自动收口卡）"
    if kind == KIND_REVIEW:
        return f"复审 PR #{n}：双问在 {ctx['head8']} 上重跑（自动收口卡）"
    if kind == KIND_REBASE:
        return f"rebase PR #{n}：{ctx['branch']} 落后 main {ctx['count']} 提交（自动收口卡）"
    return f"补证据 PR #{n}：qa PASS / R7 档位（自动收口卡）"


def recently_updated(updated_at: str | None, now: dt.datetime, minutes: int = QUIET_MINUTES) -> bool:
    """``updatedAt`` 在静默窗口内 → True；缺失/解析失败 → False（不拦正常动作）。"""
    if not updated_at:
        return False
    try:
        stamp = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    age = (now.astimezone(dt.timezone.utc) - stamp.astimezone(dt.timezone.utc)).total_seconds()
    # 负 age = 快照与 gh 时钟有偏差（或更新发生在 now 之后），同样按「刚更新」处理
    return abs(age) < minutes * 60


def card_assignee(kind: str) -> str:
    """建卡 assignee。

    · merge → repo（R15：合并权归 repo，内核 `_validate_merge_assignee` 只放行 repo）
    · review / evidence / rebase → pm：内核建卡闸门要求 assignee=engineer 的卡正文带
      qa 签署的「## 验收标准」（`QA-SPEC: qa`），零 token 脚本无权代签 —— 所以 rebase
      卡也交 pm 派（同一口径见 SOUL「不建工程卡直接派给 engineer」）。见卡 t_b79ce810。
    """
    return {"merge": "repo", "review": "pm", "rebase": "pm", "evidence": "pm"}[kind]


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--pr", type=int, action="append", default=None,
                    help="只判这几个 PR 号（验收演练用；默认判全部开着的 PR）")
    ap.add_argument("--db", default=DB, help="看板库路径（默认真板；演练时指向副本）")
    ap.add_argument("--max-cards", type=int, default=MAX_PER_RUN)
    args = ap.parse_args()
    apply = args.apply
    db_path = args.db

    # ① 环境闸门先过：import / 版本钉 / 读库。任一失败 → rc2，一张卡都不建。
    try:
        db, prclaim, reviewhead, merge_guard = load_tb()
    except EnvError as exc:
        return env_fail(str(exc))
    con = None
    try:
        con = db.connect(db_path)
        rows = db.iter_claim_rows(con)
    except Exception as exc:  # noqa: BLE001 —— BoardError / sqlite3.Error / 任何读库异常
        return env_fail(f"读板失败（{db_path}）：{type(exc).__name__}: {exc}")

    try:
        rc, out, err = run([GH, "pr", "list", "--state", "open", "--json",
                            "number,title,headRefName,headRefOid,baseRefName,changedFiles,isDraft,updatedAt"])
        if rc != 0:
            log(f"gh pr list failed rc={rc} {(err or out).strip()[:150]}")
            print("gh failed:", (err or out).strip()[:200])
            return 1
        prs = json.loads(out or "[]")
        missing_prs: list[int] = []
        if args.pr:
            wanted = set(args.pr)
            prs = [p for p in prs if p["number"] in wanted]
            missing = wanted - {p["number"] for p in prs}
            for n in sorted(missing):
                log(f"PR #{n} 不在开着的 PR 里（已合 / 已关），本轮跳过")
                missing_prs.append(n)

        st = load_state()
        # 旧版状态文件按 PR 号做键（"56"），新版按「PR:卡种」做键（"56:review"）；
        # 旧键永远不会再命中，加载时丢掉，别让它把后续调试带偏。
        legacy = [k for k in st if ":" not in k]
        if legacy:
            log(f"丢弃旧版状态键 {','.join(sorted(legacy))}")
            st = {k: v for k, v in st.items() if ":" in k}
            save_state(st)  # 落盘一次，别每轮都重复告警
        now = dt.datetime.now()
        acted, skipped, undecided = [], [], []
        skipped += [(n, "不在开着的 PR 里（已合 / 已关）") for n in missing_prs]
        created, failed = [], []
        would: list[tuple[int, str, str, str, str]] = []
        decisions = []

        for pr in prs:
            n, title = pr["number"], pr.get("title") or ""
            head_ref = pr.get("headRefName") or ""
            head = pr.get("headRefOid") or ""
            base = pr.get("baseRefName") or "main"
            if pr.get("isDraft"):
                skipped.append((n, "draft"))
                continue
            if recently_updated(pr.get("updatedAt"), now):
                skipped.append((n, f"最近 {QUIET_MINUTES} 分钟内更新过，留出合并窗口"))
                continue
            if head_ref.startswith("evidence/") or any(k in title for k in SKIP_TITLE):
                skipped.append((n, "证据/不合并类"))
                continue
            # 唯一判据：未结卡正文顶格 `PR-CLAIM: #<n>`（prclaim.audit；只用 claims，不看 refs）
            try:
                claims = prclaim.audit(rows, n).claims
            except Exception as exc:  # noqa: BLE001 —— 判据自己坏了也不许降级
                return env_fail(f"prclaim.audit({n}) 失败：{type(exc).__name__}: {exc}")
            if len(claims) == 1:
                skipped.append((n, f"已有认领卡 {claims[0].id}"))
                continue
            if len(claims) >= 2:
                skipped.append((n, f"重复认领 {len(claims)} 张 {','.join(c.id for c in claims)}"))
                continue

            # 三项前置：全部判「满足」才可能是合并卡；任一「判不了」→ 不建卡
            try:
                evidence = collect_evidence(con, n)
            except EnvError as exc:
                return env_fail(str(exc))
            review = review_state(reviewhead, n, head)
            behind = behind_state(merge_guard, n, base, head_ref)
            kind, why = decide(review, behind, evidence)
            decisions.append((n, kind, why))

            if kind == "none":
                undecided.append((n, why))
                continue
            key = f"{n}:{kind}"
            prev = st.get(key)
            if prev:
                age_h = (now - dt.datetime.fromisoformat(prev["at"])).total_seconds() / 3600
                if age_h < DEDUPE_HOURS:
                    skipped.append((n, f"{age_h:.1f}h 前已为「{kind}」建过 {prev['task']}"))
                    continue
            if len(acted) >= args.max_cards:
                skipped.append((n, "本轮配额已满"))
                continue

            ctx = {
                "n": n,
                "head": head,
                "head8": head[:8],
                "branch": head_ref,
                "title": title[:60],
                "files": pr.get("changedFiles"),
                "count": behind.get("count", "?"),
                "cards": "、".join(evidence["cards"]) or "（无）",
                "qa_line": "",
                "grade_line": "",
                "qa_run": (evidence["qa"] or {}).get("run", "-"),
                "grade_letter": (evidence["grade"] or {}).get("letter", "-"),
                "grade_task": (evidence["grade"] or {}).get("task", "-"),
                "review_why": "、".join(f"{f} {s} {k}" for f, s, k in review.get("marks", [])) or
                              f"缺 {','.join(review.get('missing') or [])}",
                "evidence_why": _evidence_why(evidence),
                "behind_note": behind["state"] == "behind",
                "behind_count": behind.get("count", "?"),
            }
            if kind == KIND_MERGE:
                qa = evidence["qa"] or {}
                grade = evidence["grade"] or {}
                when = f"，{qa['at']:%F %H:%M}" if qa.get("at") else ""
                ctx["qa_line"] = f"run {qa.get('run')}（{qa.get('by') or 'qa'}{when}）"
                ctx["title"] = (title or "")[:40]
                ctx["grade_line"] = f"{grade.get('letter')}｜理由: {grade.get('reason')}"
                ctx["review_why"] = f"defect + regression 均绑定 {head[:8]}"
            assignee = card_assignee(kind)
            body = build_body(kind, ctx)
            # 卡面（R16）贴着上限时先缩可变字段，缩完还超才拒建（fail-closed）
            if kind == KIND_MERGE and len(body.encode("utf-8")) > BODY_LIMIT:
                ctx["title"] = (title or "")[:24]
                ctx["grade_line"] = (
                    f"{(evidence['grade'] or {}).get('letter')}｜理由: "
                    f"{_short((evidence['grade'] or {}).get('reason', ''), 40)}"
                )
                body = build_body(kind, ctx)
            size = len(body.encode("utf-8"))
            head_line = card_title(kind, n, ctx).splitlines()[0]
            if MERGE_TITLE_TRIGGER.match(head_line) and assignee != "repo":
                undecided.append((n, f"标题命中合并语义但 assignee={assignee}，拒建"))
                continue
            if size > BODY_LIMIT:
                undecided.append((n, f"卡面 {size} 字节 > {BODY_LIMIT}（R16），拒建"))
                log(f"PR #{n} body too long: {size}")
                continue

            if apply:
                # 建卡前最后一次复查：gh pr list 到 create 之间可能已经合并/关闭。
                rc_state, out_state, _ = run(
                    [GH, "pr", "view", str(n), "--json", "state", "--jq", ".state"]
                )
                state = (out_state or "").strip()
                if rc_state == 0 and state != "OPEN":
                    skipped.append((n, f"建卡前复查 PR 状态={state}"))
                    continue
            acted.append((n, kind, head_line, size, assignee))
            if not apply:
                would.append((n, kind, head_line, body, assignee))
            if apply:
                rc2, out2, err2 = run([HERMES, "kanban", "--board", BOARD, "create", head_line,
                                       "--assignee", assignee, "--body", body],
                                      cwd="/home/liu")
                card_id = ""
                for tok in (out2 or "").split():
                    if tok.startswith("t_"):
                        card_id = tok
                if rc2 == 0 and card_id:
                    st[key] = {"task": card_id, "at": now.isoformat(timespec="seconds"),
                               "kind": kind, "head": head}
                    save_state(st)
                    created.append((n, kind, card_id))
                    log(f"create {kind} for PR #{n} rc=0 card={card_id} head={head[:8]}")
                else:
                    # 失败不吞：stderr 原文进 digest（2026-10-31 t_a7dea181 —— 之前只记 rc=2 card=）
                    raw = err2.strip() or out2.strip() or "（无 stderr 也无 stdout）"
                    failed.append((n, kind, rc2, raw))
                    log(f"create {kind} for PR #{n} FAILED rc={rc2} stderr={raw[:300]}")
    finally:
        if con is not None:
            con.close()

    # ② digest：每轮覆盖写；决策逐条列出（含「判不了」与拒建原因）
    drill = []
    if REPO != "/home/liu/project/team-board":
        drill.append(f"REPO={REPO}")
    if GH != "/home/liu/.local/bin/gh":
        drill.append(f"GH={GH}")
    if db_path != DB:
        drill.append(f"DB={db_path}")
    lines = [f"# 孤儿 PR 自动收口 {now:%F %H:%M} ({'APPLY' if apply else 'DRY-RUN'})", ""]
    if drill:
        lines += [f"演练模式（非生产路径，cron 不会这样跑）：{' '.join(drill)}", ""]
    if decisions:
        lines += ["## 逐 PR 决策（三项前置：双问 → 落后 → 证据）"]
        for n, kind, why in [(d[0], d[1], d[2]) for d in decisions]:
            lines.append(f"- PR #{n}：{why}")
        lines.append("")
    if apply:
        if created:
            lines += ["## 建卡成功"] + [f"- PR #{n} → {kind} {t}" for n, kind, t in created] + [""]
        if failed:
            lines += ["## 建卡失败（rc≠0，stderr 原文）"]
            for n, kind, rc2, raw in failed:
                lines += [f"- PR #{n} {kind} rc={rc2}", "", "```", raw, "```", ""]
    elif acted:
        lines += ["## 将建卡（dry-run，未落库）"]
        lines += [f"- PR #{n} → {kind}（assignee={a}）「{t}」{size} 字节" for n, kind, t, size, a in acted] + [""]
    if undecided:
        lines += ["## 判不了 → 未建卡（fail-closed）"] + [f"- PR #{n}：{why}" for n, why in undecided] + [""]
    if skipped:
        lines += ["## 跳过"] + [f"- PR #{n}：{why}" for n, why in skipped] + [""]
    write_digest(lines)
    if not apply:
        chunks = [f"# 本轮 dry-run 会建的卡（{now:%F %H:%M}）", ""]
        for n, kind, title, body, assignee in would:
            chunks += [f"## PR #{n} → {kind}（assignee={assignee}）", "",
                       f"**标题**：{title}", "", "```text", body.rstrip(), "```", ""]
        if not would:
            chunks.append("（本轮没有要建的卡）")
        with open(DRY_BODIES, "w", encoding="utf-8") as fh:
            fh.write("\n".join(chunks) + "\n")

    for n, kind, t in created:
        print(f"APPLY PR #{n} -> {kind} {t}")
    for n, kind, rc2, raw in failed:
        print(f"FAIL  PR #{n} {kind} rc={rc2}: {raw.splitlines()[0][:160]}")
    if not apply:
        for n, kind, t, size, a in acted:
            print(f"DRY   PR #{n} -> {kind}[{a}]「{t}」{size}B")
    for n, why in undecided:
        print(f"undec PR #{n}: {why}")
    for n, why in skipped:
        print(f"skip  PR #{n}: {why}")
    if not decisions and not skipped:
        print("no open PRs")
    return 1 if failed else 0


def _evidence_why(evidence: dict) -> str:
    if not evidence["cards"]:
        return "找不到认领该 PR 的 done 实现卡"
    miss = []
    if not evidence["qa"]:
        miss.append("qa PASS run")
    if not evidence["grade"]:
        miss.append("pm 的 R7 档位")
    return "缺 " + "、".join(miss) if miss else "齐全"


if __name__ == "__main__":
    sys.exit(main())
