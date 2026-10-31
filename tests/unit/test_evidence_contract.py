"""``evidence`` 契约 v1 的单测：kind 表、角色矩阵、payload 校验、judge 纯函数。

只测纯函数（不连库、不碰 stdout、不读环境）；emit 的 rc 三态在
``tests/unit/test_evidence_emit.py``，取数与报告在 ``tests/unit/test_evidence_audit.py``。

七个 verdict 每个至少一条用例（类名里写了 verdict 名），八类越界各一条 ``ValueError``，
判定优先级与「同 kind 取 id 最大一条」各钉一条，防后续 E3/E4 改判定时悄悄放宽。
"""

from __future__ import annotations

import pytest

from team_board import evidence

HEAD = "a" * 40
OLD_HEAD = "b" * 40
HEAD64 = "0123456789abcdef" * 4
PR = 42


def base_events() -> list[evidence.Event]:
    """四槽齐、四槽全绑 ``HEAD``、角色全对的基线。"""
    return [
        evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "B", "reason": "常规改动", "head_sha": HEAD},
            run_id=101,
            profile="pm",
        ),
        evidence.Event(
            id=2,
            kind="qa_pass",
            payload={"head_sha": HEAD, "run_id": 202},
            run_id=202,
            profile="qa",
        ),
        evidence.Event(
            id=3,
            kind="review",
            payload={"kind": "defect", "head_sha": HEAD, "verdict": "pass"},
            run_id=101,
            profile="pm",
        ),
        evidence.Event(
            id=4,
            kind="review",
            payload={"kind": "regression", "head_sha": HEAD, "verdict": "pass"},
            run_id=202,
            profile="qa",
        ),
    ]


def states(judgment: evidence.Judgment) -> dict[str, str]:
    return {slot.name: slot.state for slot in judgment.slots}


class TestKindTable:
    def test_kinds_exact_order(self) -> None:
        assert evidence.KINDS == (
            "spec_sign",
            "impl_done",
            "tier",
            "qa_pass",
            "review",
            "boss_approve",
            "merged",
        )

    def test_role_rules_cover_every_kind(self) -> None:
        assert set(evidence.ROLE_RULES) == set(evidence.KINDS)

    @pytest.mark.parametrize(
        ("kind", "roles"),
        [
            ("spec_sign", {"qa"}),
            ("impl_done", {"engineer"}),
            ("tier", {"pm"}),
            ("qa_pass", {"qa"}),
            ("review", {"pm", "qa"}),
            ("boss_approve", {"paimeng"}),
            ("merged", {"repo"}),
        ],
    )
    def test_role_matrix_is_literal(self, kind: str, roles: set[str]) -> None:
        """角色矩阵逐字取契约；每个值都是 frozenset（调用方不可改）。"""
        assert evidence.ROLE_RULES[kind] == frozenset(roles)
        assert isinstance(evidence.ROLE_RULES[kind], frozenset)

    def test_emittable_kinds_excludes_merged_only(self) -> None:
        assert evidence.EMITTABLE_KINDS == (
            "spec_sign",
            "impl_done",
            "tier",
            "qa_pass",
            "review",
            "boss_approve",
        )
        assert "merged" not in evidence.EMITTABLE_KINDS
        assert "merged" in evidence.KINDS

    def test_req_slots_exact_and_sources_valid(self) -> None:
        assert evidence.REQ_SLOTS == (
            "tier",
            "qa_pass",
            "review:defect",
            "review:regression",
        )
        for name in evidence.REQ_SLOTS:
            kind, disc = evidence.SLOT_SOURCES[name]
            assert kind in evidence.KINDS
            if disc is not None:
                assert disc in evidence.REVIEW_KINDS

    def test_seven_verdicts_are_distinct_literals(self) -> None:
        verdicts = {
            evidence.VERDICT_PASS,
            evidence.VERDICT_MISSING,
            evidence.VERDICT_HEAD_MISMATCH,
            evidence.VERDICT_BLOCKED,
            evidence.VERDICT_NO_BOSS,
            evidence.VERDICT_CROSS_CARD,
            evidence.VERDICT_ROLE_MISMATCH,
        }
        assert len(verdicts) == 7
        assert evidence.VERDICT_PASS == "PASS"
        assert evidence.VERDICT_MISSING == "缺证据"
        assert evidence.VERDICT_HEAD_MISMATCH == "证据对新 head 无效"
        assert evidence.VERDICT_BLOCKED == "双问阻塞"
        assert evidence.VERDICT_NO_BOSS == "缺老板点头"
        assert evidence.VERDICT_CROSS_CARD == "证据串卡"
        assert evidence.VERDICT_ROLE_MISMATCH == "角色不符"
        assert len(evidence.VERDICT_PRIORITY) == 6
        assert evidence.VERDICT_PASS not in evidence.VERDICT_PRIORITY


