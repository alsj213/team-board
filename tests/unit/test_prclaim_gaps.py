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
