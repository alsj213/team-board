import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from team_board.db import connect, load_rework_counts, load_tasks
from team_board.model import ARCHIVED, TITLE_MAX
from tests.fixtures.make_board import FIXTURE_NOW, build_board

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_NAMES = {
    "archived_old",
    "running_stale",
    "running_fresh",
    "review_stale",
    "review_fresh",
    "blocked_one",
    "done_today",
    "done_yesterday",
    "null_start",
    "long_title",
}


def dump(db: Path) -> list[tuple[str, str, int | None, int | None]]:
    """导出 (id, status, started_at, completed_at)，用于确定性比对。"""
    conn = connect(db)
    try:
        tasks = load_tasks(conn)
    finally:
        conn.close()
    return [(t.id, t.status, t.started_at, t.completed_at) for t in tasks]


def test_fixture_now_is_2026_10_28_noon_cst() -> None:
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(FIXTURE_NOW + 8 * 3600)) == (
        "2026-10-28 12:00:00"
    )


def test_build_board_returns_ten_semantic_ids(tmp_path: Path) -> None:
    ids = build_board(tmp_path / "a.db", now=FIXTURE_NOW)
    assert set(ids) == EXPECTED_NAMES
    assert len(set(ids.values())) == 10
    assert all(value.startswith("t_") for value in ids.values())


def test_build_board_keeps_semantic_id_stable(tmp_path: Path) -> None:
    a = build_board(tmp_path / "a.db", now=FIXTURE_NOW)
    b = build_board(tmp_path / "b.db", now=FIXTURE_NOW)
    assert a == b


def test_build_board_is_deterministic(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    build_board(a, now=FIXTURE_NOW)
    build_board(b, now=FIXTURE_NOW)
    assert dump(a) == dump(b)


def test_build_board_creates_only_two_tables(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    build_board(db, now=FIXTURE_NOW)
    conn = connect(db)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
    finally:
        conn.close()
    assert names == ["task_events", "tasks"]


def test_build_board_refuses_real_board_path() -> None:
    from team_board.model import DEFAULT_DB

    with pytest.raises(ValueError):
        build_board(DEFAULT_DB, now=FIXTURE_NOW)


def test_long_title_exceeds_title_max(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    ids = build_board(db, now=FIXTURE_NOW)
    conn = connect(db)
    try:
        tasks = {t.id: t for t in load_tasks(conn)}
    finally:
        conn.close()
    title = tasks[ids["long_title"]].title
    # 全角字符按 2 列宽算，必然远超 TITLE_MAX
    width = sum(2 if ord(ch) > 0x2E80 else 1 for ch in title)
    assert width >= 60
    assert len(title) > TITLE_MAX


def test_fixture_derived_numbers(tmp_path: Path) -> None:
    """卡上定死的验收数值，fixture 一旦漂了就炸。"""
    db = tmp_path / "a.db"
    ids = build_board(db, now=FIXTURE_NOW)
    conn = connect(db)
    try:
        tasks = load_tasks(conn)
        counts = load_rework_counts(conn)
    finally:
        conn.close()

    assert len([t for t in tasks if t.status != ARCHIVED]) == 9
    assert len([t for t in tasks if t.status == "running"]) == 2
    assert len([t for t in tasks if t.status == "blocked"]) == 1

    # 2026-10-28 当天完成 1 张
    done_days = [
        time.strftime("%Y-%m-%d", time.gmtime(t.completed_at + 8 * 3600))
        for t in tasks
        if t.completed_at is not None
    ]
    assert done_days.count("2026-10-28") == 1
    assert done_days.count("2026-10-27") == 1

    # engineer 交付 1 / 被驳回 2 / 平均耗时 25；pm 交付 1 / 0 / 60
    for assignee, delivered, rework, avg in (
        ("engineer", 1, 2, 25),
        ("pm", 1, 0, 60),
    ):
        done = [t for t in tasks if t.assignee == assignee and t.status == "done"]
        assert len(done) == delivered
        assert sum(counts.get(t.id, 0) for t in done) == rework
        assert sum(t.duration_minutes(FIXTURE_NOW) for t in done) // len(done) == avg

    assert counts == {ids["done_today"]: 2}

    # 异常：running_stale 75 > 60、review_stale 45 > 30
    by_id = {t.id: t for t in tasks}
    assert by_id[ids["running_stale"]].duration_minutes(FIXTURE_NOW) == 75
    assert by_id[ids["review_stale"]].duration_minutes(FIXTURE_NOW) == 45
    assert by_id[ids["running_stale"]].status == "running"
    assert by_id[ids["review_stale"]].status == "review"


def test_main_prints_json(tmp_path: Path) -> None:
    out = tmp_path / "cli.db"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "fixtures" / "make_board.py"),
            "--out",
            str(out),
            "--now",
            str(FIXTURE_NOW),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload == build_board(tmp_path / "direct.db", now=FIXTURE_NOW)
    assert dump(out) == dump(tmp_path / "direct.db")