class TestHeadShape:
    @pytest.mark.parametrize("head", [HEAD, HEAD64])
    def test_valid_heads(self, head: str) -> None:
        assert evidence.HEAD_RE.fullmatch(head) is not None
        assert evidence.validate_payload("qa_pass", {"head_sha": head, "run_id": 1})[
            "head_sha"
        ] == head

    @pytest.mark.parametrize(
        "head",
        [
            "a" * 39,
            "a" * 41,
            "a" * 63,
            "a" * 65,
            "A" * 40,
            "a" * 39 + "A",
            " " + "a" * 40,
            "a" * 40 + " ",
            "a" * 40 + "\n",
            "g" * 40,
            "a" * 39 + "-",
            "",
        ],
        ids=[
            "39位",
            "41位",
            "63位",
            "65位",
            "全大写",
            "尾位大写",
            "前导空格",
            "尾随空格",
            "尾随换行",
            "非十六进制",
            "横线",
            "空串",
        ],
    )
    def test_bad_heads_rejected(self, head: str) -> None:
        """整串 fullmatch：缩写、大写、空白、非十六进制一个都不放过。"""
        assert evidence.HEAD_RE.fullmatch(head) is None
        with pytest.raises(ValueError, match="head_sha"):
            evidence.validate_payload("qa_pass", {"head_sha": head, "run_id": 1})

    def test_head_is_compared_verbatim(self) -> None:
        """不做大小写/前缀归一：大小写不同的 sha 就是不同 head。"""
        events = base_events()
        judgment = evidence.judge(events, HEAD.upper(), PR)
        assert judgment.verdict == evidence.VERDICT_HEAD_MISMATCH


class TestValidatePayloadHappy:
    @pytest.mark.parametrize(
        ("kind", "payload"),
        [
            ("spec_sign", {}),
            ("impl_done", {"pr": PR, "head_sha": HEAD}),
            ("tier", {"tier": "A", "reason": "只动 docs", "head_sha": HEAD}),
            ("qa_pass", {"head_sha": HEAD, "run_id": 202}),
            ("review", {"kind": "defect", "head_sha": HEAD, "verdict": "pass"}),
            ("boss_approve", {"pr": PR, "approved_by": "boss"}),
        ],
    )
    def test_returns_equivalent_dict(self, kind: str, payload: dict[str, object]) -> None:
        assert evidence.validate_payload(kind, payload) == payload

    def test_returns_copy_not_the_same_object(self) -> None:
        payload: dict[str, object] = {"pr": PR, "head_sha": HEAD}
        out = evidence.validate_payload("impl_done", payload)
        assert out == payload
        assert out is not payload

    def test_does_not_mutate_original(self) -> None:
        payload: dict[str, object] = {"tier": "C", "reason": "删库脚本", "head_sha": HEAD}
        snapshot = dict(payload)
        evidence.validate_payload("tier", payload)
        assert payload == snapshot

    @pytest.mark.parametrize("head", [HEAD, HEAD64])
    def test_both_head_lengths_accepted(self, head: str) -> None:
        assert evidence.validate_payload("impl_done", {"pr": 1, "head_sha": head})["head_sha"] == head

    def test_reason_length_boundary_200_accepted(self) -> None:
        payload = {"tier": "A", "reason": "x" * 200, "head_sha": HEAD}
        assert evidence.validate_payload("tier", payload)["reason"] == "x" * 200

    def test_pr_boundaries_accepted(self) -> None:
        assert evidence.validate_payload("impl_done", {"pr": 1, "head_sha": HEAD})["pr"] == 1
        assert (
            evidence.validate_payload(
                "impl_done", {"pr": evidence.PR_MAX, "head_sha": HEAD}
            )["pr"]
            == evidence.PR_MAX
        )


