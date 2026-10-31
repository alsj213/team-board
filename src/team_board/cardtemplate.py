"""卡正文模板的固定段落 / 字节 warn / 存量切点的唯一判据（纯函数）。

四种消费者共用这一份实现，不许各写一份：

- ``scan``：单卡判据 —— 命中固定段落标题（红）、正文超过 ``WARN_BYTES``
  （warn，目标 ``TARGET_BYTES``）、缺 ``TEMPLATE:`` 引用行（warn）；
- ``fixed_hits`` / ``has_template_ref``：``scan`` 的两条判据本身，单独暴露给
  需要逐条报的消费者（``body-audit`` 的明细列）；
- ``rewrite_hint``：红项的一句话改写指引（文案固定，含模板引用行的**准确写法**）；
- ``FIXED_SECTION_EPOCH``：存量切点 —— 此刻之前创建的卡是存量，**不判红**，
  warn 照判（存量卡不许被追溯改写，见 R16 ⑤）。

判定口径（一个字都不放宽）：

- 固定段落标题 = 顶格一行 ``## <标题>``，标题取自 :data:`FIXED_HEADINGS`；
  ``###`` 不算、标题中段出现也不算、行尾有多余文字不算；
- 字面量 ``## 完成契约`` 之类的正则**不许另写一份**：:data:`FIXED_HEADING_RE`
  必须由 :data:`FIXED_HEADINGS` 推出（内核侧用同一行 join 构造，镜像测试比对
  ``.pattern`` 逐字相等，见 ``tests/unit/test_cardtemplate_kernel.py``）；
- 字节一律 UTF-8 字节数，算法复用 :func:`team_board.bodylimit.body_bytes`
  （本模块不另留第二份字节口径）；``WARN_BYTES`` 是 **warn** 线，
  2048 字节硬上限仍归 ``bodylimit`` —— 本模块不改、不引用它做 red 判定；
- ``body`` 为 NULL 按空串判（没有正文 = 没有 ``TEMPLATE:`` 行 → warn），
  ``created_at`` 为 NULL 无从证明它建于新规之后 → 按存量处理（不判红）。

本模块不读文件、不读环境、不连库、不 print —— 全是纯函数，便于单测。
"""

from __future__ import annotations

import re

from . import bodylimit

#: 模板文件名（``rewrite_hint`` 文案与引用行都用它拼，不许另写字面量）
TEMPLATE_FILE = "card-template.md"

#: 模板版本号；改版本 = 改引用行的唯一期望值（``TEMPLATE: card-template.md#<版本>``）
TEMPLATE_VERSION = "v3"

#: 模板引用行的唯一形态：**顶格整行**，版本是 ``v`` + 十进制数字
TEMPLATE_REF_RE = re.compile(r"^TEMPLATE: card-template\.md#(v\d+)$", re.MULTILINE)

#: 模板固定段落标题（顺序即 red 元组的最小顺序口径，不许改名、不许换序）
FIXED_HEADINGS = (
    "完成契约",
    "验收三段式",
    "验收三段",
    "边界默认值",
    "R6 三条",
    "交付方式",
    "长内容去哪",
)

#: 固定段落标题的唯一正则：由 :data:`FIXED_HEADINGS` 推出，**不许另写字面量**
FIXED_HEADING_RE = re.compile(
    r"^##\s*(?:" + "|".join(map(re.escape, FIXED_HEADINGS)) + r")\s*$", re.MULTILINE
)

#: warn 的目标字节数（写卡时的自检目标，不判红）
TARGET_BYTES = 800

#: warn 的字节线：严格大于它才 warn（等于不放行错误、不等于就告警）
WARN_BYTES = 1200

#: 存量切点：2026-11-01 00:00 CST；此前的卡 = 存量，不判红
FIXED_SECTION_EPOCH = 1793462400

#: ``scan`` 的两个 warn 标记（只此两个字面量，消费者按字面量匹配）
WARN_OVER = "over-1200"  # 字节 > WARN_BYTES（目标 ≤ TARGET_BYTES）
WARN_NO_TEMPLATE = "no-template-ref"  # 正文无 TEMPLATE_REF_RE 命中行

#: ``scan`` 的返回值：(red, warn)，两个元组都可为空
ScanResult = tuple[tuple[str, ...], tuple[str, ...]]


def is_stock(created_at: int | None, cutoff: int = FIXED_SECTION_EPOCH) -> bool:
    """是否存量卡（``created_at < cutoff``）。

    ``created_at`` 为 NULL 时无从证明它建于切点之后 → 按存量处理（不判红）：
    比「把未知时间的卡当新卡判死」保守，且不会让读取层多一条脏数据出口。
    """
    if created_at is None:
        return True
    return created_at < cutoff


def fixed_hits(body: str | None) -> tuple[str, ...]:
    """命中的固定段落标题（去掉 ``##`` 前缀、原样标题，按首次出现顺序去重）。"""
    if not body:
        return ()
    text = str(body)
    hits: list[str] = []
    for match in FIXED_HEADING_RE.finditer(text):
        title = match.group(0).strip().lstrip("#").strip()
        if title not in hits:
            hits.append(title)
    return tuple(hits)


def has_template_ref(body: str | None) -> bool:
    """正文里是否有模板引用行（``TEMPLATE: card-template.md#v3`` 这种顶格整行）。"""
    if not body:
        return False
    return TEMPLATE_REF_RE.search(str(body)) is not None


def scan(
    body: str | None,
    created_at: int | None,
    cutoff: int = FIXED_SECTION_EPOCH,
) -> ScanResult:
    """单卡判定 → ``(red, warn)``。

    - 存量（``created_at < cutoff``）：``red`` 恒为 ``()``，``warn`` 照判；
    - 否则正文命中 :data:`FIXED_HEADING_RE` 的行 → ``red`` 列出这些标题；
    - 字节 > :data:`WARN_BYTES` → ``warn`` 含 :data:`WARN_OVER`；
    - 无 :data:`TEMPLATE_REF_RE` 命中行 → ``warn`` 含 :data:`WARN_NO_TEMPLATE`。

    ``warn`` 的顺序固定：先 ``over-1200`` 再 ``no-template-ref``。
    """
    warns: list[str] = []
    if bodylimit.body_bytes(body) > WARN_BYTES:
        warns.append(WARN_OVER)
    if not has_template_ref(body):
        warns.append(WARN_NO_TEMPLATE)
    red = () if is_stock(created_at, cutoff) else fixed_hits(body)
    return red, tuple(warns)


def template_ref_line() -> str:
    """模板引用行的准确写法（``rewrite_hint`` 与消费者共用的唯一来源）。"""
    return f"TEMPLATE: {TEMPLATE_FILE}#{TEMPLATE_VERSION}"


def rewrite_hint(hit: str) -> str:
    """红项的一句话改写指引（固定文案，必须含 ``TEMPLATE: card-template.md#v3``）。"""
    return (
        f"「{hit}」属模板固定段落：删掉它，正文首行改为 "
        f"{template_ref_line()}（固定段落由 dispatcher 注入）。"
    )
