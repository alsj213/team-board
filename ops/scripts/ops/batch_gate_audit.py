#!/usr/bin/env python3
"""统一门禁分析（只读）：判断每张开放 PR 能否直接合入。

对每张 PR：
  1. gh pr checks 是否全 SUCCESS
  2. PR 评论里的 REVIEW-HEAD（复审时的 sha）与当前 head 的 patch-id 是否一致
     —— 一致 = 内容与复审时完全相同（只是 rebase 换了 sha），证据仍成立
  3. 是否落后 main（需 rebase）
输出表：PR / head / CI / 复审sha / patch-id 一致? / 落后? / 结论
用法：batch_gate_audit.py [--json]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

REPO = "/home/liu/project/team-board"
GH = "/home/liu/.local/bin/gh"
SKIP_TITLES = ("证据 PR", "不合并")


def run(cmd, cwd=REPO, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def patch_id(ref_range: str) -> str:
    """对一个提交区间的 diff 求稳定指纹（跨 rebase 不变）。"""
    rc, out, _ = run(["bash", "-lc", f"git log -p --no-merges {ref_range} | git patch-id --stable"])
    if rc != 0 or not out:
        return ""
    # 输出形如 "<patchid> <commit>"，取所有行的 patchid 拼接
    ids = sorted({ln.split()[0] for ln in out.splitlines() if ln.strip()})
    return "-".join(ids)


def main() -> int:
    rc, out, err = run([GH, "pr", "list", "--state", "open", "--json",
                        "number,title,headRefOid,headRefName,mergeable"])
    if rc != 0:
        print("gh failed:", err[:200])
        return 1
    prs = json.loads(out or "[]")
    run(["git", "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*"])
    _, main_sha, _ = run(["git", "rev-parse", "origin/main"])
    rows = []
    for pr in prs:
        n, head = pr["number"], pr["headRefOid"]
        title = pr.get("title") or ""
        if any(k in title for k in SKIP_TITLES):
            rows.append(dict(pr=n, head=head[:7], ci="-", reviewed="-", pid="-", behind="-", verdict="跳过(证据PR)"))
            continue
        rc_ci, ci_out, _ = run([GH, "pr", "checks", str(n), "--json", "state"])
        states = sorted({c["state"] for c in json.loads(ci_out or "[]")}) if rc_ci == 0 else ["ERR"]
        ci_ok = states == ["SUCCESS"]
        rc_c, cm, _ = run([GH, "pr", "view", str(n), "--json", "comments"])
        reviewed = ""
        if rc_c == 0:
            bodies = "\n".join(c.get("body", "") for c in json.loads(cm or {}).get("comments", []))
            found = re.findall(r"REVIEW-HEAD:\s*([0-9a-f]{7,40})", bodies)
            if found:
                reviewed = found[-1]
        cur_pid = patch_id(f"{main_sha}..{head}")
        rev_pid = ""
        if reviewed:
            _, base, _ = run(["git", "merge-base", main_sha, reviewed])
            if base:
                rev_pid = patch_id(f"{base}..{reviewed}")
        pid_ok = bool(cur_pid) and cur_pid == rev_pid
        _, anc, _ = run(["bash", "-lc", f"git merge-base --is-ancestor {main_sha} {head}"])
        behind = "否" if anc == 0 else "是"
        if not ci_ok:
            verdict = "CI 未全绿"
        elif not reviewed:
            verdict = "无双问证据"
        elif not pid_ok:
            verdict = "内容已变(证据失效)"
        elif behind == "是":
            verdict = "可合(需先 rebase)"
        else:
            verdict = "可直接合"
        rows.append(dict(pr=n, head=head[:7], ci=",".join(states), reviewed=(reviewed or "-")[:7],
                         pid="一致" if pid_ok else "不一致", behind=behind, verdict=verdict))
    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    print(f"{'PR':<5}{'head':<9}{'CI':<10}{'复审sha':<9}{'patch-id':<9}{'落后':<5}结论")
    for r in rows:
        print(f"#{r['pr']:<4}{r['head']:<9}{r['ci'][:9]:<10}{r['reviewed']:<9}{r['pid']:<9}{r['behind']:<5}{r['verdict']}")
    ok = [r for r in rows if r["verdict"].startswith("可")]
    print(f"\n可直接处理 {len(ok)} / 共 {len(rows)}；main={main_sha[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
