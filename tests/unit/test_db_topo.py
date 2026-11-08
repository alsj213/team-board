"""``db.load_topo_cards`` 的单元测试：列映射、NULL 归空串、缺列喊错、顺序、只读。

判据见 E1-16：NULL ``assignee`` / ``created_by`` 读成 ``""``；缺 ``created_by`` 列的库
（``tests/fixtures/make_board.py`` 生成的 7 列库）抛 ``db.BoardError`` 且文案含
「读取 tasks.created_by 失败」。真实库的形状按契约 §七 建（8 列，末列 ``created_by``）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from team_board import db
from team_board.topology import TopoCard, topology_payload
from tests.fixtures.make_board import FIXTURE_NOW, build_board

_SCHEMA = (
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,"
    " created_at INTEGER, started_at INTEGER, completed_at INTEGER, created_by TEXT)"
)


def _make_topo_db(path: Path, rows: list[tuple[object, ...]]) -> Path:
    """建 8 列（含 ``created_by``）的最小库，按给定行灌数。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_SCHEMA)
        conn.executemany(
            "INSERT INTO tasks (id, title, assignee, status, created_at,"
            " started_at, completed_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def topo_board(tmp_path: Path) -> Path:
    return _make_topo_db(
        tmp_path / "topo.db",
        [
            ("t_a", "user 拆的卡", "pm", "running", FIXTURE_NOW - 300, None, None, "user"),
            ("t_b", "pm 派的卡", "engineer", "review", FIXTURE_NOW - 200, None, None, "pm"),
            ("t_c", "自环卡", "pm", "done", FIXTURE_NOW - 100, None, None, "pm"),
            ("t_d", "NULL 派发者", "qa", "todo", FIXTURE_NOW - 50, None, None, None),
            ("t_e", "NULL 接卡人", None, "archived", FIXTURE_NOW, None, None, "chief"),
        ],
    )


def test_load_topo_cards_maps_columns_and_null_to_empty_string(topo_board: Path) -> None:
    """5 个字段一一映射；NULL 的 ``assignee`` / ``created_by`` 读成空串。"""
    conn = db.connect(topo_board)
    try:
        cards = db.load_topo_cards(conn)
    finally:
        conn.close()

    assert all(isinstance(card, TopoCard) for card in cards)
    by_id = {card.id: card for card in cards}
    assert set(by_id) == {"t_a", "t_b", "t_c", "t_d", "t_e"}

    assert by_id["t_a"] == TopoCard(
        id="t_a",
        title="user 拆的卡",
        assignee="pm",
        status="running",
        created_by="user",
    )
    # NULL → ""（不是 None，也不抛）
    assert by_id["t_d"].created_by == ""
    assert by_id["t_e"].assignee == ""
    assert by_id["t_e"].created_by == "chief"
    # archived 卡也载入
    assert by_id["t_e"].status == "archived"


def test_load_topo_cards_ordered_by_created_at_then_id(topo_board: Path) -> None:
    """按 ``(created_at, id)`` 升序：``created_at`` 与 id 的次序都被覆盖。"""
    conn = db.connect(topo_board)
    try:
        cards = db.load_topo_cards(conn)
    finally:
        conn.close()

    assert [card.id for card in cards] == ["t_a", "t_b", "t_c", "t_d", "t_e"]


def test_load_topo_cards_missing_created_by_column_raises_board_error(tmp_path: Path) -> None:
    """make_board 生成的库没有 ``created_by`` 列 → ``BoardError``，文案逐字含读取失败串。"""
    board = tmp_path / "kanban.db"
    build_board(board, now=FIXTURE_NOW)

    conn = db.connect(board)
    try:
        with pytest.raises(db.BoardError) as excinfo:
            db.load_topo_cards(conn)
    finally:
        conn.close()

    message = str(excinfo.value)
    assert "读取 tasks.created_by 失败" in message
    assert "no such column" in message


def test_load_topo_cards_feeds_payload_end_to_end(topo_board: Path) -> None:
    """数据层 + 聚合层串起来：节点集是 assignee ∪ created_by，边按派发关系去重。"""
    conn = db.connect(topo_board)
    try:
        payload = topology_payload(db.load_topo_cards(conn))
    finally:
        conn.close()

    names = [node["name"] for node in payload["nodes"]]
    assert set(names) == {"user", "pm", "engineer", "qa", "chief"}
    assert {"from": "user", "to": "pm"} in payload["edges"]
    assert {"from": "pm", "to": "engineer"} in payload["edges"]
    by_name = {node["name"]: node for node in payload["nodes"]}
    # t_a 是 pm 名下的 running 卡 → active；t_b 只是 review → 不亮
    assert by_name["pm"]["active"] is True
    assert by_name["engineer"]["active"] is False
    assert set(by_name["pm"]) == {"name", "display", "level", "active"}
    assert all("cards" not in edge for edge in payload["edges"])


def test_load_topo_cards_is_readonly(topo_board: Path) -> None:
    """连接仍是 ``mode=ro``：聚合层不写库。"""
    conn = db.connect(topo_board)
    try:
        db.load_topo_cards(conn)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at)"
                " VALUES ('t_new', 'x', 'pm', 'todo', 1)"
            )
    finally:
        conn.close()
