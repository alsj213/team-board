"""按显示宽度对齐的纯文本渲染工具（只为 team-board 的命令输出服务）。

这里的"宽度"一律指终端显示列数：东亚宽字符（``unicodedata.east_asian_width``
返回 ``W`` 或 ``F``，例如汉字、全角标点）记 2 列，其余记 1 列。制表符 ``\\t``
不处理——调用方不要把 Tab 喂进来。

三个函数都是纯函数：不读写文件、不读环境、不打印、不碰数据库。
E3/E4/E5 只许调用这里，不许另写渲染。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

#: 截断标记，占 1 列（east_asian_width 为 A，按 1 列算）
ELLIPSIS = "…"

#: 列间固定间隔
CELL_GAP = "  "


def _char_width(ch: str) -> int:
    """单个字符的显示宽度：W/F（宽/全角）记 2，其余记 1。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(s: str) -> int:
    """字符串在终端里占的列数。"""
    return sum(_char_width(ch) for ch in s)


def truncate(s: str, width: int) -> str:
    """按显示宽度截断到不超过 ``width`` 列。

    - 不超宽：原样返回（不做任何改动）。
    - 超宽：保留尽可能多的前缀字符，末尾补一个 ``ELLIPSIS``（占 1 列）。
    - ``width <= 0``：返回 ``""``。
    - 绝不在全角字符中间劈开（逐字符累加宽度，放不下就停），所以结果宽度满足
      ``width - 1 <= display_width(result) <= width``；全角字符占 2 列，
      ``width - 1`` 为奇数时会少 1 列（如 ``truncate("中文中文", 4) == "中…"``，宽 3）。
    """
    if width <= 0:
        return ""
    if display_width(s) <= width:
        return s

    budget = width - 1
    kept: list[str] = []
    used = 0
    for ch in s:
        w = _char_width(ch)
        if used + w > budget:
            break
        kept.append(ch)
        used += w
    return "".join(kept) + ELLIPSIS


def format_table(
    headers: list[str],
    rows: list[list[str]],
    right_align: Sequence[int] = (),
) -> str:
    """渲染「表头 / 分隔线 / 数据行」三段的等宽表格。

    - 列宽 = 该列所有单元格（含表头）的最大显示宽度；``rows`` 为空时只用表头定宽。
    - 列间固定 2 个空格；``right_align`` 里的列号右对齐，其余左对齐（表头同列一致）。
    - 分隔行每段 ``-`` 的个数严格等于该列列宽。
    - 行间用 ``"\\n"``，整体无尾随换行。
    - 行尾不留空格：最后一列右侧补的空格会被裁掉，因此该列不满宽的行会比整表
      宽度短（整表宽度以分隔行为准）。这是"列内补齐"与"行尾无空格"两条规则的
      折中——想整表逐行等宽，就让最后一列的内容自然填满该列。
    - 行比表头短时按空串补齐；多出来的列被忽略。``headers`` 为空时返回 ``""``。
    """
    cols = len(headers)
    if cols == 0:
        return ""

    widths: list[int] = []
    for j in range(cols):
        cells = [headers[j]]
        cells.extend(row[j] if j < len(row) else "" for row in rows)
        widths.append(max(display_width(cell) for cell in cells))

    right = set(right_align)

    def render(cells: Sequence[str]) -> str:
        parts: list[str] = []
        for j, cell in enumerate(cells):
            pad = " " * (widths[j] - display_width(cell))
            parts.append(pad + cell if j in right else cell + pad)
        return CELL_GAP.join(parts).rstrip()

    def render_row(row: Sequence[str]) -> str:
        return render([row[j] if j < len(row) else "" for j in range(cols)])

    lines = [render(headers)]
    lines.append(CELL_GAP.join("-" * w for w in widths).rstrip())
    lines.extend(render_row(row) for row in rows)
    return "\n".join(lines)
