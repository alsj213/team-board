"""``prclaim`` 的缺口判据（``pr-claim-audit --gaps``）纯函数单测。

样例逐条照卡 t_cd645098 评论 1 第 5 节：应触发 5 条、不该触发 4 条。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest

from team_board import prclaim


class TestNeedsClaim:
    @pytest.mark.parametrize(
        "title",
        [
            "合并卡（pm）：pr-claim 缺口稽核",
            "收口卡：父卡由 ops 代关",
            "合并 PR #47 的收尾",
            "收口：子卡全 done",
            "收口（pm）：父卡可关，子卡全部 PASS",
        ],
    )
    def test_triggering_prefixes(self, title: str) -> None:
        assert prclaim.needs_claim(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            "PR-CLAIM 建卡闸门：合并卡缺认领行即 rc2",
            "返工（engineer）：PR #46 合并卡文案要改",
            "合并前置（engineer）：先跑全量测试",
            "合并模式 gh 目标仓钉死",
        ],
    )
    def test_non_triggering_titles(self, title: str) -> None:
        """关键词出现在标题中后段不算 —— 只认标题前缀（正文关键词版误中 60 张）。"""
        assert prclaim.needs_claim(title) is False

    def test_blank_and_none(self) -> None:
        assert prclaim.needs_claim(None) is False
        assert prclaim.needs_claim("") is False

    def test_hit_snippet(self) -> None:
        assert prclaim.trigger_hit("合并卡（pm）：x") == "合并卡"
        assert prclaim.trigger_hit("收口卡：x") == "收口卡"
        assert prclaim.trigger_hit("合并 PR #47 的收尾") == "合并 PR #47"
        assert prclaim.trigger_hit("收口：x") == "收口："
        assert prclaim.trigger_hit("返工（engineer）：x") == ""


class TestFirstClaimPr:
    def test_first_line_claim(self) -> None:
        assert prclaim.first_claim_pr("PR-CLAIM: #47\n\n目标：…") == 47

    def test_leading_blank_lines_are_skipped(self) -> None:
        assert prclaim.first_claim_pr("\n\nPR-CLAIM: #47\n目标") == 47

    def test_claim_not_on_first_line(self) -> None:
        assert prclaim.first_claim_pr("目标：…\n\nPR-CLAIM: #47") is None

    def test_indented_claim_is_not_a_claim(self) -> None:
        assert prclaim.first_claim_pr("  PR-CLAIM: #47") is None

    def test_trailing_text_is_not_a_claim(self) -> None:
        assert prclaim.first_claim_pr("PR-CLAIM: #47（我领了）") is None

    def test_trailing_space_is_not_a_claim(self) -> None:
        assert prclaim.first_claim_pr("PR-CLAIM: #47 ") is None

    def test_missing_space_after_colon(self) -> None:
        assert prclaim.first_claim_pr("PR-CLAIM:#47") is None

    def test_hash_zero(self) -> None:
        assert prclaim.first_claim_pr("PR-CLAIM: #0") is None

    def test_crlf_first_line_is_not_a_claim(self) -> None:
        """与 CLAIM_RE 同口径：``\\r`` 是尾随字符，不放行。"""
        assert prclaim.first_claim_pr("PR-CLAIM: #47\r\n目标") is None

    def test_none_and_empty_body(self) -> None:
        assert prclaim.first_claim_pr(None) is None
        assert prclaim.first_claim_pr("") is None
        assert prclaim.first_claim_pr("\n\n") is None

    def test_first_line_keeps_original_text(self) -> None:
        assert prclaim.first_line("\n  目标：  保留空白\n") == "  目标：  保留空白"
        assert prclaim.first_line(None) == ""


class TestAuditGaps:
    @staticmethod
    def _row(
        card: str, title: str, body: str, status: str = "running"
    ) -> tuple[str, str, str, str, str]:
        return (card, status, "engineer", title, body)

    def test_gap_only_when_trigger_without_first_line_claim(self) -> None:
        rows = [
            self._row("t_a", "合并卡（pm）：x", "目标：…"),
            self._row("t_b", "合并卡（pm）：y", "PR-CLAIM: #47\n目标"),
            self._row("t_c", "工作卡（engineer）：z", "目标：…"),
        ]
        report = prclaim.audit_gaps(rows)
        assert report.pending == 3
        assert [row.id for row in report.gaps] == ["t_a"]
        assert report.ok is False

    def test_claim_on_second_line_is_still_a_gap(self) -> None:
        rows = [self._row("t_a", "收口卡：x", "目标：…\nPR-CLAIM: #47")]
        assert [row.id for row in prclaim.audit_gaps(rows).gaps] == ["t_a"]

    def test_no_gaps(self) -> None:
        rows = [self._row("t_a", "合并卡（pm）：x", "PR-CLAIM: #47")]
        report = prclaim.audit_gaps(rows)
        assert report.ok is True
        assert report.gaps == ()

    def test_empty_board(self) -> None:
        report = prclaim.audit_gaps([])
        assert (report.pending, report.ok) == (0, True)

    def test_first_line_truncated_to_limit(self) -> None:
        long_line = "目标：" + "字" * 100
        rows = [self._row("t_a", "合并卡（pm）：x", long_line)]
        gap = prclaim.audit_gaps(rows).gaps[0]
        assert gap.first == long_line[: prclaim.FIRST_LINE_LIMIT]
        assert len(gap.first) == prclaim.FIRST_LINE_LIMIT

    def test_hit_and_assignee_carried(self) -> None:
        rows = [("t_a", "blocked", "pm", "收口：x", "")]
        gap = prclaim.audit_gaps(rows).gaps[0]
        assert (gap.hit, gap.status, gap.assignee, gap.first) == ("收口：", "blocked", "pm", "")


class TestRenderGaps:
    def test_no_gap_render(self) -> None:
        report = prclaim.GapReport(pending=2, gaps=())
        assert prclaim.render_gaps(report).splitlines() == [
            "未结卡 2 张",
            "结论: 无缺口（合并类卡都带 PR-CLAIM）",
        ]
        assert prclaim.exit_code_gaps(report) == 0

    def test_gap_render(self) -> None:
        report = prclaim.GapReport(
            pending=2,
            gaps=(
                prclaim.GapRow(
                    id="t_a", status="running", assignee="engineer", hit="合并卡", first="目标：…"
                ),
            ),
        )
        assert prclaim.render_gaps(report).splitlines() == [
            "未结卡 2 张",
            "GAP t_a [running] engineer 触发=合并卡 首行=目标：…",
            "结论: 缺口 1 张（逐张把 PR-CLAIM: #<n> 放到正文首行）",
        ]
        assert prclaim.exit_code_gaps(report) == 1

    def test_control_chars_are_escaped_in_render(self) -> None:
        """``\\r`` / ``\\t`` 在报告里转义成可见字符：终端不会被回车拉回行首。"""
        report = prclaim.GapReport(
            pending=1,
            gaps=(
                prclaim.GapRow(
                    id="t_crlf", status="running", assignee="engineer", hit="合并卡",
                    first="PR-CLAIM: #47\r",
                ),
            ),
        )
        line = prclaim.render_gaps(report).splitlines()[1]
        assert line == r"GAP t_crlf [running] engineer 触发=合并卡 首行=PR-CLAIM: #47\r"
        assert "\r" not in line

    def test_title_newline_does_not_split_the_gap_line(self) -> None:
        """标题带换行（``合并\\nPR #5``）时，报告物理行数仍固定为 3 行。

        ``TRIGGER_RE`` 里的 ``\\s*`` 会吃掉换行，``hit`` 于是带真换行；只转义
        ``first`` 的话一条 GAP 会被撑成两条物理行，下游按行解析直接错位。
        """
        rows = [("t_nl", "running", "engineer", "合并\nPR #5 的收尾", "目标：…")]
        report = prclaim.audit_gaps(rows)
        assert report.gaps[0].hit == "合并\nPR #5"  # 判定侧保留原文
        lines = prclaim.render_gaps(report).splitlines()
        assert len(lines) == 3
        assert lines[1] == (
            r"GAP t_nl [running] engineer 触发=合并\x0aPR #5 首行=目标：…"
        )

    def test_other_columns_are_escaped_too(self) -> None:
        """id / status / assignee 同样走 :func:`prclaim.visible`：任一列带控制字符都不许断行。"""
        report = prclaim.GapReport(
            pending=1,
            gaps=(
                prclaim.GapRow(
                    id="t_a\x00", status="run\rning", assignee="engi\tneer",
                    hit="合并卡", first="目标：…",
                ),
            ),
        )
        lines = prclaim.render_gaps(report).splitlines()
        assert len(lines) == 3
        assert lines[1] == (
            r"GAP t_a\x00 [run\rning] engi\tneer 触发=合并卡 首行=目标：…"
        )

    def test_visible_leaves_normal_text_alone(self) -> None:
        assert prclaim.visible("目标：…)") == "目标：…)"
        assert prclaim.visible("a\tb") == "a\\tb"
        assert prclaim.visible("\x00\x7f") == "\\x00\\x7f"


class TestKernelDifferencePin:
    """钉住「缺口稽核」与本仓 :data:`prclaim.CLAIM_RE` 之间刻意保留的差异集。

    内核 ``_validate_pr_claim`` 取首行用 ``ln.strip()`` + ``str.splitlines()``；
    本仓 :func:`prclaim.first_line` 不 strip、按 ``\\n`` 切（与 :data:`CLAIM_RE`
    对齐，``merge_guard`` 也只看 ``CLAIM_RE``）。两边在 **4 类**形态上不同：

    - 前导空白（``strip()`` 吃掉）：空格 / tab / NBSP / VT / FF / FS / U+2028 …
    - 行尾空白（``strip()`` 吃掉）：空格 / tab / 上表各类空白
    - CRLF 行尾（行尾是 ``\\r``）
    - 其它换行符（``splitlines()`` 额外在这些字符处断行）：单独 ``\\r`` / ``\\v`` /
      ``\\f`` / ``\\x1c`` / ``\\u2028`` / ``\\u2029``

    每一类里的**字符级变体**都在 ``TABLE`` 里有代表行；方向恒为「内核 ACCEPT /
    本仓 GAP」（:meth:`test_kernel_never_accepts_what_audit_rejects_interchangeably`）。

    本表**只钉本仓一侧**：表里「内核建卡」列是写死的期望字符串，**不 import 内核**，
    所以内核那边改成同样严格时这张表**不会**自己红（说它会红就是假话）。本仓改成
    mirror 内核、或稽核放宽/收紧，这张表才红，逼着同轮改 ``docs/pr-claim-gate.md``
    §2/§5。内核侧的现行行为由 :class:`TestKernelLiveComparison` 在设了
    ``HERMES_KERNEL_KANBAN_DB`` 时 exec 核对（CI runner 上没有 Hermes 源码 → skip）。
    """

    TITLE = "合并卡（pm）：k"

    #: (样例名, 正文, 内核建卡, 本仓缺口稽核)
    TABLE: tuple[tuple[str, str, str, str], ...] = (
        ("正常", "PR-CLAIM: #47\n目标", "ACCEPT", "OK"),
        ("首行缩进", "  PR-CLAIM: #47\n目标", "ACCEPT", "GAP"),
        ("首行 tab", "\tPR-CLAIM: #47\n目标", "ACCEPT", "GAP"),
        ("前导 NBSP", "\xa0PR-CLAIM: #47\n目标", "ACCEPT", "GAP"),
        ("前导 VT", "\x0bPR-CLAIM: #47\n目标", "ACCEPT", "GAP"),
        ("CRLF", "PR-CLAIM: #47\r\n目标", "ACCEPT", "GAP"),
        ("行尾空格", "PR-CLAIM: #47 \n目标", "ACCEPT", "GAP"),
        ("行尾 tab", "PR-CLAIM: #47\t\n目标", "ACCEPT", "GAP"),
        ("CR 断行", "PR-CLAIM: #47\r目标", "ACCEPT", "GAP"),
        ("VT 断行", "PR-CLAIM: #47\x0b目标", "ACCEPT", "GAP"),
        ("行尾尾随文字", "PR-CLAIM: #47 我领了\n目标", "REJECT", "GAP"),
        ("冒号后缺空格", "PR-CLAIM:#47\n目标", "REJECT", "GAP"),
        ("#0", "PR-CLAIM: #0\n目标", "REJECT", "GAP"),
        ("前导零", "PR-CLAIM: #034\n目标", "REJECT", "GAP"),
        ("13 位毫秒", "PR-CLAIM: #1793160000000\n目标", "ACCEPT", "OK"),
        ("第二行才有", "目标：…\nPR-CLAIM: #47", "REJECT", "GAP"),
        ("完全没有", "目标：…", "REJECT", "GAP"),
    )

    #: 差异行名 → 所属类。4 类固定 = {前导空白, 行尾空白, CRLF, 其它换行符}。
    #: ``test_diff_rows_are_exactly_the_four_classes`` 断言「差异行集合 = 本表键集合」
    #: 且「类集合恰是 4 类」—— 表里新增一条差异却没归类、或归成第 5 类，都会红。
    DIFF_CLASSES: ClassVar[dict[str, str]] = {
        "首行缩进": "前导空白",
        "首行 tab": "前导空白",
        "前导 NBSP": "前导空白",
        "前导 VT": "前导空白",
        "行尾空格": "行尾空白",
        "行尾 tab": "行尾空白",
        "CRLF": "CRLF",
        "CR 断行": "其它换行符",
        "VT 断行": "其它换行符",
    }

    @staticmethod
    def _audit(body: str) -> str:
        return "OK" if prclaim.first_claim_pr(body) is not None else "GAP"

    def test_audit_side_matches_table(self) -> None:
        actual = {name: self._audit(body) for name, body, _k, _a in self.TABLE}
        expected = {name: a for name, _b, _k, a in self.TABLE}
        assert actual == expected

    def test_diff_rows_are_exactly_the_four_classes(self) -> None:
        """差异行 == 归类表键集合，且只占 4 类（多一类 / 少一条差异都红）。"""
        diffs = [name for name, _b, k, a in self.TABLE if (k == "ACCEPT") != (a == "OK")]
        assert sorted(self.DIFF_CLASSES) == sorted(diffs)
        assert set(self.DIFF_CLASSES.values()) == {
            "前导空白",
            "行尾空白",
            "CRLF",
            "其它换行符",
        }

    def test_every_class_has_at_least_one_row(self) -> None:
        """4 类都要有代表行：字符级变体是同一类的不同字符，不是新类。"""
        for cls in {*self.DIFF_CLASSES.values()}:
            assert [n for n, c in self.DIFF_CLASSES.items() if c == cls], cls

    def test_kernel_never_accepts_what_audit_rejects_interchangeably(self) -> None:
        """稽核只在「内核放过、我们判死」方向差 —— 没有反向差异（我们更宽）。"""
        reverse = [name for name, _b, k, a in self.TABLE if k == "REJECT" and a == "OK"]
        assert reverse == []

    def test_audit_pass_implies_claim_re_matches(self) -> None:
        """「不会更宽」：首行过了，``CLAIM_RE`` 一定也认它（merge_guard 与稽核同口径）。"""
        for name, body, _k, verdict in self.TABLE:
            if verdict != "OK":
                continue
            assert prclaim.CLAIM_RE.search(body) is not None, name
            assert prclaim.first_claim_pr(body) in prclaim.claimed_prs(body), name

    def test_claim_number_has_no_upper_bound(self) -> None:
        """认领行的 ``n`` 只要求 ≥1 的 ASCII 十进制正整数、无上限。

        ``--pr`` 参数那个 ``1..999999999`` 上限只约束命令行（:func:`prclaim.parse_pr`），
        不约束认领行 —— 与 ``CLAIM_RE`` / 内核 ``_PR_CLAIM_LINE_RE`` 一致。
        """
        for body in ("PR-CLAIM: #1000000000", "PR-CLAIM: #1793160000000"):
            assert prclaim.first_claim_pr(body) is not None, body
            assert prclaim.CLAIM_RE.search(body) is not None, body
        for body in ("PR-CLAIM: #0", "PR-CLAIM: #00", "PR-CLAIM: #034", "PR-CLAIM: #-1"):
            assert prclaim.first_claim_pr(body) is None, body
        with pytest.raises(prclaim.ClaimAuditError):
            prclaim.parse_pr("1793160000000")


# --------------------------------------------------------------------------- 内核实跑复核

#: 从内核源码里只 exec 这几段（不 import ``hermes_cli``：CI 没装 Hermes）
_KERNEL_NAMES = (
    "_PR_CLAIM_TRIGGER_RE",
    "_PR_CLAIM_LINE_RE",
    "PrClaimMissingError",
    "_validate_pr_claim",
)


def _kernel_source_path() -> Path | None:
    """内核 ``kanban_db.py`` 的位置 —— **只认** ``HERMES_KERNEL_KANBAN_DB``。

    不设默认路径：默认指 ``~/.hermes/hermes-agent/...`` 会让全量 pytest 依赖**仓库外**
    的文件 —— 换一台没装 Hermes 源码、或内核改了路径的机器上，这里会红成仓库自身的失败。
    没设 / 指不到 → ``None`` → 整个类 skip（本仓 CI 不依赖它）。
    """
    raw = os.environ.get("HERMES_KERNEL_KANBAN_DB", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_file() else None


def _load_kernel(path: Path) -> dict[str, Any]:
    """按 AST 取出建卡闸门那几段源码并 exec，返回它们的命名空间。

    只取需要的顶层节点：整文件 exec 会把 ``kanban_db`` 的依赖全拖进来（CI 没有）。
    """
    source = path.read_text(encoding="utf-8")
    segments: list[str] = []
    found: set[str] = set()
    for node in ast.parse(source).body:
        name: str | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        if name in _KERNEL_NAMES:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, name
            segments.append(segment)
            found.add(name)
    missing = set(_KERNEL_NAMES) - found
    assert not missing, f"内核源码里找不到 {sorted(missing)}（{path}）"
    namespace: dict[str, Any] = {"re": re}
    exec(compile("\n\n".join(segments), str(path), "exec"), namespace)
    return namespace


@pytest.mark.skipif(
    _kernel_source_path() is None,
    reason="本机没有 Hermes 内核源码（可用 HERMES_KERNEL_KANBAN_DB 指定 kanban_db.py）",
)
class TestKernelLiveComparison:
    """用**真内核源码**复核 :class:`TestKernelDifferencePin` 那张表的「内核建卡」列。

    那张表的这一列是硬编码字符串，内核侧改动它不会自己红；本类在
    ``HERMES_KERNEL_KANBAN_DB`` 指向的 ``kanban_db.py`` 存在时 exec 两条正则与
    ``_validate_pr_claim``，按真实内核重判每一行，并断言两条正则文本与本仓逐字相同。
    没设 / 指不到就整类 skip（CI 上就是这样；本仓不设默认路径，全量测试不依赖仓库外的文件）。
    """

    @staticmethod
    def _kernel() -> dict[str, Any]:
        path = _kernel_source_path()
        assert path is not None
        return _load_kernel(path)

    def test_regex_text_is_char_for_char_identical(self) -> None:
        kernel = self._kernel()
        assert kernel["_PR_CLAIM_TRIGGER_RE"].pattern == prclaim.TRIGGER_RE.pattern
        # 内核那份带 re.MULTILINE（worker 自救写认领行用），本仓首行版不带 —— 只比文本
        assert kernel["_PR_CLAIM_LINE_RE"].pattern == prclaim.FIRST_CLAIM_RE.pattern

    def test_table_kernel_column_matches_live_kernel(self) -> None:
        kernel = self._kernel()
        validate = kernel["_validate_pr_claim"]
        missing = kernel["PrClaimMissingError"]
        title = TestKernelDifferencePin.TITLE
        actual: dict[str, str] = {}
        for name, body, _expected, _audit in TestKernelDifferencePin.TABLE:
            try:
                validate(title, body)
            except missing:
                actual[name] = "REJECT"
            else:
                actual[name] = "ACCEPT"
        expected = {
            name: kernel_verdict
            for name, _body, kernel_verdict, _audit in TestKernelDifferencePin.TABLE
        }
        assert actual == expected
