"""``reviewhead`` 纯逻辑层单测：标记解析、判定、渲染、参数形态、分页解析。

这一层不碰 gh：取数在 ``tests/integration/test_cli_review_head_audit.py`` 里用假 gh
跑。这里锁的是「什么算一条标记」「三档 rc 的判据」「rc2 的参数边界」。
"""

from __future__ import annotations

import pytest

from team_board import cli, reviewhead

HEAD = "d2be80da5e30f8560579f6010217f05349127498"  # 40 位（PR #34 的 head）
OTHER = "a" * 40
HEAD_64 = "0123456789abcdef" * 4  # 64 位


def body(*lines: str) -> str:
    return "\n".join(lines)


def marked(sha: str, kind: str, *, prefix: str = "") -> str:
    return body(f"{prefix}REVIEW-HEAD: {sha}", f"{prefix}REVIEW-KIND: {kind}")


class TestRegexIsTheSpec:
    """两条正则逐字就是卡面给的那两条（改一个字符都要先改卡）。"""

    def test_head_regex_literal(self) -> None:
        assert reviewhead.HEAD_RE.pattern == r"^REVIEW-HEAD: ([0-9a-f]{40}|[0-9a-f]{64})$"

    def test_kind_regex_literal(self) -> None:
        assert reviewhead.KIND_RE.pattern == r"^REVIEW-KIND: (defect|regression)$"

    def test_both_regex_are_multiline(self) -> None:
        assert reviewhead.HEAD_RE.flags & reviewhead.re.M
        assert reviewhead.KIND_RE.flags & reviewhead.re.M


class TestParseMarks:
    def test_both_lines_is_one_mark(self) -> None:
        marks = reviewhead.parse_marks([marked(HEAD, "defect")])
        assert marks == (reviewhead.Mark(sha=HEAD, kind="defect"),)

    def test_surrounding_text_is_fine(self) -> None:
        text = body(
            "## 缺陷视角",
            "阻塞：P2-1 --branch 未 quote",
            "",
            f"REVIEW-HEAD: {HEAD}",
            "REVIEW-KIND: defect",
            "",
            "依据：gh pr checks rc=0",
        )
        assert reviewhead.parse_marks([text]) == (reviewhead.Mark(HEAD, "defect"),)

    def test_64_hex_accepted(self) -> None:
        assert reviewhead.parse_marks([marked(HEAD_64, "regression")]) == (
            reviewhead.Mark(HEAD_64, "regression"),
        )

    def test_only_head_line_is_not_a_mark(self) -> None:
        assert reviewhead.parse_marks([body(f"REVIEW-HEAD: {HEAD}")]) == ()

    def test_only_kind_line_is_not_a_mark(self) -> None:
        assert reviewhead.parse_marks([body("REVIEW-KIND: defect")]) == ()

    @pytest.mark.parametrize("indent", [" ", "  ", "\t"])
    def test_indented_lines_are_not_marks(self, indent: str) -> None:
        assert reviewhead.parse_marks([marked(HEAD, "defect", prefix=indent)]) == ()

    def test_trailing_space_is_not_a_mark(self) -> None:
        text = body(f"REVIEW-HEAD: {HEAD} ", "REVIEW-KIND: defect")
        assert reviewhead.parse_marks([text]) == ()

    def test_crlf_is_not_a_mark(self) -> None:
        # 卡面给的 ``$`` 是「\n 之前」，行尾多一个 \r 就不匹配；这是照抄语义，不是漏判
        text = f"REVIEW-HEAD: {HEAD}\r\nREVIEW-KIND: defect\r\n"
        assert reviewhead.parse_marks([text]) == ()

    def test_uppercase_sha_is_not_a_mark(self) -> None:
        assert reviewhead.parse_marks([marked(HEAD.upper(), "defect")]) == ()

    @pytest.mark.parametrize("n", [39, 41, 63, 65])
    def test_wrong_length_sha_is_not_a_mark(self, n: int) -> None:
        assert reviewhead.parse_marks([marked("a" * n, "defect")]) == ()

    def test_unknown_kind_is_not_a_mark(self) -> None:
        text = body(f"REVIEW-HEAD: {HEAD}", "REVIEW-KIND: other")
        assert reviewhead.parse_marks([text]) == ()

    def test_first_line_wins_within_one_body(self) -> None:
        text = body(
            f"REVIEW-HEAD: {HEAD}",
            "REVIEW-KIND: defect",
            f"REVIEW-HEAD: {OTHER}",
            "REVIEW-KIND: regression",
        )
        assert reviewhead.parse_marks([text]) == (reviewhead.Mark(HEAD, "defect"),)

    def test_one_mark_per_body(self) -> None:
        assert len(reviewhead.parse_marks([marked(HEAD, "defect")])) == 1

    def test_empty_and_none_bodies(self) -> None:
        assert reviewhead.parse_marks([]) == ()
        assert reviewhead.parse_marks(["", None]) == ()  # type: ignore[list-item]

    def test_order_preserved(self) -> None:
        bodies = [marked(HEAD, "defect"), marked(OTHER, "regression")]
        assert reviewhead.parse_marks(bodies) == (
            reviewhead.Mark(HEAD, "defect"),
            reviewhead.Mark(OTHER, "regression"),
        )

    def test_short_is_first_12(self) -> None:
        assert reviewhead.Mark(HEAD, "defect").short == "d2be80da5e30"


