"""``tip`` 纯函数与广播流程单测：字段解析、落后判定、dry-run/apply、失败记录。"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from team_board import tip
from team_board.train import CommandResult

TIP = "a" * 40
HEAD_OLD = "b" * 40
HEAD_NEW = "c" * 40


def result(argv: Sequence[str], rc: int = 0, out: str = "", err: str = "") -> CommandResult:
    return CommandResult(argv=tuple(argv), returncode=rc, stdout=out, stderr=err)


class FakeRun:
    """假 git/gh：按 argv 前缀回放；``ancestor`` 是 head→rc 的映射。"""

    def __init__(
        self,
        *,
        ancestor: dict[str, int] | None = None,
        update_rc: int = 0,
        prs: list[dict[str, object]] | None = None,
        fetch_rc: int = 0,
    ) -> None:
        self.ancestor = ancestor or {}
        self.update_rc = update_rc
        self.prs = prs if prs is not None else self.default_prs()
        self.fetch_rc = fetch_rc
        self.calls: list[tuple[str, ...]] = []

    @staticmethod
    def default_prs() -> list[dict[str, object]]:
        return [
            {
                "number": 91,
                "headRefName": "wt/t_c5dd88f3",
                "baseRefName": "main",
                "headRefOid": HEAD_OLD,
                "isDraft": False,
            },
            {
                "number": 92,
                "headRefName": "wt/t_b0c6e4fe",
                "baseRefName": "main",
                "headRefOid": HEAD_NEW,
                "isDraft": False,
            },
            {
                "number": 93,
                "headRefName": "cursor/other",
                "baseRefName": "main",
                "headRefOid": HEAD_NEW,
                "isDraft": False,
            },
        ]

    def __call__(self, argv: Sequence[str], cwd: str) -> CommandResult:
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[:2] == ("git", "fetch"):
            return result(argv, rc=self.fetch_rc)
        if argv[:2] == ("git", "rev-parse"):
            return result(argv, out=TIP + "\n")
        if argv[:3] == ("git", "remote", "get-url"):
            return result(argv, out="git@github.com:owner/repo.git\n")
        if argv[:3] == ("gh", "pr", "list"):
            return result(argv, out=json.dumps(self.prs))
        if argv[:2] == ("git", "merge-base"):
            return result(argv, rc=self.ancestor.get(argv[-1], 0))
        if argv[:3] == ("gh", "pr", "update-branch"):
            return result(argv, rc=self.update_rc, err="boom" if self.update_rc else "")
        return result(argv, rc=1, err=f"unexpected: {argv}")


class TestParsePrList:
    def test_valid(self) -> None:
        prs = tip.parse_pr_list(FakeRun.default_prs())
        assert [pr.number for pr in prs] == [91, 92, 93]
        assert prs[0].applicable is True
        assert prs[2].applicable is False

    def test_not_array(self) -> None:
        with pytest.raises(tip.TipError, match="不是数组"):
            tip.parse_pr_list({"number": 1})

    def test_missing_field(self) -> None:
        with pytest.raises(tip.TipError, match="字段缺失"):
            tip.parse_pr_list([{"number": 1}])


class TestApplicable:
    @pytest.mark.parametrize(
        ("base", "head", "draft", "expected"),
        [
            ("main", "wt/t_1234abcd", False, True),
            ("main", "t_1234abcd", False, False),
            ("dev", "wt/t_1234abcd", False, False),
            ("main", "wt/t_1234abcd", True, False),
            ("main", "cursor/x", False, False),
        ],
    )
    def test_filters(self, base: str, head: str, draft: bool, expected: bool) -> None:
        pr = tip.PrTip(1, head, HEAD_NEW, base, draft)
        assert pr.applicable is expected


class TestBroadcast:
    def test_dry_run_reports_behind(self) -> None:
        fake = FakeRun(ancestor={HEAD_OLD: 1, HEAD_NEW: 0})
        report = tip.broadcast("repo", run=fake)
        assert report.dry_run is True
        assert report.tip == TIP
        assert [pr.number for pr in report.behind] == [91]
        assert [pr.number for pr in report.updated] == []
        assert not any(argv[:3] == ("gh", "pr", "update-branch") for argv in fake.calls)

    def test_apply_updates_only_behind(self) -> None:
        fake = FakeRun(ancestor={HEAD_OLD: 1, HEAD_NEW: 0})
        report = tip.broadcast("repo", apply=True, run=fake)
        assert report.dry_run is False
        updates = [argv for argv in fake.calls if argv[:3] == ("gh", "pr", "update-branch")]
        assert len(updates) == 1
        assert updates[0][3] == "91"
        assert [pr.number for pr in report.updated] == [91]

    def test_apply_records_failure(self) -> None:
        fake = FakeRun(ancestor={HEAD_OLD: 1, HEAD_NEW: 0}, update_rc=2)
        report = tip.broadcast("repo", apply=True, run=fake)
        assert [pr.number for pr in report.failed] == [91]
        assert "boom" in report.failed[0].update_detail

    def test_unknown_ancestor_is_not_behind(self) -> None:
        fake = FakeRun(ancestor={HEAD_OLD: 5, HEAD_NEW: 0})
        report = tip.broadcast("repo", run=fake)
        assert report.behind == ()
        assert report.prs[0].behind is None

    def test_fetch_failure_raises(self) -> None:
        fake = FakeRun(fetch_rc=1)
        with pytest.raises(tip.TipError, match="git fetch"):
            tip.broadcast("repo", run=fake)


class TestRender:
    def test_render_dry_run(self) -> None:
        report = tip.broadcast("repo", run=FakeRun(ancestor={HEAD_OLD: 1, HEAD_NEW: 0}))
        text = tip.render(report)
        assert "#91 wt/t_c5dd88f3 | 落后" in text
        assert text.splitlines()[-1].startswith("结论: dry-run")

    def test_render_apply_failure(self) -> None:
        report = tip.broadcast(
            "repo", apply=True, run=FakeRun(ancestor={HEAD_OLD: 1, HEAD_NEW: 0}, update_rc=2)
        )
        text = tip.render(report)
        assert "update-branch rc=2" in text
        assert text.splitlines()[-1].startswith("结论: 1 条")

    def test_render_json(self) -> None:
        report = tip.broadcast("repo", run=FakeRun(ancestor={HEAD_OLD: 1, HEAD_NEW: 0}))
        payload = json.loads(tip.render_json(report))
        assert payload["tip"] == TIP
        assert payload["prs"][0]["behind"] is True
