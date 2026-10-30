"""cli ``pr-claim-audit`` 的集成测试：在 tmp_path 里造真 sqlite，验 rc 三档与文案。

只建 ``tasks`` 一张表（列名与真实库一致的最小集合），不依赖 fixtures/make_board
的固定卡 —— 这里的断言全是「哪张卡被算成认领/引用、退出码是几」。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from team_board import cli

PR = 34


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
def unique_board(tmp_path: Path) -> Path:
    """恰好一张未结卡认领 PR #34（另一张只引用）：rc0。"""
    return make_db(
        tmp_path / "unique.db",
        [
            ("t_a", "running", "engineer", "PR-CLAIM: #34"),
            ("t_b", "todo", "pm", "PR-REF: #34"),
            ("t_c", "todo", "pm", "无关卡"),
        ],
    )


@pytest.fixture()
def dupe_board(tmp_path: Path) -> Path:
    """两张未结卡都认领 PR #34：rc1（重复认领）。"""
    return make_db(
        tmp_path / "dupe.db",
        [
            ("t_a", "running", "engineer", "PR-CLAIM: #34"),
            ("t_b", "review", "pm", "PR-CLAIM: #34"),
        ],
    )


@pytest.fixture()
def none_board(tmp_path: Path) -> Path:
    """没有卡认领 PR #34（只有引用）：rc1（无人认领）。"""
    return make_db(
        tmp_path / "none.db",
        [("t_a", "running", "engineer", "PR-REF: #34")],
    )


class TestExitCodes:
    def test_single_claim_is_rc0(
        self, unique_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(unique_board))
        assert (code, err) == (0, "")
        assert out.splitlines() == [
            "未结卡 3 张",
            "CLAIM t_a [running] engineer",
            "REF t_b [todo] pm",
            "结论: 认领唯一 → t_a",
        ]

    def test_two_claims_is_rc1(
        self, dupe_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(dupe_board))
        assert (code, err) == (1, "")
        assert "CLAIM t_a [running] engineer" in out
        assert "CLAIM t_b [review] pm" in out
        assert out.splitlines()[-1].startswith("结论: 重复认领")

    def test_no_claim_is_rc1(
        self, none_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(none_board))
        assert (code, err) == (1, "")
        assert "REF t_a [running] engineer" in out
        assert out.splitlines()[-1] == "结论: 无未结卡认领该 PR（合并前必须先认领）"

    @pytest.mark.parametrize("value", ["0", "-1", "1793160000000", "", "abc", "1_0"])
    def test_bad_pr_is_rc2(
        self, value: str, unique_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "pr-claim-audit", "--pr", value, "--db", str(unique_board))
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: ")

    def test_missing_pr_is_argparse_rc2(
        self, unique_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["pr-claim-audit", "--db", str(unique_board)])
        assert excinfo.value.code == 2
        capsys.readouterr()

    def test_missing_db_is_rc2_with_empty_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", "/nonexistent/x.db")
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
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(junk))
        assert (code, out) == (2, "")
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
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(bare))
        assert (code, out) == (2, "")
        assert err.count("\n") == 1
        assert "tasks" in err

    def test_open_local_only_never_writes_the_db(
        self, unique_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = unique_board.read_bytes()
        run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(unique_board))
        assert unique_board.read_bytes() == before


class TestScope:
    def test_done_and_archived_claims_do_not_count(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """已结/已归档卡上的认领行不参与判定：唯一未结卡认领 → rc0。"""
        board = make_db(
            tmp_path / "closed.db",
            [
                ("t_done", "done", "pm", "PR-CLAIM: #34"),
                ("t_arch", "archived", "pm", "PR-CLAIM: #34"),
                ("t_open", "running", "engineer", "PR-CLAIM: #34"),
            ],
        )
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(board))
        assert (code, err) == (0, "")
        assert "未结卡 1 张" in out
        assert "CLAIM t_open [running] engineer" in out
        assert "t_done" not in out
        assert "t_arch" not in out

    def test_closed_card_alone_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(
            tmp_path / "only-done.db",
            [("t_done", "done", "pm", "PR-CLAIM: #34")],
        )
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(board))
        assert (code, err) == (1, "")
        assert out.splitlines()[0] == "未结卡 0 张"
        assert "CLAIM " not in out

    def test_indented_or_trailing_claim_line_is_ignored(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """顶格 + 无尾随字符是硬要求：宽松写法不算认领，因此 rc1。"""
        board = make_db(
            tmp_path / "loose.db",
            [
                ("t_a", "running", "engineer", "  PR-CLAIM: #34"),
                ("t_b", "running", "engineer", "PR-CLAIM: #34 我领了"),
            ],
        )
        code, out, err = run(capsys, "pr-claim-audit", "--pr", str(PR), "--db", str(board))
        assert (code, err) == (1, "")
        assert out.splitlines()[-1].startswith("结论: 无未结卡认领该 PR")
