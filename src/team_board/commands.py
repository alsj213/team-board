"""子命令的渲染层：只把 Task 列表变成字符串，不 print、不读写文件。

所有函数一律返回不带尾随换行的 str；``render_export`` 返回单行 JSON 文本。
"""

from __future__ import annotations

import datetime
import json

from . import db, fmt
from .model import (
    ARCHIVED,
    DOING_STALE_MINUTES,
    REVIEW_STALE_MINUTES,
    STATUS_ORDER,
    TITLE_MAX,
    Task,
)

# 已知状态的排序rank；不在 STATUS_ORDER 里的脏状态排在最后（同 rank 内仍按耗时/id 排）
_STATUS_RANK = {status: idx for idx, status in enumerate(STATUS_ORDER)}

EMPTY_BOARD = "（看板上没有未归档卡）"

EMPTY_ASSIGNEE = "—"


def _assignee_text(task: Task) -> str:
    """负责人：空字符串或 None 都显示破折号。"""
    return task.assignee if task.assignee else EMPTY_ASSIGNEE


def _visible_sorted(tasks: list[Task], now: int) -> list[Task]:
    """未归档卡，按 list 的口径排序。

    排序：状态按 STATUS_ORDER 下标升序 → 同状态内耗时降序 → id 升序。
    """
    visible = [task for task in tasks if task.status != ARCHIVED]
    return sorted(
        visible,
        key=lambda task: (
            _STATUS_RANK.get(task.status, len(STATUS_ORDER)),
            -task.duration_minutes(now),
            task.id,
        ),
    )


def render_list(tasks: list[Task], now: int) -> str:
    """看板总览表：卡号 / 负责人 / 状态 / 耗时(分) / 标题。

    - 只含 status != archived 的卡；
    - 排序：状态按 STATUS_ORDER 下标升序 → 同状态内耗时降序 → id 升序；
    - 耗时列右对齐；标题按 TITLE_MAX 截断；
    - 一张可显示的卡都没有时返回单行 EMPTY_BOARD（不带表头）。
    """
    order = _visible_sorted(tasks, now)
    if not order:
        return EMPTY_BOARD

    headers = ["卡号", "负责人", "状态", "耗时(分)", "标题"]
    rows = [
        [
            task.id,
            _assignee_text(task),
            task.status or "",
            str(task.duration_minutes(now)),
            fmt.truncate(task.title or "", TITLE_MAX),
        ]
        for task in order
    ]
    return fmt.format_table(headers, rows, right_align=(3,))


def render_export(tasks: list[Task], now: int) -> str:
    """未归档卡的 JSON 快照（机器读，单行）。

    - 只含 ``status != archived`` 的卡，排序与 ``render_list`` 完全一致；
    - 返回 ``json.dumps(payload, ensure_ascii=False)`` 的单行文本：无 indent、
      无 sort_keys，不带尾随换行；
    - 每个对象的键顺序固定 ``id, assignee, status, duration_minutes, title``，
      类型恒为 ``str, str, str, int, str``；``assignee`` / ``status`` / ``title``
      为 None 时输出空串（不套列表用的「—」）；
    - ``title`` 不截断（不用 TITLE_MAX）；
    - ``duration_minutes`` 一律来自 ``Task.duration_minutes(now)``，不另写算法；
    - 一张可显示的卡都没有（全 archived 或 tasks 为空）时返回 ``[]``。
    """
    payload = [
        {
            "id": task.id,
            "assignee": task.assignee or "",
            "status": task.status or "",
            "duration_minutes": task.duration_minutes(now),
            "title": task.title or "",
        }
        for task in _visible_sorted(tasks, now)
    ]
    return json.dumps(payload, ensure_ascii=False)


