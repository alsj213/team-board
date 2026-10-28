"""team-board 的数据模型：状态常量与 Task。

耗时口径：从「开始」到「结束」的分钟数，向下取整。
- 开始：started_at；为 None 时回退到 created_at。
- 结束：completed_at；为 None（还没结束）时算到调用方传入的时间点 now。
- 脏数据（结束早于开始）夹到 0，不报错。
"""

from __future__ import annotations

from dataclasses import dataclass

ARCHIVED = "archived"

# archived 不在 STATUS_ORDER 里：它是"已归档"而非看板上的流转状态
STATUS_ORDER = (
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "review",
    "done",
)

DEFAULT_DB = "/home/liu/.hermes/kanban/boards/devteam/kanban.db"

DOING_STALE_MINUTES = 60
REVIEW_STALE_MINUTES = 30
TITLE_MAX = 40


@dataclass(frozen=True)
class Task:
    """看板上的一张卡。字段与 tasks 表一一对应。"""

    id: str
    title: str
    assignee: str
    status: str
    created_at: int
    started_at: int | None
    completed_at: int | None

    def start_ts(self) -> int:
        """开始时间：started_at，缺省回退 created_at。"""
        return self.started_at if self.started_at is not None else self.created_at

    def duration_minutes(self, now: int) -> int:
        """从开始到结束的分钟数（向下取整）；未结束算到 now；负数夹 0。"""
        end = self.completed_at if self.completed_at is not None else now
        return max(0, (end - self.start_ts()) // 60)
