#!/usr/bin/env python3
"""cron --no-agent 包装器：R8 第 4 条的执行者证据检查（pm 的 run 是否产生 tracked 生产代码改动）。

stdout 为空 = cron 不推送。规则：
  · 被检脚本 rc=0（CLEAN）        -> 无输出，并清掉上次告警状态
  · rc=1（有告警）/ rc=2（查不了） -> 首次出现或告警内容变化时才输出；同样的告警不重复刷屏
  · 同一告警超过 24h 未消失        -> 复报一次（避免"静默即遗忘"）

环境变量（只给自测用，正常 cron 不设）：
  PMTC_REPO  覆盖仓库路径（默认 /home/liu/project/team-board）
  PMTC_DB    覆盖看板路径（默认 devteam kanban.db）
  PMTC_CHECK 覆盖被检脚本路径
  PMTC_STATE 覆盖状态文件（默认 <脚本目录>/.pm_touched_watch.state.json）
  PMTC_NOW   覆盖"当前时间"（epoch 秒，测试 24h 复报用）
"""
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.environ.get("PMTC_CHECK", "/home/liu/.hermes/profiles/ops/workspace/pm_touched_code.py")
REPO = os.environ.get("PMTC_REPO", "/home/liu/project/team-board")
DB = os.environ.get("PMTC_DB", "/home/liu/.hermes/kanban/boards/devteam/kanban.db")
STATE = os.environ.get("PMTC_STATE", os.path.join(HERE, ".pm_touched_watch.state.json"))
REPORT_EVERY = 24 * 3600


def now():
    v = os.environ.get("PMTC_NOW")
    return int(v) if v else int(time.time())


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def repo_toplevel(path):
    """仓库的 toplevel；失败返回 (None, stderr)。

    git 会向上找 .git —— team-board 的 .git 坏掉/被移走时，--repo 会静默落到上层仓库
    （本机 /home/liu 自己就是个 git 仓库），被检脚本照常输出 CLEAN。这一层必须在这里挡住，
    否则"检查失效"会被读成"没有问题"。
    """
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()
    return os.path.realpath(r.stdout.strip()), ""


def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(obj):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
    except OSError:
        pass  # 状态写不进去不该把检查本身弄挂


def main():
    top, gerr = repo_toplevel(REPO)
    want = os.path.realpath(REPO)
    if top is None or top != want:
        rc = 2
        out = ("ERROR: 仓库不可读，或 --repo 落到了上层仓库\n"
               "  期望 toplevel %s\n  实际 toplevel %s\n  %s\n"
               "  ↑ git 向上找 .git：team-board 的 .git 坏掉/被移走时，检查会静默扫到 /home/liu 并报 CLEAN。"
               % (want, top or "-", gerr))
    else:
        cp = subprocess.run(
            [sys.executable, CHECK, "--repo", REPO, "--db", DB],
            capture_output=True, text=True,
        )
        out = (cp.stdout or "") + (cp.stderr or "")
        rc = cp.returncode
    t = now()
    state = load_state()

    if rc == 0:
        if state.get("sig"):
            save_state({})
        return 0

    keep = [ln.rstrip() for ln in out.splitlines()
            if "ALERT" in ln or ln.strip().startswith("ERROR") or ln.strip().startswith("VERDICT")]
    if not keep:
        keep = [ln.rstrip() for ln in out.splitlines() if ln.strip()][-6:]
    body = "\n".join(keep)
    sig = hashlib.sha256(("%d\n%s" % (rc, body)).encode("utf-8")).hexdigest()[:16]

    same = state.get("sig") == sig
    fresh_enough = same and (t - int(state.get("last_report", 0))) < REPORT_EVERY
    if fresh_enough:
        return 0  # 同一告警，24h 内不重复推

    head = "[R8-4 告警] pm 的 run 产生了 tracked 生产代码改动" if rc == 1 else \
           "[R8-4 告警] 检查自身跑不动（rc=%d）—— 这不是 CLEAN，是没查到" % rc
    if same:
        head += "（仍未处置，24h 复报）"
    print(head)
    print("时间 %s   rc=%d" % (stamp(t), rc))
    print("模式 严格归属（窗口内提交须带本卡卡号才计入告警；--loose 只作对照）")
    print("复跑 python3 %s --repo %s --db %s" % (CHECK, REPO, DB))
    print("-" * 60)
    print(body)
    print("-" * 60)
    if rc == 1:
        print("处置：先跑 who_did_it.py <卡号> 核归因（R8 第 1–3 条），再按 R3 建卡；")
        print("      不要凭 tasks.assignee 或提交 author 下结论。")
    else:
        print("处置：查仓库/看板可读性（/home/liu/project/team-board、devteam kanban.db），")
        print("      检查本身失效期间该条 R8 证据等于没有，修好前不许用它的 CLEAN 下结论。")
    save_state({"sig": sig, "last_report": t, "rc": rc})
    return 0


if __name__ == "__main__":
    sys.exit(main())
