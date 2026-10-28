"""故意违规样例（证据 PR 专用，合并前删除）：验证三类闸门各自会红。"""

from __future__ import annotations

import json  # ← 风格类：未使用导入，应被 ruff F401 拦住
import subprocess


def counter(n: int) -> str:
    # ← 类型类：返回类型不符，应被 mypy return-value 拦住
    return n


def run(cmd: str) -> None:
    # ← 安全类：shell=True，应被 bandit B602 拦住
    subprocess.run(cmd, shell=True)
