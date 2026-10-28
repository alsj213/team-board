"""cli.main 的 export 集成测试：退出码 0 / 1 / 2，以及真实 stdout/stderr。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from team_board import cli
from tests.fixtures.make_board import FIXTURE_NOW, build_board


@pytest.fixture()
def board(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "b.db"
    ids = build_board(path, now=FIXTURE_NOW)
    return path, ids


def test_export_returns_zero_and_prints_single_line_json(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, ids = board
    rc = cli.main(["export", "--db", str(path), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1  # print 只补一个换行 → 单行 JSON
    payload = json.loads(captured.out)
    assert isinstance(payload, list) and len(payload) == 9
    assert ids["running_stale"] in captured.out
    assert ids["archived_old"] not in captured.out


def test_empty_board_returns_zero_and_empty_array(
    board: tuple[Path, dict[str, str]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """全 archive / 无卡 → stdout 就是 `[]`，rc 0（不是列表的空板文案）。"""
    path, _ = board
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty))
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT,"
            " created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()

    rc = cli.main(["export", "--db", str(empty), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == "[]\n"


def test_missing_db_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.db"
    rc = cli.main(["export", "--db", str(missing), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("错误: ")
    assert "数据源不存在" in captured.err


def test_not_a_database_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a sqlite file at all")
    rc = cli.main(["export", "--db", str(junk), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("错误: ")


def test_sqlite_without_tasks_table_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """合法 SQLite 但没有 tasks 表 → 走 db.BoardError → rc1，不冒 traceback。"""
    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    try:
        conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    rc = cli.main(["export", "--db", str(other), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("错误: ")
    assert "tasks" in captured.err
    assert "Traceback" not in captured.err


def _assert_rejected_now(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """越界 / 非整数 --now：argparse 自己拦下 → rc2、提「基准时间」、无 traceback。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert captured.out == ""
    assert "基准时间" in captured.err
    assert "Traceback" not in captured.err


def test_millisecond_now_exits_two(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    _assert_rejected_now(
        capsys, ["export", "--db", str(path), "--now", "1793160000000"]
    )


def test_astronomical_now_exits_two(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    _assert_rejected_now(
        capsys, ["export", "--db", str(path), "--now", "999999999999999999"]
    )


def test_empty_now_exits_two(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    _assert_rejected_now(capsys, ["export", "--db", str(path), "--now", ""])


def test_non_numeric_now_exits_two(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    _assert_rejected_now(capsys, ["export", "--db", str(path), "--now", "abc"])


def test_bad_flag_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["export", "--bogus"])
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert captured.out == ""
    assert "bogus" in captured.err


@pytest.mark.parametrize("now", ["0", "-86400"])
def test_now_domain_edges_are_accepted(
    board: tuple[Path, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
    now: str,
) -> None:
    """--now 取值域两端（0 / 负数）都 rc0，且耗时全部夹到 >= 0。"""
    path, _ = board
    rc = cli.main(["export", "--db", str(path), "--now", now])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert len(payload) == 9
    assert all(row["duration_minutes"] >= 0 for row in payload)


def test_real_process(board: tuple[Path, dict[str, str]]) -> None:
    """真进程：console script 跑 export，退出码与单行 JSON 输出。"""
    path, ids = board
    exe = Path(__file__).resolve().parents[2] / ".venv/bin/team-board"
    if not exe.exists():  # pragma: no cover - 环境里没有 console script 时跳过
        pytest.skip("console script 不存在")

    run = subprocess.run(
        [str(exe), "export", "--db", str(path), "--now", str(FIXTURE_NOW)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stderr == ""
    assert run.stdout.count("\n") == 1
    payload = json.loads(run.stdout)
    assert len(payload) == 9
    assert ids["done_today"] in run.stdout
    assert ids["archived_old"] not in run.stdout
