"""cli ``health`` 的集成测试：真 sqlite + rc 两档 + 文本/JSON 契约。

fixture 与单测共用 ``tests.unit.test_health.build_health_db``（同一份固定数字，
不复制），``NOW`` 也来自那里 —— 断言里出现的分钟数因此都能手算复核。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from team_board import cli
from tests.unit.test_health import NOW, STAMP, build_health_db


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    return build_health_db(tmp_path / "health.db")


@pytest.fixture()
def empty_board(tmp_path: Path) -> Path:
    return build_health_db(tmp_path / "empty.db", runs=(), events=(), tasks=())


def test_empty_board_prints_zeros_and_rc0(
    empty_board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = run(capsys, "health", "--db", str(empty_board), "--now", str(NOW))
    assert rc == 0
    assert err == ""
    assert out == (
        f"看板健康（窗口 7 天；快照 {STAMP}）\n"
        "runs: 0 次 / 0 分钟\n"
        "rework: 0 张卡 / 0 次\n"
        "ready: 0 张（最老 —）\n"
        "结论: 正常（无 ready 积压，窗口内无返工）\n"
    )


def test_empty_board_json_has_all_zero_counters(
    empty_board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = run(
        capsys, "health", "--db", str(empty_board), "--now", str(NOW), "--json"
    )
    assert rc == 0
    assert err == ""
    assert json.loads(out) == {
        "window_days": 7,
        "runs": {},
        "rework": {"cards": 0, "events": 0},
        "ready": {"count": 0, "oldest_card": None, "oldest_minutes": None},
        "generated_at": NOW,
    }


def test_fixture_json_matches_contract(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """固定 fixture 的 JSON 逐项核对：runs 分钟 / rework / ready 年龄。"""
    rc, out, err = run(
        capsys,
        "health",
        "--db",
        str(board),
        "--now",
        str(NOW),
        "--since-days",
        "7",
        "--json",
    )
    assert rc == 0
    assert err == ""
    payload = json.loads(out)
    assert list(payload) == [
        "window_days",
        "runs",
        "rework",
        "ready",
        "generated_at",
    ]
    assert payload == {
        "window_days": 7,
        "runs": {
            "": {"count": 1, "minutes": 1},
            "engineer": {"count": 4, "minutes": 50},
            "pm": {"count": 2, "minutes": 1},
            "qa": {"count": 2, "minutes": 2},
        },
        "rework": {"cards": 2, "events": 3},
        "ready": {"count": 3, "oldest_card": "t_c3", "oldest_minutes": 180},
        "generated_at": NOW,
    }


def test_fixture_text_ends_with_conclusion(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = run(capsys, "health", "--db", str(board), "--now", str(NOW))
    assert rc == 0
    assert err == ""
    lines = out.splitlines()
    assert lines[0] == f"看板健康（窗口 7 天；快照 {STAMP}）"
    assert lines[1] == "runs: 9 次 / 54 分钟"
    assert lines[-1] == "结论: 关注（窗口内返工 3 次）"
    assert out.endswith("\n")


def test_since_days_narrows_the_window(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = run(
        capsys,
        "health",
        "--db",
        str(board),
        "--now",
        str(NOW),
        "--since-days",
        "1",
        "--json",
    )
    assert rc == 0
    assert err == ""
    payload = json.loads(out)
    assert payload["window_days"] == 1
    assert payload["runs"]["engineer"] == {"count": 3, "minutes": 40}
    # ready 与窗口无关
    assert payload["ready"] == {
        "count": 3,
        "oldest_card": "t_c3",
        "oldest_minutes": 180,
    }


def test_default_window_days_is_seven_without_flag(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = run(capsys, "health", "--db", str(board), "--now", str(NOW), "--json")
    assert rc == 0
    assert json.loads(out)["window_days"] == 7


@pytest.mark.parametrize(
    "bad",
    ["0", "-1", "", "abc", "+7", " 7", "1_0", "7.0", "١٢٣"],
)
def test_since_days_rejects_bad_values(
    board: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    """0 / 负数 / 空串 / 怪形态一律 rc2，且 stderr 恰一行。"""
    rc, out, err = run(
        capsys, "health", "--db", str(board), "--now", str(NOW), "--since-days", bad
    )
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: --since-days")
    assert err.count("\n") == 1


def test_since_days_accepts_large_positive(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """大窗口是合法输入（全量历史），不是参数错误。"""
    rc, out, err = run(
        capsys,
        "health",
        "--db",
        str(board),
        "--now",
        str(NOW),
        "--since-days",
        "1000000",
        "--json",
    )
    assert rc == 0
    assert err == ""
    assert json.loads(out)["window_days"] == 1000000


def test_missing_table_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """缺 ``task_runs``：rc2、stdout 空、stderr 一行点名缺哪张表。"""
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, created_at INTEGER)")
        conn.execute(
            "CREATE TABLE task_events (task_id TEXT, kind TEXT, created_at INTEGER)"
        )
        conn.commit()
    finally:
        conn.close()

    rc, out, err = run(capsys, "health", "--db", str(path), "--now", str(NOW))
    assert rc == 2
    assert out == ""
    assert err == "错误: 缺少表: task_runs\n"


def test_non_sqlite_file_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "not-a-db.txt"
    path.write_text("这不是 SQLite\n", encoding="utf-8")
    rc, out, err = run(capsys, "health", "--db", str(path), "--now", str(NOW))
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1


def test_missing_db_file_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = run(
        capsys, "health", "--db", str(tmp_path / "nope.db"), "--now", str(NOW)
    )
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: 数据源不存在")


def test_thirteen_digit_now_is_rejected_by_argparse(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """13 位毫秒（``--now`` 填错了单位）→ argparse rc2，不冒 traceback。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["health", "--db", str(board), "--now", "1793160000000"])
    assert excinfo.value.code == 2
    capsys.readouterr()


@pytest.mark.parametrize("as_json", [False, True])
def test_health_never_writes_the_db(
    board: Path, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    args = ["health", "--db", str(board), "--now", str(NOW)]
    if as_json:
        args.append("--json")
    rc, _, _ = run(capsys, *args)
    assert rc == 0
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before
