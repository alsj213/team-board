"""``flow`` 单测：卡面契约与交卡预检的纯判定。"""

from __future__ import annotations

import json

from team_board import flow

CARD = "t_1234abcd"
HEAD = "a" * 40
OLD = "b" * 40


def body(**overrides: str) -> str:
    values = {
        "PATH-CLAIM": "src/a.py",
        "TEST-PLAN": "quick",
        "CONSUMER": "team-board card-audit --card t_1234abcd",
        "QA-SPEC": "qa",
    }
    values.update(overrides)
    return "\n".join(f"{key}: {value}" for key, value in values.items()) + "\n"


class TestCardAudit:
    def test_complete_card_passes(self) -> None:
        report = flow.audit_card(CARD, "x", body())
        assert report.ok is True
        assert report.kind == "impl"

    def test_merge_card_is_exempt(self) -> None:
        report = flow.audit_card(CARD, "收口：合并 PR #9", "PR-CLAIM: #9\n")
        assert report.ok is True
        assert report.kind == "merge"

    def test_impl_card_with_pr_claim_is_not_exempt(self) -> None:
        """标题无合并语义时，首行 PR-CLAIM 不豁免（claim_pr 会给任何 PR 卡补）。"""

        report = flow.audit_card(
            CARD, "脚本化流程预检：card-audit + handoff-check", "PR-CLAIM: #94\n"
        )
        assert report.kind == "impl"
        assert report.ok is False
        codes = {item.code for item in report.findings}
        assert codes == {
            "path-claim-missing",
            "test-plan-missing",
            "consumer-missing",
            "qa-spec-missing",
        }

    def test_impl_card_with_pr_claim_and_contract_passes(self) -> None:
        report = flow.audit_card(CARD, "x", "PR-CLAIM: #94\n" + body())
        assert report.ok is True
        assert report.kind == "impl"

    def test_merge_title_without_first_line_claim_is_reported(self) -> None:
        report = flow.audit_card(CARD, "收口：合并 PR #9", "## 目标\n")
        assert report.kind == "merge"
        assert report.ok is False
        assert [item.code for item in report.findings] == ["pr-claim-missing"]
        assert flow.render_card(report).splitlines()[-1] == "结论: 不通过（1 条）"

    def test_missing_each_line_is_reported(self) -> None:
        report = flow.audit_card(CARD, "x", "## 目标\n")
        codes = {item.code for item in report.findings}
        assert codes == {
            "path-claim-missing",
            "test-plan-missing",
            "consumer-missing",
            "qa-spec-missing",
        }

    def test_invalid_path_is_reported(self) -> None:
        report = flow.audit_card(CARD, "x", body(**{"PATH-CLAIM": "/etc/passwd"}))
        codes = {item.code for item in report.findings}
        assert "path-claim-invalid" in codes

    def test_bad_test_plan_value_is_reported(self) -> None:
        report = flow.audit_card(CARD, "x", body(**{"TEST-PLAN": "deep"}))
        codes = {item.code for item in report.findings}
        assert "test-plan-missing" in codes

    def test_render_and_json(self) -> None:
        report = flow.audit_card(CARD, "x", body())
        assert flow.render_card(report).splitlines()[-1] == "结论: 通过"
        payload = json.loads(flow.render_card_json(report))
        assert payload["ok"] is True


class TestHandoff:
    def test_pass(self) -> None:
        report = flow.check_handoff(
            CARD, HEAD, "review", impl_heads=(HEAD,), rework_count=0
        )
        assert report.ok is True

    def test_missing_impl_done(self) -> None:
        report = flow.check_handoff(
            CARD, HEAD, "review", impl_heads=(), rework_count=0
        )
        assert [item.code for item in report.findings] == ["impl-done-missing"]

    def test_stale_impl_done(self) -> None:
        report = flow.check_handoff(
            CARD, HEAD, "review", impl_heads=(OLD,), rework_count=0
        )
        assert [item.code for item in report.findings] == ["impl-done-stale"]
        assert OLD in report.findings[0].message

    def test_rework_budget(self) -> None:
        report = flow.check_handoff(
            CARD, HEAD, "review", impl_heads=(HEAD,), rework_count=2
        )
        assert [item.code for item in report.findings] == ["rework-budget"]

    def test_merge_requires_done(self) -> None:
        report = flow.check_handoff(
            CARD, HEAD, "merge", impl_heads=(HEAD,), rework_count=0, status="review"
        )
        assert [item.code for item in report.findings] == ["impl-not-done"]

    def test_sha256_head_is_supported(self) -> None:
        """``evidence.HEAD_RE`` 允许 64 位 head，这里不能把它判成“没有 impl_done”。"""

        long_head = "c" * 64
        report = flow.check_handoff(
            CARD, long_head, "review", impl_heads=(long_head,), rework_count=0
        )
        assert report.ok is True
        stale = flow.check_handoff(
            CARD, HEAD, "review", impl_heads=(long_head,), rework_count=0
        )
        assert [item.code for item in stale.findings] == ["impl-done-stale"]

    def test_bad_stage_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="stage"):
            flow.check_handoff(CARD, HEAD, "nope", impl_heads=(HEAD,), rework_count=0)


class TestAutoActionExemption:
    """自动收口动作卡（复审 / rebase / 补证据）不走实现卡契约闸门。"""

    def test_review_card_is_exempt(self) -> None:
        report = flow.audit_card(
            CARD, "复审 PR #100：x（自动收口卡）", "PR-CLAIM: #100\n\n## 目标\n只评审。\n"
        )
        assert report.kind == "auto"
        assert report.findings == ()

    def test_merge_auto_card_still_requires_pr_claim(self) -> None:
        report = flow.audit_card(CARD, "合并 PR #100（自动收口卡）", "## 目标\n")
        assert report.kind == "merge"
        assert [f.code for f in report.findings] == ["pr-claim-missing"]


class TestNonImplementationAssignee:
    """pm/qa/ops 的卡不是实现卡，不适用 PATH-CLAIM 契约。"""

    def test_pm_card_is_exempt(self) -> None:
        report = flow.audit_card(CARD, "需求卡：给看板加 Bot 状态页", "## 问题\n", assignee="pm")
        assert report.kind == "non-impl"
        assert report.findings == ()

    def test_engineer_card_still_requires_contract(self) -> None:
        report = flow.audit_card(CARD, "实现卡", "## 目标\n", assignee="engineer")
        assert report.kind == "impl"
        assert [f.code for f in report.findings] == [
            "path-claim-missing",
            "test-plan-missing",
            "consumer-missing",
            "qa-spec-missing",
        ]
