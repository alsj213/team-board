"""merges.py 的单元测试：认账三字段的解析 / 校验 / 生成 / 渲染。

这些期望值是 Ⅵ-2（``merge-audit``）与 Ⅵ-3（``merge-record``）接线的契约地基：
以后要放宽或收紧判定，先改这里的期望值（红→绿），别直接改实现。

末段 ``TestGitEnvIsolation`` 是唯一碰真 git 的一节：锁 ``_git`` 的执行环境 ——
父进程继承来的 ``GIT_DIR`` / ``GIT_WORK_TREE`` 不许把稽核引到另一个仓库
（卡 t_11ef8cf3）。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from team_board.merges import (
    KIND_BAD_FORMAT,
    KIND_DUPLICATE,
    KIND_MERGED_BY_MISMATCH,
    KIND_MISMATCH,
    KIND_MISSING,
    KIND_NOT_MERGE,
    KIND_NOT_TRAILER,
    MergeAuditError,
    MergeRecord,
    MergeReport,
    Problem,
    audit,
    build_message,
    card_assignee,
    check_merged_by,
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
    "merged-by: pm/run137\n"
    "merge-card: t_c9f5866a\n"
    "merge-pr: #15\n"
)


def kinds(problems: tuple[Problem, ...]) -> list[str]:
    return [problem.kind for problem in problems]


class TestParseTrailers:
    def test_full_trailer_block_is_parsed_without_problems(self):
        values, problems = parse_trailers(GOOD)
        assert values == {
            "merged-by": "pm/run137",
            "merge-card": "t_c9f5866a",
            "merge-pr": "#15",
        }
        assert problems == ()

    def test_blank_lines_after_block_are_skipped(self):
        values, problems = parse_trailers(GOOD + "\n\n")
        assert values["merge-pr"] == "#15"
        assert problems == ()

    def test_other_trailer_keys_are_ignored_silently(self):
        message = GOOD + "Signed-off-by: someone <s@example.com>\n"
        values, problems = parse_trailers(message)
        assert "Signed-off-by" not in values
        assert values["merged-by"] == "pm/run137"
        assert problems == ()

    def test_duplicate_key_reports_once_and_keeps_last_value(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "merged-by: pm/run137\n"
            "merge-card: t_c9f5866a\n"
            "merged-by: pm/run138\n"
            "merge-pr: #15\n"
        )
        values, problems = parse_trailers(message)
        assert kinds(problems) == [KIND_DUPLICATE]
        assert "merged-by" in problems[0].detail
        assert "2" in problems[0].detail
        assert values["merged-by"] == "pm/run138"
        assert problems[0].sha == ""

    def test_key_in_body_is_reported_as_not_in_trailer_zone(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "merge-card: t_c9f5866a\n"
            "\n"
            "merged-by: pm/run137\n"
            "merge-pr: #15\n"
        )
        values, problems = parse_trailers(message)
        assert kinds(problems) == [KIND_NOT_TRAILER]
        assert "merge-card" in problems[0].detail
        # 块外出现的键不算块内取值
        assert "merge-card" not in values

    def test_empty_message_yields_nothing(self):
        values, problems = parse_trailers("")
        assert values == {}
        assert problems == ()

    def test_crlf_message_does_not_leak_carriage_return(self):
        message = GOOD.replace("\n", "\r\n")
        values, problems = parse_trailers(message)
        assert problems == ()
        assert values["merge-pr"] == "#15"


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
            "merged-by: pm/run137\n"
            "merge-card: t_c9f5866a\n"
        )
        record, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_MISSING]
        assert "merge-pr" in problems[0].detail
        assert problems[0].sha == SHA
        assert record.pr is None
        assert record.by == "pm/run137"

    def test_key_only_in_body_is_not_in_trailer_zone_instead_of_missing(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "merge-card: t_c9f5866a\n"
            "\n"
            "merged-by: pm/run137\n"
            "merge-pr: #15\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_NOT_TRAILER]
        assert "merge-card" in problems[0].detail
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
            "merged-by: pm/run137\n"
            "merge-card: t_c9f5866a\n"
            "merge-pr: #0\n"
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
            f"merged-by: {value}\n"
            "merge-card: t_c9f5866a\n"
            "merge-pr: #15\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_BAD_FORMAT]
        assert "merged-by" in problems[0].detail
        assert value in problems[0].detail

    def test_illegal_merge_card_value(self):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "merged-by: pm/run137\n"
            "merge-card: t_ZZZZZZZZ\n"
            "merge-pr: #15\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_BAD_FORMAT]
        assert "merge-card" in problems[0].detail
        assert "t_ZZZZZZZZ" in problems[0].detail

    @pytest.mark.parametrize("value", ["0", "-1", "#", "#abc", ""])
    def test_illegal_merge_pr_values(self, value):
        message = (
            "Merge PR #15（t_c9f5866a）\n"
            "\n"
            "merged-by: pm/run137\n"
            "merge-card: t_c9f5866a\n"
            f"merge-pr: {value}\n"
        )
        _, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        assert kinds(problems) == [KIND_BAD_FORMAT]
        assert "merge-pr" in problems[0].detail

    def test_huge_pr_number_is_accepted(self):
        message = (
            "Merge PR 1793160000000（t_c9f5866a）\n"
            "\n"
            "merged-by: pm/run137\n"
            "merge-card: t_c9f5866a\n"
            "merge-pr: #1793160000000\n"
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
        assert "merge-card" in problems[0].detail
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
            "merged-by: pm/run137\n"
            "merge-card: t_c9f5866a\n"
            "merge-pr: #15\n"
        )

    def test_output_has_exactly_three_segments(self):
        message = build_message(card=CARD, by=BY, pr=15, subject="Merge PR #15")
        assert message.split("\n\n") == [
            "Merge PR #15",
            "merged-by: pm/run137\nmerge-card: t_c9f5866a\nmerge-pr: #15\n",
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
        problem = Problem(KIND_MISSING, SHA, "merge-pr 缺失")
        text = render_report(self._report((), (problem,), ()))
        assert "判定（0 条）:" in text
        assert "问题（1 处）:" in text
        assert f"a1b2c3d {KIND_MISSING}：merge-pr 缺失" in text
        assert text.split("\n")[-1] == "结论：发现 1 个问题"
        assert "退出码" not in text

    def test_ok_property(self):
        assert self._report((), ()).ok is True
        assert self._report((), (Problem(KIND_MISSING, "", "-"),)).ok is False


#: 同一键在 trailer 块里出现两次（结构类问题：重复字段）
DUP_MESSAGE = (
    "Merge PR #15（t_c9f5866a）\n"
    "\n"
    "merged-by: pm/run1\n"
    "merge-card: t_c9f5866a\n"
    "merge-pr: #15\n"
    "merged-by: pm/run2\n"
)

#: 键写在正文里、末尾块里没有（结构类问题：字段未落在 trailer 区）
MISPLACED_MESSAGE = (
    "merge-card: t_c9f5866a\n"
    "正文第二行\n"
    "\n"
    "merged-by: pm/run1\n"
    "merge-pr: #15\n"
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


# ------------------------------------------ 大小写：旧的三个大写键一律不认


class TestOldUppercaseKeysRejected:
    """改名后 ``Merge-By`` / ``Merge-Card`` / ``Merge-PR`` 不再接受（也不做兼容别名）。"""

    UPPER = (
        "Merge PR #15（t_c9f5866a）\n"
        "\n"
        "Merge-By: pm/run137\n"
        "Merge-Card: t_c9f5866a\n"
        "Merge-PR: #15\n"
    )

    def test_trailer_block_yields_no_values_and_no_structural_problem(self):
        # 大写键不属于 TRAILER_KEYS → 当无关键忽略，解析层不报结构问题
        values, problems = parse_trailers(self.UPPER)
        assert values == {}
        assert problems == ()

    def test_record_reports_all_three_lowercase_keys_missing(self):
        record, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), self.UPPER)
        assert kinds(problems) == [KIND_MISSING, KIND_MISSING, KIND_MISSING]
        assert [problem.detail for problem in problems] == [
            "merged-by 缺失",
            "merge-card 缺失",
            "merge-pr 缺失",
        ]
        assert record.by is None
        assert record.card is None
        assert record.pr is None
        assert record.source == "trailer"

    def test_build_message_emits_lowercase_only(self):
        message = build_message(card=CARD, by=BY, pr=15)
        assert "merged-by: pm/run137" in message
        assert "merge-card: t_c9f5866a" in message
        assert "merge-pr: #15" in message
        for upper in ("Merge-By", "Merge-Card", "Merge-PR"):
            assert upper not in message


# --------------------------------------------------- 判据①：merged-by == assignee


def make_record(
    by: str | None, *, source: str = "trailer", card: str | None = CARD
) -> MergeRecord:
    """造一条记录（``by`` 为 ``None`` 表示该字段缺失 / 格式非法）。"""

    return MergeRecord(
        sha=SHA,
        subject="Merge PR #15",
        parents=("p1", "p2") if source == "trailer" else (),
        by=by,
        card=card,
        pr="#15",
        source=source,
    )


class TestCheckMergedBy:
    def test_matching_profile_passes(self):
        assert check_merged_by(make_record("pm/run137"), "pm") == ()

    def test_mismatching_profile_reports_exact_detail(self):
        problems = check_merged_by(make_record("engineer/run9"), "pm")
        assert kinds(problems) == [KIND_MERGED_BY_MISMATCH]
        assert problems[0].detail == "merged-by=engineer/run9 assignee=pm"
        assert problems[0].sha == SHA

    @pytest.mark.parametrize("by", ["pm2/run1", "p/run1", "pm-x/run1", "ops/run137"])
    def test_profile_compared_exactly_not_by_prefix_or_substring(self, by):
        assert kinds(check_merged_by(make_record(by), "pm")) == [KIND_MERGED_BY_MISMATCH]

    def test_legacy_record_is_not_judged(self):
        legacy = legacy_record(SHA, "Merge pull request #9 from alsj213/wt/t_c9f5866a")
        assert legacy is not None
        assert check_merged_by(legacy, "pm") == ()

    def test_missing_by_is_not_double_reported(self):
        # 值缺失 / 格式非法已由「缺字段 / 格式非法」报过，判据① 不再添一条
        assert check_merged_by(make_record(None), "pm") == ()


#: 看板库 tasks 表（判据① 只读 assignee）
_BOARD_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    assignee TEXT,
    status TEXT,
    created_at INTEGER,
    started_at INTEGER,
    completed_at INTEGER
);
"""


