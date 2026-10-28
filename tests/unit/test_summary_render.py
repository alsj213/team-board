"""render_summary 的单元测试：三个计数、异常清单与排序、--date 口径、阈值边界、空板。

fixture 的语义（now=FIXTURE_NOW=2026-10-28 12:00 CST）：
    running_stale  75 分钟  → 异常（阈值 60）
    running_fresh  10 分钟
    review_stale   45 分钟  → 异常（阈值 30）
    review_fresh    5 分钟
    blocked_one   120 分钟  → 阻塞 1
    done_today      完成于 2026-10-28 11:55
    done_yesterday  完成于 2026-10-27 12:00
    archived_old    归档卡，任何计数都不该看到它
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from team_board import commands, db, fmt
from team_board.model import (
    DOING_STALE_MINUTES,
    REVIEW_STALE_MINUTES,
    TITLE_MAX,
    Task,
)
from tests.fixtures.make_board import FIXTURE_NOW, LONG_TITLE, build_board

DAY = datetime.date(2026, 10, 28)
SNAPSHOT = "2026-10-28 12:00"

Board = tuple[list[Task], dict[str, int], dict[str, str]]


def _load(path: Path) -> tuple[list[Task], dict[str, int]]:
    conn = db.connect(path)
    try:
        return db.load_tasks(conn), db.load_rework_counts(conn)
    finally:
        conn.close()


@pytest.fixture()
def board(tmp_path: Path) -> Board:
    path = tmp_path / "b.db"
    ids = build_board(path, now=FIXTURE_NOW)
    tasks, rework = _load(path)
    return tasks, rework, ids


def _render(board: Board, day: datetime.date) -> str:
    tasks, rework, _ = board
    return commands.render_summary(tasks, rework, FIXTURE_NOW, day)


def _counts(text: str) -> list[int]:
    """把 完成 / 在跑 / 阻塞 / 异常 四行解析成整数。"""
    lines = text.splitlines()
    assert len(lines) >= 5
    return [int(lines[i].split(": ")[1]) for i in range(1, 5)]


def _detail(text: str) -> list[str]:
    """异常明细行（第 6 行起）。"""
    return text.splitlines()[5:]


def _task(
    task_id: str,
    status: str,
    *,
    minutes: int,
    title: str = "t",
    completed_minutes_ago: int | None = None,
) -> Task:
    """造一张 started_at 正好 ``minutes`` 分钟前（或已完成）的卡。"""
    started_at = FIXTURE_NOW - minutes * 60
    return Task(
        id=task_id,
        title=title,
        assignee="pm",
        status=status,
        created_at=started_at,
        started_at=started_at,
        completed_at=(
            None
            if completed_minutes_ago is None
            else FIXTURE_NOW - completed_minutes_ago * 60
        ),
    )


# --- 1. 逐字断言（模板写死，id 用 fixture 的） -----------------------------


def test_verbatim_fixture_output(board: Board) -> None:
    ids = board[2]
    expected = "\n".join(
        [
            f"看板概览（快照 {SNAPSHOT}；完成按 2026-10-28）",
            "完成: 1",
            "在跑: 2",
            "阻塞: 1",
            "异常: 2",
            f"  - running 75 分钟（阈值 60）{ids['running_stale']} 「running_stale」",
            f"  - review 45 分钟（阈值 30）{ids['review_stale']} 「review_stale」",
        ]
    )
    assert _render(board, DAY) == expected


def test_no_trailing_newline(board: Board) -> None:
    text = _render(board, DAY)
    assert text
    assert not text.endswith("\n")
    assert "\r" not in text


# --- 2. 三个计数 + 异常条数与顺序 ------------------------------------------


def test_counts_on_fixture_day(board: Board) -> None:
    text = _render(board, DAY)
    assert _counts(text) == [1, 2, 1, 2]
    assert text.splitlines()[0] == f"看板概览（快照 {SNAPSHOT}；完成按 2026-10-28）"


def test_detail_order_stale_first(board: Board) -> None:
    ids = board[2]
    lines = _detail(_render(board, DAY))
    assert len(lines) == 2
    # 分钟降序：75（running_stale）在前，45（review_stale）在后
    assert lines[0] == (
        f"  - running 75 分钟（阈值 60）{ids['running_stale']} 「running_stale」"
    )
    assert lines[1] == (
        f"  - review 45 分钟（阈值 30）{ids['review_stale']} 「review_stale」"
    )


def test_archived_card_absent(board: Board) -> None:
    ids = board[2]
    text = _render(board, DAY)
    assert ids["archived_old"] not in text
    assert "archived" not in text


# --- 3. --date 口径：只影响完成数 ------------------------------------------


def test_previous_day_only_completed_changes(board: Board) -> None:
    today = _render(board, DAY)
    prev = _render(board, datetime.date(2026, 10, 27))
    assert prev.splitlines()[0] == (
        f"看板概览（快照 {SNAPSHOT}；完成按 2026-10-27）"
    )
    assert _counts(prev) == [1, 2, 1, 2]
    assert _detail(prev) == _detail(today)  # 在跑/阻塞/异常与 --date 无关


def test_day_before_yesterday_no_completion(board: Board) -> None:
    text = _render(board, datetime.date(2026, 10, 26))
    assert _counts(text) == [0, 2, 1, 2]
    assert _detail(text) == _detail(_render(board, DAY))


def test_completion_attributed_to_its_own_day() -> None:
    """done_today / done_yesterday 各自只算进自己那一天，互不串台。"""
    tasks = [
        _task("t_today", "done", minutes=30, completed_minutes_ago=5),
        _task("t_yesterday", "done", minutes=1500, completed_minutes_ago=1440),
    ]
    today = commands.render_summary(tasks, {}, FIXTURE_NOW, DAY)
    yesterday = commands.render_summary(
        tasks, {}, FIXTURE_NOW, datetime.date(2026, 10, 27)
    )
    two_days_ago = commands.render_summary(
        tasks, {}, FIXTURE_NOW, datetime.date(2026, 10, 26)
    )
    assert _counts(today)[0] == 1
    assert _counts(yesterday)[0] == 1
    assert _counts(two_days_ago)[0] == 0

    only_yesterday = [tasks[1]]
    assert _counts(commands.render_summary(only_yesterday, {}, FIXTURE_NOW, DAY))[0] == 0
    assert (
        _counts(
            commands.render_summary(
                only_yesterday, {}, FIXTURE_NOW, datetime.date(2026, 10, 27)
            )
        )[0]
        == 1
    )


# --- 4. 阈值边界：恰好等于不算异常 -----------------------------------------


def test_exact_threshold_is_not_stale() -> None:
    tasks = [
        _task("t_run60", "running", minutes=DOING_STALE_MINUTES),
        _task("t_rev30", "review", minutes=REVIEW_STALE_MINUTES),
    ]
    text = commands.render_summary(tasks, {}, FIXTURE_NOW, DAY)
    assert _counts(text) == [0, 1, 0, 0]
    assert text.splitlines() == [
        f"看板概览（快照 {SNAPSHOT}；完成按 2026-10-28）",
        "完成: 0",
        "在跑: 1",
        "阻塞: 0",
        "异常: 0",
    ]


def test_one_minute_over_threshold_is_stale() -> None:
    tasks = [
        _task("t_run61", "running", minutes=DOING_STALE_MINUTES + 1),
        _task("t_rev31", "review", minutes=REVIEW_STALE_MINUTES + 1),
    ]
    text = commands.render_summary(tasks, {}, FIXTURE_NOW, DAY)
    assert _counts(text)[3] == 2
    assert _detail(text) == [
        "  - running 61 分钟（阈值 60）t_run61 「t」",
        "  - review 31 分钟（阈值 30）t_rev31 「t」",
    ]


# --- 5. 排序第二级：同分钟按 id 升序 ---------------------------------------


def test_equal_minutes_sorted_by_id() -> None:
    tasks = [
        _task("t_bb", "running", minutes=70),
        _task("t_aa", "running", minutes=70),
    ]
    text = commands.render_summary(tasks, {}, FIXTURE_NOW, DAY)
    assert [line.split("）")[1].split(" 「")[0] for line in _detail(text)] == [
        "t_aa",
        "t_bb",
    ]


# --- 6. 标题截断 + 「」 ----------------------------------------------------


def test_anomaly_title_truncated_in_brackets() -> None:
    task = _task("t_long", "running", minutes=75, title=LONG_TITLE)
    detail = _detail(commands.render_summary([task], {}, FIXTURE_NOW, DAY))
    assert len(detail) == 1
    line = detail[0]
    assert line.startswith("  - running 75 分钟（阈值 60）t_long 「")
    assert line.endswith("」")
    cell = line.split("「", 1)[1][:-1]
    assert cell != LONG_TITLE
    assert cell.endswith("…")
    assert fmt.display_width(cell) <= TITLE_MAX
    assert LONG_TITLE.startswith(cell[:-1])


# --- 7. 空板 ---------------------------------------------------------------


def test_empty_board(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    build_board(path, now=FIXTURE_NOW)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM task_events")
        conn.commit()
    finally:
        conn.close()

    tasks, rework = _load(path)
    assert tasks == []
    text = commands.render_summary(tasks, rework, FIXTURE_NOW, DAY)
    assert text.splitlines() == [
        f"看板概览（快照 {SNAPSHOT}；完成按 2026-10-28）",
        "完成: 0",
        "在跑: 0",
        "阻塞: 0",
        "异常: 0",
    ]
    assert len(text.splitlines()) == 5
