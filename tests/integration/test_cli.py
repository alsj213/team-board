"""cli.main 的集成测试：退出码 0 / 1 / 2，以及真实 stdout/stderr。"""

from __future__ import annotations

import shutil
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


def test_list_returns_zero_and_prints_table(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, ids = board
    rc = cli.main(["list", "--db", str(path), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert ids["running_stale"] in captured.out
    assert "卡号" in captured.out and "耗时(分)" in captured.out
    assert "archived_old" not in captured.out
    assert not captured.out.endswith("\n\n")  # print 只补一个换行


def test_missing_db_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.db"
    rc = cli.main(["list", "--db", str(missing), "--now", str(FIXTURE_NOW)])
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
    rc = cli.main(["list", "--db", str(junk), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.startswith("错误: ")


def test_bad_flag_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["list", "--bogus"])
    assert excinfo.value.code == 2
    assert "bogus" in capsys.readouterr().err


def test_unknown_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["nosuchcmd"])
    assert excinfo.value.code == 2
    assert "nosuchcmd" in capsys.readouterr().err


def test_no_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_bad_date_on_summary_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["summary", "--date", "2026-13-99"])
    assert excinfo.value.code == 2
    capsys.readouterr()


def _assert_rejected_now(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """越界 --now：argparse 自己拦下 → rc2、stderr 提「基准时间」、无 traceback。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert captured.out == ""
    assert "基准时间" in captured.err
    assert "Traceback" not in captured.err


def test_summary_millisecond_now_exits_two(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    _assert_rejected_now(
        capsys,
        ["summary", "--db", str(path), "--now", "1793160000000"],
    )


def test_list_millisecond_now_exits_two(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """改前这条是 rc0（静默把 13 位毫秒当秒算），本条锁三个子命令一致。"""
    path, _ = board
    _assert_rejected_now(
        capsys,
        ["list", "--db", str(path), "--now", "1793160000000"],
    )


def test_member_huge_now_exits_two(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """锁 OSError 分支（本机 datetime.fromtimestamp 抛 Errno 75）。"""
    path, _ = board
    _assert_rejected_now(
        capsys,
        ["member", "engineer", "--db", str(path), "--now", "999999999999999999"],
    )


def test_out_of_range_now_exits_two_in_real_process(
    board: tuple[Path, dict[str, str]]
) -> None:
    """真进程：越界 --now 走 argparse 的 usage + exit 2，不冒 traceback。"""
    path, _ = board
    exe = Path(__file__).resolve().parents[2] / ".venv/bin/team-board"
    if not exe.exists():  # pragma: no cover - 环境里没有 console script 时跳过
        pytest.skip("console script 不存在")

    run = subprocess.run(
        [str(exe), "summary", "--db", str(path), "--now", "1793160000000"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 2, run.stderr
    assert "基准时间" in run.stderr
    assert "Traceback" not in run.stderr
    assert run.stdout == ""


def test_null_status_card_still_renders(
    board: tuple[Path, dict[str, str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """脏库（tasks.status IS NULL）：表照常出，脏行状态列空白且排最后。"""
    path, ids = board
    dirty = tmp_path / "dirty.db"
    shutil.copyfile(path, dirty)
    conn = sqlite3.connect(str(dirty))
    try:
        conn.execute("UPDATE tasks SET status=NULL WHERE id=?", (ids["null_start"],))
        conn.commit()
    finally:
        conn.close()

    rc = cli.main(["list", "--db", str(dirty), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""

    lines = captured.out.splitlines()
    assert lines[0].startswith("卡号")
    assert len(lines) == 2 + 9  # 表头 + 分隔行 + 9 张未归档卡
    assert ids["done_today"] in captured.out
    assert lines[-1].startswith(ids["null_start"])
    assert lines[-1].split() == [ids["null_start"], "pm", "90", "null_start"]


def test_console_script_exit_codes(board: tuple[Path, dict[str, str]]) -> None:
    """真进程：console script 的退出码与 stdout（幂等、可复跑）。"""
    path, ids = board
    exe = Path(__file__).resolve().parents[2] / ".venv/bin/team-board"
    if not exe.exists():  # pragma: no cover - 环境里没有 console script 时跳过
        pytest.skip("console script 不存在")

    ok = subprocess.run(
        [str(exe), "list", "--db", str(path), "--now", str(FIXTURE_NOW)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert ids["long_title"] in ok.stdout
    assert ok.stdout.splitlines()[0].startswith("卡号")

    bad_db = subprocess.run(
        [str(exe), "list", "--db", str(path.parent / "missing.db")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_db.returncode == 1
    assert bad_db.stderr.startswith("错误: ")

    bad_arg = subprocess.run(
        [str(exe), "list", "--bogus"], capture_output=True, text=True, check=False
    )
    assert bad_arg.returncode == 2


def _make_double_null_db(path: Path) -> None:
    """只建临时库：一张 tasks（含双 NULL 脏行）+ 空 task_events。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                created_at INTEGER, started_at INTEGER, completed_at INTEGER
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks VALUES"
            " ('t_null_ts', 'NULL 时间戳卡', 'pm', 'running', NULL, NULL, NULL)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("sub", ["list", "export", "summary"])
def test_double_null_timestamps_exits_one_without_traceback(
    tmp_path: Path, sub: str
) -> None:
    """真进程：双 NULL 时间戳的库，三个子命令都走「错误: …」+ rc1，不冒 traceback。"""
    exe = Path(__file__).resolve().parents[2] / ".venv/bin/team-board"
    if not exe.exists():  # pragma: no cover - 环境里没有 console script 时跳过
        pytest.skip("console script 不存在")

    db = tmp_path / "null_ts.db"
    _make_double_null_db(db)

    run = subprocess.run(
        [str(exe), sub, "--db", str(db)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1, run.stderr
    assert run.stdout == ""
    assert run.stderr.startswith("错误: "), run.stderr
    assert "t_null_ts" in run.stderr
    assert "缺少 created_at/started_at" in run.stderr
    assert "Traceback" not in run.stderr
    assert "TypeError" not in run.stderr


# --------------------------------------------------------------------------
# list --limit N（1-200）：只截数据行，不改排序，不传时输出逐字不变
# --------------------------------------------------------------------------

LIMIT_REJECTED = [
    "0",
    "-1",
    "201",
    "abc",
    " 5",
    "5 ",
    "+5",
    "1_0",
    "١٢٣",
    "1.0",
    "1793160000000",
    "99999999999999999999",
    "",
]


def _run_list(
    capsys: pytest.CaptureFixture[str],
    path: Path,
    *extra: str,
) -> tuple[int, list[str], str]:
    rc = cli.main(["list", "--db", str(path), "--now", str(FIXTURE_NOW), *extra])
    captured = capsys.readouterr()
    assert captured.err == ""
    return rc, captured.out.splitlines(), captured.out


def test_list_limit_three_prints_two_plus_three_lines(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    _, full_lines, _full_out = _run_list(capsys, path)
    rc, lines, _ = _run_list(capsys, path, "--limit", "3")
    assert rc == 0
    assert len(lines) == 2 + 3
    assert lines[0] == full_lines[0]  # 表头不截
    assert lines[1] == full_lines[1]  # 分隔行不截
    assert lines[2:] == full_lines[2:5]  # 排序不变，只取前 3


def test_list_limit_one_keeps_header(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    rc, lines, _ = _run_list(capsys, path, "--limit", "1")
    assert rc == 0
    assert len(lines) == 3
    assert lines[0].startswith("卡号")


def test_list_limit_200_is_byte_identical(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    _, _, full_out = _run_list(capsys, path)
    rc, _, limited_out = _run_list(capsys, path, "--limit", "200")
    assert rc == 0
    assert limited_out == full_out


def test_list_limit_equal_to_card_count_is_byte_identical(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    _, _, full_out = _run_list(capsys, path)
    rc, _, limited_out = _run_list(capsys, path, "--limit", "9")
    assert rc == 0
    assert limited_out == full_out


def test_list_limit_leading_zero_means_seven(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """``007`` 是合法 ASCII 数字 → int 值 7（不是 3）。"""
    path, _ = board
    rc, lines, _ = _run_list(capsys, path, "--limit", "007")
    assert rc == 0
    assert len(lines) == 2 + 7


def test_list_limit_equals_form(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    rc, lines, _ = _run_list(capsys, path, "--limit=3")
    assert rc == 0
    assert len(lines) == 2 + 3


def test_list_limit_repeated_last_wins(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    rc, lines, _ = _run_list(capsys, path, "--limit", "3", "--limit", "5")
    assert rc == 0
    assert len(lines) == 2 + 5


@pytest.mark.parametrize("value", LIMIT_REJECTED)
def test_list_limit_rejects_bad_values(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str], value: str
) -> None:
    path, _ = board
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["list", "--db", str(path), "--now", str(FIXTURE_NOW), "--limit", value]
        )
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert captured.out == ""
    assert "--limit" in captured.err


def test_list_limit_missing_value_exits_two(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["list", "--db", str(path), "--now", str(FIXTURE_NOW), "--limit"])
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert "expected one argument" in captured.err


def test_list_help_exposes_limit_metavar(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["list", "--help"])
    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    assert "--limit N" in captured.out


@pytest.mark.parametrize("sub", ["summary", "export"])
def test_limit_only_belongs_to_list(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str], sub: str
) -> None:
    path, _ = board
    with pytest.raises(SystemExit) as excinfo:
        cli.main([sub, "--db", str(path), "--now", str(FIXTURE_NOW), "--limit", "3"])
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert "--limit" in captured.err


def test_limit_not_accepted_by_member(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = board
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "member",
                "engineer",
                "--db",
                str(path),
                "--now",
                str(FIXTURE_NOW),
                "--limit",
                "3",
            ]
        )
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert "--limit" in captured.err


def test_list_limit_does_not_mask_missing_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺库仍走「错误: …」+ rc1，--limit 不掩盖数据源错误。"""
    missing = tmp_path / "no-such.db"
    rc = cli.main(
        ["list", "--db", str(missing), "--now", str(FIXTURE_NOW), "--limit", "3"]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.startswith("错误: ")
