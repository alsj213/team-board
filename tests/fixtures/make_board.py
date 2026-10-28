"""确定性看板 fixture 库生成器（E3–E5 测试共用）。

用法：
    python3 tests/fixtures/make_board.py --out PATH --now TS   # stdout 打印 JSON {语义名: task_id}

只建 tasks 与 task_events 两张表，字段与真实库一致（task_events 只保留本工具用到的列）。
同一 --now 下两次生成的两份库，导出的 (id, status, started_at, completed_at) 完全一致。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    # 直接当脚本跑（python3 tests/fixtures/make_board.py）时，让 src/ 里的包可导入
    _SRC = Path(__file__).resolve().parents[2] / "src"
    if _SRC.is_dir() and str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

from team_board.model import DEFAULT_DB

# 2026-10-28 12:00:00 CST；测试统一用它，避开跨午夜的坑
FIXTURE_NOW = 1793160000

_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    assignee TEXT,
    status TEXT,
    created_at INTEGER,
    started_at INTEGER,
    completed_at INTEGER
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY,
    task_id TEXT,
    kind TEXT,
    created_at INTEGER
);
"""

# (语义名, status, assignee, created_at 偏移分钟, started_at 偏移分钟|None, completed_at 偏移分钟|None)
_CARDS: tuple[tuple[str, str, str, int, int | None, int | None], ...] = (
    ("archived_old", "archived", "pm", 200, None, None),
    ("running_stale", "running", "engineer", 80, 75, None),
    ("running_fresh", "running", "pm", 12, 10, None),
    ("review_stale", "review", "pm", 50, 45, None),
    ("review_fresh", "review", "engineer", 6, 5, None),
    ("blocked_one", "blocked", "pm", 130, 120, None),
    ("done_today", "done", "engineer", 35, 30, 5),
    ("done_yesterday", "done", "pm", 1500, 1500, 1440),
    ("null_start", "ready", "pm", 90, None, None),
    ("long_title", "todo", "engineer", 20, None, None),
)

LONG_TITLE = "中文长标题截断验证" * 6

# done_today 上有 2 条 changes_requested（其余卡 0 条）
_REWORK_EVENTS: tuple[tuple[str, int], ...] = (
    ("done_today", 20),
    ("done_today", 8),
)


def task_id(name: str) -> str:
    """语义名 -> 固定 task id（同一名字永远同一个 id）。"""
    return f"t_{name}"


def build_board(path: str | Path, *, now: int) -> dict[str, str]:
    """在 path 建库灌数，返回 {语义名: task_id}。"""
    out = Path(path)
    if out.resolve() == Path(DEFAULT_DB).resolve():
        raise ValueError(f"拒绝覆盖真实看板库: {out}")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    conn = sqlite3.connect(str(out))
    try:
        conn.executescript(_SCHEMA)
        for name, status, assignee, created_off, started_off, completed_off in _CARDS:
            conn.execute(
                "INSERT INTO tasks"
                " (id, title, assignee, status, created_at, started_at, completed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id(name),
                    LONG_TITLE if name == "long_title" else name,
                    assignee,
                    status,
                    now - created_off * 60,
                    None if started_off is None else now - started_off * 60,
                    None if completed_off is None else now - completed_off * 60,
                ),
            )
        for idx, (name, off) in enumerate(_REWORK_EVENTS, start=1):
            conn.execute(
                "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (?, ?, ?, ?)",
                (idx, task_id(name), "changes_requested", now - off * 60),
            )
        conn.commit()
    finally:
        conn.close()

    return {name: task_id(name) for name, *_ in _CARDS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 team-board 测试用 fixture 库")
    parser.add_argument("--out", required=True, help="输出 SQLite 路径")
    parser.add_argument("--now", type=int, default=FIXTURE_NOW, help="基准 epoch 秒")
    args = parser.parse_args(argv)

    ids = build_board(args.out, now=args.now)
    print(json.dumps(ids, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
