"""health 模块的单元测试：固定 fixture 逐项核对 runs 分钟 / rework / ready 年龄。

fixture 时间是常量（``NOW``），不读真实时钟；边界写在数字里：

- ``t_a1`` 窗口内已结束的 run（1800 秒）；
- ``t_a2`` 窗口内仍在跑的 run（600 秒，算到 ``NOW``）；
- ``t_a3`` 另一个 profile；
- ``t_a4`` 起点在窗口外（``CUTOFF - 1``），再「结束于 NOW 附近」也不算 —— 窗口
  只看 ``started_at``；
- ``t_a5`` / ``t_a6`` 各 30 秒：**先求和再整除 60** 才是 60 秒 = 1 分钟，
  逐 run 取整会得 0；
- ``t_a7`` profile 为 NULL（归成空串，不丢 run）；
- ``t_a8`` 脏数据（``ended_at < started_at``）夹 0，但次数照计；
- ``t_a9`` 3 天前（默认窗口内、``--since-days 1`` 外）；
- ``t_a10`` 起点**恰好等于** ``CUTOFF``（左闭，计入）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from team_board import db, health

#: 固定基准时间：2023-11-14T22:13:20Z
NOW = 1_700_000_000

WEEK = 7 * 86400

#: 快照串按**本机时区**渲染（与 ``health.render`` 同源），不在测试里假设 UTC
STAMP = datetime.fromtimestamp(NOW).strftime("%Y-%m-%d %H:%M")

#: 默认 7 天窗口的左边界
CUTOFF = NOW - WEEK

#: ``(task_id, profile, status, started_at, ended_at)``
RUN_ROWS: tuple[tuple[str, str | None, str, int, int | None], ...] = (
    ("t_a1", "engineer", "done", NOW - 3600, NOW - 1800),
    ("t_a2", "engineer", "running", NOW - 600, None),
    ("t_a3", "qa", "done", NOW - 120, NOW - 60),
    ("t_a4", "engineer", "done", CUTOFF - 1, NOW - 10),
    ("t_a5", "pm", "done", NOW - 30, NOW),
    ("t_a6", "pm", "done", NOW - 40, NOW - 10),
    ("t_a7", None, "done", NOW - 120, NOW - 60),
    ("t_a8", "engineer", "done", NOW - 400, NOW - 500),
    ("t_a9", "engineer", "done", NOW - 3 * 86400, NOW - 3 * 86400 + 600),
    ("t_a10", "qa", "done", CUTOFF, CUTOFF + 60),
)

#: ``(task_id, kind, created_at)``
EVENT_ROWS: tuple[tuple[str, str, int], ...] = (
    ("t_b1", "changes_requested", NOW - 100),
    ("t_b1", "changes_requested", NOW - 200),
    ("t_b2", "changes_requested", NOW - 300),
    ("t_b3", "changes_requested", CUTOFF - 1),
    ("t_b1", "completed", NOW - 50),
)

#: ``(id, status, created_at, started_at)``
TASK_ROWS: tuple[tuple[str, str, int, int | None], ...] = (
    ("t_c1", "ready", NOW - 7200, None),
    ("t_c2", "ready", NOW - 60, None),
    # started_at 优先于 created_at：年龄按 started_at 算（180 分钟，最老）
    ("t_c3", "ready", NOW - 40000, NOW - 10800),
    ("t_c4", "running", NOW - 100000, NOW - 90000),
    ("t_c5", "done", NOW - 200000, NOW - 190000),
)


def build_health_db(
    path: Path,
    *,
    runs: tuple[tuple[str, str | None, str, int, int | None], ...] = RUN_ROWS,
    events: tuple[tuple[str, str, int], ...] = EVENT_ROWS,
    tasks: tuple[tuple[str, str, int, int | None], ...] = TASK_ROWS,
) -> Path:
    """建三张表（列取真实库的最小集合）并灌入 fixture，返回路径。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " created_at INTEGER NOT NULL, started_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
            " profile TEXT, status TEXT NOT NULL,"
            " started_at INTEGER NOT NULL, ended_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
            " kind TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO tasks (id, status, created_at, started_at) VALUES (?, ?, ?, ?)",
            tasks,
        )
        conn.executemany(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at)"
            " VALUES (?, ?, ?, ?, ?)",
            runs,
        )
        conn.executemany(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, ?)",
            events,
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    return build_health_db(tmp_path / "health.db")


