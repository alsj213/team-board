#!/usr/bin/env python3
"""paimeng 硬边界：terminal 里禁止交付动作（代码/推送/PR/合并）。"""
from __future__ import annotations
import json
import re
import sys

PATTERNS = (
    re.compile(r"\bgit\b[^\n;&|]*\b(?:commit|push)\b", re.I),
    re.compile(r"\bgh\s+pr\s+(?:create|merge)\b", re.I),
    re.compile(r"\bteam[-_]board(?:\.cli)?\s+merge_guard\b", re.I),
    re.compile(r"\bmerge_guard\b", re.I),
    re.compile(r"\bgh\s+api\b[^\n]*(?:mergePullRequest|/merge\b)", re.I),
    re.compile(r"\bmerge-record\b", re.I),
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") != "terminal":
        return 0
    args = data.get("tool_input") or {}
    cmd = str(args.get("command") or args.get("cmd") or "")
    for pattern in PATTERNS:
        if pattern.search(cmd):
            print(json.dumps({
                "action": "block",
                "message": (
                    "paimeng 角色边界：不得写代码/commit/push/建 PR/合并。"
                    "请建卡或转 pm，由 engineer→qa→repo 走完整链路；"
                    "被拦命令不会执行。"
                ),
            }, ensure_ascii=False))
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
