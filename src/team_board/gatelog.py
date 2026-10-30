"""闸门（gate）日志落盘的纯函数与唯一阈值常量。

本模块是三个消费者的**唯一来源**，不许各写一份：

- ``OUTPUT_LIMIT_BYTES``：单条 ``task_comments.body`` / ``tasks.result`` 的 UTF-8
  字节硬上限，``output-audit`` 只 import 不重写；
- ``failure_lines`` / ``counts_line``：从检查器原始输出里挑失败行、解析计数行；
- ``write_log`` / ``render_summary``：全量输出落 ``<log_dir>/<gate>-<卡号>.log``，
  stdout 只回一段摘要。

本模块：纯函数 + 一次写盘 —— 不连库、不读环境变量、不 print。
"""

from __future__ import annotations

import os
import re

#: 单条 ``task_comments.body`` / ``tasks.result`` 的 UTF-8 字节硬上限
OUTPUT_LIMIT_BYTES = 8192

#: 摘要里最多列多少条失败行
FAIL_LINE_LIMIT = 20

#: 日志目录默认值（相对 CWD）
LOG_DIR_DEFAULT = "evidence"


class GateLogError(Exception):
    """闸门日志相关的可预期错误：名字形态非法、写盘失败。"""


#: gate 名：字母数字开头，其后字母数字与 ``._-``
_GATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: 卡号：``t_`` + 8 位小写十六进制
_CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")

#: 第一轮挑行：行内出现任一标记即收
_HIT_MARKERS = ("FAILED", "ERROR", "error:", "错误:", "Traceback")

#: pytest 的 ``E   `` 前缀（行首 "E" + 三个空格）
_E_PREFIX = "E   "

#: pytest 风格计数：``12 passed`` / ``2 failed`` / ``1 error`` / ``3 errors`` / ``4 skipped``
_COUNTS_RE = re.compile(r"\b(\d+)\s+(passed|failed|errors?|skipped)\b")

#: 计数四项的输出顺序（恒全出现）
_COUNT_KEYS = ("passed", "failed", "error", "skipped")

#: 一个计数都没解析到时的固定文案
UNPARSED = "未解析"


def log_filename(gate: str, card: str) -> str:
    """日志文件名 ``f"{gate}-{card}.log"``，两个参数都先过形态校验。

    空串、含空格、含 ``"/"`` 一律非法 —— 不靠 ``os.path.join`` 兜底，
    路径分隔符必须在落盘前就被拦住。
    """
    if not _GATE_RE.match(gate):
        raise GateLogError(f"gate 形态非法: {gate}")
    if not _CARD_RE.match(card):
        raise GateLogError(f"卡号形态非法: {card}")
    return f"{gate}-{card}.log"


def _candidates(text: str) -> tuple[tuple[str, ...], bool]:
    """候选行与「是否来自第一轮」。

    第一轮：收含失败标记的行，行尾空白 rstrip 后去重保序；
    第一轮为空时退回全部非空行（顺序不变），由调用方按末尾截取。
    """
    lines = [line.rstrip() for line in text.splitlines()]
    hits: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line:
            continue
        hit = line.startswith(_E_PREFIX) or any(marker in line for marker in _HIT_MARKERS)
        if hit and line not in seen:
            seen.add(line)
            hits.append(line)
    if hits:
        return tuple(hits), True
    return tuple(line for line in lines if line), False


def _select(text: str, limit: int) -> tuple[tuple[str, ...], bool, int]:
    """挑出本节要列的行，并回带「是否第一轮命中」与候选总数。

    返回 ``(本节列出的行, 是否第一轮命中, 候选总数)``：

    - 第一轮命中 → 取**前** ``limit`` 条命中行；分母 = 去重后命中行数；
    - 第一轮为空 → 取**后** ``limit`` 条非空行（tail window，原序不重排）；
      分母 = 全部非空行数，**不受** ``limit`` 截断；
    - ``limit <= 0`` 或 ``text`` 为空 → ``((), False, 0)``。

    分子（``len(本节列出的行)``）与分母不等 = 截断在摘要里可见。
    """
    if limit <= 0 or not text:
        return (), False, 0
    candidates, from_first_round = _candidates(text)
    if from_first_round:
        return candidates[:limit], True, len(candidates)
    return candidates[-limit:], False, len(candidates)