def build(path: Path, *, now: int = NOW, window_days: int = 7) -> health.HealthReport:
    conn = db.connect(path)
    try:
        return health.build(conn, now=now, window_days=window_days)
    finally:
        conn.close()


def test_fixture_report_is_exact(board: Path) -> None:
    """默认 7 天窗口：四个 profile、返工 2 卡 3 次、ready 3 张最老 t_c3。"""
    assert build(board) == health.HealthReport(
        window_days=7,
        runs=(
            ("", health.RunStat(count=1, minutes=1)),
            ("engineer", health.RunStat(count=4, minutes=50)),
            ("pm", health.RunStat(count=2, minutes=1)),
            ("qa", health.RunStat(count=2, minutes=2)),
        ),
        rework=health.ReworkStat(cards=2, events=3),
        ready=health.ReadyStat(count=3, oldest_card="t_c3", oldest_minutes=180),
        generated_at=NOW,
    )


def test_window_is_left_closed_on_started_at(board: Path) -> None:
    """``started_at == CUTOFF`` 计入；早一秒的 ``t_a4`` 不计（同一 profile 次数差 1）。"""
    report = build(board)
    engineer = dict(report.runs)["engineer"]
    # fixture 里 engineer 窗口内是 a1 / a2 / a8 / a9；a4 早于窗口，不进来
    assert engineer.count == 4
    qa = dict(report.runs)["qa"]
    assert qa == health.RunStat(count=2, minutes=2)


def test_one_day_window_drops_older_runs_and_events(board: Path) -> None:
    """``--since-days 1``：3 天前的 run 与 CUTOFF 上的 run / 返工一起出窗。"""
    report = build(board, window_days=1)
    assert dict(report.runs) == {
        "": health.RunStat(count=1, minutes=1),
        "engineer": health.RunStat(count=3, minutes=40),
        "pm": health.RunStat(count=2, minutes=1),
        "qa": health.RunStat(count=1, minutes=1),
    }
    assert report.rework == health.ReworkStat(cards=2, events=3)
    # ready 与窗口无关：永远描述 now 那一刻
    assert report.ready == health.ReadyStat(count=3, oldest_card="t_c3", oldest_minutes=180)


def test_runs_minutes_sum_seconds_before_flooring(board: Path) -> None:
    """pm 两次 30 秒 = 1 分钟；逐 run 取整会算成 0。"""
    assert dict(build(board).runs)["pm"] == health.RunStat(count=2, minutes=1)


def test_empty_board_is_all_zero(tmp_path: Path) -> None:
    """空库：所有计数 0，ready 的最老是 None；文本仍以结论行收尾。"""
    path = build_health_db(tmp_path / "empty.db", runs=(), events=(), tasks=())
    report = build(path)
    assert report.runs == ()
    assert report.rework == health.ReworkStat(cards=0, events=0)
    assert report.ready == health.ReadyStat(
        count=0, oldest_card=None, oldest_minutes=None
    )
    assert health.render(report).splitlines() == [
        f"看板健康（窗口 7 天；快照 {STAMP}）",
        "runs: 0 次 / 0 分钟",
        "rework: 0 张卡 / 0 次",
        "ready: 0 张（最老 —）",
        "结论: 正常（无 ready 积压，窗口内无返工）",
    ]
    assert health.render_json(report) == (
        '{"window_days": 7, "runs": {}, "rework": {"cards": 0, "events": 0},'
        ' "ready": {"count": 0, "oldest_card": null, "oldest_minutes": null},'
        ' "generated_at": 1700000000}'
    )


def test_render_text_fixture(board: Path) -> None:
    """文本：首行标题、runs 总计 + profile 明细、结论在最后一行。"""
    lines = health.render(build(board)).splitlines()
    assert lines[0] == f"看板健康（窗口 7 天；快照 {STAMP}）"
    assert lines[1] == "runs: 9 次 / 54 分钟"
    assert lines[2:6] == [
        "  （无 profile）: 1 次 / 1 分钟",
        "  engineer: 4 次 / 50 分钟",
        "  pm: 2 次 / 1 分钟",
        "  qa: 2 次 / 2 分钟",
    ]
    assert lines[6] == "rework: 2 张卡 / 3 次"
    assert lines[7] == "ready: 3 张（最老 t_c3，180 分钟）"
    assert lines[-1] == "结论: 关注（窗口内返工 3 次）"


