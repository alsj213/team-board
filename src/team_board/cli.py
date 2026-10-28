"""team-board 命令行入口：argparse 解析 → 只读打开看板库 → 渲染 → 打印。

退出码：
- 0：成功
- 1：数据源有问题（db.BoardError，连接失败或 render_* 抛的 BoardError）
- 2：argparse 自己判定参数/子命令非法（含 ``--now`` 越界、``--date`` 非 YYYY-MM-DD）

``branch-audit`` 子命令自己的退出码：
- 0：无偏差
- 1：发现偏差（提交归属 / checkout 偏移）
- 2：参数或环境错误（``branches.AuditError`` → stderr ``错误: …``、stdout 空、
  不冒 traceback；argparse 级坏参数照旧 rc2）
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime

from . import branches, commands, db, web
from .model import DEFAULT_DB

#: serve --bind 只接受的「本机」地址（不给 0.0.0.0 / 空串留口子）
#:
#: IPv6 回环字面量不在表里：``web.make_server`` 用的 ``ThreadingHTTPServer``
#: 的 ``address_family`` 恒为 ``AF_INET``，拿 IPv6 字面量去 bind 必然
#: ``gaierror``（实测 ``[Errno -9] Address family for hostname not supported``）。
#: IPv6 支持属于另一件事，不做「文档说行、真跑起不来」的假选项 ——
#: 拒绝行为有集成测试锁住。
_LOCAL_BINDS = frozenset({"127.0.0.1", "localhost"})

#: ``--port`` 只接受 ASCII 十进制数字：``int()`` 认空白、正负号、下划线与非
#: ASCII 数字（``"١٢٣"`` → 123），比 README 表格的「1–65535 的整数」宽得多。
_PORT_DIGITS = re.compile(r"[0-9]+", re.ASCII)


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


def _parse_port(value: str) -> int:
    """``serve --port``：纯 ASCII 十进制数字且 1 <= n <= 65535，否则 rc2。

    只认 ``0-9``：``int()`` 会静默接受 ``" 8787"``（空白）、``"+8787"``（正号）、
    ``"1_000"``（下划线）和 ``"١٢٣"``（阿拉伯-印度数字，= 123），比 README 的
    「1–65535 的整数」宽得多，这里全部拒绝。
    ``0`` 与负数也不放行（``0`` 只对 ``web.make_server`` 内部有意义，CLI 层不暴露）。
    """
    if not isinstance(value, str) or _PORT_DIGITS.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            f"端口应为 1-65535 的 ASCII 十进制整数: {value!r}"
        )
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"端口超出 1-65535: {value!r}")
    return port


def _parse_bind(value: str) -> str:
    """``serve --bind``：只接受本机地址，其它（含 ``0.0.0.0``、空串、IPv6 字面量）rc2。"""
    if value not in _LOCAL_BINDS:
        raise argparse.ArgumentTypeError(
            f"只允许本机地址 127.0.0.1 / localhost: {value!r}"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    """建 CLI 前端：prog=team-board，共**五个**子命令。

    - ``list`` / ``summary`` / ``member`` / ``export``：共享 ``--db`` / ``--now``
      （``summary`` 另有 ``--date``，``member`` 另有位置参数 ``name``）；
    - ``serve``：只认 ``--db`` / ``--port`` / ``--bind``，**没有** ``--now``
      （快照时刻取服务进程的真实时钟，``web.make_server`` 的 clock 不用 CLI 传）。
    """
    parser = argparse.ArgumentParser(
        prog="team-board",
        description="团队看板的只读命令行查看器",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("list", "列出未归档卡（按状态与耗时排序）"),
        ("summary", "当日汇总"),
        ("member", "单个成员的卡"),
        ("export", "导出未归档卡的 JSON 快照"),
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

    audit_cmd = subparsers.add_parser(
        "branch-audit",
        help="稽核卡分支提交归属与 checkout 偏移（0 无偏差 / 1 有偏差 / 2 参数或环境错误）",
    )
    audit_cmd.add_argument(
        "--repo",
        default=".",
        metavar="PATH",
        help="要稽核的 git 仓库路径（默认当前目录）",
    )
    audit_cmd.add_argument(
        "--main-branch",
        default="main",
        metavar="NAME",
        help="主分支名（默认 main）",
    )
    audit_cmd.add_argument(
        "--branch",
        default=None,
        metavar="NAME",
        help="只稽核这一条分支（接受 wt/t_xxx 或 t_xxx；默认查全部卡分支）",
    )
    audit_cmd.add_argument(
        "--allow-file",
        default=None,
        metavar="PATH",
        help="豁免表路径（默认 <repo>/.team-board/branch-audit-allow.txt）",
    )

    serve = subparsers.add_parser(
        "serve", help="启动只读 Web 看板（默认 127.0.0.1:8787）"
    )
    serve.add_argument(
        "--db",
        default=DEFAULT_DB,
        metavar="PATH",
        help=f"看板 SQLite 路径（默认 {DEFAULT_DB}）",
    )
    serve.add_argument(
        "--port",
        type=_parse_port,
        default=8787,
        metavar="PORT",
        help="监听端口（1-65535，默认 8787）",
    )
    serve.add_argument(
        "--bind",
        type=_parse_bind,
        default="127.0.0.1",
        metavar="ADDR",
        help="监听地址（只允许 127.0.0.1 / localhost）",
    )

    return parser


def _run_serve(args: argparse.Namespace) -> int:
    """``serve`` 子命令：先验库可读 → 起服务 → 阻塞到 Ctrl-C。

    - 库不可读（BoardError）→ stderr ``错误: …`` + rc1，不进入 listen；
    - 端口被占等 OSError → stderr ``错误: 无法监听 …`` + rc1；
    - Ctrl-C 收尾 → rc0，``finally`` 里一定 ``server_close()``。
    """
    try:
        db.connect(args.db).close()
    except db.BoardError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    try:
        server = web.make_server(args.db, bind=args.bind, port=args.port)
    except OSError as exc:
        print(
            f"错误: 无法监听 {args.bind}:{args.port}: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        print(
            f"服务已启动: http://{args.bind}:{args.port}/ "
            f"（数据库 {args.db}，只读；Ctrl-C 退出）",
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()

    return 0


def _run_branch_audit(args: argparse.Namespace) -> int:
    """``branch-audit`` 子命令：AuditError → stderr ``错误: …`` + rc2，不冒 traceback。"""

    try:
        report = branches.audit(
            args.repo,
            main_branch=args.main_branch,
            only_branch=args.branch,
            allow_file=args.allow_file,
        )
    except branches.AuditError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    print(branches.render_report(report))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "branch-audit":
        return _run_branch_audit(args)

    if args.command == "serve":
        return _run_serve(args)

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
        elif args.command == "export":
            text = commands.render_export(tasks, now)
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
