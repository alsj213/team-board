"""``prclaim`` 的纯函数单测：认领行正则、判定、渲染、``--pr`` 取值域。

这里只测纯函数（不连库、不碰 stdout）；CLI 的 rc 三态在
``tests/integration/test_cli_pr_claim_audit.py``。
"""

from __future__ import annotations

import pytest

from team_board import prclaim

PR = 34


class TestClaimRegex:
    def test_exact_line_is_a_claim(self) -> None:
        assert prclaim.claimed_prs("PR-CLAIM: #34") == (34,)

    def test_line_among_other_lines(self) -> None:
        body = "目标：某某。\n\nPR-CLAIM: #34\n\n## 验收\n- …"
        assert prclaim.claimed_prs(body) == (34,)

    def test_leading_space_is_not_a_claim(self) -> None:
        """顶格是硬要求：缩进一格的认领行不算。"""
        assert prclaim.claimed_prs("  PR-CLAIM: #34") == ()

    def test_trailing_space_is_not_a_claim(self) -> None:
        assert prclaim.claimed_prs("PR-CLAIM: #34 ") == ()

    def test_trailing_text_is_not_a_claim(self) -> None:
        assert prclaim.claimed_prs("PR-CLAIM: #34（我领了）") == ()

    def test_lowercase_keyword_is_not_a_claim(self) -> None:
        assert prclaim.claimed_prs("pr-claim: #34") == ()

    def test_hash_zero_is_not_a_claim(self) -> None:
        assert prclaim.claimed_prs("PR-CLAIM: #0") == ()

    def test_leading_zero_is_not_a_claim(self) -> None:
        assert prclaim.claimed_prs("PR-CLAIM: #034") == ()

    def test_crlf_line_ending_is_not_a_claim(self) -> None:
        """行尾必须有 ``$``：``\\r`` 是尾随字符，不放过。"""
        assert prclaim.claimed_prs("PR-CLAIM: #34\r\n") == ()

    def test_repeated_lines_are_reported(self) -> None:
        assert prclaim.claimed_prs("PR-CLAIM: #34\nPR-CLAIM: #34") == (34, 34)

    def test_none_and_empty_body(self) -> None:
        assert prclaim.claimed_prs(None) == ()
        assert prclaim.claimed_prs("") == ()

    def test_ref_and_claim_are_separate(self) -> None:
        body = "PR-REF: #34\nPR-CLAIM: #34"
        assert prclaim.claimed_prs(body) == (34,)
        assert prclaim.referenced_prs(body) == (34,)

    def test_ref_line_alone_is_not_a_claim(self) -> None:
        assert prclaim.claims("PR-REF: #34", PR) is False
        assert prclaim.references("PR-REF: #34", PR) is True

    def test_other_pr_number_does_not_match(self) -> None:
        assert prclaim.claims("PR-CLAIM: #340", PR) is False
        assert prclaim.claims("PR-CLAIM: #3", PR) is False


class TestAudit:
    @staticmethod
    def _row(card: str, body: str, status: str = "running") -> tuple[str, str, str, str]:
        return (card, status, "engineer", body)

    def test_single_claim_is_unique(self) -> None:
        rows = [
            self._row("t_a", "PR-CLAIM: #34"),
            self._row("t_b", "PR-REF: #34"),
            self._row("t_c", "与这个 PR 无关"),
        ]
        report = prclaim.audit(rows, PR)
        assert report.pending == 3
        assert [row.id for row in report.claims] == ["t_a"]
        assert [row.id for row in report.refs] == ["t_b"]
        assert report.ok is True
        assert report.sole_claim_id == "t_a"
        assert prclaim.exit_code(report) == 0

    def test_no_claim_is_rc1(self) -> None:
        report = prclaim.audit([self._row("t_b", "PR-REF: #34")], PR)
        assert report.claims == ()
        assert report.ok is False
        assert report.sole_claim_id is None
        assert prclaim.exit_code(report) == 1

    def test_two_claims_is_rc1_and_no_sole_id(self) -> None:
        rows = [
            self._row("t_a", "PR-CLAIM: #34"),
            self._row("t_b", "PR-CLAIM: #34"),
        ]
        report = prclaim.audit(rows, PR)
        assert [row.id for row in report.claims] == ["t_a", "t_b"]
        assert report.ok is False
        assert report.sole_claim_id is None
        assert prclaim.exit_code(report) == 1

    def test_card_claiming_twice_counts_once(self) -> None:
        """重复认领按「卡」数，不按「行」数。"""
        rows = [self._row("t_a", "PR-CLAIM: #34\nPR-CLAIM: #34")]
        report = prclaim.audit(rows, PR)
        assert [row.id for row in report.claims] == ["t_a"]
        assert report.ok is True

    def test_claiming_card_is_not_listed_as_ref(self) -> None:
        rows = [self._row("t_a", "PR-CLAIM: #34\nPR-REF: #34")]
        report = prclaim.audit(rows, PR)
        assert [row.id for row in report.claims] == ["t_a"]
        assert report.refs == ()

    def test_empty_body_claims_nothing(self) -> None:
        report = prclaim.audit([self._row("t_a", "")], PR)
        assert report.pending == 1
        assert report.claims == ()
        assert report.refs == ()


class TestRender:
    def test_unique_report_shape(self) -> None:
        report = prclaim.audit(
            [
                ("t_a", "running", "engineer", "PR-CLAIM: #34"),
                ("t_b", "todo", "pm", "PR-REF: #34"),
            ],
            PR,
        )
        lines = prclaim.render(report).splitlines()
        assert lines == [
            "未结卡 2 张",
            "CLAIM t_a [running] engineer",
            "REF t_b [todo] pm",
            "结论: 认领唯一 → t_a",
        ]

    def test_none_report_has_no_claim_line(self) -> None:
        report = prclaim.audit([("t_b", "todo", "pm", "")], PR)
        lines = prclaim.render(report).splitlines()
        assert lines[0] == "未结卡 1 张"
        assert not any(line.startswith("CLAIM ") for line in lines)
        assert lines[-1] == "结论: 无未结卡认领该 PR（合并前必须先认领）"

    def test_dupe_report_lists_every_claim(self) -> None:
        report = prclaim.audit(
            [
                ("t_a", "running", "engineer", "PR-CLAIM: #34"),
                ("t_b", "review", "pm", "PR-CLAIM: #34"),
            ],
            PR,
        )
        lines = prclaim.render(report).splitlines()
        assert lines[1].startswith("CLAIM t_a ")
        assert lines[2].startswith("CLAIM t_b ")
        assert lines[-1].startswith("结论: 重复认领")


class TestParsePr:
    @pytest.mark.parametrize("value", ["1", "34", "999999999"])
    def test_valid_range(self, value: str) -> None:
        assert prclaim.parse_pr(value) == int(value)

    @pytest.mark.parametrize(
        "value",
        ["", "0", "-1", "-34", "1_0", "+34", " 34", "34 ", "３４", "٣٤", "abc", "3.4"],
    )
    def test_invalid_shape_is_error(self, value: str) -> None:
        with pytest.raises(prclaim.ClaimAuditError):
            prclaim.parse_pr(value)

    @pytest.mark.parametrize("value", ["1000000000", "1793160000000", "99999999999999999999"])
    def test_over_cap_is_error(self, value: str) -> None:
        with pytest.raises(prclaim.ClaimAuditError) as excinfo:
            prclaim.parse_pr(value)
        assert "上限" in str(excinfo.value)
