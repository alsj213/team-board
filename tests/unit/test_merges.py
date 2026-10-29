"""merges.py 的单元测试：认账三字段的解析 / 校验 / 生成 / 渲染。

这些期望值是 Ⅵ-2（``merge-audit``）与 Ⅵ-3（``merge-record``）接线的契约地基：
以后要放宽或收紧判定，先改这里的期望值（红→绿），别直接改实现。
"""

from __future__ import annotations

import pytest

from team_board.merges import (
    KIND_BAD_FORMAT,
    KIND_DUPLICATE,
    KIND_MISMATCH,
    KIND_MISSING,
    KIND_NOT_MERGE,
    KIND_NOT_TRAILER,
    MergeAuditError,
    MergeRecord,
    MergeReport,
    Problem,
    build_message,
    check_message,
    legacy_record,
    parse_trailers,
    record_from_commit,
    render_report,
)

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
CARD = "t_c9f5866a"
BY = "pm/run137"

GOOD = (
    "Merge PR #15（t_c9f5866a）\n"
    "\n"
    "Merge-By: pm/run137\n"
    "Merge-Card: t_c9f5866a\n"
    "Merge-PR: #15\n"
)


def kinds(problems: tuple[Problem, ...]) -> list[str]:
    return [problem.kind for problem in problems]


class TestParseTrailers:
    def test_full_trailer_block_is_parsed_without_problems(self):
        values, problems = parse_trailers(GOOD)
        assert values == {
            "Merge-By": "pm/run137",
            "Merge-Card": "t_c9f5866a",
            "Merge-PR": "#15",
        }
        assert problems == ()

    def test_blank_lines_after_block_are_skipped(self):
        values, problems = parse_trailers(GOOD + "\n\n")
        assert values["Merge-PR"] == "#15"
        assert problems == ()

    def test_other_trailer_keys_are_ignored_silently(self):
        message = GOOD + "Signed-off-by: someone <s@example.com>\n"
        values, problems = parse_trailers(message)
        assert "Signed-off-by" not in values
        assert values["Merge-By"] == "pm/run137"
        assert problems == ()

    def test_duplicate_key_reports_once_and_keeps_last_value(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-Card: t_c9f5866a\n"
            "Merge-By: pm/run138\n"
            "Merge-PR: #15\n"
        )
        values, problems = parse_trailers(message)
        assert kinds(problems) == [KIND_DUPLICATE]
        assert "Merge-By" in problems[0].detail
        assert "2" in problems[0].detail
        assert values["Merge-By"] == "pm/run138"
        assert problems[0].sha == ""

    def test_key_in_body_is_reported_as_not_in_trailer_zone(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "Merge-Card: t_c9f5866a\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-PR: #15\n"
        )
        values, problems = parse_trailers(message)
        assert kinds(problems) == [KIND_NOT_TRAILER]
        assert "Merge-Card" in problems[0].detail
        # 块外出现的键不算块内取值
        assert "Merge-Card" not in values

    def test_empty_message_yields_nothing(self):
        values, problems = parse_trailers("")
        assert values == {}
        assert problems == ()

    def test_crlf_message_does_not_leak_carriage_return(self):
        message = GOOD.replace("\n", "\r\n")
        values, problems = parse_trailers(message)
        assert problems == ()
        assert values["Merge-PR"] == "#15"


