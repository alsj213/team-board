"""看板健康指标：profile run 成本 / 返工 / ready 年龄（只读）。

数据源三张表：``tasks`` / ``task_runs`` / ``task_events``。连接由调用方用
:func:`team_board.db.connect`（URI ``mode=ro``）打开，本模块只发 SELECT ——
不建表、不写入、不 PRAGMA 落盘。

口径（全部围绕调用方给的 ``now`` 一个时间点，测试因此可复现）：

- **窗口**：``now - window_days * 86400``（左闭）。``window_days`` 默认
  :data:`DEFAULT_WINDOW_DAYS`（7 天）。窗口只影响 ``runs`` 与 ``rework``；
  ``ready`` 永远描述 ``now`` 那一刻的快照，与窗口无关。
- **runs.count**：窗口内 ``task_runs.started_at >= 窗口起点`` 的 run 行数。
- **runs.minutes**：同一 profile 的 run 秒数**先求和，再整除 60**（向下取整）。
  未结束的 run（``ended_at IS NULL``）算到 ``now``；``ended_at < started_at``
  的脏数据夹到 0。逐 run 取整再求和会系统性少算，本模块不做。
- **runs 的 profile**：NULL / 空串一律归成空串 ``""``（不丢 run，渲染层负责
  把它显示成「（无 profile）」）。
- **rework**：``task_events`` 里 ``kind = 'changes_requested'`` 且
  ``created_at >= 窗口起点`` 的事件。``events`` 是事件行数，``cards`` 是其中
  出现的不同 ``task_id`` 个数。
- **ready**：``tasks.status = 'ready'`` 的卡。年龄 =
  ``max(0, (now - COALESCE(started_at, created_at)) // 60)``；``oldest_*`` 取
  年龄最大的那张，年龄相同按 id 升序取第一张。没有 ready 卡时
  ``oldest_card`` / ``oldest_minutes`` 都是 ``None``（JSON 里是 ``null``）。

三种失败（缺表 / 窗口非法 / 读不出来）一律抛 :class:`HealthError` 或
:class:`team_board.db.BoardError`，由 CLI 渲染成 stderr 一行 ``错误: …`` + rc2。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "READY_BACKLOG_MINUTES",
    "REQUIRED_TABLES",
    "HealthError",
    "HealthReport",
    "ReadyStat",
    "ReworkStat",
    "RunStat",
    "build",
    "render",
    "render_json",
]

#: ``--since-days`` 的默认窗口
DEFAULT_WINDOW_DAYS = 7

#: ready 积压阈值：最老 ready 卡年龄**严格大于**它才算积压（结论行用）
READY_BACKLOG_MINUTES = 24 * 60

#: 本命令依赖的表；缺任一张即 rc2（不猜、不降级成 0）
REQUIRED_TABLES = ("task_events", "task_runs", "tasks")

#: 文本里「看不出 profile」的占位
NO_PROFILE = "（无 profile）"


class HealthError(Exception):
    """健康指标算不出来：缺表 / 窗口非法 / SQL 执行失败。"""


@dataclass(frozen=True)
class RunStat:
    """一个 profile 在窗口内的 run 成本。"""

    count: int
    minutes: int


@dataclass(frozen=True)
class ReworkStat:
    """窗口内的返工：事件行数与涉及的不同卡数。"""

    cards: int
    events: int


@dataclass(frozen=True)
class ReadyStat:
    """``now`` 那一刻的 ready 快照：张数 + 最老那张。"""

    count: int
    oldest_card: str | None
    oldest_minutes: int | None


@dataclass(frozen=True)
class HealthReport:
    """一次 ``health`` 的全部数字；``runs`` 按 profile 升序、键唯一。"""

    window_days: int
    runs: tuple[tuple[str, RunStat], ...]
    rework: ReworkStat
    ready: ReadyStat
    generated_at: int


def _require_tables(conn: sqlite3.Connection) -> None:
    """缺表就停：真实库里没有这几张表时不能把数字算成 0。"""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise HealthError(f"读取 sqlite_master 失败: {exc}") from exc
    present = {str(row[0]) for row in rows}
    missing = [name for name in REQUIRED_TABLES if name not in present]
    if missing:
        raise HealthError(f"缺少表: {', '.join(missing)}")


def _load_runs(
    conn: sqlite3.Connection, *, cutoff: int, now: int
) -> tuple[tuple[str, RunStat], ...]:
    """窗口内 run 按 profile 汇总：(profile, count, 秒数) → 先求和再整除 60。"""
    sql = (
        "SELECT profile, started_at, ended_at FROM task_runs "
        "WHERE started_at >= ?"
    )
    try:
        rows = conn.execute(sql, (cutoff,)).fetchall()
    except sqlite3.Error as exc:
        raise HealthError(f"读取 task_runs 失败: {exc}") from exc

    counts: dict[str, int] = {}
    seconds: dict[str, int] = {}
    for profile, started_at, ended_at in rows:
        key = "" if profile is None else str(profile)
        start = int(started_at)
        end = now if ended_at is None else int(ended_at)
        counts[key] = counts.get(key, 0) + 1
        seconds[key] = seconds.get(key, 0) + max(0, end - start)

    return tuple(
        (profile, RunStat(count=counts[profile], minutes=seconds[profile] // 60))
        for profile in sorted(counts)
    )


def _load_rework(conn: sqlite3.Connection, *, cutoff: int) -> ReworkStat:
    """窗口内 ``changes_requested``：事件行数 + 不同卡数。"""
    sql = (
        "SELECT COUNT(*), COUNT(DISTINCT task_id) FROM task_events "
        "WHERE kind = 'changes_requested' AND created_at >= ?"
    )
    try:
        row = conn.execute(sql, (cutoff,)).fetchone()
    except sqlite3.Error as exc:
        raise HealthError(f"读取 task_events 失败: {exc}") from exc
    events = 0 if row is None or row[0] is None else int(row[0])
    cards = 0 if row is None or row[1] is None else int(row[1])
    return ReworkStat(cards=cards, events=events)


def _load_ready(conn: sqlite3.Connection, *, now: int) -> ReadyStat:
    """``status='ready'`` 的张数与最老那张（年龄大 → id 小者优先）。"""
    sql = (
        "SELECT id, COALESCE(started_at, created_at) FROM tasks "
        "WHERE status = 'ready'"
    )
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise HealthError(f"读取 tasks 失败: {exc}") from exc

    oldest_card: str | None = None
    oldest_minutes: int | None = None
    for card_id, ts in rows:
        if ts is None:
            # 脏数据（started_at 与 created_at 都缺）：仍然计入张数，只是算不出年龄
            continue
        minutes = max(0, (now - int(ts)) // 60)
        card = str(card_id)
        if oldest_minutes is None or minutes > oldest_minutes:
            oldest_card = card
            oldest_minutes = minutes
        elif (
            minutes == oldest_minutes
            and oldest_card is not None
            and card < oldest_card
        ):
            oldest_card = card
    return ReadyStat(count=len(rows), oldest_card=oldest_card, oldest_minutes=oldest_minutes)


def build(
    conn: sqlite3.Connection,
    *,
    now: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> HealthReport:
    """算一次完整报告；只读，不改 ``conn`` 的配置。"""
    if window_days < 1:
        raise HealthError(f"window_days 必须 >= 1: {window_days}")

    _require_tables(conn)
    cutoff = now - window_days * 86400
    return HealthReport(
        window_days=window_days,
        runs=_load_runs(conn, cutoff=cutoff, now=now),
        rework=_load_rework(conn, cutoff=cutoff),
        ready=_load_ready(conn, now=now),
        generated_at=now,
    )


def _conclusion(report: HealthReport) -> str:
    """结论行：ready 积压（最老 > :data:`READY_BACKLOG_MINUTES`）与返工分开判。"""
    backlog = (
        report.ready.oldest_minutes is not None
        and report.ready.oldest_minutes > READY_BACKLOG_MINUTES
    )
    rework = report.rework.events > 0

    if backlog and rework:
        return (
            f"结论: 关注（ready 积压 {report.ready.count} 张，"
            f"最老 {report.ready.oldest_minutes} 分钟；"
            f"窗口内返工 {report.rework.events} 次）"
        )
    if backlog:
        return (
            f"结论: 关注（ready 积压 {report.ready.count} 张，"
            f"最老 {report.ready.oldest_minutes} 分钟）"
        )
    if rework:
        return f"结论: 关注（窗口内返工 {report.rework.events} 次）"
    return "结论: 正常（无 ready 积压，窗口内无返工）"


def render(report: HealthReport) -> str:
    """文本报告：首行标题、末尾一行结论，中间每段一行（profile 明细缩进两格）。

    空库（没有 run / 没有返工 / 没有 ready 卡）时三个计数全是 0，结论行是
    ``结论: 正常（无 ready 积压，窗口内无返工）``。不带尾随换行、不 print。
    """
    stamp = datetime.datetime.fromtimestamp(report.generated_at).strftime(
        "%Y-%m-%d %H:%M"
    )
    total_count = sum(stat.count for _, stat in report.runs)
    total_minutes = sum(stat.minutes for _, stat in report.runs)

    lines = [
        f"看板健康（窗口 {report.window_days} 天；快照 {stamp}）",
        f"runs: {total_count} 次 / {total_minutes} 分钟",
    ]
    for profile, stat in report.runs:
        lines.append(f"  {profile or NO_PROFILE}: {stat.count} 次 / {stat.minutes} 分钟")

    lines.append(f"rework: {report.rework.cards} 张卡 / {report.rework.events} 次")
    if report.ready.oldest_card is None:
        lines.append(f"ready: {report.ready.count} 张（最老 —）")
    else:
        lines.append(
            f"ready: {report.ready.count} 张"
            f"（最老 {report.ready.oldest_card}，{report.ready.oldest_minutes} 分钟）"
        )
    lines.append(_conclusion(report))
    return "\n".join(lines)


def render_json(report: HealthReport) -> str:
    """单行 JSON（无 indent / 无 sort_keys / ``ensure_ascii=False``）。

    键顺序固定：``window_days`` / ``runs`` / ``rework`` / ``ready`` /
    ``generated_at``；``runs`` 内按 profile 升序。
    """
    payload = {
        "window_days": report.window_days,
        "runs": {
            profile: {"count": stat.count, "minutes": stat.minutes}
            for profile, stat in report.runs
        },
        "rework": {"cards": report.rework.cards, "events": report.rework.events},
        "ready": {
            "count": report.ready.count,
            "oldest_card": report.ready.oldest_card,
            "oldest_minutes": report.ready.oldest_minutes,
        },
        "generated_at": report.generated_at,
    }
    return json.dumps(payload, ensure_ascii=False)
