"""render_member 的单元测试：交付 / 被驳回 / 平均耗时 三个口径 + 查无此人。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from team_board import commands, db
from tests.fixtures.make_board import FIXTURE_NOW, build_board

# fixture 里 engineer 唯一的已交付卡是 done_today（started -30，completed -5 分钟）
ENGINEER = (
    "成员: engineer\n"
    "交付: 1\n"
    "被驳回: 2\n"
    "平均耗时: 25 分钟（样本 1 张已交付卡）"
)

# fixture 里 pm 唯一的已交付卡是 done_yesterday（started -1500，completed -1440 分钟）
PM = (
    "成员: pm\n"
    "交付: 1\n"
    "被驳回: 0\n"
    "平均耗时: 60 分钟（样本 1 张已交付卡）"
)


@pytest.fixture()
def board_path(tmp_path: Path) -> Path:
    path = tmp_path / "b.db"
    build_board(path, now=FIXTURE_NOW)
    return path


def _render(path: Path, name: str) -> str:
    conn = db.connect(path)
    try:
        return commands.render_member(
            name,
            db.load_tasks(conn),
            db.load_rework_counts(conn),
            FIXTURE_NOW,
        )
    finally:
        conn.close()


def _add_card(
    path: Path,
    *,
    task_id: str,
    assignee: str,
    status: str,
    created_off: int,
    started_off: int | None = None,
    completed_off: int | None = None,
) -> None:
    """往 fixture 库里补一张卡（fixture 是临时文件，可以写）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO tasks"
            " (id, title, assignee, status, created_at, started_at, completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                task_id,
                assignee,
                status,
                FIXTURE_NOW - created_off * 60,
                None if started_off is None else FIXTURE_NOW - started_off * 60,
                None if completed_off is None else FIXTURE_NOW - completed_off * 60,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _add_rework_event(path: Path, *, task_id: str, event_id: int) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO task_events (id, task_id, kind, created_at)"
            " VALUES (?, ?, 'changes_requested', ?)",
            (event_id, task_id, FIXTURE_NOW),
        )
        conn.commit()
    finally:
        conn.close()


def test_engineer_exact_four_lines(board_path: Path) -> None:
    assert _render(board_path, "engineer") == ENGINEER


def test_pm_exact_four_lines(board_path: Path) -> None:
    assert _render(board_path, "pm") == PM


def test_no_trailing_newline_and_four_lines(board_path: Path) -> None:
    text = _render(board_path, "engineer")
    assert not text.endswith("\n")
    assert len(text.split("\n")) == 4


def test_no_delivered_card_prints_dash_line(board_path: Path) -> None:
    _add_card(
        board_path,
        task_id="t_qa_blocked",
        assignee="qa",
        status="blocked",
        created_off=10,
        started_off=8,
    )
    assert _render(board_path, "qa") == (
        "成员: qa\n"
        "交付: 0\n"
        "被驳回: 0\n"
        "平均耗时: —（无已交付卡）"
    )


def test_archived_counts_for_rework_but_not_for_delivery(board_path: Path) -> None:
    # pm 的 archived_old：算被驳回，但不算交付，也不进平均耗时样本
    _add_rework_event(board_path, task_id="t_archived_old", event_id=99)
    assert _render(board_path, "pm") == (
        "成员: pm\n"
        "交付: 1\n"
        "被驳回: 1\n"
        "平均耗时: 60 分钟（样本 1 张已交付卡）"
    )


def test_average_floors_integer_division(board_path: Path) -> None:
    # 两张已交付卡：25 分钟（done_today）+ 8 分钟 → 33 // 2 = 16
    _add_card(
        board_path,
        task_id="t_done_short",
        assignee="engineer",
        status="done",
        created_off=20,
        started_off=10,
        completed_off=2,
    )
    assert _render(board_path, "engineer") == (
        "成员: engineer\n"
        "交付: 2\n"
        "被驳回: 2\n"
        "平均耗时: 16 分钟（样本 2 张已交付卡）"
    )


def test_unknown_member_raises_board_error(board_path: Path) -> None:
    conn = db.connect(board_path)
    try:
        tasks = db.load_tasks(conn)
        rework = db.load_rework_counts(conn)
    finally:
        conn.close()

    with pytest.raises(db.BoardError) as excinfo:
        commands.render_member("nosuch", tasks, rework, FIXTURE_NOW)
    assert "nosuch" in str(excinfo.value)


def test_empty_board_raises_board_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    build_board(path, now=FIXTURE_NOW)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM task_events")
        conn.commit()
    finally:
        conn.close()

    conn = db.connect(path)
    try:
        with pytest.raises(db.BoardError):
            commands.render_member("engineer", db.load_tasks(conn), {}, FIXTURE_NOW)
    finally:
        conn.close()