class TestRecordFromCommit:
    def test_full_fields_make_a_clean_record(self):
        record, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), GOOD)
        assert problems == ()
        assert record == MergeRecord(
            sha=SHA,
            subject="Merge PR #15",
            parents=("p1", "p2"),
            by="pm/run137",
            card="t_c9f5866a",
            pr="#15",
            source="trailer",
        )

    def test_missing_merge_pr_reports_exactly_one_missing_field(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-Card: t_c9f5866a\n"
        )
        record, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_MISSING]
        assert "Merge-PR" in problems[0].detail
        assert problems[0].sha == SHA
        assert record.pr is None
        assert record.by == "pm/run137"

    def test_key_only_in_body_is_not_in_trailer_zone_instead_of_missing(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "Merge-Card: t_c9f5866a\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-PR: #15\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_NOT_TRAILER]
        assert "Merge-Card" in problems[0].detail
        assert KIND_MISSING not in kinds(problems)

    def test_single_parent_is_reported_as_not_a_merge_commit(self):
        record, problems = record_from_commit(SHA, "Merge PR #15", ("a",), GOOD)
        assert kinds(problems) == [KIND_NOT_MERGE]
        assert problems[0].sha == SHA
        # 不抛异常；字段仍按 message 填齐，交由调用方按 problems 判
        assert record.card == "t_c9f5866a"
        assert record.source == "trailer"

    def test_no_parents_is_reported_as_not_a_merge_commit(self):
        _, problems = record_from_commit(SHA, "Merge PR #15", (), GOOD)
        assert kinds(problems) == [KIND_NOT_MERGE]

    def test_non_merge_commit_with_bad_trailers_reports_both(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-Card: t_c9f5866a\n"
            "Merge-PR: #0\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("a",), message)
        assert kinds(problems) == [KIND_NOT_MERGE, KIND_BAD_FORMAT]


class TestFormatValidation:
    @pytest.mark.parametrize(
        "value",
        ["pm", "pm-run137", "run137", "PM/run1", "pm/run"],
    )
    def test_illegal_merge_by_values(self, value):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            f"Merge-By: {value}\n"
            "Merge-Card: t_c9f5866a\n"
            "Merge-PR: #15\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_BAD_FORMAT]
        assert "Merge-By" in problems[0].detail
        assert value in problems[0].detail

    def test_illegal_merge_card_value(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-Card: t_ZZZZZZZZ\n"
            "Merge-PR: #15\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_BAD_FORMAT]
        assert "Merge-Card" in problems[0].detail
        assert "t_ZZZZZZZZ" in problems[0].detail

    @pytest.mark.parametrize("value", ["0", "-1", "#", "#abc", ""])
    def test_illegal_merge_pr_values(self, value):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-Card: t_c9f5866a\n"
            f"Merge-PR: {value}\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_BAD_FORMAT]
        assert "Merge-PR" in problems[0].detail

    def test_huge_pr_number_is_accepted(self):
        message = (
            "Merge PR 1793160000000（t_c9f5866a）\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-Card: t_c9f5866a\n"
            "Merge-PR: #1793160000000\n"
        )
        record, problems = record_from_commit(SHA, "Merge PR 1793160000000", ("p1", "p2"), message)
        assert problems == ()
        assert record.pr == "#1793160000000"

    def test_all_three_keys_missing_reports_three_problems(self):
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), "just a body\n")
        assert kinds(problems) == [KIND_MISSING, KIND_MISSING, KIND_MISSING]


class TestLegacyRecord:
    def test_default_github_subject_hits(self):
        subject = "Merge pull request #9 from alsj213/wt/t_f43fa954"
        record = legacy_record(SHA, subject)
        assert record == MergeRecord(
            sha=SHA,
            subject=subject,
            parents=(),
            by=None,
            card="t_f43fa954",
            pr="#9",
            source="legacy",
        )

    def test_head_branch_without_wt_prefix_hits(self):
        record = legacy_record(SHA, "Merge pull request #3 from alsj213/t_0b50ab6f")
        assert record is not None
        assert record.card == "t_0b50ab6f"
        assert record.pr == "#3"

    def test_other_subject_misses(self):
        assert legacy_record(SHA, "feat(t_c9f5866a): add merges.py") is None
        assert legacy_record(SHA, "Merge pull request #9 from alsj213/main") is None


class TestCheckMessage:
    def test_build_then_check_closes_the_loop(self):
        message = build_message(card=CARD, by=BY, pr=15)
        assert check_message(message) == ()

    def test_build_then_check_closes_the_loop_with_explicit_subject(self):
        message = build_message(card=CARD, by=BY, pr=15, subject="Merge PR #15（t_c9f5866a）")
        assert check_message(message, card=CARD, by=BY, pr=15) == ()

    def test_empty_message_reports_three_missing_fields(self):
        assert kinds(check_message("")) == [KIND_MISSING, KIND_MISSING, KIND_MISSING]

    def test_mismatching_card_parameter(self):
        problems = check_message(build_message(card=CARD, by=BY, pr=15), card="t_0000aaaa")
        assert kinds(problems) == [KIND_MISMATCH]
        assert "Merge-Card" in problems[0].detail
        assert problems[0].sha == ""

    def test_mismatching_by_and_pr_parameters(self):
        problems = check_message(build_message(card=CARD, by=BY, pr=15), by="pm/run138", pr=16)
        assert kinds(problems) == [KIND_MISMATCH, KIND_MISMATCH]

    def test_matching_parameters_add_no_problems(self):
        problems = check_message(build_message(card=CARD, by=BY, pr=15), card=CARD, by=BY, pr=15)
        assert problems == ()


class TestBuildMessage:
    def test_default_subject_uses_expected_wording(self):
        assert build_message(card=CARD, by=BY, pr=15) == (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "Merge-By: pm/run137\n"
            "Merge-Card: t_c9f5866a\n"
            "Merge-PR: #15\n"
        )

    def test_output_has_exactly_three_segments(self):
        message = build_message(card=CARD, by=BY, pr=15, subject="Merge PR #15")
        assert message.split("\n\n") == [
            "Merge PR #15",
            "Merge-By: pm/run137\nMerge-Card: t_c9f5866a\nMerge-PR: #15\n",
        ]

    @pytest.mark.parametrize("card", ["t_ZZZZZZZZ", "t_c9f5866", "", "c9f5866a"])
    def test_illegal_card_raises(self, card):
        with pytest.raises(MergeAuditError):
            build_message(card=card, by=BY, pr=15)

    @pytest.mark.parametrize("by", ["pm", "pm-run137", "run137", "PM/run1", "pm/run"])
    def test_illegal_by_raises(self, by):
        with pytest.raises(MergeAuditError):
            build_message(card=CARD, by=by, pr=15)

    @pytest.mark.parametrize("pr", [0, -1, -15])
    def test_non_positive_pr_raises(self, pr):
        with pytest.raises(MergeAuditError):
            build_message(card=CARD, by=BY, pr=pr)

    def test_bool_pr_raises(self):
        with pytest.raises(MergeAuditError):
            build_message(card=CARD, by=BY, pr=True)

    def test_illegal_argument_never_returns_message(self):
        # 参数非法时既不静默生成 message，也不返回降级结果
        with pytest.raises(MergeAuditError):
            build_message(card="t_ZZZZZZZZ", by=BY, pr=15)


class TestRenderReport:
    def _report(self, records, problems, history=()) -> MergeReport:
        return MergeReport(
            repo="/repo/team-board",
            baseline="origin/main",
            rev="HEAD",
            records=records,
            problems=problems,
            history=history,
        )

    def test_clean_report_ends_with_clean_conclusion(self):
        record = legacy_record(SHA, "Merge pull request #9 from alsj213/wt/t_f43fa954")
        assert record is not None
        trailer_record, _ = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), GOOD)
        text = render_report(self._report((trailer_record,), (), (record,)))
        lines = text.split("\n")
        assert lines[0] == "合并认账稽核（合并提交三字段）"
        assert "仓库: /repo/team-board" in text
        assert "基线: origin/main" in text
        assert "区间: HEAD" in text
        assert "判定（1 条）:" in text
        assert "  1. a1b2c3d t_c9f5866a #15 by=pm/run137" in text
        assert "问题（0 处）:" in text
        assert "历史（未判定，1 条）:" in text
        assert "  1. a1b2c3d t_f43fa954 #9 by=无法判定" in text
        assert lines[-1] == "结论：无问题"
        assert "退出码" not in text

    def test_one_problem_report_ends_with_count(self):
        problem = Problem(KIND_MISSING, SHA, "Merge-PR 缺失")
        text = render_report(self._report((), (problem,), ()))
        assert "判定（0 条）:" in text
        assert "问题（1 处）:" in text
        assert f"a1b2c3d {KIND_MISSING}：Merge-PR 缺失" in text
        assert text.split("\n")[-1] == "结论：发现 1 个问题"
        assert "退出码" not in text

    def test_ok_property(self):
        assert self._report((), ()).ok is True
        assert self._report((), (Problem(KIND_MISSING, "", "-"),)).ok is False


