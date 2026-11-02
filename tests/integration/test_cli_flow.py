"""``card-audit`` / ``handoff-check`` CLI 集成测试（真 sqlite）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from team_board import cli

CARD = "t_1234abcd"
HEAD = "a" * 40
OLD = "b" * 40

BODY = """PATH-CLAIM: src/a.py
TEST-PLAN: quick
CONSUMER: team-board card-audit --card t_1234abcd
QA-SPEC: qa
"""


def make_db(
    path: Path,
    *,
    body: str = BODY,
    status: str = "running",
    events: list[tuple[str, str]] | None = None,
) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, status TEXT, assignee TEXT, title TEXT, body TEXT)"
        )
        conn.execute(
            "CREATE TABLE task_events ("
            "id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            (CARD, status, "engineer", "测试卡", body),
        )
        for index, (kind, payload) in enumerate(events or [], start=1):
            conn.execute(
                "INSERT INTO task_events VALUES (?, ?, ?, ?)",
                (index, CARD, kind, payload),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestCardAudit:
    def test_pass(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(tmp_path / "ok.db")
        code, out, err = run(capsys, "card-audit", "--card", CARD, "--db", str(board))
        assert (code, err) == (0, "")
        assert out.splitlines()[-1] == "结论: 通过"

    def test_missing_lines_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(tmp_path / "bad.db", body="## 目标\n")
        code, out, _ = run(capsys, "card-audit", "--card", CARD, "--db", str(board))
        assert code == 1
        assert "PATH-CLAIM" in out
        assert out.splitlines()[-1].startswith("结论: 不通过")

    def test_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(tmp_path / "json.db", body="## 目标\n")
        code, out, _ = run(
            capsys, "card-audit", "--card", CARD, "--db", str(board), "--json"
        )
        assert code == 1
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["findings"][0]["code"] == "path-claim-missing"

    def test_missing_db_is_rc2(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "card-audit", "--card", CARD, "--db", "/no/such.db")
        assert code == 2
        assert out == ""
        assert err.startswith("错误:")


class TestHandoff:
    def test_pass(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(
            tmp_path / "pass.db",
            events=[("impl_done", json.dumps({"head_sha": HEAD, "pr": 9}))],
        )
        code, out, err = run(
            capsys,
            "handoff-check",
            "--card",
            CARD,
            "--head",
            HEAD,
            "--db",
            str(board),
        )
        assert (code, err) == (0, "")
        assert out.splitlines()[-1] == "结论: 可以交卡"

    def test_stale_head_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "stale.db",
            events=[("impl_done", json.dumps({"head_sha": OLD, "pr": 9}))],
        )
        code, out, _ = run(
            capsys,
            "handoff-check",
            "--card",
            CARD,
            "--head",
            HEAD,
            "--db",
            str(board),
        )
        assert code == 1
        assert "impl-done-stale" in out

    def test_rework_budget_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "rework.db",
            events=[
                ("impl_done", json.dumps({"head_sha": HEAD, "pr": 9})),
                ("changes_requested", "{}"),
                ("changes_requested", "{}"),
            ],
        )
        code, out, _ = run(
            capsys,
            "handoff-check",
            "--card",
            CARD,
            "--head",
            HEAD,
            "--db",
            str(board),
        )
        assert code == 1
        assert "rework-budget" in out

    def test_bad_head_is_rc2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(tmp_path / "head.db")
        code, out, err = run(
            capsys,
            "handoff-check",
            "--card",
            CARD,
            "--head",
            "abc",
            "--db",
            str(board),
        )
        assert code == 2
        assert out == ""
        assert "40 或 64 位" in err

    def test_sha256_head_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--head`` 接受 64 位（与 ``evidence.HEAD_RE`` 一致）。"""

        long_head = "c" * 64
        board = make_db(
            tmp_path / "sha256.db",
            events=[("impl_done", json.dumps({"head_sha": long_head, "pr": 9}))],
        )
        code, out, err = run(
            capsys,
            "handoff-check",
            "--card",
            CARD,
            "--head",
            long_head,
            "--db",
            str(board),
        )
        assert (code, err) == (0, "")
        assert out.splitlines()[-1] == "结论: 可以交卡"

    def test_head_57_hex_is_rc2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        board = make_db(tmp_path / "odd.db")
        code, out, err = run(
            capsys,
            "handoff-check",
            "--card",
            CARD,
            "--head",
            "d" * 57,
            "--db",
            str(board),
        )
        assert code == 2
        assert out == ""
        assert "40 或 64 位" in err

    def test_merge_stage_not_done_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "merge.db",
            status="running",
            events=[("impl_done", json.dumps({"head_sha": HEAD, "pr": 9}))],
        )
        code, out, _ = run(
            capsys,
            "handoff-check",
            "--card",
            CARD,
            "--head",
            HEAD,
            "--stage",
            "merge",
            "--db",
            str(board),
        )
        assert code == 1
        assert "impl-not-done" in out

