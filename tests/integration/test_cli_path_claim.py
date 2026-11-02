"""``path-claim-audit`` / ``path-claim-check`` 的 CLI 集成测试（真 sqlite）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from team_board import cli


def make_db(path: Path, rows: list[tuple[str, str, str, str | None]]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, assignee TEXT, body TEXT)"
        )
        conn.executemany(
            "INSERT INTO tasks (id, status, assignee, body) VALUES (?, ?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()
    return path


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def conflict_board(tmp_path: Path) -> Path:
    return make_db(
        tmp_path / "conflict.db",
        [
            ("t_aaaaaaaa", "running", "engineer", "PATH-CLAIM: src/"),
            ("t_bbbbbbbb", "ready", "engineer", "PATH-CLAIM: src/a.py"),
            ("t_cccccccc", "done", "engineer", "PATH-CLAIM: src/"),  # 已结不参与
            ("t_dddddddd", "review", "qa", "无路径"),
            ("t_eeeeeeee", "ready", "engineer", "PATH-CLAIM: docs/"),
        ],
    )


def test_audit_conflict_rc1(conflict_board: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "path-claim-audit", "--db", str(conflict_board))
    assert (code, err) == (1, "")
    assert "t_aaaaaaaa ↔ t_bbbbbbbb" in out
    assert "src/ ↔ src/a.py" in out
    assert out.splitlines()[-1].startswith("结论: 1 对冲突")


def test_audit_json(conflict_board: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "path-claim-audit", "--db", str(conflict_board), "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["conflicts"][0]["cards"] == ["t_aaaaaaaa", "t_bbbbbbbb"]


def test_audit_no_conflict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    board = make_db(
        tmp_path / "ok.db",
        [
            ("t_aaaaaaaa", "running", "engineer", "PATH-CLAIM: src/a.py"),
            ("t_bbbbbbbb", "ready", "engineer", "PATH-CLAIM: docs/"),
        ],
    )
    code, out, err = run(capsys, "path-claim-audit", "--db", str(board))
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == "结论: 无路径冲突"


def test_audit_parse_issue_is_rc1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    board = make_db(
        tmp_path / "bad.db",
        [("t_aaaaaaaa", "running", "engineer", "PATH-CLAIM: /etc/passwd")],
    )
    code, out, _ = run(capsys, "path-claim-audit", "--db", str(board))
    assert code == 1
    assert "不允许绝对路径" in out


def test_audit_missing_db_is_rc2(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "path-claim-audit", "--db", "/no/such.db")
    assert code == 2
    assert out == ""
    assert err.startswith("错误:")


def test_check_card_conflict_rc1(
    conflict_board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(
        capsys, "path-claim-check", "--card", "t_bbbbbbbb", "--db", str(conflict_board)
    )
    assert (code, err) == (1, "")
    assert "t_aaaaaaaa ↔ t_bbbbbbbb" in out


def test_check_card_safe_rc0(
    conflict_board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(
        capsys, "path-claim-check", "--card", "t_eeeeeeee", "--db", str(conflict_board)
    )
    assert (code, err) == (0, "")
    assert "结论: 无路径冲突" in out


def test_check_card_without_paths_is_rc1(
    conflict_board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(
        capsys, "path-claim-check", "--card", "t_dddddddd", "--db", str(conflict_board)
    )
    assert code == 1
    assert "没有 PATH-CLAIM" in err


def test_check_card_bad_id_is_rc2(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "path-claim-check", "--card", "bad", "--db", "/no/such.db")
    assert code == 2
    assert out == ""
    assert "卡号形态非法" in err
