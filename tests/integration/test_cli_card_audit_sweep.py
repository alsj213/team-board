"""``card-audit --sweep``：派工前扫描 ready/todo 缺契约卡并可机械 block。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from team_board import cli

GOOD = """PATH-CLAIM: src/a.py
TEST-PLAN: quick
CONSUMER: team-board card-audit --card t_1234abcd
QA-SPEC: qa
"""


def make_db(path: Path, rows: list[tuple[str, str, str, str, str]]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, status TEXT, assignee TEXT, title TEXT, body TEXT,"
            "created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
        )
        for card_id, status, assignee, title, body in rows:
            conn.execute(
                "INSERT INTO tasks (id, status, assignee, title, body, created_at, started_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, 100, 100, 200)",
                (card_id, status, assignee, title, body),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestSweep:
    def test_good_card_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(tmp_path / "good.db", [("t_1234abcd", "ready", "engineer", "x", GOOD)])
        code, out, err = run(capsys, "card-audit", "--sweep", "--db", str(board))
        assert (code, err) == (0, "")
        assert "0 张异常" in out

    def test_bad_card_reports_rc1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(tmp_path / "bad.db", [("t_1234abcd", "ready", "engineer", "x", "## 目标\n")])
        code, out, _ = run(capsys, "card-audit", "--sweep", "--db", str(board))
        assert code == 1
        assert "t_1234abcd | invalid" in out
        assert "path-claim-missing" in out

    def test_merge_card_is_skipped(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(
            tmp_path / "merge.db",
            [("t_1234abcd", "ready", "repo", "收口：合并 PR #9", "PR-CLAIM: #9\n")],
        )
        code, out, err = run(capsys, "card-audit", "--sweep", "--db", str(board))
        assert (code, err) == (0, "")
        assert "0 张异常" in out

    def test_apply_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        board = make_db(tmp_path / "apply.db", [("t_1234abcd", "ready", "engineer", "x", "## 目标\n")])
        calls: list[tuple[str, ...]] = []

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv: tuple[str, ...], **kwargs: Any) -> Proc:
            calls.append(tuple(argv))
            return Proc()

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        code, out, err = run(capsys, "card-audit", "--sweep", "--apply", "--db", str(board))
        assert (code, err) == (0, "")
        assert "blocked" in out
        assert calls and calls[0][:5] == ("hermes", "kanban", "--board", "devteam", "block")

    def test_apply_block_failure_is_rc2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        board = make_db(tmp_path / "fail.db", [("t_1234abcd", "ready", "engineer", "x", "## 目标\n")])

        class Proc:
            returncode = 1
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Proc())
        code, out, err = run(capsys, "card-audit", "--sweep", "--apply", "--db", str(board))
        assert code == 2
        assert out == ""
        assert "boom" in err

    def test_limit(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rows = [
            (f"t_{i:08x}", "ready", "engineer", f"x{i}", "## 目标\n")
            for i in range(1, 4)
        ]
        board = make_db(tmp_path / "limit.db", rows)
        code, out, _ = run(
            capsys, "card-audit", "--sweep", "--limit", "2", "--db", str(board)
        )
        assert code == 1
        assert out.count("invalid") == 2

    def test_bad_limit_is_rc2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(tmp_path / "x.db", [])
        code, out, err = run(capsys, "card-audit", "--sweep", "--limit", "0", "--db", str(board))
        assert (code, out) == (2, "")
        assert "limit" in err


def test_auto_action_card_is_not_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """自动复审卡只有 PR-CLAIM，不应被 card-audit 机械 block。"""
    board = make_db(
        tmp_path / "auto.db",
        [("t_1234abcd", "ready", "pm", "复审 PR #100：x（自动收口卡）", "PR-CLAIM: #100\n")],
    )
    code, out, err = run(capsys, "card-audit", "--sweep", "--db", str(board))
    assert (code, err) == (0, "")
    assert "0 张异常" in out
