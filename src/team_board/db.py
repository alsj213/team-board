"""board.db 的只读访问层。

一律用 URI ``mode=ro`` 打开，绝不写入、绝不建表。连接由调用方负责 close。
只读 tasks 与 task_events 两张表（不假设 task_runs / task_links 存在）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import bodylimit
from .model import DEFAULT_DB, Task


class BoardError(Exception):
    """看板数据源不可用：找不到 / 打不开 / 不是合法库 / 读取失败。"""


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """只读打开 SQLite 库；失败时抛 BoardError（一句话原因）。"""
    db_path = Path(path)
    if not db_path.exists():
        raise BoardError(f"数据源不存在: {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BoardError(f"打不开数据源 {db_path}: {exc}") from exc

    # 连接本身是惰性的：真读一次才算确认它是个合法库
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise BoardError(f"不是合法的 SQLite 库 {db_path}: {exc}") from exc
    return conn


_TASK_COLUMNS = "id, title, assignee, status, created_at, started_at, completed_at"


def load_tasks(conn: sqlite3.Connection) -> list[Task]:
    """全部卡（含 archived），按 (created_at, id) 升序。

    ``created_at`` 与 ``started_at`` 同时为 NULL 的卡是脏数据：耗时口径
    （``model.Task.start_ts`` 回退链）无从取值，在这里直接抛 BoardError，
    由 ``cli.main`` 统一渲染成 ``错误: …`` + 退出码 1，不让裸 TypeError 泄到
    stderr。只缺 ``started_at`` 的卡仍按旧口径回退到 ``created_at``。
    """
    # 列名来自模块常量 _TASK_COLUMNS（无外部输入，不是注入面）→ bandit B608 假阳性
    sql = f"SELECT {_TASK_COLUMNS} FROM tasks ORDER BY created_at, id"  # nosec B608
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise BoardError(f"读取 tasks 失败: {exc}") from exc

    tasks: list[Task] = []
    for row in rows:
        if row[4] is None and row[5] is None:
            raise BoardError(
                f"任务 {row[0]} 缺少 created_at/started_at，无法计算耗时"
            )
        tasks.append(
            Task(
                id=row[0],
                title=row[1],
                assignee=row[2],
                status=row[3],
                created_at=row[4],
                started_at=row[5],
                completed_at=row[6],
            )
        )
    return tasks


_BODY_COLUMNS = "id, status, assignee, body"

#: ``iter_body_rows`` 的两种口径（``include_done`` 决定用哪一条）
_BODY_SCOPE_PENDING = "status NOT IN ('done', 'archived')"
_BODY_SCOPE_ALL = "status != 'archived'"


def iter_body_rows(
    conn: sqlite3.Connection, *, include_done: bool
) -> list[tuple[str, str, str, int | None]]:
    """``(id, status, assignee, body 的 UTF-8 字节数|None)``，按 id 升序。

    - ``include_done=False``：只要未结卡（``status NOT IN ('done','archived')``）；
    - ``include_done=True``：未归档卡全要（``status != 'archived'``）；
    - body 为 NULL 的行第 4 列是 ``None``（不是 0）：NULL 与「空 body」要分得开；
    - 字节数在 Python 侧用 :func:`bodylimit.body_bytes` 算，SQL 里不做长度运算
      （SQLite 的 ``length()`` 数的是字符，不是 UTF-8 字节）；
    - ``assignee`` / ``status`` 的 NULL 归成空串，渲染层不必再判空。
    """
    where = _BODY_SCOPE_ALL if include_done else _BODY_SCOPE_PENDING
    # where 来自模块常量（无外部输入，不是注入面）→ bandit B608 假阳性
    sql = f"SELECT {_BODY_COLUMNS} FROM tasks WHERE {where} ORDER BY id"  # nosec B608
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise BoardError(f"读取 tasks.body 失败: {exc}") from exc

    return [
        (
            str(row[0]),
            "" if row[1] is None else str(row[1]),
            "" if row[2] is None else str(row[2]),
            None if row[3] is None else bodylimit.body_bytes(str(row[3])),
        )
        for row in rows
    ]


#: ``iter_output_rows`` 的两段 SQL：评论（``task_comments.body``）与结果（``tasks.result``）
_COMMENT_COLUMNS = "c.task_id, c.id, c.body"
_RESULT_COLUMNS = "id, result"


def iter_output_rows(
    conn: sqlite3.Connection, *, include_done: bool
) -> list[tuple[str, str, str, int]]:
    """``(task_id, source, row_id, 字节数)``，按 ``(task_id, source, row_id)`` 升序。

    - ``source``：``"comment"``（``task_comments.body``）或 ``"result"``（``tasks.result``）；
    - ``row_id``：评论取主键的十进制字符串，result 固定 ``"-"``（一张卡只有一个 result）；
    - 口径与 :func:`iter_body_rows` 一致：``include_done=False`` → 未结卡
      （``status NOT IN ('done','archived')``）；``True`` → 未归档卡
      （``status != 'archived'``）。result 段额外要求 ``result IS NOT NULL``
      （没有结果 = 没有可稽核的内容，不记 0 字节）；
    - 字节数在 Python 侧用 :func:`bodylimit.body_bytes` 算（SQLite 的 ``length()``
      数的是字符，不是 UTF-8 字节），NULL / 空串都记 0；
    - 评论行按 ``(task_id, source, row_id)`` 元组升序返回，``row_id`` 是十进制字符串，
      排序因此是字典序 —— 口径写在契约里，不在这里改写。
    """
    scope = _BODY_SCOPE_ALL if include_done else _BODY_SCOPE_PENDING
    # scope 来自模块常量（无外部输入，不是注入面）→ bandit B608 假阳性
    comment_sql = (
        f"SELECT {_COMMENT_COLUMNS} FROM task_comments c "  # nosec B608
        f"JOIN tasks t ON t.id = c.task_id WHERE t.{scope}"
    )
    result_sql = (
        f"SELECT {_RESULT_COLUMNS} FROM tasks "  # nosec B608
        f"WHERE {scope} AND result IS NOT NULL"
    )

    rows: list[tuple[str, str, str, int]] = []
    try:
        for task_id, comment_id, body in conn.execute(comment_sql).fetchall():
            rows.append(
                (
                    str(task_id),
                    "comment",
                    str(comment_id),
                    bodylimit.body_bytes(None if body is None else str(body)),
                )
            )
        for task_id, result in conn.execute(result_sql).fetchall():
            rows.append(
                (
                    str(task_id),
                    "result",
                    "-",
                    bodylimit.body_bytes(None if result is None else str(result)),
                )
            )
    except sqlite3.Error as exc:
        raise BoardError(f"读取 task_comments 失败: {exc}") from exc
    return sorted(rows)


def iter_claim_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """``(id, status, assignee, body)`` 的未结卡，按 id 升序。

    口径固定为 :data:`_BODY_SCOPE_PENDING`（``status NOT IN ('done','archived')``）：
    ``pr-claim-audit`` 只看未结卡，已结卡一律不参与认领唯一性判定。
    ``body`` 为 NULL 时归成空串（没有正文 = 没有认领行），``status`` / ``assignee``
    的 NULL 同样归成空串，渲染层不必再判空。
    """
    # where 来自模块常量（无外部输入，不是注入面）→ bandit B608 假阳性
    sql = f"SELECT {_BODY_COLUMNS} FROM tasks WHERE {_BODY_SCOPE_PENDING} ORDER BY id"  # nosec B608
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise BoardError(f"读取 tasks.body 失败: {exc}") from exc

    return [
        (
            str(row[0]),
            "" if row[1] is None else str(row[1]),
            "" if row[2] is None else str(row[2]),
            "" if row[3] is None else str(row[3]),
        )
        for row in rows
    ]


def load_rework_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """task_id -> 'changes_requested' 事件数；0 次的不进字典。"""
    sql = (
        "SELECT task_id, COUNT(*) FROM task_events "
        "WHERE kind = 'changes_requested' GROUP BY task_id"
    )
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise BoardError(f"读取 task_events 失败: {exc}") from exc
    return {row[0]: int(row[1]) for row in rows if row[1]}
