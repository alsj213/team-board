"""cli ``pr-claim-audit --gaps`` 的集成测试：真 sqlite + rc 三档 + 互斥参数。

与 ``test_cli_pr_claim_audit.py`` 分开一份：``--gaps`` 要读 ``tasks.title``，
建库时多一列；``--pr`` 的旧用例一列不改（回归靠另一份文件）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from team_board import cli


def make_db(
    path: Path, rows: list[tuple[str, str, str | None, str | None, str | None]]
) -> Path:
    """建只有 ``tasks`` 表的库；``rows`` = (id, status, assignee, title, body)。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, status TEXT, assignee TEXT, title TEXT, body TEXT)"
        )
        conn.executemany(
            "INSERT INTO tasks (id, status, assignee, title, body) VALUES (?, ?, ?, ?, ?)",
            rows,
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
    """无缺口：合并卡首行带认领行，非合并卡不带也无所谓。"""
    return make_db(
        tmp_path / "clean.db",
        [
            ("t_a", "running", "engineer", "合并卡（pm）：x", "PR-CLAIM: #47\n目标：…"),
            ("t_b", "todo", "pm", "工作卡（engineer）：y", "目标：…"),
        ],
    )


class TestGapsExitCodes:
    def test_no_gap_is_rc0(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "pr-claim-audit", "--gaps", "--db", str(clean_board))
        assert (code, err) == (0, "")
        assert out.splitlines() == [
            "未结卡 2 张",
            "结论: 无缺口（合并类卡都带 PR-CLAIM）",
        ]

    def test_one_gap_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "gap.db",
            [
                ("t_gap", "running", "engineer", "合并卡（pm）：x", "目标：…"),
                ("t_ok", "todo", "pm", "工作卡（engineer）：y", "目标：…"),
            ],
        )
        code, out, err = run(capsys, "pr-claim-audit", "--gaps", "--db", str(board))
        assert (code, err) == (1, "")
        assert "t_gap" in out
        assert out.splitlines()[1] == "GAP t_gap [running] engineer 触发=合并卡 首行=目标：…"
        assert out.splitlines()[-1] == "结论: 缺口 1 张（逐张把 PR-CLAIM: #<n> 放到正文首行）"

    def test_gap_fixed_by_adding_first_line_is_rc0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """同一张卡：先缺认领行 rc1，补首行后 rc0（缺口可被修掉）。"""
        db_path = tmp_path / "fix.db"
        make_db(db_path, [("t_gap", "running", "engineer", "合并卡（pm）：x", "目标：…")])
        first = run(capsys, "pr-claim-audit", "--gaps", "--db", str(db_path))
        assert first[0] == 1

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE tasks SET body = ? WHERE id = 't_gap'",
                ("PR-CLAIM: #47\n目标：…",),
            )
            conn.commit()
        finally:
            conn.close()

        code, out, err = run(capsys, "pr-claim-audit", "--gaps", "--db", str(db_path))
        assert (code, err) == (0, "")
        assert out.splitlines()[-1] == "结论: 无缺口（合并类卡都带 PR-CLAIM）"

    def test_done_and_archived_cards_are_out_of_scope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "closed.db",
            [
                ("t_done", "done", "pm", "合并卡（pm）：x", "目标：…"),
                ("t_arch", "archived", "pm", "合并卡（pm）：y", "目标：…"),
                ("t_open", "running", "engineer", "合并卡（pm）：z", "PR-CLAIM: #47"),
            ],
        )
        code, out, err = run(capsys, "pr-claim-audit", "--gaps", "--db", str(board))
        assert (code, err) == (0, "")
        assert out.splitlines()[0] == "未结卡 1 张"
        assert "t_done" not in out and "t_arch" not in out

    def test_null_title_and_body_are_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """NULL 归空串：标题 NULL → 不触发；body NULL 的合并卡 → 缺口。"""
        board = make_db(
            tmp_path / "nulls.db",
            [
                ("t_null_title", "todo", None, None, "PR-CLAIM: #1"),
                ("t_null_body", "running", None, "收口卡：x", None),
            ],
        )
        code, out, err = run(capsys, "pr-claim-audit", "--gaps", "--db", str(board))
        assert (code, err) == (1, "")
        assert "t_null_body" in out
        assert "t_null_title" not in out
        assert out.splitlines()[1] == "GAP t_null_body [running]  触发=收口卡 首行="


class TestGapsErrors:
    def test_pr_and_gaps_are_mutually_exclusive(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(
                ["pr-claim-audit", "--pr", "47", "--gaps", "--db", str(clean_board)]
            )
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err != ""

    def test_neither_pr_nor_gaps_is_argparse_rc2(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["pr-claim-audit", "--db", str(clean_board)])
        assert excinfo.value.code == 2
        capsys.readouterr()

    def test_missing_db_is_rc2_with_empty_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(
            capsys, "pr-claim-audit", "--gaps", "--db", "/nonexistent/x.db"
        )
        assert (code, out) == (2, "")
        assert err.count("\n") == 1
        assert err.startswith("错误: ")

    def test_db_without_title_column_is_rc2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """老库没有 title 列 → 读库失败按参数/环境错 rc2，不冒 traceback。"""
        bare = tmp_path / "no-title.db"
        conn = sqlite3.connect(bare)
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, assignee TEXT, body TEXT)")
        conn.commit()
        conn.close()
        code, out, err = run(capsys, "pr-claim-audit", "--gaps", "--db", str(bare))
        assert (code, out) == (2, "")
        assert err.count("\n") == 1
        assert err.startswith("错误: ")

    def test_non_sqlite_file_is_rc2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        junk = tmp_path / "junk.db"
        junk.write_text("这不是 SQLite 库。\n", encoding="utf-8")
        code, out, err = run(capsys, "pr-claim-audit", "--gaps", "--db", str(junk))
        assert (code, out) == (2, "")
        assert err.count("\n") == 1
        assert err.startswith("错误: ")

    def test_gaps_mode_never_writes_the_db(
        self, clean_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = clean_board.read_bytes()
        run(capsys, "pr-claim-audit", "--gaps", "--db", str(clean_board))
        assert clean_board.read_bytes() == before
