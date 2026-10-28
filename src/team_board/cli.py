"""team-board 命令行入口：argparse 解析 → 只读打开看板库 → 渲染 → 打印。

退出码：
- 0：成功
- 1：数据源有问题（db.BoardError，连接失败或 render_* 抛的 BoardError）
- 2：argparse 自己判定参数/子命令非法（含 ``--now`` 越界、``--date`` 非 YYYY-MM-DD）
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime

from . import commands, db
from .model import DEFAULT_DB


def _parse_date(value: str) -> date:
    """按 YYYY-MM-DD 严格解析；非法时交给 argparse 报错（退出码 2）。"""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期应为 YYYY-MM-DD: {value!r}"
        ) from exc


def _parse_now(value: str) -> int:
    """把 ``--now`` 解析成本平台 datetime 能表示的 epoch 秒。

    非整数、或 ``datetime.fromtimestamp`` 抛 ValueError / OSError /
    OverflowError（13 位毫秒、天文数字等）都交给 argparse 报错（退出码 2）。
    负数 epoch 合法，不拦。
    """
    try:
        epoch = int(value)
        datetime.fromtimestamp(epoch)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(
            f"基准时间超出可表示范围: {value!r}"
        ) from exc
    return epoch


def build_parser() -> argparse.ArgumentParser:
    """建 CLI 前端：prog=team-board，三个子命令共享 --db / --now。"""
    parser = argparse.ArgumentParser(
        prog="team-board",
        description="团队看板的只读命令行查看器",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("list", "列出未归档卡（按状态与耗时排序）"),
        ("summary", "当日汇总"),
        ("member", "单个成员的卡"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--db",
            default=DEFAULT_DB,
            metavar="PATH",
            help=f"看板 SQLite 路径（默认 {DEFAULT_DB}）",
        )
        sub.add_argument(
            "--now",
            type=_parse_now,
            default=None,
            metavar="EPOCH",
            help="基准时间（epoch 秒，默认当前时间）",
        )
        if name == "summary":
            sub.add_argument(
                "--date",
                type=_parse_date,
                default=None,
                metavar="YYYY-MM-DD",
                help="汇总日期（默认取 --now 所在日期）",
            )
        if name == "member":
            sub.add_argument("name", help="成员名（对应 tasks.assignee）")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    now = args.now if args.now is not None else int(time.time())

    conn = None
    try:
        conn = db.connect(args.db)
        tasks = db.load_tasks(conn)
        if args.command == "list":
            text = commands.render_list(tasks, now)
        elif args.command == "summary":
            rework = db.load_rework_counts(conn)
            day = (
                args.date
                if args.date is not None
                else datetime.fromtimestamp(now).date()
            )
            text = commands.render_summary(tasks, rework, now, day)
        elif args.command == "member":
            rework = db.load_rework_counts(conn)
            text = commands.render_member(args.name, tasks, rework, now)
        else:  # pragma: no cover - argparse 已拦截未知子命令
            parser.error(f"未知子命令: {args.command}")
    except db.BoardError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()

    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
