"""``merge-card --sweep``：done 实现卡自动建合并卡（幂等/跳过/限流）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from team_board import cli, mergecard

CARD = "t_1234abcd"
OTHER = "t_5678abcd"
PR = 94
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def make_db(path: Path, rows: list[tuple[str, str, str, str, str]], events: list[tuple[str, str, str]]) -> Path:
    """rows=id/status/assignee/title/body；events=task_id/kind/payload。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, status TEXT, assignee TEXT, title TEXT, body TEXT,"
            "created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_events ("
            "id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)"
        )
        for card_id, status, assignee, title, body in rows:
            conn.execute(
                "INSERT INTO tasks (id, status, assignee, title, body, created_at, started_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, 100, 100, 200)",
                (card_id, status, assignee, title, body),
            )
        for index, (task_id, kind, payload) in enumerate(events, start=1):
            conn.execute(
                "INSERT INTO task_events VALUES (?, ?, ?, ?)",
                (index, task_id, kind, payload),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture(autouse=True)
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.train, "repo_slug", lambda repo, remote, run: "owner/repo")
    def fake_view(repo: str, slug: str, pr: int) -> dict[str, str]:
        return {
            "state": "OPEN",
            "head": HEAD,
            "title": "x",
            "head_ref": f"wt/{CARD}" if pr == PR else f"wt/t_{pr:08x}",
        }

    monkeypatch.setattr(cli, "_gh_pr_view", fake_view)


def done_board(tmp_path: Path, *, extra_rows: list[tuple[str, str, str, str, str]] | None = None) -> Path:
    rows = [(CARD, "done", "engineer", "实现卡", "PATH-CLAIM: src/a.py")]
    rows.extend(extra_rows or [])
    events = [(CARD, "impl_done", json.dumps({"pr": PR, "head_sha": HEAD}))]
    return make_db(tmp_path / "board.db", rows, events)


