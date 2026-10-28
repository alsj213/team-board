import dataclasses

import pytest

from team_board.model import (
    ARCHIVED,
    DEFAULT_DB,
    DOING_STALE_MINUTES,
    REVIEW_STALE_MINUTES,
    STATUS_ORDER,
    TITLE_MAX,
    Task,
)

NOW = 1793160000


def make(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "t_x",
        "title": "卡",
        "assignee": "pm",
        "status": "running",
        "created_at": NOW - 100 * 60,
        "started_at": NOW - 90 * 60,
        "completed_at": None,
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def test_done_card_uses_completed_at_not_now() -> None:
    task = make(status="done", completed_at=NOW - 30 * 60)
    # 开始 now-90min，结束 now-30min -> 60 分钟，与 now 无关
    assert task.duration_minutes(NOW) == 60
    assert task.duration_minutes(NOW + 10_000) == 60


def test_running_card_counts_to_now() -> None:
    task = make(status="running", started_at=NOW - 75 * 60, completed_at=None)
    assert task.duration_minutes(NOW) == 75


def test_missing_started_at_falls_back_to_created_at() -> None:
    task = make(status="ready", created_at=NOW - 90 * 60, started_at=None)
    assert task.start_ts() == NOW - 90 * 60
    assert task.duration_minutes(NOW) == 90


def test_started_at_wins_over_created_at() -> None:
    task = make(created_at=NOW - 100 * 60, started_at=NOW - 40 * 60)
    assert task.start_ts() == NOW - 40 * 60
    assert task.duration_minutes(NOW) == 40


def test_dirty_completed_before_started_clamps_to_zero() -> None:
    task = make(started_at=NOW - 10 * 60, completed_at=NOW - 30 * 60)
    assert task.duration_minutes(NOW) == 0


def test_duration_floors_partial_minutes() -> None:
    # 90 秒 -> 1 分钟
    task = make(started_at=NOW - 95, completed_at=None)
    assert task.duration_minutes(NOW) == 1
    task2 = make(started_at=NOW - 59, completed_at=None)
    assert task2.duration_minutes(NOW) == 0


def test_task_is_frozen() -> None:
    task = make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.status = "done"  # type: ignore[misc]


def test_constants() -> None:
    assert ARCHIVED == "archived"
    assert ARCHIVED not in STATUS_ORDER
    assert STATUS_ORDER == (
        "triage",
        "todo",
        "scheduled",
        "ready",
        "running",
        "blocked",
        "review",
        "done",
    )
    assert DEFAULT_DB == "/home/liu/.hermes/kanban/boards/devteam/kanban.db"
    assert (DOING_STALE_MINUTES, REVIEW_STALE_MINUTES, TITLE_MAX) == (60, 30, 40)