class TestValidatePayloadErrors:
    @pytest.mark.parametrize(
        ("kind", "payload", "needle"),
        [
            # head 39 位 / head 含大写
            ("qa_pass", {"head_sha": "a" * 39, "run_id": 1}, "head_sha"),
            ("qa_pass", {"head_sha": "A" * 40, "run_id": 1}, "head_sha"),
            # verdict 写 "PASS" / tier 写 "D"
            ("review", {"kind": "defect", "head_sha": HEAD, "verdict": "PASS"}, "verdict"),
            ("tier", {"tier": "D", "reason": "x", "head_sha": HEAD}, "tier"),
            # reason 空串 / 纯空白 / 超 200 字符
            ("tier", {"tier": "A", "reason": "", "head_sha": HEAD}, "reason"),
            ("tier", {"tier": "A", "reason": "   ", "head_sha": HEAD}, "reason"),
            ("tier", {"tier": "A", "reason": "x" * 201, "head_sha": HEAD}, "reason"),
            # pr 写 0 / 写 13 位毫秒 / bool / 字符串 / 浮点 / 超上限
            ("impl_done", {"pr": 0, "head_sha": HEAD}, "pr"),
            ("impl_done", {"pr": 1793160000000, "head_sha": HEAD}, "pr"),
            ("impl_done", {"pr": True, "head_sha": HEAD}, "pr"),
            ("impl_done", {"pr": "42", "head_sha": HEAD}, "pr"),
            ("impl_done", {"pr": 1.0, "head_sha": HEAD}, "pr"),
            ("impl_done", {"pr": evidence.PR_MAX + 1, "head_sha": HEAD}, "pr"),
            ("boss_approve", {"pr": 0, "approved_by": "boss"}, "pr"),
            # run_id 非正整数
            ("qa_pass", {"head_sha": HEAD, "run_id": 0}, "run_id"),
            ("qa_pass", {"head_sha": HEAD, "run_id": -3}, "run_id"),
            ("qa_pass", {"head_sha": HEAD, "run_id": False}, "run_id"),
            # 缺键 / 多键
            ("impl_done", {"pr": PR}, "缺键"),
            ("impl_done", {"pr": PR, "head_sha": HEAD, "extra": 1}, "多键"),
            # spec_sign 必须恰为空 dict
            ("spec_sign", {"head_sha": HEAD}, "多键"),
            # review 取值域
            ("review", {"kind": "security", "head_sha": HEAD, "verdict": "pass"}, "kind"),
            ("review", {"kind": "defect", "head_sha": HEAD, "verdict": "blocked"}, "verdict"),
            # boss_approve 的 approved_by 必须恰为 boss
            ("boss_approve", {"pr": PR, "approved_by": "pm"}, "approved_by"),
        ],
        ids=[
            "head39",
            "head大写",
            "verdict大写PASS",
            "tierD",
            "reason空串",
            "reason纯空白",
            "reason201",
            "pr0",
            "pr13位",
            "prTrue",
            "pr字符串",
            "pr浮点",
            "pr超上限",
            "boss_pr0",
            "run_id0",
            "run_id负数",
            "run_idFalse",
            "缺键",
            "多键",
            "spec_sign非空",
            "review_kind越界",
            "review_verdict越界",
            "boss_approved_by错",
        ],
    )
    def test_bad_payloads_raise_value_error(
        self, kind: str, payload: dict[str, object], needle: str
    ) -> None:
        with pytest.raises(ValueError, match=needle):
            evidence.validate_payload(kind, payload)

    def test_merged_is_not_emittable(self) -> None:
        """kind "merged" 进 emit 集合：ValueError 且文案点出 trailer 承载。"""
        with pytest.raises(ValueError, match="trailer"):
            evidence.validate_payload("merged", {})
        with pytest.raises(ValueError, match="merge_audit"):
            evidence.validate_payload("merged", {"head_sha": HEAD})

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind 非法"):
            evidence.validate_payload("foo", {})

    def test_payload_must_be_mapping(self) -> None:
        with pytest.raises(ValueError, match="payload 非法"):
            evidence.validate_payload("tier", ["A", "x", HEAD])  # type: ignore[arg-type]