def failure_lines(text: str, limit: int = FAIL_LINE_LIMIT) -> tuple[str, ...]:
    """从检查器输出里挑出失败行，总数不超过 ``limit``。

    - 第一轮命中（含 ``FAILED`` / ``ERROR`` / ``error:`` / ``错误:`` / ``Traceback``
      / 行首 ``E   ``）→ 取前 ``limit`` 条；
    - 第一轮为空 → 退回末尾 ``limit`` 条非空行；
    - ``limit <= 0`` 或 ``text`` 为空串 → 空 tuple。

    实现只转调 :func:`_select`，对外语义与历史一致，不许改。
    """
    return _select(text, limit)[0]


def counts_line(text: str) -> str:
    """把 pytest 风格计数解析成 ``passed=N failed=N error=N skipped=N``。

    正则 ``\\b(\\d+)\\s+(passed|failed|errors?|skipped)\\b``，``error`` / ``errors``
    归并到同一项，同名项逐项累加；一项都没有 → :data:`UNPARSED`。
    命中任一项时四项恒全出现、按固定顺序。
    """
    totals = dict.fromkeys(_COUNT_KEYS, 0)
    found = False
    for raw, kind in _COUNTS_RE.findall(text):
        found = True
        key = "error" if kind.startswith("error") else kind
        totals[key] += int(raw)
    if not found:
        return UNPARSED
    return " ".join(f"{key}={totals[key]}" for key in _COUNT_KEYS)


def render_summary(
    *, gate: str, card: str, exit_code: int, text: str, log_path: str
) -> str:
    """渲染闸门摘要（不带尾随换行）。

    两节的分子/分母口径不同，别混：

    - ``失败行（<已列>/<命中总数>，≤20）:``：分母是第一轮命中**去重后**的行数，
      只取前 20 条；两个数字不等 = 截断在摘要里可见。
    - ``末尾行（<已列>/<非空行总数>，≤20）:``：只在第一轮零命中且存在非空行时
      出现，取最后 20 条（tail window，原序不重排）；分母是全部非空行数，
      **不受** 20 截断。
    - 第一轮零命中时失败节恒为 ``0/0``，其下**一条行都不挂**（尾巴行只进末尾节）；
      ``text`` 为空串或全空白时连末尾节也不出现。
    """
    shown, from_first_round, total = _select(text, FAIL_LINE_LIMIT)
    out = [
        f"gate: {gate}",
        f"card: {card}",
        f"exit: {exit_code}",
        f"counts: {counts_line(text)}",
    ]
    if from_first_round:
        out.append(f"失败行（{len(shown)}/{total}，≤{FAIL_LINE_LIMIT}）:")
        out.extend(f"  {line}" for line in shown)
    else:
        out.append(f"失败行（0/0，≤{FAIL_LINE_LIMIT}）:")
        if total:
            out.append(f"末尾行（{len(shown)}/{total}，≤{FAIL_LINE_LIMIT}）:")
            out.extend(f"  {line}" for line in shown)
    out.append(f"log: {log_path}")
    return "\n".join(out)


def write_log(
    text: str, *, gate: str, card: str, log_dir: str = LOG_DIR_DEFAULT
) -> str:
    """把全量输出写到 ``<log_dir>/<gate>-<card>.log``，返回该文件的绝对路径。

    ``log_dir`` 不存在就建（``exist_ok=True``）；``OSError`` 一律包成
    :class:`GateLogError`，调用方只需处理一种异常。
    """
    path = os.path.join(log_dir, log_filename(gate, card))
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        raise GateLogError(f"写日志失败: {exc}") from exc
    return os.path.abspath(path)
