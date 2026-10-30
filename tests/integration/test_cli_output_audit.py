"""cli output-audit 的集成测试：在 tmp_path 里造真 sqlite，验 rc 三档与关键文案。

自建最小库（``tasks(id, status, assignee, result)`` + ``task_comments(id, task_id, body)``），
**不用** ``tests/fixtures/make_board.py`` —— 那个 fixture 没有 ``task_comments`` 表。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from team_board import cli, gatelog

LIMIT = gatelog.OUTPUT_LIMIT_BYTES


def make_db(
    path: Path,
    *,
    tasks: list[tuple[str, str, str | None]],
    comments: list[tuple[int, str, str | None]] | None = None,
    with_comments_table: bool = True,
) -> Path:
    """建只读目标库：``tasks`` 为 (id, status, result)，``comments`` 为 (id, task_id, body)。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, status TEXT, assignee TEXT, result TEXT)"
        )
        conn.executemany(
            "INSERT INTO tasks (id, status, assignee, result) VALUES (?, ?, 'pm', ?)",
            tasks,
        )
        if with_comments_table:
            conn.execute(
                "CREATE TABLE task_comments ("
                "id INTEGER PRIMARY KEY, task_id TEXT, body TEXT)"
            )
            conn.executemany(
                "INSERT INTO task_comments (id, task_id, body) VALUES (?, ?, ?)",
                comments or [],
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
    """两条都在限内的评论（恰好 8192 与 6 字节中文）：rc0。"""
    return make_db(
        tmp_path / "clean.db",
        tasks=[("t_a", "todo", None), ("t_b", "running", None)],
        comments=[(1, "t_a", "a" * LIMIT), (2, "t_b", "中文")],
    )


class TestExitCodes:
    def test_no_over_limit_is_rc0(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "output-audit", "--db", str(clean_board))
        assert (code, err) == (0, "")
        assert "合计：2 条；超限：0 条" in out
        assert out.splitlines()[-1] == "超限清单（0 条）：—"

    def test_over_limit_comment_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "over.db",
            tasks=[("t_a", "todo", None)],
            comments=[(7, "t_a", "a" * (LIMIT + 1))],
        )
        code, out, err = run(capsys, "output-audit", "--db", str(board))
        assert (code, err) == (1, "")
        assert "合计：1 条；超限：1 条" in out
        assert "超限清单（1 条）：" in out
        assert f"  t_a  comment#7  {LIMIT + 1} 字节" in out

    def test_over_limit_result_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "result.db",
            tasks=[("t_a", "running", "a" * 9000)],
            comments=[],
        )
        code, out, err = run(capsys, "output-audit", "--db", str(board))
        assert (code, err) == (1, "")
        assert "  t_a  result#-  9000 字节" in out

    def test_missing_db_is_rc2_with_empty_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "output-audit", "--db", "/nonexistent/x.db")
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
        code, out, err = run(capsys, "output-audit", "--db", str(junk))
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: ")

    def test_missing_comments_table_is_rc2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bare = make_db(
            tmp_path / "bare.db", tasks=[("t_a", "todo", None)], with_comments_table=False
        )
        code, out, err = run(capsys, "output-audit", "--db", str(bare))
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: ")
        assert "读取 task_comments 失败" in err

    def test_unknown_flag_is_argparse_rc2(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["output-audit", "--db", str(clean_board), "--nope"])
        assert excinfo.value.code == 2
        capsys.readouterr()

    def test_open_local_only_never_writes_the_db(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = clean_board.read_bytes()
        run(capsys, "output-audit", "--db", str(clean_board))
        assert clean_board.read_bytes() == before


class TestScope:
    @pytest.fixture()
    def mixed(self, tmp_path: Path) -> Path:
        """done 卡 9000 字节评论、archived 卡 9000 字节评论、todo 卡 100 字节评论。"""
        return make_db(
            tmp_path / "mixed.db",
            tasks=[
                ("t_done", "done", None),
                ("t_arch", "archived", None),
                ("t_todo", "todo", None),
            ],
            comments=[
                (1, "t_done", "a" * 9000),
                (2, "t_arch", "a" * 9000),
                (3, "t_todo", "a" * 100),
            ],
        )

    def test_default_scope_skips_done_and_archived(
        self, mixed: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "output-audit", "--db", str(mixed))
        assert (code, err) == (0, "")
        assert "范围：未结卡（status ∉ {done, archived}）" in out
        assert "合计：1 条；超限：0 条" in out

    def test_all_scope_includes_done_but_not_archived(
        self, mixed: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "output-audit", "--db", str(mixed), "--all")
        assert (code, err) == (1, "")
        assert "范围：未归档卡（status ≠ archived）" in out
        assert "合计：2 条；超限：1 条" in out
        assert "  t_done  comment#1  9000 字节" in out
        assert "t_arch" not in out

    def test_null_result_is_not_counted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "nullresult.db", tasks=[("t_a", "todo", None)], comments=[]
        )
        code, out, err = run(capsys, "output-audit", "--db", str(board))
        assert (code, err) == (0, "")
        assert "合计：0 条；超限：0 条" in out


class TestDbLayer:
    def test_iter_output_rows_shapes_and_order(self, clean_board: Path) -> None:
        from team_board import db

        conn = db.connect(clean_board)
        try:
            rows = db.iter_output_rows(conn, include_done=False)
        finally:
            conn.close()
        assert rows == [
            ("t_a", "comment", "1", LIMIT),
            ("t_b", "comment", "2", 6),
        ]