class TestCardFromBranch:
    @pytest.mark.parametrize(
        ("branch", "expected"),
        [
            ("wt/t_f62480b3", "t_f62480b3"),
            ("wt/t_00000000", "t_00000000"),
            ("wt/t_x", None),
            ("wt/t_F62480B3", None),
            ("wt/t_f62480b3x", None),
            ("wt/t_f62480b", None),
            ("feature/x", None),
            ("t_f62480b3", None),
            ("", None),
            ("wt/wt/t_f62480b3", None),
        ],
    )
    def test_branch_shapes(self, branch: str, expected: str | None) -> None:
        assert evidence.card_from_branch(branch) == expected

    def test_non_string_is_none(self) -> None:
        assert evidence.card_from_branch(None) is None
        assert evidence.card_from_branch(123) is None


class TestNormalize:
    def test_sorted_compact_and_unicode_kept(self) -> None:
        text = evidence.normalize({"tier": "A", "reason": "只动 docs"})
        assert text == '{"reason":"只动 docs","tier":"A"}'

    def test_key_order_does_not_matter(self) -> None:
        a = evidence.normalize({"b": 1, "a": 2})
        b = evidence.normalize({"a": 2, "b": 1})
        assert a == b == '{"a":2,"b":1}'

    def test_empty_payload(self) -> None:
        assert evidence.normalize({}) == "{}"

    def test_idempotent(self) -> None:
        payload = {"pr": PR, "head_sha": HEAD}
        once = evidence.normalize(payload)
        assert evidence.normalize({"pr": PR, "head_sha": HEAD}) == once


