"""render_export 的单元测试：JSON 形状、键顺序、类型、值抽样、排序、空板。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from team_board import commands, db
from team_board.model import Task
from tests.fixtures.make_board import FIXTURE_NOW, LONG_TITLE, build_board

# D4：每个对象的键顺序固定为此五元
EXPORT_KEYS = ["id", "assignee", "status", "duration_minutes", "title"]


def _load(path: Path) -> list[Task]:
    conn = db.connect(path)
    try:
        return db.load_tasks(conn)
    finally:
        conn.close()


@pytest.fixture()
def exported(tmp_path: Path) -> tuple[list[dict], str]:
    """fixture 库（10 张卡，9 张未归档）→ (解析后的 payload, 原始字符串)。"""
    path = tmp_path / "b.db"
    build_board(path, now=FIXTURE_NOW)
    text = commands.render_export(_load(path), FIXTURE_NOW)
    return json.loads(text), text


def test_top_level_is_list_of_9(exported: tuple[list[dict], str]) -> None:
    payload, _ = exported
    assert isinstance(payload, list)
    assert len(payload) == 9


def test_keys_order_and_types(exported: tuple[list[dict], str]) -> None:
    payload, _ = exported
    for row in payload:
        assert list(row.keys()) == EXPORT_KEYS
        assert isinstance(row["duration_minutes"], int)
        assert not isinstance(row["duration_minutes"], bool)
        for key in ("id", "assignee", "status", "title"):
            assert isinstance(row[key], str), (key, row[key])


def test_sampled_values(exported: tuple[list[dict], str]) -> None:
    payload, text = exported
    by_id = {row["id"]: row for row in payload}
    assert by_id["t_running_stale"]["duration_minutes"] == 75
    assert by_id["t_done_today"]["duration_minutes"] == 25
    assert by_id["t_null_start"]["duration_minutes"] == 90
    # 机器读：完整标题，不按 TITLE_MAX 截断
    assert by_id["t_long_title"]["title"] == LONG_TITLE
    assert len(LONG_TITLE) > 40
    # archived 不出现在任何地方
    assert "t_archived_old" not in by_id
    assert "t_archived_old" not in text


def test_order_matches_render_list(tmp_path: Path) -> None:
    """同一 (tasks, now)：export 的卡号顺序 == render_list 的数据行顺序。"""
    path = tmp_path / "b.db"
    build_board(path, now=FIXTURE_NOW)
    tasks = _load(path)

    payload = json.loads(commands.render_export(tasks, FIXTURE_NOW))
    lines = commands.render_list(tasks, FIXTURE_NOW).splitlines()
    list_ids = [line.split()[0] for line in lines[2:]]  # 跳过表头 + 分隔行

    assert [row["id"] for row in payload] == list_ids
    assert len(list_ids) == 9


def test_deterministic(tmp_path: Path) -> None:
    """同一 (tasks, now) 连调两次 → 字符串完全相等；且是单行、无尾随换行。"""
    path = tmp_path / "b.db"
    build_board(path, now=FIXTURE_NOW)
    tasks = _load(path)

    once = commands.render_export(tasks, FIXTURE_NOW)
    twice = commands.render_export(tasks, FIXTURE_NOW)
    assert once == twice
    assert "\n" not in once
    assert not once.endswith("\n")


def test_none_fields_become_empty_strings() -> None:
    """脏库 None：assignee/status/title 输出空串（不是列表用的「—」）。"""
    task = Task(
        id="t_dirty",
        title=None,  # type: ignore[arg-type]
        assignee=None,  # type: ignore[arg-type]
        status=None,  # type: ignore[arg-type]
        created_at=FIXTURE_NOW - 60,
        started_at=None,
        completed_at=None,
    )
    payload = json.loads(commands.render_export([task], FIXTURE_NOW))
    assert payload == [
        {
            "id": "t_dirty",
            "assignee": "",
            "status": "",
            "duration_minutes": 1,
            "title": "",
        }
    ]
    assert list(payload[0].keys()) == EXPORT_KEYS


def test_empty_board_is_empty_array(tmp_path: Path) -> None:
    """一张卡都没有 → ``[]``（不是 EMPTY_BOARD 文案）。"""
    path = tmp_path / "empty.db"
    build_board(path, now=FIXTURE_NOW)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM tasks")
        conn.commit()
    finally:
        conn.close()
    assert commands.render_export(_load(path), FIXTURE_NOW) == "[]"


def test_only_archived_board_is_empty_array() -> None:
    task = Task(
        id="t_old",
        title="old",
        assignee="pm",
        status="archived",
        created_at=0,
        started_at=None,
        completed_at=None,
    )
    assert commands.render_export([task], FIXTURE_NOW) == "[]"