def test_json_field_order_is_fixed(board: Path) -> None:
    """JSON 键顺序 = 卡片契约里的顺序（下游按行读也不受影响，但顺序锁住）。"""
    assert list(json.loads(health.render_json(build(board)))) == [
        "window_days",
        "runs",
        "rework",
        "ready",
        "generated_at",
    ]


def test_ready_backlog_and_rework_in_conclusion(tmp_path: Path) -> None:
    """最老 ready 超过 24 小时 + 有返工 → 两个原因都写进结论行。"""
    path = build_health_db(
        tmp_path / "backlog.db",
        runs=(),
        events=(("t_b1", "changes_requested", NOW - 60),),
        tasks=(("t_d1", "ready", NOW - health.READY_BACKLOG_MINUTES * 60 - 60, None),),
    )
    report = build(path)
    assert report.ready == health.ReadyStat(
        count=1,
        oldest_card="t_d1",
        oldest_minutes=health.READY_BACKLOG_MINUTES + 1,
    )
    assert health.render(report).splitlines()[-1] == (
        "结论: 关注（ready 积压 1 张，最老 1441 分钟；窗口内返工 1 次）"
    )


def test_missing_table_is_an_error(tmp_path: Path) -> None:
    """缺 ``task_runs`` → HealthError（不能把数字算成 0）。"""
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, created_at INTEGER)")
        conn.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT, created_at INTEGER)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(health.HealthError) as excinfo:
        build(path)
    assert str(excinfo.value) == "缺少表: task_runs"


def test_non_positive_window_is_rejected(board: Path) -> None:
    conn = db.connect(board)
    try:
        with pytest.raises(health.HealthError):
            health.build(conn, now=NOW, window_days=0)
    finally:
        conn.close()


def test_build_does_not_modify_the_file(board: Path) -> None:
    """只读实测：算完报告后库文件字节不变（mode=ro 之外再要一条数据面证据）。"""
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    build(board)
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before


def test_by_card_groups_runs_and_rework(board: Path) -> None:
    """按卡明细：分钟降序、同分钟按次数、再按卡号；只含窗口内的卡。"""
    conn = db.connect(board)
    try:
        report = health.build(conn, now=NOW, window_days=7, by_card=True)
    finally:
        conn.close()
    assert report.cards is not None
    by_card = {stat.card: stat for stat in report.cards}
    assert by_card["t_a1"] == health.CardStat(
        card="t_a1", runs=1, minutes=30, rework=0, profiles=("engineer",)
    )
    assert by_card["t_b1"] == health.CardStat(
        card="t_b1", runs=0, minutes=0, rework=2, profiles=()
    )
    assert by_card["t_a7"] == health.CardStat(
        card="t_a7", runs=1, minutes=1, rework=0, profiles=("",)
    )
    assert "t_a4" not in by_card  # 窗口外
    assert "t_b3" not in by_card  # 返工在窗口外
    assert [stat.card for stat in report.cards[:3]] == ["t_a1", "t_a2", "t_a9"]


def test_by_card_is_off_by_default(board: Path) -> None:
    """默认不计算 cards：既有相等断言和 JSON 键都不变。"""
    report = build(board)
    assert report.cards is None
    assert "cards" not in json.loads(health.render_json(report))


def test_by_card_text_and_json(board: Path) -> None:
    conn = db.connect(board)
    try:
        report = health.build(conn, now=NOW, window_days=7, by_card=True)
    finally:
        conn.close()
    lines = health.render(report).splitlines()
    assert lines[6].startswith("cards: 11 张有成本/返工")
    assert lines[7] == "  t_a1: 1 次 / 30 分钟 / 返工 0 次（engineer）"
    assert "  t_b1: 0 次 / 0 分钟 / 返工 2 次（—）" in lines
    payload = json.loads(health.render_json(report))
    assert payload["cards"][0] == {
        "card": "t_a1",
        "runs": 1,
        "minutes": 30,
        "rework": 0,
        "profiles": ["engineer"],
    }
    assert list(payload)[-1] == "generated_at"


def test_by_card_empty_board(tmp_path: Path) -> None:
    path = build_health_db(tmp_path / "empty.db", runs=(), events=(), tasks=())
    conn = db.connect(path)
    try:
        report = health.build(conn, now=NOW, window_days=7, by_card=True)
    finally:
        conn.close()
    assert report.cards == ()
    lines = health.render(report).splitlines()
    assert "cards: 0 张有成本/返工（按分钟降序）" in lines
    assert json.loads(health.render_json(report))["cards"] == []