def make_board(path: Path, cards: dict[str, str | None]) -> Path:
    """造一个只有 ``tasks`` 表的看板库，返回路径。"""

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_BOARD_SCHEMA)
        for card, assignee in cards.items():
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status) VALUES (?, ?, ?, ?)",
                (card, card, assignee, "done"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


class TestCardAssignee:
    def test_reads_assignee_from_board(self, tmp_path: Path):
        board = make_board(tmp_path / "board.db", {CARD: "pm"})
        assert card_assignee(board, CARD) == "pm"

    def test_accepts_str_path(self, tmp_path: Path):
        board = make_board(tmp_path / "board.db", {CARD: "engineer"})
        assert card_assignee(str(board), CARD) == "engineer"

    def test_board_file_bytes_are_unchanged(self, tmp_path: Path):
        # 只读打开：调用前后库文件逐字节相同（不建表、不写）
        board = make_board(tmp_path / "board.db", {CARD: "pm"})
        before = board.read_bytes()
        card_assignee(board, CARD)
        assert board.read_bytes() == before

    def test_missing_card_raises_with_card_id(self, tmp_path: Path):
        board = make_board(tmp_path / "board.db", {CARD: "pm"})
        with pytest.raises(MergeAuditError) as excinfo:
            card_assignee(board, "t_99999999")
        assert "t_99999999" in str(excinfo.value)

    def test_missing_db_raises(self, tmp_path: Path):
        with pytest.raises(MergeAuditError) as excinfo:
            card_assignee(tmp_path / "nonexistent.db", CARD)
        assert "看板库不可读" in str(excinfo.value)

    def test_file_that_is_not_sqlite_raises(self, tmp_path: Path):
        bogus = tmp_path / "bogus.db"
        bogus.write_bytes(b"not a sqlite database\n")
        with pytest.raises(MergeAuditError) as excinfo:
            card_assignee(bogus, CARD)
        assert "看板库不可读" in str(excinfo.value)

    def test_db_without_tasks_table_raises(self, tmp_path: Path):
        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).close()
        with pytest.raises(MergeAuditError) as excinfo:
            card_assignee(empty, CARD)
        assert "tasks 表失败" in str(excinfo.value)

    def test_null_assignee_raises_instead_of_passing(self, tmp_path: Path):
        board = make_board(tmp_path / "board.db", {CARD: None})
        with pytest.raises(MergeAuditError) as excinfo:
            card_assignee(board, CARD)
        assert "assignee 为空" in str(excinfo.value)

    @pytest.mark.parametrize("card", ["", "t_ZZZZZZZZ", "t_c9f5866", "c9f5866a"])
    def test_illegal_card_raises(self, tmp_path: Path, card: str):
        board = make_board(tmp_path / "board.db", {CARD: "pm"})
        with pytest.raises(MergeAuditError):
            card_assignee(board, card)