class TestAnalyze:
    def test_both_kinds_on_head_is_ok(self) -> None:
        report = reviewhead.analyze(
            HEAD, [marked(HEAD, "defect"), marked(HEAD, "regression")]
        )
        assert report.ok is True
        assert report.missing == ()
        assert report.kinds_on_head == {"defect", "regression"}
        assert report.bodies_scanned == 2
        assert len(report.marks) == 2

    def test_missing_regression(self) -> None:
        report = reviewhead.analyze(HEAD, [marked(HEAD, "defect")])
        assert report.ok is False
        assert report.missing == ("regression",)

    def test_missing_defect(self) -> None:
        report = reviewhead.analyze(HEAD, [marked(HEAD, "regression")])
        assert report.missing == ("defect",)

    def test_no_marks_at_all(self) -> None:
        report = reviewhead.analyze(HEAD, ["随便一条评论", ""])
        assert report.ok is False
        assert report.marks == ()
        assert report.missing == ("defect", "regression")

    def test_other_sha_only_is_missing_both(self) -> None:
        report = reviewhead.analyze(
            HEAD, [marked(OTHER, "defect"), marked(OTHER, "regression")]
        )
        assert report.missing == ("defect", "regression")

    def test_one_stale_does_not_count(self) -> None:
        report = reviewhead.analyze(
            HEAD, [marked(HEAD, "defect"), marked(OTHER, "regression")]
        )
        assert report.missing == ("regression",)

    def test_duplicate_kind_does_not_fill_the_other(self) -> None:
        report = reviewhead.analyze(
            HEAD, [marked(HEAD, "defect"), marked(HEAD, "defect")]
        )
        assert report.missing == ("regression",)


class TestRender:
    def test_ok_text(self) -> None:
        report = reviewhead.analyze(
            HEAD, [marked(HEAD, "defect"), marked(HEAD, "regression")]
        )
        assert reviewhead.render(report).splitlines() == [
            "扫描正文 2 条；带标记行 2 条",
            f"  OK {HEAD[:12]} defect",
            f"  OK {HEAD[:12]} regression",
            f"结论: head {HEAD[:12]} 的 defect + regression 各 >=1 条 → 绑定成立",
        ]

    def test_stale_and_ok_mixed(self) -> None:
        report = reviewhead.analyze(
            HEAD, [marked(OTHER, "defect"), marked(HEAD, "defect"), marked(HEAD, "regression")]
        )
        lines = reviewhead.render(report).splitlines()
        assert f"  STALE {OTHER[:12]} defect" in lines
        assert lines[0] == "扫描正文 3 条；带标记行 3 条"
        assert lines[-1].endswith("绑定成立")

    def test_missing_kinds_text(self) -> None:
        report = reviewhead.analyze(HEAD, [marked(HEAD, "defect")])
        lines = reviewhead.render(report).splitlines()
        assert lines[-1] == f"结论: head {HEAD[:12]} 缺 regression → head 变了要重跑双问"

    def test_no_mark_text(self) -> None:
        report = reviewhead.analyze(HEAD, ["no marker here"])
        assert reviewhead.render(report).splitlines() == [
            "扫描正文 1 条；带标记行 0 条",
            "结论: 无结构化标记 → 该 PR 的双问不可机器绑定（按缺证据处理）",
        ]

    def test_empty_bodies_render_zero(self) -> None:
        report = reviewhead.analyze(HEAD, [])
        assert reviewhead.render(report).splitlines()[0] == "扫描正文 0 条；带标记行 0 条"


