"""``prclaim`` 的缺口判据（``pr-claim-audit --gaps``）纯函数单测。

样例逐条照卡 t_cd645098 评论 1 第 5 节：应触发 5 条、不该触发 4 条。
"""

from __future__ import annotations

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

    def test_visible_leaves_normal_text_alone(self) -> None:
        assert prclaim.visible("目标：…)") == "目标：…)"
        assert prclaim.visible("a\tb") == "a\\tb"
        assert prclaim.visible("\x00\x7f") == "\\x00\\x7f"


class TestKernelDifferencePin:
    """钉住「缺口稽核」与内核建卡闸门（卡 ``t_c6ab9672``）的差异集。

    内核 ``_validate_pr_claim`` 取首行用 ``ln.strip()`` + ``str.splitlines()``；
    本仓 :func:`prclaim.first_line` 不 strip、按 ``\\n`` 切（与 :data:`CLAIM_RE`
    对齐，``merge_guard`` 也只看 ``CLAIM_RE``）。因此两边在**恰好 4 种形态**上
    不同：首行缩进 / tab / CRLF / 行尾空格 —— 内核 ACCEPT，稽核报 GAP。

    这张表是内联期望（不 import 内核，CI 不依赖 Hermes 源码）；内核那份若改成
    同样严格、或本仓改成 mirror 内核，这张表会先红，逼着同轮改
    ``docs/pr-claim-gate.md`` §2/§5。
    """

    TITLE = "合并卡（pm）：k"

    #: (样例名, 正文, 内核建卡, 本仓缺口稽核)
    TABLE: tuple[tuple[str, str, str, str], ...] = (
        ("正常", "PR-CLAIM: #47\n目标", "ACCEPT", "OK"),
        ("首行缩进", "  PR-CLAIM: #47\n目标", "ACCEPT", "GAP"),
        ("首行 tab", "\tPR-CLAIM: #47\n目标", "ACCEPT", "GAP"),
        ("CRLF", "PR-CLAIM: #47\r\n目标", "ACCEPT", "GAP"),
        ("行尾空格", "PR-CLAIM: #47 \n目标", "ACCEPT", "GAP"),
        ("行尾尾随文字", "PR-CLAIM: #47 我领了\n目标", "REJECT", "GAP"),
        ("冒号后缺空格", "PR-CLAIM:#47\n目标", "REJECT", "GAP"),
        ("#0", "PR-CLAIM: #0\n目标", "REJECT", "GAP"),
        ("第二行才有", "目标：…\nPR-CLAIM: #47", "REJECT", "GAP"),
        ("完全没有", "目标：…", "REJECT", "GAP"),
    )

    @staticmethod
    def _audit(body: str) -> str:
        return "OK" if prclaim.first_claim_pr(body) is not None else "GAP"

    def test_audit_side_matches_table(self) -> None:
        actual = {name: self._audit(body) for name, body, _k, _a in self.TABLE}
        expected = {name: a for name, _b, _k, a in self.TABLE}
        assert actual == expected

    def test_diff_set_is_exactly_the_four_stripped_forms(self) -> None:
        diffs = [name for name, _b, k, a in self.TABLE if (k == "ACCEPT") != (a == "OK")]
        assert diffs == ["首行缩进", "首行 tab", "CRLF", "行尾空格"]

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
