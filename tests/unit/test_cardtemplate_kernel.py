"""内核镜像：内核 ``FIXED_HEADINGS`` / ``FIXED_HEADING_RE`` 与本仓逐字一致。

照 ``tests/unit/test_prclaim_gaps.py`` 的形态：按 AST 只取需要的顶层节点再 ``exec``，
**不 import ``hermes_cli``**（CI runner 上没有 Hermes 源码 → 整类 skip，可用
``HERMES_KERNEL_KANBAN_DB`` 指到 ``kanban_db.py``）。

内核侧目前还没有这两个常量（内核闸门子卡尚未落地）→ 本文件在本机也走 skip。
卡 t_7c17f775 的验收写死：「内核暂无该常量时 skip（内核闸门子卡落地后必须非 skip）」
—— 所以这里只放**真镜像**断言，不写「内核应该长什么样」的硬编码期望表：
一旦内核拿到常量，本文件自动变成真校验，两边正则文本不一致就红。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any

import pytest

from team_board import cardtemplate

#: 内核 ``kanban_db.py`` 的默认位置（宿主 Hermes 安装目录）
_KERNEL_DEFAULT = Path.home() / ".hermes" / "hermes-agent" / "hermes_cli" / "kanban_db.py"

#: 从内核源码里只 exec 这两个名字（整文件 exec 会把 ``kanban_db`` 的依赖全拖进来）
_KERNEL_NAMES = ("FIXED_HEADINGS", "FIXED_HEADING_RE")


def _kernel_source_path() -> Path | None:
    """内核 ``kanban_db.py`` 的位置：环境变量优先，否则 :data:`_KERNEL_DEFAULT`。"""
    raw = os.environ.get("HERMES_KERNEL_KANBAN_DB", "").strip()
    candidates = [Path(raw)] if raw else []
    candidates.append(_KERNEL_DEFAULT)
    return next((path for path in candidates if path.is_file()), None)


def _kernel_namespace(path: Path) -> dict[str, Any]:
    """按 AST 取出 ``_KERNEL_NAMES`` 里**存在**的那几段并 exec，返回命名空间。"""
    source = path.read_text(encoding="utf-8")
    segments: list[str] = []
    for node in ast.parse(source).body:
        name: str | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
        elif isinstance(node, ast.AnnAssign):
            name = getattr(node.target, "id", None)
        if name in _KERNEL_NAMES:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, name
            segments.append(segment)
    namespace: dict[str, Any] = {"re": re}
    if segments:
        exec(compile("\n\n".join(segments), str(path), "exec"), namespace)
    return namespace


def _kernel_or_skip() -> dict[str, Any]:
    """取内核命名空间；内核还没有这两个常量时 skip（不是失败）。"""
    path = _kernel_source_path()
    assert path is not None
    namespace = _kernel_namespace(path)
    missing = [name for name in _KERNEL_NAMES if name not in namespace]
    if missing:
        pytest.skip(
            f"内核源码暂无 {missing}（{path}）：内核闸门子卡落地后本测试必须非 skip"
        )
    return namespace


@pytest.mark.skipif(
    _kernel_source_path() is None,
    reason="本机没有 Hermes 内核源码（可用 HERMES_KERNEL_KANBAN_DB 指定 kanban_db.py）",
)
class TestKernelMirror:
    """内核侧常量与本仓逐字相等 —— 改任一 ``FIXED_HEADINGS`` 条目就要同步两处。"""

    def test_fixed_headings_identical(self) -> None:
        kernel = _kernel_or_skip()
        assert tuple(kernel["FIXED_HEADINGS"]) == cardtemplate.FIXED_HEADINGS

    def test_fixed_heading_re_pattern_char_for_char_identical(self) -> None:
        kernel = _kernel_or_skip()
        assert kernel["FIXED_HEADING_RE"].pattern == cardtemplate.FIXED_HEADING_RE.pattern

    def test_kernel_regex_is_join_of_kernel_headings(self) -> None:
        """内核那份也必须由它自己的 ``FIXED_HEADINGS`` 推出（同一行 join 构造）。"""
        kernel = _kernel_or_skip()
        derived = r"^##\s*(?:" + "|".join(
            map(re.escape, tuple(kernel["FIXED_HEADINGS"]))
        ) + r")\s*$"
        assert kernel["FIXED_HEADING_RE"].pattern == derived

    def test_both_sides_flag_multiline(self) -> None:
        kernel = _kernel_or_skip()
        assert kernel["FIXED_HEADING_RE"].flags & re.MULTILINE
        assert cardtemplate.FIXED_HEADING_RE.flags & re.MULTILINE

    def test_same_probe_lines_agree(self) -> None:
        """同一组探针行（含正例/反例）两边判定一致 —— 镜像不是只比字符串。"""
        kernel = _kernel_or_skip()
        kernel_re = kernel["FIXED_HEADING_RE"]
        probes = [
            "## 完成契约",
            "##完成契约",
            "## " + cardtemplate.FIXED_HEADINGS[-1],
            "### 完成契约",
            "## 目标",
            "前文 ## 完成契约",
            "",
        ]
        for probe in probes:
            assert (kernel_re.search(probe) is not None) == (
                cardtemplate.FIXED_HEADING_RE.search(probe) is not None
            ), probe
