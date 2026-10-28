"""web.py 数据层的单元测试：分组、卡片形状、空板、summary 口径对齐、HTML 常量。

fixture 口径（now=FIXTURE_NOW=2026-10-28 12:00 CST）：
    running_stale   75 分钟（> 60）→ 异常 1
    running_fresh   10 分钟
    review_stale    45 分钟（> 30）→ 异常 1
    review_fresh     5 分钟
    blocked_one    120 分钟
    done_today      完成于 2026-10-28 11:55
    done_yesterday  完成于 2026-10-27 12:00
    null_start      ready（queued 列）
    long_title      todo（title 长于 TITLE_MAX，且不进任何列）
    archived_old    archived（不进任何列）
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from team_board import commands, db, fmt, web
from team_board.model import TITLE_MAX, Task
from tests.fixtures.make_board import FIXTURE_NOW, LONG_TITLE, build_board

DAY = datetime.date(2026, 10, 28)
SNAPSHOT = "2026-10-28 12:00"

CARD_KEYS = ["id", "assignee", "status", "duration_minutes", "title"]

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


def _column_ids(payload: dict) -> dict[str, list[str]]:
    return {
        name: [card["id"] for card in cards]
        for name, cards in payload["columns"].items()
    }


# --- 1. board_payload 分组 -------------------------------------------------


def test_board_payload_columns(board: Board) -> None:
    tasks, _, ids = board
    payload = web.board_payload(tasks, FIXTURE_NOW)
    columns = _column_ids(payload)

    assert list(columns) == ["running", "queued", "blocked", "done"]
    # 列内按耗时降序：stale(75) 在 fresh(10) 之前
    assert columns["running"] == [ids["running_stale"], ids["running_fresh"]]
    assert columns["queued"] == [ids["null_start"]]
    assert columns["blocked"] == [ids["blocked_one"]]
    # done 列同样按耗时降序：done_yesterday 60 分（started→completed）> done_today 25 分
    assert columns["done"] == [ids["done_yesterday"], ids["done_today"]]

    in_columns = {task_id for value in columns.values() for task_id in value}
    for name in ("archived_old", "long_title", "review_stale", "review_fresh"):
        assert ids[name] not in in_columns

    # 每张卡都是 5 键、duration_minutes 是 int
    for cards in payload["columns"].values():
        for card in cards:
            assert list(card) == CARD_KEYS
            assert isinstance(card["duration_minutes"], int)


# --- 2. card_payload 的键顺序、类型、不截断 ---------------------------------


def test_card_payload_keys_and_types(board: Board) -> None:
    tasks, _, ids = board
    long_task = next(task for task in tasks if task.id == ids["long_title"])
    assert len(long_task.title) > TITLE_MAX

    card = web.card_payload(long_task, FIXTURE_NOW)
    assert list(card) == CARD_KEYS
    assert isinstance(card["id"], str)
    assert isinstance(card["assignee"], str)
    assert isinstance(card["status"], str)
    assert isinstance(card["duration_minutes"], int)
    assert isinstance(card["title"], str)
    # title 不截断：原样返回
    assert card["title"] == long_task.title
    assert card["duration_minutes"] == long_task.duration_minutes(FIXTURE_NOW)


def test_card_payload_none_fields_become_empty_string() -> None:
    task = Task(
        id="t_none",
        title=None,  # type: ignore[arg-type]
        assignee=None,  # type: ignore[arg-type]
        status=None,  # type: ignore[arg-type]
        created_at=FIXTURE_NOW - 600,
        started_at=None,
        completed_at=None,
    )
    card = web.card_payload(task, FIXTURE_NOW)
    assert list(card) == CARD_KEYS
    assert card["assignee"] == ""
    assert card["status"] == ""
    assert card["title"] == ""
    assert card["duration_minutes"] == 10


# --- 3. 空 tasks -----------------------------------------------------------


def test_empty_tasks_all_columns_empty() -> None:
    payload = web.board_payload([], FIXTURE_NOW)
    assert list(payload["columns"]) == ["running", "queued", "blocked", "done"]
    assert payload["columns"] == {
        "running": [],
        "queued": [],
        "blocked": [],
        "done": [],
    }


# --- 4. summary_payload 与 render_summary 同口径 ---------------------------


def _text_counts(text: str) -> tuple[int, int, int, int]:
    """把 render_summary 的 完成/在跑/阻塞/异常 四行解析成整数。"""
    lines = text.splitlines()
    assert len(lines) >= 5
    completed, running, blocked, anomaly = (
        int(lines[i].split(": ")[1]) for i in range(1, 5)
    )
    return completed, running, blocked, anomaly


def test_summary_payload_matches_render_summary(board: Board) -> None:
    tasks, rework, ids = board
    text = commands.render_summary(tasks, rework, FIXTURE_NOW, DAY)
    payload = web.summary_payload(tasks, rework, FIXTURE_NOW, DAY)

    assert list(payload) == [
        "snapshot",
        "date",
        "completed",
        "running",
        "blocked",
        "anomaly",
        "anomalies",
    ]

    completed, running, blocked, anomaly = _text_counts(text)
    assert (payload["completed"], payload["running"], payload["blocked"],
            payload["anomaly"]) == (completed, running, blocked, anomaly)
    assert len(payload["anomalies"]) == anomaly

    # fixture 下的实际数字（与 tests/unit/test_summary_render.py 的快照一致：
    # running_stale 75>60 与 review_stale 45>30 两条异常，故 anomaly=2）
    assert (payload["completed"], payload["running"], payload["blocked"]) == (1, 2, 1)
    assert payload["anomaly"] == 2

    # snapshot / date 与文本首行同口径
    first = text.splitlines()[0]
    assert payload["snapshot"] == SNAPSHOT
    assert f"快照 {payload['snapshot']}" in first
    assert f"完成按 {payload['date']}" in first
    assert payload["date"] == DAY.isoformat()

    # 明细顺序 = stats 顺序（分钟降序），标题按 TITLE_MAX 截断
    assert [item["id"] for item in payload["anomalies"]] == [
        ids["running_stale"],
        ids["review_stale"],
    ]
    by_id = {task.id: task for task in tasks}
    for item in payload["anomalies"]:
        task = by_id[item["id"]]
        assert list(item) == ["status", "minutes", "threshold", "id", "title"]
        assert item["status"] == task.status
        assert item["minutes"] == task.duration_minutes(FIXTURE_NOW)
        assert item["title"] == fmt.truncate(task.title or "", TITLE_MAX)
        assert len(text.splitlines()) == 5 + payload["anomaly"]


def test_summary_payload_numbers_come_from_summary_stats(board: Board) -> None:
    """payload 的四个数字必须与 commands.summary_stats 完全一致（同一份计算）。"""
    tasks, rework, _ = board
    stats = commands.summary_stats(tasks, FIXTURE_NOW, DAY)
    payload = web.summary_payload(tasks, rework, FIXTURE_NOW, DAY)
    assert payload["snapshot"] == datetime.datetime.fromtimestamp(
        stats.snapshot_ts
    ).strftime("%Y-%m-%d %H:%M")
    assert payload["date"] == stats.day.isoformat()
    assert payload["completed"] == stats.completed
    assert payload["running"] == stats.running
    assert payload["blocked"] == stats.blocked
    assert payload["anomaly"] == len(stats.anomalies)


def test_summary_payload_empty_board(tmp_path: Path) -> None:
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
    payload = web.summary_payload(tasks, rework, FIXTURE_NOW, DAY)
    assert payload["completed"] == 0
    assert payload["running"] == 0
    assert payload["blocked"] == 0
    assert payload["anomaly"] == 0
    assert payload["anomalies"] == []


# --- 5. index_html --------------------------------------------------------


def test_index_html_is_self_contained() -> None:
    html = web.index_html()
    assert html.startswith("<!doctype html>")
    for name in ("running", "queued", "blocked", "done"):
        assert f'<h2>{name}</h2>' in html
        assert f'data-col="{name}"' in html
    assert "5000" in html
    assert str(web.REFRESH_MS) == "5000"
    assert '<div id="summary">' in html
    assert '<label><input type="checkbox" id="autorefresh" checked> 自动刷新</label>' in html
    for name in ("running", "queued", "blocked", "done"):
        assert f'<section class="col" data-col="{name}">' in html
    assert "/api/summary" in html
    assert "/api/board" in html
    # 零外部引用
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src=" not in html
    assert '<link rel="stylesheet"' not in html
    # 数据渲染只走 textContent / replaceChildren，不拼 innerHTML
    assert "innerHTML" not in html
    assert "<style>" in html and "<script>" in html
    assert "clearInterval" in html and "setInterval" in html