#: 同一键在 trailer 块里出现两次（结构类问题：重复字段）
DUP_MESSAGE = (
    "Merge PR #15（t_c9f5866a）\n"
    "\n"
    "Merge-By: pm/run1\n"
    "Merge-Card: t_c9f5866a\n"
    "Merge-PR: #15\n"
    "Merge-By: pm/run2\n"
)

#: 键写在正文里、末尾块里没有（结构类问题：字段未落在 trailer 区）
MISPLACED_MESSAGE = (
    "Merge-Card: t_c9f5866a\n"
    "正文第二行\n"
    "\n"
    "Merge-By: pm/run1\n"
    "Merge-PR: #15\n"
)

STRUCTURAL_CASES = pytest.mark.parametrize(
    ("message", "kind"),
    [(DUP_MESSAGE, KIND_DUPLICATE), (MISPLACED_MESSAGE, KIND_NOT_TRAILER)],
    ids=["重复字段", "字段未落在 trailer 区"],
)


class TestRecordFromCommitCarriesSha:
    """F1：``record_from_commit`` 的结构类问题必须带提交 sha，不能是 ``""``。"""

    @STRUCTURAL_CASES
    def test_every_problem_carries_the_commit_sha(self, message, kind):
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert [problem.kind for problem in problems] == [kind]
        # 每一条都是入参 sha —— 不是只有「缺字段 / 格式非法」那两条路才带
        assert [problem.sha for problem in problems] == [SHA]

    @STRUCTURAL_CASES
    def test_render_report_line_starts_with_short_sha_not_dash(self, message, kind):
        record, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        text = render_report(
            MergeReport(
                repo="/repo/team-board",
                baseline="origin/main",
                rev="HEAD",
                records=(record,),
                problems=problems,
                history=(),
            )
        )
        lines = [line for line in text.split("\n") if kind in line]
        assert len(lines) == 1
        assert lines[0].startswith(f"  1. {SHA[:7]} ")
        assert f"  1. - {kind}" not in text

    @STRUCTURAL_CASES
    def test_parse_trailers_keeps_empty_sha_for_candidate_message(self, message, kind):
        # 对照：候选 message 没有提交上下文，口径仍是 ""
        _, problems = parse_trailers(message)
        assert [problem.kind for problem in problems] == [kind]
        assert [problem.sha for problem in problems] == [""]


