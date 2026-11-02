"""``pathclaim`` 纯函数单测：顶格解析、路径归一、目录/glob 相交与冲突排序。"""

from __future__ import annotations

import json

import pytest

from team_board import pathclaim


def claim(card: str, *paths: str) -> pathclaim.Claim:
    return pathclaim.Claim(card_id=card, status="running", assignee="engineer", paths=paths)


class TestParseBody:
    def test_single_line_comma_separated(self) -> None:
        paths, issues = pathclaim.parse_body(
            "PATH-CLAIM: src/a.py, tests/test_a.py\n\n正文"
        )
        assert paths == ("src/a.py", "tests/test_a.py")
        assert issues == ()

    def test_multiple_lines_union_and_dedupe(self) -> None:
        paths, _ = pathclaim.parse_body(
            "PATH-CLAIM: src/a.py\nPATH-CLAIM: src/a.py, src/b.py\n"
        )
        assert paths == ("src/a.py", "src/b.py")

    def test_indented_line_is_not_a_claim(self) -> None:
        paths, issues = pathclaim.parse_body("  PATH-CLAIM: src/a.py\n")
        assert paths == ()
        assert issues == ()

    def test_directory_claim_keeps_trailing_slash(self) -> None:
        paths, _ = pathclaim.parse_body("PATH-CLAIM: src/team_board/\n")
        assert paths == ("src/team_board/",)

    def test_leading_dot_slash_normalized(self) -> None:
        paths, _ = pathclaim.parse_body("PATH-CLAIM: ./src/a.py\n")
        assert paths == ("src/a.py",)

    @pytest.mark.parametrize(
        "line",
        [
            "PATH-CLAIM: /etc/passwd",
            "PATH-CLAIM: ~/secret",
            "PATH-CLAIM: src/../secret",
            "PATH-CLAIM: src\\win.py",
            "PATH-CLAIM: .",
            "PATH-CLAIM: src/a.py,",
            "PATH-CLAIM: ",
        ],
    )
    def test_invalid_lines_are_issues(self, line: str) -> None:
        paths, issues = pathclaim.parse_body(line + "\n")
        assert paths == ()
        assert len(issues) == 1
        assert "PATH-CLAIM" in issues[0]

    def test_empty_body(self) -> None:
        assert pathclaim.parse_body("") == ((), ())


class TestOverlap:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("src/a.py", "src/a.py"),
            ("src/team_board/", "src/team_board/foo.py"),
            ("src/team_board/", "src/team_board/deep/foo.py"),
            ("src/*.py", "src/a.py"),
            ("src/**/a.py", "src/x/y/a.py"),
            ("src/*", "src/deep/a.py"),  # fnmatch 的 * 跨 /
        ],
    )
    def test_overlaps(self, a: str, b: str) -> None:
        assert pathclaim.overlap(a, b) is True
        assert pathclaim.overlap(b, a) is True

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("src/a.py", "src/b.py"),
            ("src/team_board/", "tests/test_a.py"),
            ("src/*.py", "tests/test_a.py"),
            ("src/a/", "src/ab/c.py"),
        ],
    )
    def test_disjoint(self, a: str, b: str) -> None:
        assert pathclaim.overlap(a, b) is False


class TestAudit:
    def test_no_conflict(self) -> None:
        claims = (claim("t_aaaaaaaa", "src/a.py"), claim("t_bbbbbbbb", "src/b.py"))
        report = pathclaim.audit(claims)
        assert report.ok is True
        assert report.conflicts == ()
        assert pathclaim.exit_code(report) == 0

    def test_conflict_pair_sorted(self) -> None:
        claims = (claim("t_bbbbbbbb", "src/a.py"), claim("t_aaaaaaaa", "src/"))
        report = pathclaim.audit(claims)
        assert report.ok is False
        assert [(c.card_a, c.card_b, c.path_a, c.path_b) for c in report.conflicts] == [
            ("t_aaaaaaaa", "t_bbbbbbbb", "src/", "src/a.py")
        ]
        assert pathclaim.exit_code(report) == 1

    def test_empty_paths_do_not_conflict(self) -> None:
        claims = (claim("t_aaaaaaaa"), claim("t_bbbbbbbb", "src/a.py"))
        assert pathclaim.audit(claims).ok is True

    def test_parse_issue_blocks_pass(self) -> None:
        issue = pathclaim.Issue(card_id="t_aaaaaaaa", line="x", reason="路径为空")
        report = pathclaim.audit((claim("t_aaaaaaaa"),), (issue,))
        assert report.ok is False
        assert pathclaim.exit_code(report) == 1

    def test_check_card_only_compares_target(self) -> None:
        claims = (
            claim("t_aaaaaaaa", "src/a.py"),
            claim("t_bbbbbbbb", "src/"),
            claim("t_cccccccc", "docs/"),
        )
        report = pathclaim.check_card(claims, "t_aaaaaaaa")
        assert [(c.card_a, c.card_b) for c in report.conflicts] == [
            ("t_aaaaaaaa", "t_bbbbbbbb")
        ]

    def test_check_card_without_claim_is_empty(self) -> None:
        claims = (claim("t_aaaaaaaa"), claim("t_bbbbbbbb", "src/a.py"))
        report = pathclaim.check_card(claims, "t_aaaaaaaa")
        assert report.conflicts == ()
        assert report.ok is True


class TestRender:
    def test_render_conflict(self) -> None:
        report = pathclaim.audit(
            (claim("t_aaaaaaaa", "src/a.py"), claim("t_bbbbbbbb", "src/"))
        )
        text = pathclaim.render(report)
        assert "t_aaaaaaaa ↔ t_bbbbbbbb" in text
        assert text.splitlines()[-1].startswith("结论: 1 对冲突")

    def test_render_json_roundtrip(self) -> None:
        report = pathclaim.audit(
            (claim("t_aaaaaaaa", "src/a.py"), claim("t_bbbbbbbb", "src/"))
        )
        payload = json.loads(pathclaim.render_json(report))
        assert payload["ok"] is False
        assert payload["conflicts"] == [
            {"cards": ["t_aaaaaaaa", "t_bbbbbbbb"], "paths": ["src/a.py", "src/"]}
        ]


def test_validate_path_rejects_empty() -> None:
    with pytest.raises(ValueError, match="路径为空"):
        pathclaim.validate_path("  ")
