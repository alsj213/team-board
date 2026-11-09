#!/usr/bin/env python3
"""core_patch_check — 检查我们对 Hermes 本体打的补丁还在不在。

为什么要它：`hermes update` 会覆盖 hermes-agent/ 下的文件，而我们有几个补丁
必须改本体才能生效（脚本类补丁不会丢）。升级后如果没人检查，表现是
「行为悄悄退回旧样子」，不报错、很难发现。

每个补丁登记一条「文件 + 必须存在的标记」，缺失即 MISSING，整体 rc=1。
建议每天跑一次（低峰），或 update 之后手动跑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/liu/.hermes/hermes-agent")

# (说明, 文件, 必须出现的标记)
PATCHES: list[tuple[str, str, str]] = [
    (
        "active_pr 守卫：从未跑过的卡不适用（合并卡不再被卡死）",
        "hermes_cli/kanban_db_dispatch.py",
        "A card that has never run cannot have spawned the worker",
    ),
    (
        "返工归因：request_changes 接受 cause / cause_quote",
        "hermes_cli/kanban_db.py",
        "cause_quote must be a verbatim substring of reason",
    ),
    (
        "返工归因：CLI --cause / --cause-quote",
        "hermes_cli/kanban_parser.py",
        '"--cause-quote"',
    ),
    (
        "返工归因：agent 工具 schema 带 cause",
        "tools/kanban_tools_schemas.py",
        "Rework attribution (required by R14",
    ),
    (
        "返工归因：agent 工具 handler 透传 cause",
        "tools/kanban_tools.py",
        'cause=args.get("cause")',
    ),
    (
        "建卡正文上限 2048B（CLI + 工具两路）",
        "hermes_cli/kanban_db.py",
        "_MAX_TASK_BODY_BYTES",
    ),
    (
        "kanban_show 收窄（Cursor 文件即上下文：完整正文落盘给路径）",
        "tools/kanban_tools.py",
        "PAIMENG-PATCH-SHOW-COMPACT",
    ),
    (
        "建卡 PR-CLAIM 闸门（CLI + 工具两路共用 create_task 校验）",
        "hermes_cli/kanban_db.py",
        "PR-CLAIM 闸门拦下建卡",
    ),
    (
        "建卡 PR-CLAIM 闸门：CLI 退出码 rc2",
        "hermes_cli/kanban.py",
        "PrClaimMissingError",
    ),
    (
        "worker 自救：hermes kanban claim 子命令",
        "hermes_cli/kanban_parser.py",
        '"claim"',
    ),
    (
        "worker 自救：set_pr_claim 写入实现",
        "hermes_cli/kanban_db.py",
        "def set_pr_claim",
    ),
    (
        "worker 自救：CLI _cmd_claim",
        "hermes_cli/kanban.py",
        "_cmd_claim",
    ),
    (
        # 上面三条的前两条标记（'"claim"' / "_cmd_claim"）在本次改动前就已存在，
        # 只能证明文件在、不能证明补丁在 —— 补一条只属于 --pr 的标记兜底。
        "worker 自救：claim --pr 参数与 PR-CLAIM 帮助文本",
        "hermes_cli/kanban_parser.py",
        "PR-CLAIM: #<n> 写到卡正文首行",
    ),
    (
        # agent 工具路径：真 worker shell 里 CLI 被 delegate 围栏挡死（rc1 实测）。
        "worker 自救 B：agent 工具 kanban_claim_pr handler",
        "tools/kanban_tools.py",
        "def _handle_claim_pr",
    ),
    (
        "worker 自救 B：kanban_claim_pr schema 与注册",
        "tools/kanban_tools_schemas.py",
        "KANBAN_CLAIM_PR_SCHEMA",
    ),
]


def main() -> int:
    missing = 0
    print(f"补丁自检 @ {ROOT}")
    for desc, rel, marker in PATCHES:
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  MISSING   {desc}\n            文件读不到: {rel} ({exc})")
            missing += 1
            continue
        if marker in text:
            print(f"  OK        {desc}")
        else:
            print(f"  MISSING   {desc}\n            在 {rel} 里找不到标记: {marker[:60]}")
            missing += 1

    print()
    if missing:
        print(f"结论：{missing} 条补丁丢失（很可能是 hermes update 覆盖）—— 需要重打。")
        return 1
    print("结论：全部补丁在位。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