class TestJudgePass:
    def test_four_slots_bound_to_head_pass(self) -> None:
        judgment = evidence.judge(base_events(), HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_PASS
        assert judgment.ok is True
        assert judgment.missing == ()
        assert "四槽齐" in judgment.detail

    def test_slots_follow_req_slot_order(self) -> None:
        judgment = evidence.judge(base_events(), HEAD, PR)
        assert tuple(slot.name for slot in judgment.slots) == evidence.REQ_SLOTS
        assert all(slot.state == evidence.SLOT_OK for slot in judgment.slots)

    def test_64_hex_head_passes(self) -> None:
        events = [
            evidence.Event(
                id=1,
                kind="tier",
                payload={"tier": "A", "reason": "docs", "head_sha": HEAD64},
                run_id=1,
                profile="pm",
            ),
            evidence.Event(
                id=2,
                kind="qa_pass",
                payload={"head_sha": HEAD64, "run_id": 2},
                run_id=2,
                profile="qa",
            ),
            evidence.Event(
                id=3,
                kind="review",
                payload={"kind": "defect", "head_sha": HEAD64, "verdict": "pass"},
                run_id=1,
                profile="pm",
            ),
            evidence.Event(
                id=4,
                kind="review",
                payload={"kind": "regression", "head_sha": HEAD64, "verdict": "pass"},
                run_id=2,
                profile="qa",
            ),
        ]
        assert evidence.judge(events, HEAD64, PR).verdict == evidence.VERDICT_PASS

    def test_review_slots_accept_either_pm_or_qa(self) -> None:
        """review 的允许集合是 {pm, qa}：四人四槽之外换手也不算角色不符。"""
        events = base_events()
        events[2] = evidence.Event(
            id=3,
            kind="review",
            payload={"kind": "defect", "head_sha": HEAD, "verdict": "pass"},
            run_id=202,
            profile="qa",
        )
        events[3] = evidence.Event(
            id=4,
            kind="review",
            payload={"kind": "regression", "head_sha": HEAD, "verdict": "pass"},
            run_id=101,
            profile="pm",
        )
        assert evidence.judge(events, HEAD, PR).verdict == evidence.VERDICT_PASS


class TestVerdictMissingEvidence:
    """verdict「缺证据」。"""

    def test_missing_qa_pass(self) -> None:
        events = [event for event in base_events() if event.kind != "qa_pass"]
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_MISSING
        assert judgment.missing == ("qa_pass",)
        assert states(judgment)["qa_pass"] == evidence.SLOT_MISSING

    def test_no_events_at_all(self) -> None:
        judgment = evidence.judge([], HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_MISSING
        assert judgment.missing == evidence.REQ_SLOTS

    def test_missing_tier(self) -> None:
        events = [event for event in base_events() if event.kind != "tier"]
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_MISSING
        assert judgment.missing == ("tier",)

    def test_missing_only_regression_slot(self) -> None:
        events = [
            event
            for event in base_events()
            if not (event.kind == "review" and event.payload["kind"] == "regression")
        ]
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_MISSING
        assert judgment.missing == ("review:regression",)


class TestVerdictHeadMismatch:
    """verdict「证据对新 head 无效」。"""

    def test_qa_pass_on_old_head(self) -> None:
        events = base_events()
        events[1] = evidence.Event(
            id=2,
            kind="qa_pass",
            payload={"head_sha": OLD_HEAD, "run_id": 202},
            run_id=202,
            profile="qa",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_HEAD_MISMATCH
        assert states(judgment)["qa_pass"] == evidence.SLOT_HEAD_MISMATCH
        assert evidence.VERDICT_HEAD_MISMATCH in judgment.detail

    def test_stale_word_never_appears(self) -> None:
        events = base_events()
        events[0] = evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "A", "reason": "x", "head_sha": OLD_HEAD},
            run_id=101,
            profile="pm",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert "STALE" not in judgment.detail
        assert "STALE" not in judgment.verdict


class TestVerdictBlocked:
    """verdict「双问阻塞」。"""

    def test_defect_block_blocks(self) -> None:
        events = base_events()
        events[2] = evidence.Event(
            id=3,
            kind="review",
            payload={"kind": "defect", "head_sha": HEAD, "verdict": "block"},
            run_id=101,
            profile="pm",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_BLOCKED
        assert states(judgment)["review:defect"] == evidence.SLOT_BLOCKED
        assert states(judgment)["review:regression"] == evidence.SLOT_OK

    def test_regression_block_blocks(self) -> None:
        events = base_events()
        events[3] = evidence.Event(
            id=4,
            kind="review",
            payload={"kind": "regression", "head_sha": HEAD, "verdict": "block"},
            run_id=202,
            profile="qa",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_BLOCKED
        assert states(judgment)["review:regression"] == evidence.SLOT_BLOCKED


class TestVerdictNoBossApproval:
    """verdict「缺老板点头」。"""

    def test_tier_c_without_boss(self) -> None:
        events = base_events()
        events[0] = evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "C", "reason": "碰 CI 权限", "head_sha": HEAD},
            run_id=101,
            profile="pm",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_NO_BOSS
        assert "boss_approve" in judgment.detail

    def test_tier_c_with_boss_on_other_pr(self) -> None:
        events = base_events()
        events[0] = evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "C", "reason": "删库脚本", "head_sha": HEAD},
            run_id=101,
            profile="pm",
        )
        events.append(
            evidence.Event(
                id=9,
                kind="boss_approve",
                payload={"pr": PR + 1, "approved_by": "boss"},
                run_id=777,
                profile="paimeng",
            )
        )
        assert evidence.judge(events, HEAD, PR).verdict == evidence.VERDICT_NO_BOSS

    def test_tier_c_with_matching_boss_passes(self) -> None:
        events = base_events()
        events[0] = evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "C", "reason": "碰 .github/", "head_sha": HEAD},
            run_id=101,
            profile="pm",
        )
        events.append(
            evidence.Event(
                id=9,
                kind="boss_approve",
                payload={"pr": PR, "approved_by": "boss"},
                run_id=777,
                profile="paimeng",
            )
        )
        assert evidence.judge(events, HEAD, PR).verdict == evidence.VERDICT_PASS

    def test_boss_not_needed_for_tier_b(self) -> None:
        assert evidence.judge(base_events(), HEAD, PR).verdict == evidence.VERDICT_PASS


