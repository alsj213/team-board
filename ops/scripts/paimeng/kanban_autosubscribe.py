#!/usr/bin/env python3
"""守夜脚本：给 devteam 板上所有未完成的卡补上飞书通知订阅。

存在的原因：agent 只有在用 `kanban_create` 工具建卡时才会自动订阅；
用 `hermes kanban create` 命令行建的卡不会订阅，卡完成时老板收不到通知。
这个脚本不依赖 agent 的纪律，每 tick 扫一次板，补齐缺口。

幂等：已有订阅的卡跳过。无输出 = 无事可做。
"""
import os
import sqlite3
import subprocess
import sys

DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
BOARD = "devteam"
CHAT_ID = "oc_bbe0682094e2acd3be2e35c9caca0f9c"
USER_ID = "ou_99def4106a99962d9a6df885878cae8a"
NOTIFIER = "chief"

OPEN = ("triage", "todo", "ready", "running", "review", "blocked", "scheduled")

DOC = ("老板口径（2026-10-30）：不要每完成一张卡就推一条消息，只在整批完成时汇总。\n"
       "因此本脚本默认停用，且受 kanban.auto_subscribe_on_create 闸门控制：\n"
       "该键为 false（七个 devteam profile 均已设为 false）时本脚本直接退出，\n"
       "即使 cron 被误恢复也不会重新给老板刷屏。")

def gate_open() -> bool:
    """只认 profile 自己的 config.yaml；缺省按关闭处理（宁可漏订阅，不可刷屏）。"""
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    path = os.path.join(home, "config.yaml")
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    in_kanban = False
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            in_kanban = raw.split(":")[0].strip() == "kanban"
            continue
        if in_kanban and raw.strip().startswith("auto_subscribe_on_create:"):
            return raw.split(":", 1)[1].strip().strip("'\"").lower() in ("true", "1", "yes")
    return False


def main() -> int:
    if not gate_open():
        print("auto-subscribe: disabled (kanban.auto_subscribe_on_create=false) — 逐卡推送已按老板口径关闭", file=sys.stderr)
        return 0

    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"auto-subscribe: cannot open board: {exc}", file=sys.stderr)
        return 1
    marks = ",".join("?" * len(OPEN))
    rows = con.execute(
        f"""select t.id, t.title from tasks t
            where t.status in ({marks})
              and not exists (select 1 from kanban_notify_subs s where s.task_id = t.id)
            order by t.created_at""",
        OPEN,
    ).fetchall()
    con.close()

    for tid, title in rows:
        cmd = [
            "hermes", "kanban", "--board", BOARD, "notify-subscribe", tid,
            "--platform", "feishu",
            "--chat-id", CHAT_ID,
            "--user-id", USER_ID,
            "--chat-type", "dm",
            "--notifier-profile", NOTIFIER,
            "--delivery-mode", "notify+wake",
        ]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if done.returncode == 0:
            print(f"auto-subscribed {tid} ({title[:40]})")
        else:
            print(f"FAILED {tid}: {done.stderr.strip()[:200]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