class TestBuildMessageParameterValidation:
    """F2：三参数非法一律 ``MergeAuditError``（不漏 ``TypeError``、不放过尾随换行）。"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"card": None, "by": BY, "pr": 15},
            {"card": CARD, "by": 123, "pr": 15},
            {"card": CARD, "by": BY, "pr": "15"},
            {"card": CARD, "by": BY, "pr": 0},
            {"card": CARD, "by": BY, "pr": -1},
            {"card": CARD + "\n", "by": BY, "pr": 15},
            {"card": CARD, "by": BY + "\n", "pr": 15},
        ],
        ids=[
            "card=None",
            "by=int",
            "pr=str",
            "pr=0",
            "pr=-1",
            "card 尾随换行",
            "by 尾随换行",
        ],
    )
    def test_illegal_arguments_raise_merge_audit_error(self, kwargs):
        with pytest.raises(MergeAuditError):
            build_message(**kwargs)

    def test_trailing_newline_never_yields_a_message_that_fails_check(self):
        # 修前：放行尾随换行 → build 出的 message 喂 check_message 报 2 条「字段未落在 trailer 区」
        with pytest.raises(MergeAuditError):
            message = build_message(card=CARD + "\n", by=BY, pr=15)
            assert check_message(message) == ()  # 若能产出，也必须闭环为零问题


class TestBuildMessageSubject:
    """F3：``subject`` 去首尾空白后为空 → 抛错；``None`` 走契约默认标题。"""

    @pytest.mark.parametrize(
        "subject",
        ["", "   ", "\n", "\t \n "],
        ids=["empty", "spaces", "newline", "mixed"],
    )
    def test_blank_subject_raises(self, subject):
        with pytest.raises(MergeAuditError):
            build_message(card=CARD, by=BY, pr=15, subject=subject)

    def test_none_subject_uses_contract_default_title(self):
        message = build_message(card=CARD, by=BY, pr=15, subject=None)
        assert message.splitlines()[0] == f"Merge PR #15（{CARD}）"
        assert message.split("\n\n")[0] == f"Merge PR #15（{CARD}）"

    def test_explicit_subject_is_kept_verbatim(self):
        message = build_message(card=CARD, by=BY, pr=15, subject="Merge PR #15")
        assert message.splitlines()[0] == "Merge PR #15"


class TestLegacyRecordZeroPr:
    """F4：``#0`` 与 trailer 路径的 ``MERGE_PR_RE`` 口径对齐。"""

    @pytest.mark.parametrize(
        "subject",
        [
            "Merge pull request #0 from alsj213/wt/t_c9f5866a",
            "Merge pull request #00 from alsj213/wt/t_c9f5866a",
        ],
        ids=["#0", "#00"],
    )
    def test_non_positive_pr_returns_none(self, subject):
        assert legacy_record(SHA, subject) is None

    def test_normal_pr_keeps_contract_unchanged(self):
        record = legacy_record(SHA, "Merge pull request #15 from alsj213/wt/t_c9f5866a")
        assert record is not None
        assert record.pr == "#15"
        assert record.by is None
        assert record.parents == ()
        assert record.card == "t_c9f5866a"
        assert record.source == "legacy"
