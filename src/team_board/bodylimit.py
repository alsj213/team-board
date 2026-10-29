"""body 硬上限的唯一常量来源，以及 ``body-audit`` 的纯函数与渲染。

三种消费者都用这里的值，不许各写一份：

- ``body_bytes`` / ``is_over`` / ``violation_message``：单卡判据（写卡前自检）；
- ``summarize``：把 ``db.iter_body_rows`` 的行汇总成分桶 + 超限清单；
- ``render``：把汇总渲染成 ``body-audit`` 的整块文本。

本模块不读文件、不读环境、不连库、不 print —— 全是纯函数，便于单测。
``BODY_LIMIT_BYTES`` 之外**不许再出现第二个上限字面量**：分桶边界、口径行、
上限文案全部由常量推出（测试断言与文档引用除外）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from . import fmt

#: body 的硬上限（UTF-8 字节数）。超过它的卡正文一律要求改放附件/评论。
BODY_LIMIT_BYTES = 2048

#: 超限提示的固定文案（一处定义，别处只调用 ``violation_message``）
OVER_LIMIT_TMPL = (
    "body 超过硬上限：{n} 字节（UTF-8），上限 {limit} 字节；"
    "长背景/证据请放附件或卡评论，卡正文只写差异部分。"
)

#: 判定列的三种取值
VERDICT_OVER = "OVER"
VERDICT_OK = "ok"
VERDICT_NULL = "NULL"

#: 明细列标题（逐字，含空格的斜杠分隔）
DETAIL_HEADER = "明细（字节数降序：卡号 / 状态 / 负责人 / 字节 / 判定）："

#: ``范围：`` 行的两种口径（默认只算未结卡，``--all`` 连 done 一起算）
SCOPE_PENDING = "未结卡（status ∉ {done, archived}）"
SCOPE_ALL = "未归档卡（status ≠ archived）"

#: 分桶右端点**含**：0 独一桶，其后每段按上限的一半/四分之一切
_QUARTER = BODY_LIMIT_BYTES // 4
_HALF = BODY_LIMIT_BYTES // 2

#: (lo, hi) 段；``hi is None`` 表示无上界；``lo == hi`` 表示单值桶
_BUCKET_SPEC: tuple[tuple[int, int | None], ...] = (
    (0, 0),
    (1, _QUARTER),
    (_QUARTER + 1, _HALF),
    (_HALF + 1, BODY_LIMIT_BYTES),
    (BODY_LIMIT_BYTES + 1, BODY_LIMIT_BYTES * 2),
    (BODY_LIMIT_BYTES * 2 + 1, None),
)

#: NULL body 单独一桶（既不是 0 字节，也不算超限）
NULL_BUCKET = "NULL"


def _bucket_label(lo: int, hi: int | None) -> str:
    """段 → 标签：``0`` / ``1-512`` / … / ``4097-``。"""
    if hi is None:
        return f"{lo}-"
    if lo == hi:
        return f"{lo}"
    return f"{lo}-{hi}"


#: 分桶标签，顺序即输出顺序（末位固定是 NULL 桶）
BUCKET_LABELS: tuple[str, ...] = (
    *(_bucket_label(lo, hi) for lo, hi in _BUCKET_SPEC),
    NULL_BUCKET,
)


def body_bytes(body: str | None) -> int:
    """body 的 UTF-8 字节数；``None`` 记 0（不是 4，别把 "NULL" 当内容）。"""
    if body is None:
        return 0
    return len(body.encode("utf-8"))


def is_over(body: str | None) -> bool:
    """严格大于硬上限才算超限（等于上限放行）。"""
    return body_bytes(body) > BODY_LIMIT_BYTES


def violation_message(n: int) -> str:
    """超限提示：把字节数与上限填进固定文案（文案本身一字不改）。"""
    return OVER_LIMIT_TMPL.format(n=n, limit=BODY_LIMIT_BYTES)


def bucket_of(nbytes: int | None) -> str:
    """字节数落在哪个分桶（``None`` → ``NULL`` 桶）。"""
    if nbytes is None:
        return NULL_BUCKET
    for (lo, hi), label in zip(_BUCKET_SPEC, BUCKET_LABELS, strict=False):
        if nbytes >= lo and (hi is None or nbytes <= hi):
            return label
    # 负数不可能来自 UTF-8 编码长度；真出现就归进首桶，不让它凭空消失
    return BUCKET_LABELS[0]


def _verdict(nbytes: int | None) -> str:
    """判定的三种取值：``OVER`` / ``ok`` / ``NULL``。"""
    if nbytes is None:
        return VERDICT_NULL
    return VERDICT_OVER if nbytes > BODY_LIMIT_BYTES else VERDICT_OK


@dataclass(frozen=True)
class BodyRow:
    """一张卡的 body 明细（``nbytes`` 为 ``None`` 表示 body 是 NULL）。"""

    id: str
    status: str
    assignee: str
    nbytes: int | None


@dataclass(frozen=True)
class Summary:
    """``body-audit`` 的全部数据。

    - ``rows``：明细行，已按「字节降序 → 同字节按卡号升序 → NULL 最后」排好；
    - ``buckets``：标签 → 张数（七个桶全在，空桶记 0）；
    - ``over``：超限卡号，顺序与明细一致；
    - ``counts``：``total`` / ``over`` / ``null`` 三个总数。
    """

    rows: tuple[BodyRow, ...]
    buckets: dict[str, int]
    over: tuple[str, ...]
    counts: dict[str, int]


def _sort_key(row: BodyRow) -> tuple[bool, int, str]:
    """字节降序（NULL 排最后），同字节按卡号升序。"""
    return (row.nbytes is None, -(row.nbytes or 0), row.id)


def summarize(rows: Iterable[tuple[str, str, str, int | None]]) -> Summary:
    """把 ``(id, status, assignee, bytes|None)`` 汇总成 :class:`Summary`。

    ``bytes`` 由调用方（``db.iter_body_rows``）在 Python 侧按 UTF-8 算好；
    NULL body 传 ``None``，它自成一桶、不进超限清单。
    """
    detail = sorted(
        (BodyRow(id=id_, status=status, assignee=assignee, nbytes=nbytes)
         for id_, status, assignee, nbytes in rows),
        key=_sort_key,
    )
    buckets = dict.fromkeys(BUCKET_LABELS, 0)
    for row in detail:
        buckets[bucket_of(row.nbytes)] += 1
    over = tuple(row.id for row in detail if row.nbytes is not None and row.nbytes > BODY_LIMIT_BYTES)
    counts = {
        "total": len(detail),
        "over": len(over),
        "null": buckets[NULL_BUCKET],
    }
    return Summary(rows=tuple(detail), buckets=buckets, over=over, counts=counts)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    """按显示宽度补空格（``right`` 右对齐），宽度用 ``fmt.display_width`` 算。"""
    gap = width - fmt.display_width(text)
    return " " * gap + text if right else text + " " * gap


def render(summary: Summary, *, db_path: str, scope: str) -> str:
    """渲染整块报告（不带尾随换行）。

    ``db_path`` 在这里取绝对路径（首行就是它）；``scope`` 传 :data:`SCOPE_PENDING`
    或 :data:`SCOPE_ALL`，原样进 ``范围：`` 行。
    列宽 = 该列所有单元格的最大显示宽度，列间固定 2 个空格，行尾不留空格。
    """
    lines = [
        os.path.abspath(db_path),
        f"口径：body 为 UTF-8 字节数；NULL body 记 NULL；上限 {BODY_LIMIT_BYTES} 字节",
        f"范围：{scope}",
        "分桶：",
    ]
    label_width = max(fmt.display_width(label) for label in BUCKET_LABELS)
    for label in BUCKET_LABELS:
        lines.append(f"  {_pad(label, label_width)}  {summary.buckets.get(label, 0)}")

    lines.append(
        f"合计：{summary.counts['total']} 张；"
        f"超限：{summary.counts['over']} 张；"
        f"NULL：{summary.counts['null']} 张"
    )
    lines.append(DETAIL_HEADER)

    cells: list[tuple[str, str, str, str, str]] = [
        (
            row.id,
            row.status,
            row.assignee,
            VERDICT_NULL if row.nbytes is None else str(row.nbytes),
            _verdict(row.nbytes),
        )
        for row in summary.rows
    ]
    if cells:
        widths = [
            max(fmt.display_width(cell[col]) for cell in cells) for col in range(5)
        ]
        for cell in cells:
            parts = [
                _pad(cell[0], widths[0]),
                _pad(cell[1], widths[1]),
                _pad(cell[2], widths[2]),
                _pad(cell[3], widths[3], right=True),
                cell[4],
            ]
            lines.append("  " + "  ".join(parts).rstrip())

    over_ids = " ".join(summary.over) if summary.over else "—"
    lines.append(f"超限清单（{summary.counts['over']} 张）：{over_ids}")
    return "\n".join(lines)
