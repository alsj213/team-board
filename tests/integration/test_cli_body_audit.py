"""cli body-audit 的集成测试：在 tmp_path 里造真 sqlite，验 rc 三档与关键文案。

只建 ``tasks`` 一张表（列名与真实库一致的最小集合），不依赖 fixtures/make_board 的
11 张固定卡 —— 这里的断言全是「这张卡在不在、算成几字节」。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from team_board import cli

LIMIT = 2048


def make_db(path: Path, rows: list[tuple[str, str, str, str | None]]) -> Path:
    """建一个只有 ``tasks`` 表的只读目标库（``rows`` 为 id/status/assignee/body）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, status TEXT, assignee TEXT, body TEXT)"
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
def clean_board(tmp_path: Path) -> Path:
    """全部卡都不超限的板：rc0。"""
    return make_db(
        tmp_path / "clean.db",
        [
            ("t_a", "todo", "pm", "a" * LIMIT),
            ("t_b", "running", "engineer", "中文"),
            ("t_c", "scheduled", "pm", ""),
        ],
    )


@pytest.fixture()
def over_board(tmp_path: Path) -> Path:
    """含一张 2049 字节卡：rc1。"""
    return make_db(
        tmp_path / "over.db",
        [
            ("t_a", "todo", "pm", "a" * LIMIT),
            ("t_b", "running", "engineer", "a" * (LIMIT + 1)),
        ],
    )


class TestExitCodes:
    def test_no_over_limit_is_rc0(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "body-audit", "--db", str(clean_board))
        assert code == 0
        assert err == ""
        assert "合计：3 张；超限：0 张；NULL：0 张" in out
        assert "超限清单（0 张）：—" in out

    def test_over_limit_is_rc1(
        self, over_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "body-audit", "--db", str(over_board))
        assert code == 1
        assert err == ""
        assert "合计：2 张；超限：1 张；NULL：0 张" in out
        assert "超限清单（1 张）：t_b" in out
        # 明细里 2049 那张是 OVER，2048 那张是 ok（边界含在「不超限」里）
        assert "engineer  " + str(LIMIT + 1) + "  OVER" in out
        assert str(LIMIT) + "  ok" in out

    def test_missing_db_is_rc2_with_empty_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "body-audit", "--db", "/nonexistent/x.db")
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: ")
        assert "x.db" in err

    def test_non_sqlite_file_is_rc2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        junk = tmp_path / "junk.db"
        junk.write_text("这不是 SQLite 库，只是一段文本。\n", encoding="utf-8")
        code, out, err = run(capsys, "body-audit", "--db", str(junk))
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: ")

    def test_missing_tasks_table_is_rc2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bare = tmp_path / "bare.db"
        conn = sqlite3.connect(bare)
        conn.execute("CREATE TABLE other (id TEXT)")
        conn.commit()
        conn.close()
        code, out, err = run(capsys, "body-audit", "--db", str(bare))
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert "tasks" in err

    def test_unknown_flag_is_argparse_rc2(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["body-audit", "--db", str(clean_board), "--nope"])
        assert excinfo.value.code == 2
        capsys.readouterr()

    def test_open_local_only_never_writes_the_db(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = clean_board.read_bytes()
        run(capsys, "body-audit", "--db", str(clean_board))
        assert clean_board.read_bytes() == before


class TestScope:
    @pytest.fixture()
    def mixed(self, tmp_path: Path) -> Path:
        """一张 done 卡 3000 字节、一张 archived 卡 3000 字节、一张 todo 卡 100 字节。"""
        return make_db(
            tmp_path / "mixed.db",
            [
                ("t_done", "done", "pm", "a" * 3000),
                ("t_arch", "archived", "pm", "a" * 3000),
                ("t_todo", "todo", "engineer", "a" * 100),
            ],
        )

    def test_default_scope_skips_done_and_archived(
        self, mixed: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "body-audit", "--db", str(mixed))
        assert (code, err) == (0, "")
        assert "范围：未结卡（status ∉ {done, archived}）" in out
        assert "合计：1 张；超限：0 张" in out
        assert "t_done" not in out
        assert "t_arch" not in out

    def test_all_scope_includes_done_but_not_archived(
        self, mixed: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "body-audit", "--db", str(mixed), "--all")
        assert (code, err) == (1, "")
        assert "范围：未归档卡（status ≠ archived）" in out
        assert "合计：2 张；超限：1 张" in out
        assert "t_done" in out
        assert "t_arch" not in out

    def test_null_body_is_reported_not_counted_as_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "null.db",
            [
                ("t_null", "todo", "pm", None),
                ("t_empty", "todo", "pm", ""),
            ],
        )
        code, out, err = run(capsys, "body-audit", "--db", str(board))
        assert (code, err) == (0, "")
        assert "合计：2 张；超限：0 张；NULL：1 张" in out
        assert "  NULL  1" in out or "NULL       1" in out
        assert "t_null" in out
        assert "  NULL  NULL" in out


class TestHeaderAndShape:
    def test_first_line_is_absolute_db_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(tmp_path / "path.db", [("t_a", "todo", "pm", "x")])
        code, out, err = run(capsys, "body-audit", "--db", str(board))
        assert (code, err) == (0, "")
        assert out.splitlines()[0] == str(board)

    def test_sections_appear_in_order(
        self, over_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(capsys, "body-audit", "--db", str(over_board))
        lines = out.splitlines()
        assert lines[1].startswith("口径：body 为 UTF-8 字节数")
        assert lines[2].startswith("范围：")
        assert lines[3] == "分桶："
        assert [line.split()[-1] for line in lines[4:11]] == [
            "0",
            "0",
            "0",
            "1",
            "1",
            "0",
            "0",
        ]
        assert lines[11].startswith("合计：")
        assert lines[12] == "明细（字节数降序：卡号 / 状态 / 负责人 / 字节 / 判定）："
        assert lines[-1].startswith("超限清单（1 张）：")
