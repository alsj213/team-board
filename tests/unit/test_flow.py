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

    def test_bad_stage_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="stage"):
            flow.check_handoff(CARD, HEAD, "nope", impl_heads=(HEAD,), rework_count=0)