class TestSweep:
    def test_dry_run_lists_plan(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = done_board(tmp_path)
        code, out, err = run(
            capsys, "merge-card", "--sweep", "--dry-run", "--db", str(board), "--repo", str(tmp_path)
        )
        assert (code, err) == (0, "")
        assert "dry-run" in out
        assert CARD in out

    def test_creates_for_open_pr(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        board = done_board(tmp_path)
        captured: dict[str, Any] = {}

        def fake_create(plan: mergecard.MergeCardPlan, *, body_path: str):
            captured["plan"] = plan
            return mergecard.CreateResult(
                plan=plan, rc=0, card_id="t_deadbeef", stdout="", stderr=""
            )

        monkeypatch.setattr(mergecard, "create_merge_card", fake_create)
        code, out, err = run(
            capsys, "merge-card", "--sweep", "--db", str(board), "--repo", str(tmp_path)
        )
        assert (code, err) == (0, "")
        assert "created" in out
        assert captured["plan"].impl_card == CARD
        assert captured["plan"].pr == PR

    def test_existing_merge_card_skips(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = done_board(
            tmp_path,
            extra_rows=[
                ("t_deadbeef", "ready", "repo", f"收口：合并 PR #{PR}", f"PR-CLAIM: #{PR}")
            ],
        )
        code, out, err = run(
            capsys, "merge-card", "--sweep", "--db", str(board), "--repo", str(tmp_path)
        )
        assert (code, err) == (0, "")
        assert "已有合并卡" in out

    def test_merged_pr_skips(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        board = done_board(tmp_path)
        monkeypatch.setattr(
            cli,
            "_gh_pr_view",
            lambda repo, slug, pr: {"state": "MERGED", "head": HEAD, "title": "x", "head_ref": f"wt/{CARD}"},
        )
        code, out, _ = run(
            capsys, "merge-card", "--sweep", "--db", str(board), "--repo", str(tmp_path)
        )
        assert code == 0
        assert "PR 非 OPEN" in out

    def test_head_mismatch_skips(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        board = done_board(tmp_path)
        monkeypatch.setattr(
            cli,
            "_gh_pr_view",
            lambda repo, slug, pr: {"state": "OPEN", "head": OTHER_HEAD, "title": "x", "head_ref": f"wt/{CARD}"},
        )
        code, out, _ = run(
            capsys, "merge-card", "--sweep", "--db", str(board), "--repo", str(tmp_path)
        )
        assert code == 0
        assert "!= impl_done" in out

    def test_no_impl_done_skips(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(
            tmp_path / "none.db",
            [(CARD, "done", "engineer", "实现卡", "")],
            [],
        )
        code, out, _ = run(
            capsys, "merge-card", "--sweep", "--db", str(board), "--repo", str(tmp_path)
        )
        assert code == 0
        assert "无 impl_done" in out

    def test_limit(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            (f"t_{i:08x}", "done", "engineer", f"实现卡{i}", "")
            for i in range(1, 4)
        ]
        events = [
            (card_id, "impl_done", json.dumps({"pr": i, "head_sha": HEAD}))
            for i, (card_id, *_rest) in enumerate(rows, start=1)
        ]
        board = make_db(tmp_path / "limit.db", rows, events)
        monkeypatch.setattr(
            cli,
            "_gh_pr_view",
            lambda repo, slug, pr: {"state": "OPEN", "head": HEAD, "title": "x", "head_ref": f"wt/t_{pr:08x}"},
        )
        code, out, _ = run(
            capsys,
            "merge-card",
            "--sweep",
            "--dry-run",
            "--limit",
            "2",
            "--db",
            str(board),
            "--repo",
            str(tmp_path),
        )
        assert code == 0
        assert out.count("dry-run") == 2

    def test_bad_limit_is_rc2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = done_board(tmp_path)
        code, out, err = run(
            capsys, "merge-card", "--sweep", "--limit", "0", "--db", str(board)
        )
        assert (code, out) == (2, "")
        assert "limit" in err

    def test_missing_args_is_rc2(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _err = run(capsys, "merge-card", "--sweep", "--db", "/no/such.db")
        assert code == 2
        assert out == ""


def test_same_pr_twice_creates_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-1：两张 done 卡同 (pr, head) 只建一张。"""

    rows = [
        (CARD, "done", "engineer", "实现卡1", ""),
        (OTHER, "done", "engineer", "实现卡2", ""),
    ]
    events = [
        (CARD, "impl_done", json.dumps({"pr": PR, "head_sha": HEAD})),
        (OTHER, "impl_done", json.dumps({"pr": PR, "head_sha": HEAD})),
    ]
    board = make_db(tmp_path / "dupe.db", rows, events)
    created: list[str] = []

    def fake_create(plan: mergecard.MergeCardPlan, *, body_path: str):
        created.append(plan.impl_card)
        return mergecard.CreateResult(
            plan=plan, rc=0, card_id="t_deadbeef", stdout="", stderr=""
        )

    monkeypatch.setattr(mergecard, "create_merge_card", fake_create)
    code, out, _ = run(
        capsys, "merge-card", "--sweep", "--db", str(board), "--repo", str(tmp_path)
    )
    assert code == 0
    assert created == [CARD]
    assert "已有合并卡" in out


def test_branch_mismatch_skips(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-2：PR headRefName 不是 wt/<卡号> 时不建卡。"""

    board = done_board(tmp_path)
    monkeypatch.setattr(
        cli,
        "_gh_pr_view",
        lambda repo, slug, pr: {
            "state": "OPEN",
            "head": HEAD,
            "title": "x",
            "head_ref": "ops/merge-card-sweep",
        },
    )
    code, out, _ = run(
        capsys, "merge-card", "--sweep", "--db", str(board), "--repo", str(tmp_path)
    )
    assert code == 0
    assert "headRefName" in out
    assert "ops/merge-card-sweep" in out