def render_summary(
    tasks: list[Task],
    rework: dict[str, int],
    now: int,
    day: datetime.date,
) -> str:
    """当日汇总：完成 / 在跑 / 阻塞 三个计数 + 异常清单。

    输出固定五段（异常明细可有可无），整体不带尾随换行、不 print：

        {"看板概览（快照 YYYY-MM-DD HH:MM；完成按 YYYY-MM-DD）"
         "完成: N"
         "在跑: N"
         "阻塞: N"
         "异常: N"
         "  - {status} {分钟} 分钟（阈值 {阈值}）{id}「{标题}」"...}

    ``--date`` 口径（day 参数）：
    - 只影响「完成数」——按 ``completed_at`` 的**自然日**（本地时区，
      不做 UTC 换算）与 ``day`` 相等来计数，且要求 ``status == "done"``；
    - 在跑 / 阻塞 / 异常永远描述**当前快照**，与 ``day`` 无关；
    - 快照时刻来自 ``now``（CLI 的 ``--now``），不是 ``day``。

    其他规则：
    - 异常：``running`` 且耗时 > DOING_STALE_MINUTES、``review`` 且耗时
      > REVIEW_STALE_MINUTES（严格大于，恰好等于阈值不算）；明细按分钟降序
      → id 升序排列，标题按 TITLE_MAX 截断并套「」；
    - 异常数为 0 时只输出 ``异常: 0``，不输出任何 ``-`` 行；
    - archived 卡的状态天然不等于 done/running/blocked，故不进入任何计数；
    - ``rework`` 本命令不用（E5 的 member 用），保留在签名里与 CLI 对齐。

    前置条件：``now`` 须为 ``datetime.fromtimestamp`` 可表示的 epoch 秒，
    越界由 CLI 的 ``--now`` 类型校验拒绝（本函数不做兜底、不夹取）。
    """
    completed = 0
    running = 0
    blocked = 0
    anomalies: list[tuple[int, Task, int]] = []

    for task in tasks:
        if task.status == "done":
            if (
                task.completed_at is not None
                and datetime.datetime.fromtimestamp(task.completed_at).date() == day
            ):
                completed += 1
        elif task.status == "running":
            running += 1
        elif task.status == "blocked":
            blocked += 1

        minutes = task.duration_minutes(now)
        if task.status == "running" and minutes > DOING_STALE_MINUTES:
            anomalies.append((minutes, task, DOING_STALE_MINUTES))
        elif task.status == "review" and minutes > REVIEW_STALE_MINUTES:
            anomalies.append((minutes, task, REVIEW_STALE_MINUTES))

    anomalies.sort(key=lambda item: (-item[0], item[1].id))

    snapshot = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"看板概览（快照 {snapshot}；完成按 {day.isoformat()}）",
        f"完成: {completed}",
        f"在跑: {running}",
        f"阻塞: {blocked}",
        f"异常: {len(anomalies)}",
    ]
    for minutes, task, threshold in anomalies:
        title = fmt.truncate(task.title or "", TITLE_MAX)
        lines.append(
            f"  - {task.status} {minutes} 分钟（阈值 {threshold}）"
            f"{task.id} 「{title}」"
        )
    return "\n".join(lines)


def render_member(
    name: str,
    tasks: list[Task],
    rework: dict[str, int],
    now: int,
) -> str:
    """单个成员的四个数字：交付 / 被驳回 / 平均耗时。

    输出固定 4 行、行首无缩进、半角冒号 + 1 个空格，数字不带千分位，
    整体不带尾随换行、不 print：

        成员: engineer
        交付: 1
        被驳回: 2
        平均耗时: 25 分钟（样本 1 张已交付卡）

    口径：

    1. 交付数 = ``status == "done"`` 且 ``assignee == name`` 的卡数；
       ``archived`` 不算交付（即便它曾经 done 过）。这些卡同时就是下面的
       「样本」。
    2. 被驳回次数 = ``Σ rework[t.id]``，求和范围是**所有**
       ``assignee == name`` 的卡，**不看状态**（含 archived、含没交付的卡）；
       ``rework`` 里缺的 task_id 记 0。
    3. 平均耗时 = 样本卡的 ``duration_minutes(now)`` 之和 // 样本数，向下取整；
       样本为 0 时不做除法，第 4 行改为 ``平均耗时: —（无已交付卡）``。
    4. 查无此人（``tasks`` 里没有任何 ``assignee == name`` 的卡，含 archived）
       → ``raise db.BoardError(f"没有 assignee 为 {name} 的卡")``。
    """
    mine = [task for task in tasks if task.assignee == name]
    if not mine:
        raise db.BoardError(f"没有 assignee 为 {name} 的卡")

    delivered = [task for task in mine if task.status == "done"]
    reworked = sum(rework.get(task.id, 0) for task in mine)

    lines = [f"成员: {name}", f"交付: {len(delivered)}", f"被驳回: {reworked}"]
    if delivered:
        total = sum(task.duration_minutes(now) for task in delivered)
        lines.append(
            f"平均耗时: {total // len(delivered)} 分钟"
            f"（样本 {len(delivered)} 张已交付卡）"
        )
    else:
        lines.append("平均耗时: —（无已交付卡）")
    return "\n".join(lines)
