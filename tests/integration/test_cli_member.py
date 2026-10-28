"""cli 层 member 子命令的集成测试：正常输出、查无此人、缺库。"""

from __future__ import annotations

from pathlib import Path

import pytest

from team_board import cli
from tests.fixtures.make_board import FIXTURE_NOW, build_board


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    path = tmp_path / "b.db"
    build_board(path, now=FIXTURE_NOW)
    return path


def test_member_prints_four_lines(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["member", "engineer", "--db", str(board), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == (
        "成员: engineer\n"
        "交付: 1\n"
        "被驳回: 2\n"
        "平均耗时: 25 分钟（样本 1 张已交付卡）\n"
    )


def test_unknown_member_exits_one_with_chinese_error(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["member", "nosuch", "--db", str(board), "--now", str(FIXTURE_NOW)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("错误: ")
    assert "nosuch" in captured.err


def test_member_missing_db_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(
        ["member", "engineer", "--db", str(tmp_path / "nope.db"), "--now", str(FIXTURE_NOW)]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.startswith("错误: ")
    assert "数据源不存在" in captured.err


def test_member_without_name_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["member", "--db", "whatever.db"])
    assert excinfo.value.code == 2
