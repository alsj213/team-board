import sqlite3
from pathlib import Path

import pytest

from team_board import bodylimit
from team_board.db import (
    BoardError,
    connect,
    iter_body_rows,
    iter_body_text_rows,
    load_rework_counts,
    load_tasks,
)
from tests.fixtures.make_board import FIXTURE_NOW, build_board


@pytest.fixture()
def board(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    db = tmp_path / "kanban.db"
    ids = build_board(db, now=FIXTURE_NOW)
    return db, ids


def test_connect_is_readonly(board: tuple[Path, dict[str, str]]) -> None:
    db, _ = board
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError) as exc:
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at)"
                " VALUES ('t_new', 'x', 'pm', 'todo', 1)"
            )
        assert "readonly" in str(exc.value).lower()
    finally:
        conn.close()


def test_connect_missing_path_raises_board_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(BoardError):
        connect(missing)
    # 只读打开不该把文件建出来
    assert not missing.exists()


def test_connect_non_sqlite_file_raises_board_error(tmp_path: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_text("这不是一个 SQLite 数据库，只是一段文本。\n", encoding="utf-8")
    with pytest.raises(BoardError):
        connect(junk)


def test_connect_directory_raises_board_error(tmp_path: Path) -> None:
    with pytest.raises(BoardError):
        connect(tmp_path)


def test_load_tasks_includes_archived_and_maps_fields(
    board: tuple[Path, dict[str, str]],
) -> None:
    db, ids = board
    conn = connect(db)
    try:
        tasks = load_tasks(conn)
    finally:
        conn.close()

    assert len(tasks) == 10
    by_id = {t.id: t for t in tasks}
    assert set(by_id) == set(ids.values())
    assert "t_archived_old" in by_id, "archived 也必须载入"

    done_today = by_id[ids["done_today"]]
    assert done_today.title == "done_today"
    assert done_today.assignee == "engineer"
    assert done_today.status == "done"
    assert done_today.created_at == FIXTURE_NOW - 35 * 60
    assert done_today.started_at == FIXTURE_NOW - 30 * 60
    assert done_today.completed_at == FIXTURE_NOW - 5 * 60

    null_start = by_id[ids["null_start"]]
    assert null_start.started_at is None
    assert null_start.completed_at is None


def test_load_tasks_ordered_by_created_at_then_id(
    board: tuple[Path, dict[str, str]],
) -> None:
    db, _ = board
    conn = connect(db)
    try:
        tasks = load_tasks(conn)
    finally:
        conn.close()

    keys = [(t.created_at, t.id) for t in tasks]
    assert keys == sorted(keys)
    assert [t.id for t in tasks]
    assert next(t.id for t in tasks) == "t_done_yesterday"
    assert [t.id for t in tasks][-1] == "t_review_fresh"


def test_load_rework_counts_only_positive(
    board: tuple[Path, dict[str, str]],
) -> None:
    db, ids = board
    conn = connect(db)
    try:
        counts = load_rework_counts(conn)
    finally:
        conn.close()

    assert counts == {ids["done_today"]: 2}
    assert all(value > 0 for value in counts.values())


def test_load_rework_counts_ignores_other_kinds(tmp_path: Path) -> None:
    db = tmp_path / "kanban.db"
    build_board(db, now=FIXTURE_NOW)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (99, 't_blocked_one', 'blocked', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    ro = connect(db)
    try:
        counts = load_rework_counts(ro)
    finally:
        ro.close()
    assert counts == {"t_done_today": 2}


def _make_tasks_db(path: Path, rows: list[tuple[object, ...]]) -> Path:
    """建只含 tasks 表的最小库，按给定行灌数（created_at/started_at 可为 NULL）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT,"
            " status TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO tasks (id, title, assignee, status, created_at,"
            " started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_load_tasks_double_null_timestamps_raises_board_error(tmp_path: Path) -> None:
    """created_at / started_at 双 NULL 是脏数据：读取层喊错，不让耗时口径炸 TypeError。"""
    db = _make_tasks_db(
        tmp_path / "null_ts.db",
        [("t_null_ts", "NULL 时间戳卡", "pm", "running", None, None, None)],
    )
    conn = connect(db)
    try:
        with pytest.raises(BoardError) as excinfo:
            load_tasks(conn)
    finally:
        conn.close()
    assert str(excinfo.value) == "任务 t_null_ts 缺少 created_at/started_at，无法计算耗时"


def test_load_tasks_allows_started_at_null_when_created_at_present(
    tmp_path: Path,
) -> None:
    """只缺 started_at 不算脏数据：仍回退到 created_at 正常算耗时。"""
    now = 1_793_160_000
    db = _make_tasks_db(
        tmp_path / "null_start.db",
        [("t_null_start", "只缺 started_at", "pm", "ready", now - 90 * 60, None, None)],
    )
    conn = connect(db)
    try:
        tasks = load_tasks(conn)
    finally:
        conn.close()

    assert len(tasks) == 1
    assert tasks[0].started_at is None
    assert tasks[0].duration_minutes(now) == 90


def _make_body_db(path: Path, rows: list[tuple[object, ...]]) -> Path:
    """建含 ``body`` 列的最小库（``iter_body_text_rows`` 要读正文原文）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT,"
            " status TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER,"
            " body TEXT)"
        )
        conn.executemany(
            "INSERT INTO tasks (id, title, assignee, status, created_at, body)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _body_db(tmp_path: Path) -> Path:
    return _make_body_db(
        tmp_path / "body.db",
        [
            ("t_a_running", "a", "engineer", "running", 1_793_400_000, "TEMPLATE: card-template.md#v3"),
            ("t_b_done", "b", None, "done", 1_793_400_100, "## 完成契约"),
            ("t_c_archived", "c", "pm", "archived", 1_793_400_200, "正文"),
            ("t_d_null_body", "d", "qa", "ready", None, None),
        ],
    )


def test_iter_body_text_rows_returns_created_at_and_raw_body(tmp_path: Path) -> None:
    conn = connect(_body_db(tmp_path))
    try:
        rows = iter_body_text_rows(conn, False)
    finally:
        conn.close()

    # 默认只取未结卡（archived / done 都不在），按 id 升序
    assert rows == [
        (
            "t_a_running",
            "running",
            "engineer",
            1_793_400_000,
            "TEMPLATE: card-template.md#v3",
        ),
        ("t_d_null_body", "ready", "qa", None, None),
    ]
    # body 是**原文**（不是字节数）：字节口径归 iter_body_rows
    assert rows[0][4] is not None and bodylimit.body_bytes(rows[0][4]) == len(
        "TEMPLATE: card-template.md#v3"
    )


def test_iter_body_text_rows_include_done_skips_archived(tmp_path: Path) -> None:
    conn = connect(_body_db(tmp_path))
    try:
        rows = iter_body_text_rows(conn, True)
    finally:
        conn.close()

    assert [row[0] for row in rows] == ["t_a_running", "t_b_done", "t_d_null_body"]
    # status / assignee 的 NULL 归成空串（与 iter_body_rows 同口径）
    assert rows[1][1:] == ("done", "", 1_793_400_100, "## 完成契约")


def test_iter_body_text_rows_is_readonly(tmp_path: Path) -> None:
    conn = connect(_body_db(tmp_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at)"
                " VALUES ('t_new', 'x', 'pm', 'todo', 1)"
            )
    finally:
        conn.close()


def test_iter_body_rows_signature_and_shape_unchanged(tmp_path: Path) -> None:
    """``iter_body_rows`` 仍是 ``(id, status, assignee, 字节数|None)`` —— 多消费者，不许跟着改。"""
    conn = connect(_body_db(tmp_path))
    try:
        rows = iter_body_rows(conn, include_done=False)
    finally:
        conn.close()

    assert rows == [
        ("t_a_running", "running", "engineer", len("TEMPLATE: card-template.md#v3")),
        ("t_d_null_body", "ready", "qa", None),
    ]

