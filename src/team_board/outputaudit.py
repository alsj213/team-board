"""``output-audit`` 的纯函数：把 ``db.iter_output_rows`` 的行汇总并渲染。

阈值只有一处来源 —— :data:`team_board.gatelog.OUTPUT_LIMIT_BYTES`，本模块不写
第二个字面量；``范围：`` 行的两种措辞直接复用 :mod:`team_board.bodylimit` 的
``SCOPE_PENDING`` / ``SCOPE_ALL``，不新造字符串。

本模块不连库、不 print、不写盘、不读环境 —— 全是纯函数，便于单测。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from .gatelog import OUTPUT_LIMIT_BYTES

#: ``口径：`` 行的固定前缀（上限数字由 ``limit`` 拼上）
SCOPE_LINE_PREFIX = "口径：单条评论 / result 的 UTF-8 字节数；上限"

#: ``合计：`` 行与清单标题都按这个模板拼
TOTAL_TMPL = "合计：{total} 条；超限：{over} 条"
OVER_HEADER_TMPL = "超限清单（{over} 条）："

#: 清单无超限时的占位符（不是 ``0 条`` 里塞空串）
EMPTY_MARK = "—"

#: 清单行里列之间的固定分隔（2 个空格）
COL_SEP = "  "


@dataclass(frozen=True)
class OutputRow:
    """一条可稽核内容：``task_comments.body`` 或 ``tasks.result``。

    ``source`` 只有 ``"comment"`` / ``"result"`` 两种取值；``row_id`` 是评论主键的
    十进制字符串或 result 的 ``"-"``。
    """

    task_id: str
    source: str
    row_id: str
    nbytes: int


@dataclass(frozen=True)
class Summary:
    """``output-audit`` 的全部数据。

    - ``rows``：全部行，按「字节降序 → 同字节按 ``(task_id, source, row_id)`` 升序」；
    - ``over``：超限行，顺序与 ``rows`` 一致（是它的子序列）；
    - ``counts``：``total`` / ``over`` 两个总数。
    """

    rows: tuple[OutputRow, ...]
    over: tuple[OutputRow, ...]
    counts: dict[str, int]


def _sort_key(row: OutputRow) -> tuple[int, str, str, str]:
    """字节降序，同字节按 ``(task_id, source, row_id)`` 升序。"""
    return (-row.nbytes, row.task_id, row.source, row.row_id)


def summarize(
    rows: Iterable[tuple[str, str, str, int]], *, limit: int = OUTPUT_LIMIT_BYTES
) -> Summary:
    """把 ``(task_id, source, row_id, nbytes)`` 汇总成 :class:`Summary`。

    严格大于 ``limit`` 才算超限（**等于上限放行**）。``nbytes`` 由调用方
    （``db.iter_output_rows``）在 Python 侧按 UTF-8 算好，本函数不再改口径。
    """
    detail = tuple(
        sorted(
            (
                OutputRow(task_id=task_id, source=source, row_id=row_id, nbytes=nbytes)
                for task_id, source, row_id, nbytes in rows
            ),
            key=_sort_key,
        )
    )
    over = tuple(row for row in detail if row.nbytes > limit)
    counts = {"total": len(detail), "over": len(over)}
    return Summary(rows=detail, over=over, counts=counts)


def render(
    summary: Summary,
    *,
    db_path: str,
    scope: str,
    limit: int = OUTPUT_LIMIT_BYTES,
) -> str:
    """渲染整块报告（不带尾随换行）。

    首行是 ``db_path`` 的绝对路径；``scope`` 传 :data:`bodylimit.SCOPE_PENDING`
    或 :data:`bodylimit.SCOPE_ALL`，原样进 ``范围：`` 行。清单只列超限行，
    行内列间 2 个空格、行尾不留空格；零超限时末行是 ``超限清单（0 条）：—``。
    """
    lines = [
        os.path.abspath(db_path),
        f"{SCOPE_LINE_PREFIX} {limit} 字节",
        f"范围：{scope}",
        TOTAL_TMPL.format(total=summary.counts["total"], over=summary.counts["over"]),
    ]
    header = OVER_HEADER_TMPL.format(over=summary.counts["over"])
    if not summary.over:
        # 零超限时占位符与标题同一行（末行即 ``超限清单（0 条）：—``）
        lines.append(f"{header}{EMPTY_MARK}")
        return "\n".join(line.rstrip() for line in lines)

    lines.append(header)
    for row in summary.over:
        lines.append(
            COL_SEP.join(
                (f"  {row.task_id}", f"{row.source}#{row.row_id}", f"{row.nbytes} 字节")
            )
        )
    return "\n".join(line.rstrip() for line in lines)