# ------------------------------------------------- 渲染：判据① 段（格式固定）


def report_with(
    records: tuple[MergeRecord, ...] = (),
    problems: tuple[Problem, ...] = (),
    history: tuple[MergeRecord, ...] = (),
    *,
    card: str | None = None,
    assignee: str | None = None,
) -> MergeReport:
    return MergeReport(
        repo="/repo/team-board",
        baseline="origin/main",
        rev="HEAD",
        records=records,
        problems=problems,
        history=history,
        card=card,
        assignee=assignee,
    )


class TestRenderJudgementOne:
    def test_section_is_absent_without_card(self):
        record, _ = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), GOOD)
        text = render_report(report_with((record,)))
        assert "判据①" not in text
        assert "assignee" not in text

    def test_pass_line_format(self):
        record, _ = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), GOOD)
        text = render_report(report_with((record,), card=CARD, assignee="pm"))
        assert (
            f"判据①（merged-by == 卡 assignee）：卡 {CARD} assignee=pm，"
            "merged-by=pm/run137 → PASS"
        ) in text

    def test_fail_line_format(self):
        record, _ = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), GOOD)
        problem = Problem(KIND_MERGED_BY_MISMATCH, SHA, "merged-by=pm/run137 assignee=engineer")
        text = render_report(
            report_with((record,), (problem,), card=CARD, assignee="engineer")
        )
        assert (
            f"判据①（merged-by == 卡 assignee）：卡 {CARD} assignee=engineer，"
            "merged-by=pm/run137 → FAIL"
        ) in text

    def test_line_says_not_applicable_when_no_trailer_record(self):
        legacy = legacy_record(SHA, "Merge pull request #9 from alsj213/wt/t_c9f5866a")
        assert legacy is not None
        text = render_report(report_with((), (), (legacy,), card=CARD, assignee="pm"))
        assert f"判据①（merged-by == 卡 assignee）：卡 {CARD} assignee=pm，" in text
        assert "区间内无该卡的 trailer 记录 → 不适用" in text

    def test_line_says_not_applicable_when_by_missing(self):
        message = "Merge PR #15（t_c9f5866a）\n\nmerge-card: t_c9f5866a\nmerge-pr: #15\n"
        record, problems = record_from_commit(SHA, "Merge PR #15", ("p1", "p2"), message)
        text = render_report(
            report_with((record,), problems, card=CARD, assignee="pm")
        )
        assert "merged-by=- → 不适用" in text
        assert "缺字段：merged-by 缺失" in text


