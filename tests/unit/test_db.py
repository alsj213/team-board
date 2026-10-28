import sqlite3
from pathlib import Path

import pytest

from team_board.db import BoardError, connect, load_rework_counts, load_tasks
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
    assert [t.id for t in tasks][0] == "t_done_yesterday"
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
