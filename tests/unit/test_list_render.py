"""render_list 的单元测试：排序、耗时、截断、表格外观、空板。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from team_board import commands, db, fmt
from team_board.commands import EMPTY_BOARD
from team_board.model import TITLE_MAX, Task
from tests.fixtures.make_board import FIXTURE_NOW, LONG_TITLE, build_board

HEADERS = ["卡号", "负责人", "状态", "耗时(分)", "标题"]

# fixture 里 9 张非 archived 卡的期望顺序与耗时（分钟）
EXPECTED = [
    ("long_title", "engineer", "todo", 20),
    ("null_start", "pm", "ready", 90),
    ("running_stale", "engineer", "running", 75),
    ("running_fresh", "pm", "running", 10),
    ("blocked_one", "pm", "blocked", 120),
    ("review_stale", "pm", "review", 45),
    ("review_fresh", "engineer", "review", 5),
    ("done_yesterday", "pm", "done", 60),
    ("done_today", "engineer", "done", 25),
]


def _load(path: Path) -> list[Task]:
    conn = db.connect(path)
    try:
        return db.load_tasks(conn)
    finally:
        conn.close()


@pytest.fixture()
def rendered(tmp_path: Path) -> tuple[str, dict[str, str]]:
    path = tmp_path / "b.db"
    ids = build_board(path, now=FIXTURE_NOW)
    return commands.render_list(_load(path), FIXTURE_NOW), ids


def _column_widths(sep: str) -> list[int]:
    """从分隔行取各列列宽。"""
    return [len(seg) for seg in sep.split("  ")]


def _column_starts(widths: list[int]) -> list[int]:
    """每列的起始显示列：列宽 + 2 空格间隔。"""
    starts = [0]
    for width in widths[:-1]:
        starts.append(starts[-1] + width + 2)
    return starts


def _slice(line: str, start: int, width: int) -> str:
    """按显示列取 [start, start+width) 区间的原文（宽字符占 2 列）。"""
    out: list[str] = []
    col = 0
    for ch in line:
        if col >= start + width:
            break
        if col >= start:
            out.append(ch)
        col += fmt.display_width(ch)
    return "".join(out)


def test_no_trailing_newline(rendered: tuple[str, dict[str, str]]) -> None:
    text, _ = rendered
    assert text
    assert not text.endswith("\n")


def test_row_count(rendered: tuple[str, dict[str, str]]) -> None:
    text, _ = rendered
    lines = text.splitlines()
    assert len(lines) == 1 + 1 + len(EXPECTED)  # 表头 + 分隔行 + 9 行


def test_order_ids_and_minutes(rendered: tuple[str, dict[str, str]]) -> None:
    text, ids = rendered
    widths = _column_widths(text.splitlines()[1])
    starts = _column_starts(widths)

    rows = []
    for line in text.splitlines()[2:]:
        rows.append(
            (
                _slice(line, starts[0], widths[0]).strip(),
                _slice(line, starts[1], widths[1]).strip(),
                _slice(line, starts[2], widths[2]).strip(),
                _slice(line, starts[3], widths[3]).strip(),
                _slice(line, starts[4], fmt.display_width(line)).strip(),
            )
        )

    assert [row[0] for row in rows] == [ids[name] for name, *_ in EXPECTED]
    assert [row[1] for row in rows] == [assignee for _, assignee, _, _ in EXPECTED]
    assert [row[2] for row in rows] == [status for _, _, status, _ in EXPECTED]
    assert [row[3] for row in rows] == [str(minutes) for *_, minutes in EXPECTED]


def test_archived_card_absent(rendered: tuple[str, dict[str, str]]) -> None:
    text, ids = rendered
    assert "archived_old" not in text
    assert ids["archived_old"] not in text


def test_long_title_truncated(rendered: tuple[str, dict[str, str]]) -> None:
    text, _ = rendered
    lines = text.splitlines()
    widths = _column_widths(lines[1])
    starts = _column_starts(widths)
    line = lines[2]  # 第一行是 long_title（todo 排最前）
    title_cell = _slice(line, starts[4], fmt.display_width(line)).strip()
    assert title_cell.endswith("…")
    assert fmt.display_width(title_cell) <= TITLE_MAX
    assert LONG_TITLE.startswith(title_cell[:-1])
    assert title_cell != LONG_TITLE


def test_duration_column_right_aligned(rendered: tuple[str, dict[str, str]]) -> None:
    text, _ = rendered
    lines = text.splitlines()
    widths = _column_widths(lines[1])
    starts = _column_starts(widths)
    start, width = starts[3], widths[3]
    # 表头「耗时(分)」显示宽 8（半角括号），恰好填满该列，所以只看数据行能否右对齐
    assert _slice(lines[0], start, width) == "耗时(分)"
    assert width == 8
    for line, (*_, minutes) in zip(lines[2:], EXPECTED, strict=True):
        cell = str(minutes)
        assert _slice(line, start, width) == " " * (width - len(cell)) + cell


def test_table_look_rstrip_and_separator(rendered: tuple[str, dict[str, str]]) -> None:
    """pm 裁决的口径：行尾零空格恒成立；分隔行各段 - 数 == 列宽。"""
    text, _ = rendered
    lines = text.splitlines()
    for line in lines:
        assert line == line.rstrip()

    sep = lines[1]
    widths = _column_widths(sep)
    assert len(widths) == 5
    for seg, width in zip(sep.split("  "), widths, strict=True):
        assert seg == "-" * width
    full = fmt.display_width(sep)
    assert full == sum(widths) + 2 * (len(widths) - 1)
    for line in lines:
        assert fmt.display_width(line) <= full


def test_columns_aligned(rendered: tuple[str, dict[str, str]]) -> None:
    """除末列外，表头与每行数据在该列起始显示列上完全对齐。"""
    text, _ = rendered
    lines = text.splitlines()
    widths = _column_widths(lines[1])
    starts = _column_starts(widths)

    assert [_slice(lines[0], starts[j], widths[j]).strip() for j in range(5)] == HEADERS
    for line, (_, assignee, status, minutes) in zip(lines[2:], EXPECTED, strict=True):
        assert _slice(line, starts[1], widths[1]).strip() == assignee
        assert _slice(line, starts[2], widths[2]).strip() == status
        assert _slice(line, starts[3], widths[3]).strip() == str(minutes)


def test_last_column_filled_means_full_width(tmp_path: Path) -> None:
    """末列自然满宽的小表：那一行的显示宽度 == 全表宽度。"""
    title = "x" * TITLE_MAX  # 恰好等于列宽，truncate 原样返回
    task = Task(
        id="t_a",
        title=title,
        assignee="pm",
        status="todo",
        created_at=FIXTURE_NOW - 60,
        started_at=None,
        completed_at=None,
    )
    text = commands.render_list([task], FIXTURE_NOW)
    lines = text.splitlines()
    full = fmt.display_width(lines[1])
    assert fmt.display_width(lines[2]) == full
    assert fmt.display_width(lines[2]) > fmt.display_width(lines[0])  # 表头末列不满宽


def test_empty_assignee_shows_dash() -> None:
    task = Task(
        id="t_a",
        title="x",
        assignee="",
        status="todo",
        created_at=FIXTURE_NOW,
        started_at=None,
        completed_at=None,
    )
    text = commands.render_list([task], FIXTURE_NOW)
    assert "—" in text


def test_empty_board_message(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    build_board(path, now=FIXTURE_NOW)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM tasks")
        conn.commit()
    finally:
        conn.close()
    assert commands.render_list(_load(path), FIXTURE_NOW) == EMPTY_BOARD
    assert EMPTY_BOARD == "（看板上没有未归档卡）"


def test_only_archived_board_is_empty() -> None:
    task = Task(
        id="t_old",
        title="old",
        assignee="pm",
        status="archived",
        created_at=0,
        started_at=None,
        completed_at=None,
    )
    assert commands.render_list([task], FIXTURE_NOW) == EMPTY_BOARD


def test_null_status_renders_blank_cell_and_sorts_last() -> None:
    """脏数据 status=None：不抛、状态列空白、按未知状态排最后（token 口径同 CLI 5）。"""
    clean = Task(
        id="t_done_today",
        title="done_today",
        assignee="engineer",
        status="done",
        created_at=FIXTURE_NOW - 35 * 60,
        started_at=FIXTURE_NOW - 30 * 60,
        completed_at=FIXTURE_NOW - 5 * 60,
    )
    dirty = Task(
        id="t_null_start",
        title="null_start",
        assignee="pm",
        status=None,  # type: ignore[arg-type]  # 手工构造脏行，模拟 tasks.status IS NULL
        created_at=FIXTURE_NOW - 90 * 60,
        started_at=None,
        completed_at=None,
    )

    text = commands.render_list([dirty, clean], FIXTURE_NOW)
    lines = text.splitlines()
    assert lines[0].startswith("卡号")
    assert len(lines) == 2 + 2
    assert clean.id in text
    assert lines[-1].startswith(dirty.id)
    assert lines[-1].split() == [dirty.id, "pm", "90", "null_start"]