class TestCheckHead:
    def test_40_ok(self) -> None:
        assert reviewhead.check_head(HEAD) == HEAD

    def test_64_ok(self) -> None:
        assert reviewhead.check_head(HEAD_64) == HEAD_64

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " ",
            "d2be80d",  # 短 sha
            "a" * 39,
            "a" * 41,
            "a" * 63,
            "a" * 65,
            HEAD.upper(),
            "g" * 40,
            "0x" + "a" * 38,
            HEAD + "\n",
            None,
            1793160000000,
        ],
    )
    def test_bad_head_raises(self, value: object) -> None:
        with pytest.raises(reviewhead.ReviewHeadError):
            reviewhead.check_head(value)


class TestCheckPr:
    def test_one_ok(self) -> None:
        assert reviewhead.check_pr("1") == 1

    def test_real_pr_ok(self) -> None:
        assert reviewhead.check_pr("34") == 34

    def test_upper_bound_ok(self) -> None:
        assert reviewhead.check_pr(str(reviewhead.PR_MAX)) == reviewhead.PR_MAX

    @pytest.mark.parametrize(
        "value",
        [
            "0",
            "00",
            "-1",
            "+1",
            " 1",
            "1 ",
            "1_0",
            "abc",
            "",
            "1.0",
            "0x1f",
            "١٢٣",  # 阿拉伯-印度数字（int() 会认，字符串判据不认）
            str(reviewhead.PR_MAX + 1),
            "1793160000000",  # 13 位毫秒
            "99999999999999999999",  # 天文数字
            None,
            34,
        ],
    )
    def test_bad_pr_raises(self, value: object) -> None:
        with pytest.raises(reviewhead.ReviewHeadError):
            reviewhead.check_pr(value)

    def test_upper_bound_matches_merge_record(self) -> None:
        """两个子命令的 PR 上限是同一个口径（防漂移）。"""

        assert reviewhead.PR_MAX == cli.MAX_MERGE_PR


class TestParsePages:
    def test_empty_stdout_is_zero_items(self) -> None:
        assert reviewhead.parse_pages("") == []

    def test_single_page(self) -> None:
        assert reviewhead.parse_pages('[{"body": "a"}, {"body": "b"}]') == [
            {"body": "a"},
            {"body": "b"},
        ]

    def test_two_pages_concatenated(self) -> None:
        out = '[{"body": "a"}]\n[{"body": "b"}, {"body": "c"}]\n'
        assert [item["body"] for item in reviewhead.parse_pages(out)] == ["a", "b", "c"]

    def test_surrounding_whitespace(self) -> None:
        assert reviewhead.parse_pages('  \n[]\n  ') == []

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(reviewhead.ReviewHeadError, match="不是合法 JSON"):
            reviewhead.parse_pages('{"body": "a"')

    def test_non_array_raises(self) -> None:
        with pytest.raises(reviewhead.ReviewHeadError, match="不是对象数组"):
            reviewhead.parse_pages('{"body": "a"}')

    def test_array_of_non_objects_raises(self) -> None:
        with pytest.raises(reviewhead.ReviewHeadError, match="非对象元素"):
            reviewhead.parse_pages('["a"]')

    def test_endpoint_appears_in_error(self) -> None:
        with pytest.raises(reviewhead.ReviewHeadError, match="pulls/34/reviews"):
            reviewhead.parse_pages("not json", endpoint="pulls/34/reviews")