class TestVerdictCrossCard:
    """verdict「证据串卡」。"""

    def test_impl_done_on_other_pr(self) -> None:
        events = base_events()
        events.append(
            evidence.Event(
                id=8,
                kind="impl_done",
                payload={"pr": PR + 5, "head_sha": HEAD},
                run_id=303,
                profile="engineer",
            )
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_CROSS_CARD
        assert "impl_done" in judgment.detail

    def test_impl_done_same_pr_is_fine(self) -> None:
        events = base_events()
        events.append(
            evidence.Event(
                id=8,
                kind="impl_done",
                payload={"pr": PR, "head_sha": HEAD},
                run_id=303,
                profile="engineer",
            )
        )
        assert evidence.judge(events, HEAD, PR).verdict == evidence.VERDICT_PASS

    def test_impl_done_latest_wins(self) -> None:
        """旧的一条串卡、新的一条对得上 → 不算串卡（同 kind 取 id 最大）。"""
        events = base_events()
        events.append(
            evidence.Event(
                id=7,
                kind="impl_done",
                payload={"pr": PR + 5, "head_sha": HEAD},
                run_id=303,
                profile="engineer",
            )
        )
        events.append(
            evidence.Event(
                id=8,
                kind="impl_done",
                payload={"pr": PR, "head_sha": HEAD},
                run_id=303,
                profile="engineer",
            )
        )
        assert evidence.judge(events, HEAD, PR).verdict == evidence.VERDICT_PASS


class TestVerdictRoleMismatch:
    """verdict「角色不符」：判定只信 run→profile，不信 kind 本身。"""

    def test_pm_emitted_qa_pass(self) -> None:
        events = base_events()
        events[1] = evidence.Event(
            id=2,
            kind="qa_pass",
            payload={"head_sha": HEAD, "run_id": 202},
            run_id=101,
            profile="pm",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_ROLE_MISMATCH
        assert states(judgment)["qa_pass"] == evidence.SLOT_ROLE_MISMATCH
        assert "pm" in judgment.detail

    def test_missing_run_id_is_role_mismatch(self) -> None:
        events = base_events()
        events[2] = evidence.Event(
            id=3,
            kind="review",
            payload={"kind": "defect", "head_sha": HEAD, "verdict": "pass"},
            run_id=None,
            profile="pm",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_ROLE_MISMATCH
        assert states(judgment)["review:defect"] == evidence.SLOT_ROLE_MISMATCH

    def test_missing_profile_is_role_mismatch(self) -> None:
        events = base_events()
        events[0] = evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "A", "reason": "docs", "head_sha": HEAD},
            run_id=101,
            profile=None,
        )
        assert evidence.judge(events, HEAD, PR).verdict == evidence.VERDICT_ROLE_MISMATCH

    def test_engineer_emitted_review_is_role_mismatch(self) -> None:
        events = base_events()
        events[2] = evidence.Event(
            id=3,
            kind="review",
            payload={"kind": "defect", "head_sha": HEAD, "verdict": "pass"},
            run_id=303,
            profile="engineer",
        )
        assert evidence.judge(events, HEAD, PR).verdict == evidence.VERDICT_ROLE_MISMATCH


class TestJudgePriority:
    """多个问题同时存在时按 角色不符 > 缺证据 > head > 双问 > 缺老板点头 > 串卡 取一个。"""

    def test_role_mismatch_beats_missing(self) -> None:
        events = [
            event
            for event in base_events()
            if event.kind not in {"qa_pass", "tier"}
        ]
        events[0] = evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "A", "reason": "docs", "head_sha": HEAD},
            run_id=101,
            profile="engineer",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_ROLE_MISMATCH
        assert evidence.VERDICT_MISSING in judgment.detail
        assert "qa_pass" in judgment.detail

    def test_missing_beats_head_mismatch(self) -> None:
        events = [
            event
            for event in base_events()
            if not (event.kind == "review" and event.payload["kind"] == "regression")
        ]
        events[1] = evidence.Event(
            id=2,
            kind="qa_pass",
            payload={"head_sha": OLD_HEAD, "run_id": 202},
            run_id=202,
            profile="qa",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_MISSING
        assert evidence.VERDICT_HEAD_MISMATCH in judgment.detail

    def test_head_mismatch_beats_blocked(self) -> None:
        events = base_events()
        events[0] = evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "A", "reason": "docs", "head_sha": OLD_HEAD},
            run_id=101,
            profile="pm",
        )
        events[2] = evidence.Event(
            id=3,
            kind="review",
            payload={"kind": "defect", "head_sha": HEAD, "verdict": "block"},
            run_id=101,
            profile="pm",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_HEAD_MISMATCH
        assert evidence.VERDICT_BLOCKED in judgment.detail

    def test_blocked_beats_no_boss(self) -> None:
        events = base_events()
        events[0] = evidence.Event(
            id=1,
            kind="tier",
            payload={"tier": "C", "reason": "碰 .github/", "head_sha": HEAD},
            run_id=101,
            profile="pm",
        )
        events[2] = evidence.Event(
            id=3,
            kind="review",
            payload={"kind": "defect", "head_sha": HEAD, "verdict": "block"},
            run_id=101,
            profile="pm",
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_BLOCKED
        assert evidence.VERDICT_NO_BOSS in judgment.detail

    def test_cross_card_is_lowest_priority(self) -> None:
        events = [
            event
            for event in base_events()
            if not (event.kind == "review" and event.payload["kind"] == "defect")
        ]
        events.append(
            evidence.Event(
                id=8,
                kind="impl_done",
                payload={"pr": PR + 5, "head_sha": HEAD},
                run_id=303,
                profile="engineer",
            )
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_MISSING
        assert evidence.VERDICT_CROSS_CARD in judgment.detail


class TestJudgeLatestWins:
    def test_newer_qa_pass_overrides_old_head(self) -> None:
        events = base_events()
        events.insert(
            0,
            evidence.Event(
                id=0,
                kind="qa_pass",
                payload={"head_sha": OLD_HEAD, "run_id": 201},
                run_id=201,
                profile="qa",
            ),
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_PASS

    def test_newer_pass_overrides_old_block(self) -> None:
        events = base_events()
        events.insert(
            2,
            evidence.Event(
                id=0,
                kind="review",
                payload={"kind": "defect", "head_sha": HEAD, "verdict": "block"},
                run_id=101,
                profile="pm",
            ),
        )
        judgment = evidence.judge(events, HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_PASS
        assert states(judgment)["review:defect"] == evidence.SLOT_OK

    def test_slot_event_is_the_latest(self) -> None:
        events = base_events()
        events.append(
            evidence.Event(
                id=99,
                kind="qa_pass",
                payload={"head_sha": HEAD, "run_id": 203},
                run_id=203,
                profile="qa",
            )
        )
        judgment = evidence.judge(events, HEAD, PR)
        qa_slot = next(slot for slot in judgment.slots if slot.name == "qa_pass")
        assert qa_slot.event is not None
        assert qa_slot.event.id == 99


class TestJudgeOptionalKnobs:
    def test_pr_none_skips_pr_bound_checks(self) -> None:
        """--pr 没给：跨卡与档位-C-需老板点头不判（其余照判）。"""
        events = base_events()
        events.append(
            evidence.Event(
                id=8,
                kind="impl_done",
                payload={"pr": 999, "head_sha": HEAD},
                run_id=303,
                profile="engineer",
            )
        )
        assert evidence.judge(events, HEAD).verdict == evidence.VERDICT_PASS

    def test_tier_required_match(self) -> None:
        events = base_events()
        assert evidence.judge(events, HEAD, PR, tier_required="B").verdict == (
            evidence.VERDICT_PASS
        )

    def test_tier_required_mismatch_is_missing(self) -> None:
        judgment = evidence.judge(base_events(), HEAD, PR, tier_required="C")
        assert judgment.verdict == evidence.VERDICT_MISSING
        assert states(judgment)["tier"] == evidence.SLOT_MISSING
        assert "要求 C" in judgment.detail

    def test_events_accept_any_iterable(self) -> None:
        """iterable（generator）也能吃：内部先收成 tuple。"""
        judgment = evidence.judge((event for event in base_events()), HEAD, PR)
        assert judgment.verdict == evidence.VERDICT_PASS


class TestZeroIo:
    def test_module_does_not_import_io_modules(self) -> None:
        """零 IO 是契约：本模块不 import sqlite3 / subprocess，也不读环境变量。

        判据用 AST（不 grep 源码：模块 docstring 里就写着「不 import sqlite3」）。
        """
        import ast
        from pathlib import Path

        module_file = evidence.__file__
        assert module_file is not None
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "sqlite3" not in imported
        assert "subprocess" not in imported
        assert "os" not in imported