# ---------------------------------- git 调用层的环境隔离（继承来的 GIT_DIR 不许劫持）
#
# 这一节与上面的纯函数用例分开：上面锁判定语义（不碰 git），这里锁 ``_git`` 的
# 执行环境 —— 只有造真仓库才验得出「``git -C <path>`` 只切目录、不隔离 GIT_DIR」。
# 期望值来自卡 t_11ef8cf3：``--repo`` 指向非仓库目录时必须报错（CLI 转 rc2），
# 而不是静默去审 GIT_DIR 指的那个仓库、报头却写 ``--repo`` 并给出假绿。

#: 会被子进程继承、把 git 指向别处的定位类变量。造仓库时一并剔掉：否则宿主自己
#: 带着 GIT_DIR 跑 pytest 时，fixture 仓库也会被造到别处去。
_LOCATION_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


def _clean_env() -> dict[str, str]:
    """造测试仓库用的环境：剔掉 git 定位类变量，固定作者/提交者。"""

    env = {
        key: value for key, value in os.environ.items() if key not in _LOCATION_ENV_KEYS
    }
    env.update(
        {
            "GIT_AUTHOR_NAME": "merges-env-test",
            "GIT_AUTHOR_EMAIL": "merges-env@example.com",
            "GIT_COMMITTER_NAME": "merges-env-test",
            "GIT_COMMITTER_EMAIL": "merges-env@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _real_git(repo: Path, *args: str) -> str:
    """在 ``repo`` 里跑 git，失败即测试失败。"""

    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def other_repo(tmp_path: Path) -> Path:
    """另一个真仓库（``main`` 上一条提交），充当 ``GIT_DIR`` 的指向目标。"""

    path = tmp_path / "other"
    path.mkdir()
    _real_git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _real_git(path, "add", "-A")
    _real_git(path, "commit", "-q", "-m", "chore: init")
    return path


def _is_inside_git_repo(path: Path) -> bool:
    """``path`` 是否被某个 git 仓库罩着（祖先目录里有 ``.git``）。"""

    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=False,
    )
    return proc.returncode == 0


def _non_repo_dir(tmp_path: Path) -> Path:
    """一个确定「不在任何 git 仓库里」的目录。

    ``tmp_path`` 可能整个落在某个仓库里（本机 ``/home/liu`` 就是个人仓），那样
    ``rev-parse --show-toplevel`` 会成功，用例根本测不到「非仓库 → 报错」这一支。
    所以这里用 git 自己探一次，落在仓库里就退到系统临时目录再探。
    """

    for root in (tmp_path, Path(tempfile.gettempdir()), Path("/tmp")):
        try:
            candidate = root / "t_11ef8cf3-nr"
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:  # 该 root 不可写 / 不存在
            continue
        if not _is_inside_git_repo(candidate):
            return candidate
    raise AssertionError("找不到不在任何 git 仓库里的目录，无法验证「非仓库 → 报错」")


def _make_repo(path: Path, content: str = "hello\n") -> Path:
    """造一个 ``main`` 上有一条提交的真仓库，返回路径。"""

    path.mkdir(parents=True, exist_ok=True)
    _real_git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text(content, encoding="utf-8")
    _real_git(path, "add", "-A")
    _real_git(path, "commit", "-q", "-m", "chore: init")
    return path


class TestGitEnvIsolation:
    """卡 t_11ef8cf3：父进程继承来的 git 定位变量不许把稽核引到另一个仓库。"""

    def test_git_dir_pointing_elsewhere_cannot_make_non_repo_pass(
        self, other_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用例 A：``GIT_DIR`` 指向真仓库 + ``--repo`` 指向非仓库 → 报错而非假绿。"""

        baseline = _real_git(other_repo, "rev-parse", "HEAD").strip()
        not_a_repo = _non_repo_dir(tmp_path)
        monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))

        with pytest.raises(MergeAuditError) as excinfo:
            audit(repo=not_a_repo, baseline=baseline)

        message = str(excinfo.value)
        assert "不是 git 工作树" in message
        assert str(not_a_repo) in message

    def test_git_work_tree_pointing_elsewhere_also_fails(
        self, other_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GIT_WORK_TREE`` 同样不许生效（与 ``GIT_DIR`` 成对设置才是完整劫持）。"""

        baseline = _real_git(other_repo, "rev-parse", "HEAD").strip()
        not_a_repo = _non_repo_dir(tmp_path)
        monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(other_repo))

        with pytest.raises(MergeAuditError):
            audit(repo=not_a_repo, baseline=baseline)

    def test_git_dir_cannot_supply_commits_from_another_repo(
        self, other_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真仓对真仓：``GIT_DIR`` 指向 B 时，A 里解析不到 B 的提交 → 报错。

        这条不依赖「非仓库目录」，是同一缺陷更硬的一面：修前 ``rev-parse --verify``
        会去 B 里查、查得到，于是报头写 A、数据全来自 B。
        """

        target = _make_repo(tmp_path / "target", "target\n")
        foreign_sha = _real_git(other_repo, "rev-parse", "HEAD").strip()
        monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))

        with pytest.raises(MergeAuditError) as excinfo:
            audit(repo=target, baseline=foreign_sha)

        assert "基线" in str(excinfo.value)

    def test_git_dir_does_not_redirect_a_real_repo_either(
        self, other_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """指向别处的 ``GIT_DIR`` 也不许改掉「审的是哪个仓」：报头仓库必须是入参。"""

        target = _make_repo(tmp_path / "target", "target\n")
        baseline = _real_git(target, "rev-parse", "HEAD").strip()
        monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))

        report = audit(repo=target, baseline=baseline)

        assert Path(report.repo).resolve() == target.resolve()
        assert report.ok

    def test_clean_env_still_audits_normally(
        self, other_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用例 C/D：不设 ``GIT_DIR``、``PATH`` 正常时照常跑通（env 没把 PATH 抹掉）。"""

        monkeypatch.delenv("GIT_DIR", raising=False)
        baseline = _real_git(other_repo, "rev-parse", "HEAD").strip()

        report = audit(repo=other_repo, baseline=baseline)

        assert report.ok
        assert Path(report.repo).resolve() == other_repo.resolve()
        assert report.records == ()


# ------------------------- 判定对象 = first-parent 合并（卡 t_d22c6399）
#
# 同一条 git 调用层的用例：只有造出「卡分支同步 main（second-parent 侧）+ 把卡分支
# 合进 main（first-parent 侧）」的真实拓扑，才验得出「只判 first-parent」。期望值来自
# 卡 t_d22c6399：同步合并（``git merge origin/main``）没合任何 PR，本来就不该有
# ``merge-card`` / ``merge-pr``，判它就是假红。

#: 分支侧同步合并的 message（真实形态，无 trailer）
SYNC_MSG = "Merge origin/main into wt/t_deadbeef（解冲突：两侧全保留）"

#: 合格的 first-parent 合并（认账三字段齐）
GOOD_PR_MSG = (
    "Merge pull request #999 from alsj213/wt/t_deadbeef\n"
    "\n"
    "merged-by: pm/run189\n"
    "merge-card: t_deadbeef\n"
    "merge-pr: #999\n"
)

#: 不合格的 first-parent 合并（无认账字段，必须仍然判红）
BAD_PR_MSG = "Merge pull request #998 from alsj213/wt/t_deadbeef"


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    """写一个文件并提交，返回新提交 sha。"""

    (repo / name).write_text(content, encoding="utf-8")
    _real_git(repo, "add", "-A")
    _real_git(repo, "commit", "-q", "-m", message)
    return _real_git(repo, "rev-parse", "HEAD").strip()


def _pr_topology(
    path: Path, pr_message: str, later_pr_message: str | None = None
) -> dict[str, str]:
    """造出 first-parent / second-parent 两条链都有的真仓，返回各节点 sha。

    拓扑（``main`` 的 first-parent 链是 baseline → main → pr）：

        baseline -- main --------- pr -- pr2
               \\                  /
                side -- sync ----+          side2 -- pr2（可选）
    """

    path.mkdir(parents=True, exist_ok=True)
    _real_git(path, "init", "-q", "-b", "main")
    shas = {"baseline": _commit(path, "README.md", "base\n", "chore: 基线")}
    # 卡分支从基线切出，之后 main 自己前进 —— 才需要「同步 main 回分支」
    _real_git(path, "checkout", "-q", "-b", "side")
    shas["side"] = _commit(path, "side.txt", "side\n", "feat: 卡分支提交")
    _real_git(path, "checkout", "-q", "main")
    shas["main"] = _commit(path, "main.txt", "main\n", "feat: main 前进")
    # 分支侧把 main 同步回来：second-parent 侧的内部合并，没有认账字段
    _real_git(path, "checkout", "-q", "side")
    _real_git(path, "merge", "-q", "--no-ff", "main", "-m", SYNC_MSG)
    shas["sync"] = _real_git(path, "rev-parse", "HEAD").strip()
    # 把卡分支合进 main：first-parent 侧的认账事件
    _real_git(path, "checkout", "-q", "main")
    _real_git(path, "merge", "-q", "--no-ff", "side", "-m", pr_message)
    shas["pr"] = _real_git(path, "rev-parse", "HEAD").strip()
    if later_pr_message is not None:
        _real_git(path, "checkout", "-q", "-b", "side2")
        shas["side2"] = _commit(path, "side2.txt", "side2\n", "feat: 第二张卡的提交")
        _real_git(path, "checkout", "-q", "main")
        _real_git(path, "merge", "-q", "--no-ff", "side2", "-m", later_pr_message)
        shas["pr2"] = _real_git(path, "rev-parse", "HEAD").strip()
    return shas


class TestFirstParentOnly:
    """卡 t_d22c6399：判定对象只有 first-parent 链上的合并提交。"""

    def test_first_parent_merge_is_judged(self, tmp_path: Path) -> None:
        """① first-parent 合并（把分支合进 main）被计入判定段。"""

        repo = tmp_path / "repo"
        shas = _pr_topology(repo, GOOD_PR_MSG)

        report = audit(repo=repo, baseline=shas["baseline"])

        assert report.problems == ()
        assert [record.sha for record in report.records] == [shas["pr"]]
        assert report.records[0].source == "trailer"
        assert report.records[0].card == "t_deadbeef"

    def test_sync_merge_is_not_judged(self, tmp_path: Path) -> None:
        """② 同步合并（second-parent 侧）不计入 records / problems。

        拓扑里确实有一条两父的同步合并（先断言它是真合并），它没有认账字段；
        修前它会被当成「缺三个字段」，于是这里拿到 6 条问题、判定段 2 条记录。
        """

        repo = tmp_path / "repo"
        shas = _pr_topology(repo, BAD_PR_MSG)
        assert len(_real_git(repo, "rev-list", "--parents", "-n", "1", shas["sync"]).split()) == 3

        report = audit(repo=repo, baseline=shas["baseline"])

        assert [record.sha for record in report.records] == [shas["pr"]]
        assert shas["sync"] not in {record.sha for record in report.records}
        assert [problem.sha for problem in report.problems] == [shas["pr"]] * 3
        assert {problem.kind for problem in report.problems} == {KIND_MISSING}

    def test_mixed_range_judges_only_first_parent(self, tmp_path: Path) -> None:
        """③ 两者混在同一区间：只判 first-parent 那条，同步合并一条都不出。"""

        repo = tmp_path / "repo"
        shas = _pr_topology(repo, BAD_PR_MSG, later_pr_message=GOOD_PR_MSG)

        report = audit(repo=repo, baseline=shas["baseline"])

        assert sorted(record.sha for record in report.records) == sorted(
            [shas["pr"], shas["pr2"]]
        )
        assert [problem.sha for problem in report.problems] == [shas["pr"]] * 3
        assert shas["sync"] not in {record.sha for record in report.records}

    def test_history_section_excludes_sync_merge(self, tmp_path: Path) -> None:
        """④ 历史段同样只取 first-parent：基线落在 PR 合并提交上时只有它自己。"""

        repo = tmp_path / "repo"
        shas = _pr_topology(repo, GOOD_PR_MSG)
        assert len(_real_git(repo, "rev-list", "--parents", "-n", "1", shas["pr"]).split()) == 3

        report = audit(repo=repo, baseline=shas["pr"])

        assert report.records == ()
        assert [record.sha for record in report.history] == [shas["pr"]]
        assert shas["sync"] not in {record.sha for record in report.history}
        assert report.ok

    def test_card_branch_tip_rev_still_judges_sync_merge(self, tmp_path: Path) -> None:
        """⑤ ``--rev`` 给卡分支尖端（CI ``pull_request`` 现状）时，同步合并照旧被判。

        锁定现状，防「承诺大于实现」：``--first-parent`` 是**沿被审 rev 的链**取数，
        不是给同步合并开豁免。rev = 卡分支尖端 → 该分支自己的同步合并就在链上，
        缺三字段即 rc1（这就是 CI ``pull_request`` 事件 ``--rev = head.sha`` 的行为）。
        """

        repo = tmp_path / "repo"
        shas = _pr_topology(repo, GOOD_PR_MSG)
        assert len(_real_git(repo, "rev-list", "--parents", "-n", "1", shas["sync"]).split()) == 3

        report = audit(repo=repo, baseline=shas["baseline"], rev=shas["sync"])

        assert [record.sha for record in report.records] == [shas["sync"]]
        assert [problem.sha for problem in report.problems] == [shas["sync"]] * 3
        assert {problem.kind for problem in report.problems} == {KIND_MISSING}
        assert not report.ok

    def test_card_side_baseline_history_only_follows_its_first_parent(
        self, tmp_path: Path
    ) -> None:
        """⑥ ``--baseline`` 给卡分支侧提交时，历史段只沿 baseline 的 first-parent 链。

        拓扑：``b0`` → 卡分支 ``side``；main 上另合一张 PR（``m1``，无 trailer）；
        卡分支把 main 同步回来（``sync``）；最后把卡分支合进 main（``pr``，三字段齐）。
        ``--baseline <sync> --rev <pr>``：判定段仍是 ``pr`` 一条、rc0（视角收窄不等于
        放宽）；历史段不再回显 second-parent 侧的 main PR 合并 ``m1``（修前 2 条 → 1 条）。
        """

        repo = tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        _real_git(repo, "init", "-q", "-b", "main")
        _commit(repo, "README.md", "base\n", "chore: 基线")
        _real_git(repo, "checkout", "-q", "-b", "side")
        _commit(repo, "side.txt", "side\n", "feat: 卡分支提交")
        _real_git(repo, "checkout", "-q", "main")
        _real_git(repo, "checkout", "-q", "-b", "other")
        _commit(repo, "other.txt", "other\n", "feat: 另一张卡")
        _real_git(repo, "checkout", "-q", "main")
        _real_git(
            repo,
            "merge",
            "-q",
            "--no-ff",
            "other",
            "-m",
            "Merge pull request #7 from alsj213/wt/t_aaaa1111",
        )
        main_pr = _real_git(repo, "rev-parse", "HEAD").strip()
        _real_git(repo, "checkout", "-q", "side")
        _real_git(repo, "merge", "-q", "--no-ff", "main", "-m", SYNC_MSG)
        sync = _real_git(repo, "rev-parse", "HEAD").strip()
        _real_git(repo, "checkout", "-q", "main")
        _real_git(repo, "merge", "-q", "--no-ff", "side", "-m", GOOD_PR_MSG)
        pr = _real_git(repo, "rev-parse", "HEAD").strip()

        # 修前取数（无 --first-parent）：历史段把 main 侧 PR 合并也数进来
        unfiltered = _real_git(repo, "log", "--merges", "--format=%h", sync)
        assert sorted(unfiltered.split()) == sorted([sync[:7], main_pr[:7]])

        report = audit(repo=repo, baseline=sync, rev=pr)

        assert [record.sha for record in report.records] == [pr]
        assert [record.sha for record in report.history] == [sync]
        assert main_pr not in {record.sha for record in report.history}
        assert report.ok
