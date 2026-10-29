"""team-board 命令行入口：argparse 解析 → 只读打开看板库 → 渲染 → 打印。

退出码：
- 0：成功
- 1：数据源有问题（db.BoardError，连接失败或 render_* 抛的 BoardError）
- 2：argparse 自己判定参数/子命令非法（含 ``--now`` 越界、``--date`` 非 YYYY-MM-DD）

``branch-audit`` 子命令自己的退出码：
- 0：无偏差
- 1：发现偏差（提交归属 / checkout 偏移 / 分支结构）
- 2：参数或环境错误（``branches.AuditError`` → stderr ``错误: …``、stdout 空、
  不冒 traceback；argparse 级坏参数照旧 rc2）。环境错误包含：``PATH`` 里没有
  ``git``、豁免表不是 UTF-8 文本、git 输出不是 UTF-8（``i18n.logOutputEncoding``
  设成 GBK、``core.quotePath=false`` + 非 UTF-8 文件名）、stdout/stderr 编码
  表示不了中文（``PYTHONIOENCODING=ascii``）

``merge-audit`` 子命令沿用同一组数字但语义是「认账问题」：
- 0：区间内合并提交都有认账三字段（``--require`` 时该卡有零问题的合并）
- 1：发现问题（缺字段 / 格式非法 / 重复字段 / 字段未落在 trailer 区 /
  ``--require`` 只有历史命中或两段都没命中）
- 2：参数或环境错误（``merges.MergeAuditError`` → stderr ``错误: …``、stdout 空；
  含仓库不是工作树、基线文件不存在 / 非 UTF-8 / 空 / 多行、``PATH`` 里没有
  ``git``、git 输出不是 UTF-8、stdout 编码表示不了中文）

``merge-record`` 子命令（合并**前**那一半）：
- 0：生成模式输出成功，或 ``--check-file`` 预检通过
- 1：``--check-file`` 预检发现认账问题（每处一行 ``问题: <kind>：<detail>``）
- 2：参数或环境错误（``merges.MergeAuditError`` → stderr ``错误: …``、stdout 空；
  含 ``--card`` / ``--by`` / ``--pr`` 形态非法、生成模式缺必需参数、``--check-file``
  与 ``--body-out`` 同给、``--body-out`` 父目录不存在、body 文件不存在 / 是目录 /
  无权限 / 非 UTF-8 / 空、stdout 编码表示不了中文）
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import branches, commands, db, merges, web
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

#: ``merge-record --pr`` 只接受 ASCII 十进制正整数（同 ``_PORT_DIGITS`` 的理由：
#: ``int()`` 会静默接受 ``" 15"`` / ``"+15"`` / ``"1_0"`` / 阿拉伯-印度数字）
_PR_DIGITS = re.compile(r"[1-9][0-9]*", re.ASCII)

#: ``merge-record --pr`` 的上限。GitHub 的 PR 号是仓库内递增计数，9 位（< 2**31）
#: 已经远超任何真实仓库；13 位毫秒（``1793160000000``）与天文数字
#: （``99999999999999999999``）都属「填错了字段」，一律 rc2 而不是收下。
MAX_MERGE_PR = 999_999_999

#: ``merge-record`` 生成模式的默认 body 路径模板（``/tmp/<卡号>-merge-body.txt``）。
#: bandit B108 假阳性：该路径**只打印**、默认不落盘（仅当显式给 ``--body-out`` 才写），
#: 且写入用 ``write_bytes`` 全量覆盖、不参与共用临时文件的竞态
DEFAULT_BODY_PATH_TMPL = "/tmp/{card}-merge-body.txt"  # nosec B108


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
    """建 CLI 前端：prog=team-board，共**八个**子命令。

    - ``list`` / ``summary`` / ``member`` / ``export``：共享 ``--db`` / ``--now``
      （``summary`` 另有 ``--date``，``member`` 另有位置参数 ``name``）；
    - ``branch-audit``：稽核卡分支提交归属与 checkout 偏移（0 无偏差 / 1 有偏差 /
      2 参数或环境错误），吃 ``--repo`` / ``--main-branch`` / ``--branch`` /
      ``--allow-file``；
    - ``merge-audit``：稽核合并提交的认账三字段（0 无问题 / 1 有问题 / 2 参数或
      环境错误），吃 ``--repo`` / ``--baseline`` / ``--baseline-file`` / ``--rev`` /
      ``--card`` / ``--require``；
    - ``merge-record``：合并**前**生成认账 body（喂 ``gh pr merge --body-file``）或
      ``--check-file`` 预检（0 成功/通过 / 1 预检有问题 / 2 参数或环境错误），吃
      ``--card`` / ``--by`` / ``--pr`` / ``--subject`` / ``--body-out`` /
      ``--check-file``；**只打印** ``gh pr merge`` 模板，不执行任何 git/gh 命令；
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
        help="稽核卡分支提交归属、checkout 偏移与分支结构（0 无偏差 / 1 有偏差 / 2 参数或环境错误）",
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

    merge_cmd = subparsers.add_parser(
        "merge-audit",
        help="稽核合并提交的认账三字段（0 无问题 / 1 有问题 / 2 参数或环境错误）",
    )
    merge_cmd.add_argument(
        "--repo",
        default=".",
        metavar="PATH",
        help="要稽核的 git 仓库路径（默认当前目录）",
    )
    merge_cmd.add_argument(
        "--baseline",
        default=None,
        metavar="REV",
        help="基线提交（判定 <基线>..<rev> 区间；与 --baseline-file 只能给一个）",
    )
    merge_cmd.add_argument(
        "--baseline-file",
        default=None,
        metavar="PATH",
        help="基线文件路径（默认 <repo>/.team-board/merge-audit-baseline）",
    )
    merge_cmd.add_argument(
        "--rev",
        default="HEAD",
        metavar="REV",
        help="区间终点（默认 HEAD）",
    )
    merge_cmd.add_argument(
        "--card",
        default=None,
        metavar="CARD",
        help="只列这张卡的合并记录（同时过滤判定段与历史段）",
    )
    merge_cmd.add_argument(
        "--require",
        action="store_true",
        help="要求 --card 指定的卡有认账合并（必须与 --card 同用）",
    )

    record_cmd = subparsers.add_parser(
        "merge-record",
        help="合并前生成认账 body 或预检（0 成功/通过 / 1 预检有问题 / 2 参数或环境错误）",
    )
    record_cmd.add_argument(
        "--card",
        default=None,
        metavar="CARD",
        help="依据哪张卡（形如 t_1234abcd；生成模式必给，预检模式可选作一致性校验）",
    )
    record_cmd.add_argument(
        "--by",
        default=None,
        metavar="PROFILE/RUN",
        help="谁按了合并（形如 pm/run137；生成模式必给，预检模式可选作一致性校验）",
    )
    record_cmd.add_argument(
        "--pr",
        default=None,
        metavar="N",
        help=f"PR 号（1-{MAX_MERGE_PR} 的 ASCII 十进制正整数；生成模式必给）",
    )
    record_cmd.add_argument(
        "--subject",
        default=None,
        metavar="TEXT",
        help="合并提交标题（默认 Merge PR #<pr>（<card>））",
    )
    record_cmd.add_argument(
        "--body-out",
        default=None,
        metavar="PATH",
        help="把认账 body 原样写入该文件（末尾恰好一个换行）；父目录不存在 → rc2",
    )
    record_cmd.add_argument(
        "--check-file",
        default=None,
        metavar="PATH",
        help="预检模式：校验该文件的认账三字段（不能与 --body-out 同给）",
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


def _write_stderr_error(message: str) -> None:
    """把 ``错误: …`` 写到 stderr，**在任何编码环境下都不崩**。

    用户可能跑在 ``LC_ALL=C PYTHONIOENCODING=ascii`` 下：这条消息里有中文，
    直接 ``print(..., file=sys.stderr)`` 会 ``UnicodeEncodeError``（于是 rc1 +
    traceback，跟「发现偏差」同码），或者被 Python 默认的 ``backslashreplace``
    转成 ``\\u9519\\u8bef: …``（那就不是 ``错误: `` 了）。所以先问 stderr 的编码
    能不能表示整行，能就正常写；不能就按 UTF-8 字节直写 ``stderr.buffer``。
    """

    line = f"错误: {message}\n"
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        line.encode(encoding)
    except UnicodeEncodeError:
        data = line.encode("utf-8")
        buffer = getattr(sys.stderr, "buffer", None)
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:  # pragma: no cover - 没有 buffer 的替身对象（极少见）
            sys.stderr.write(data.decode("utf-8", "backslashreplace"))
        return
    sys.stderr.write(line)
    sys.stderr.flush()


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
    """``branch-audit`` 子命令：AuditError → stderr ``错误: …`` + rc2，不冒 traceback。

    报告整块生成后再一次性写出：stdout 编码表示不了中文（如
    ``PYTHONIOENCODING=ascii``）属于环境级错误 → rc2、stdout 空、stderr
    ``错误: …``，不许退化成 rc1 + traceback（rc1 是「发现偏差」的专用码）。
    """

    try:
        report = branches.audit(
            args.repo,
            main_branch=args.main_branch,
            only_branch=args.branch,
            allow_file=args.allow_file,
        )
    except branches.AuditError as exc:
        _write_stderr_error(str(exc))
        return 2

    text = branches.render_report(report)
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        # TextIOWrapper 先整体编码再写出：编码失败时 stdout 里一个字节都没落
        _write_stderr_error(
            "当前 stdout 编码 "
            f"{getattr(sys.stdout, 'encoding', None)!r} 无法表示稽核报告里的中文，"
            "请改用 UTF-8（例如 PYTHONIOENCODING=utf-8 / LC_ALL=C.UTF-8）"
        )
        return 2
    return 0 if report.ok else 1


def _run_merge_audit(args: argparse.Namespace) -> int:
    """``merge-audit`` 子命令：MergeAuditError → stderr ``错误: …`` + rc2，不冒 traceback。

    报告整块生成后再一次性写出：stdout 编码表示不了中文（如
    ``PYTHONIOENCODING=ascii``）属于环境级错误 → rc2、stderr ``错误: …``，
    不许退化成 rc1 + traceback（rc1 是「有问题」的专用码）。
    """

    try:
        report = merges.audit(
            args.repo,
            baseline=args.baseline,
            baseline_file=args.baseline_file,
            rev=args.rev,
            only_card=args.card,
            require=args.require,
        )
    except merges.MergeAuditError as exc:
        _write_stderr_error(str(exc))
        return 2

    text = merges.render_report(report)
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        # TextIOWrapper 先整体编码再写出：编码失败时 stdout 里一个字节都没落
        _write_stderr_error(
            "当前 stdout 编码 "
            f"{getattr(sys.stdout, 'encoding', None)!r} 无法表示稽核报告里的中文，"
            "请改用 UTF-8（例如 PYTHONIOENCODING=utf-8 / LC_ALL=C.UTF-8）"
        )
        return 2
    return 0 if report.ok else 1


def _write_stdout(text: str) -> bool:
    """整块写 stdout；编码表示不了中文 → stderr ``错误: …`` 并返回 ``False``。

    ``TextIOWrapper`` 先整体编码再写出：编码失败时 stdout 里一个字节都没落，
    所以这里是「要么全出、要么不出」，码必须是 rc2（环境错误）而不是 rc1。
    """

    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        _write_stderr_error(
            "当前 stdout 编码 "
            f"{getattr(sys.stdout, 'encoding', None)!r} 无法表示输出里的中文，"
            "请改用 UTF-8（例如 PYTHONIOENCODING=utf-8 / LC_ALL=C.UTF-8）"
        )
        return False
    return True


def _parse_merge_pr(value: str) -> int:
    """``merge-record --pr``：ASCII 十进制正整数且不超 ``MAX_MERGE_PR``，否则 rc2。

    只用字符串接收、在这里转 int（argparse 不挂 ``type=int``）：``type=int`` 抛的是
    ``SystemExit(2)`` + argparse 自己的文案，与其它 rc2 的 ``错误: …`` 不一致，
    且它拦不住 ``0``、``1_0``（=10）、``+1``、``1793160000000``。
    """

    if not isinstance(value, str) or _PR_DIGITS.fullmatch(value) is None:
        raise merges.MergeAuditError(
            f"PR 号非法：{value!r}（应为 1-{MAX_MERGE_PR} 的 ASCII 十进制正整数）"
        )
    pr = int(value)
    if pr > MAX_MERGE_PR:
        raise merges.MergeAuditError(
            f"PR 号超出上限 {MAX_MERGE_PR}：{value!r}（13 位毫秒与天文数字都不合法）"
        )
    return pr


def _merge_identity(
    card: str | None, by: str | None, pr: str | None
) -> tuple[str | None, str | None, int | None]:
    """校验 ``--card`` / ``--by`` / ``--pr`` 的**形态**（给了才判），返回规整值。

    生成模式与预检模式共用这一处：预检里三者可选，但给了就必须合法 ——
    ``--card t_zzz`` 在两种模式下都是 rc2（参数错误），不是 rc1（认账问题）。
    """

    if card is not None and (
        not isinstance(card, str) or merges.MERGE_CARD_RE.fullmatch(card) is None
    ):
        raise merges.MergeAuditError(f"卡号非法：{card!r}（应形如 t_1234abcd）")
    if by is not None and (
        not isinstance(by, str) or merges.MERGE_BY_RE.fullmatch(by) is None
    ):
        raise merges.MergeAuditError(f"执行者标识非法：{by!r}（应形如 pm/run137）")
    return card, by, None if pr is None else _parse_merge_pr(pr)


def _read_body_file(path: str) -> str:
    """读 ``--check-file`` 指的文件（严格 UTF-8）；五类坏输入一律 rc2。

    空文件**不交给** ``check_message``：那会得到三条「缺字段」rc1，而「根本没有可
    预检的内容」是参数/环境错误（rc2），两个码必须分得开。
    """

    if not isinstance(path, str) or not path.strip():
        raise merges.MergeAuditError("--check-file 的路径为空")
    target = Path(path)
    if not target.exists():
        raise merges.MergeAuditError(f"合并 body 文件不存在：{path}")
    if not target.is_file():
        raise merges.MergeAuditError(f"合并 body 文件不是普通文件（目录？）：{path}")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise merges.MergeAuditError(
            f"合并 body 文件 {path} 不是 UTF-8 文本（第 {exc.start} 字节起解码失败）"
        ) from exc
    except OSError as exc:  # 无权限、路径失效等
        raise merges.MergeAuditError(f"无法读取合并 body 文件 {path}: {exc}") from exc
    if not text.strip():
        raise merges.MergeAuditError(f"合并 body 文件 {path} 是空文件（没有可预检的内容）")
    return text


def _write_body_file(path: str, body: str) -> None:
    """把 body 段按 UTF-8 原样写盘（``write_bytes`` 保证与 stdout 逐字节一致）。

    父目录不存在 / 目标不可写 → rc2。``body`` 由 ``merges.build_message`` 产出、
    末尾恰好一个换行，所以文件末尾也恰好一个换行。
    """

    if not isinstance(path, str) or not path.strip():
        raise merges.MergeAuditError("--body-out 的路径为空")
    target = Path(path)
    if not target.parent.is_dir():
        raise merges.MergeAuditError(f"--body-out 的父目录不存在：{target.parent}")
    try:
        target.write_bytes(body.encode("utf-8"))
    except OSError as exc:
        raise merges.MergeAuditError(f"无法写入 body 文件 {path}: {exc}") from exc


def _record_generate(
    *,
    card: str,
    by: str,
    pr: int,
    subject: str | None,
    body_out: str | None,
) -> int:
    """生成模式：打印「body 段 + 空行 + 4 行注释段」，``--body-out`` 时把 body 写盘。

    注释段 4 行 = 3 行 ``# `` 注释 + 1 行原样 ``gh pr merge`` 命令模板（该行刻意
    不加 ``# ``，否则复制出来就不能直接执行）。本函数**不执行**任何 git/gh 命令。
    """

    body = merges.build_message(card=card, by=by, pr=pr, subject=subject)
    title = subject if subject is not None else f"Merge PR #{pr}（{card}）"
    body_path = (
        body_out
        if body_out is not None
        else DEFAULT_BODY_PATH_TMPL.format(card=card)
    )
    wrote = body_out is not None
    if wrote:
        _write_body_file(body_path, body)
    written_note = "已写入" if wrote else "未写入，给了 --body-out 才写"
    lines = [
        body.rstrip("\n"),
        "",
        f"# 卡面认账块：上面 body 段的三行原样贴卡；"
        f"合并完成后用 merge-audit --card {card} --require 回读合并提交 sha 补一行",
        "# 合并命令（本工具不执行）：",
        f'gh pr merge {pr} -R alsj213/team-board --merge --subject "{title}"'
        f" --body-file {body_path}",
        f"# body 文件：{body_path}（{written_note}）",
    ]
    return 0 if _write_stdout("\n".join(lines) + "\n") else 2


def _run_merge_record(args: argparse.Namespace) -> int:
    """``merge-record`` 子命令：MergeAuditError → stderr ``错误: …`` + rc2，不冒 traceback。

    由 ``--check-file`` 决定模式（给了就是预检，不给就是生成）；两种模式下
    ``--card`` / ``--by`` / ``--pr`` 的形态校验先跑 —— 参数错误一律 rc2，
    与「预检发现认账问题」的 rc1 分开。
    """

    try:
        if args.check_file is not None and args.body_out is not None:
            raise merges.MergeAuditError(
                "--check-file 与 --body-out 不能同给（预检不写文件，生成不校验文件）"
            )
        card, by, pr = _merge_identity(args.card, args.by, args.pr)

        if args.check_file is not None:
            problems = merges.check_message(
                _read_body_file(args.check_file), card=card, by=by, pr=pr
            )
            if problems:
                text = "".join(
                    f"问题: {problem.kind}：{problem.detail}\n" for problem in problems
                )
            else:
                text = "通过：三字段齐（Merge-By/Merge-Card/Merge-PR）\n"
            if not _write_stdout(text):
                return 2
            return 1 if problems else 0

        if card is None or by is None or pr is None:
            missing = [
                name
                for name, value in (("--card", card), ("--by", by), ("--pr", pr))
                if value is None
            ]
            raise merges.MergeAuditError(
                "生成模式必须同时给 --card / --by / --pr（缺 " + " / ".join(missing) + "）"
            )
        return _record_generate(
            card=card,
            by=by,
            pr=pr,
            subject=args.subject,
            body_out=args.body_out,
        )
    except merges.MergeAuditError as exc:
        _write_stderr_error(str(exc))
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "branch-audit":
        return _run_branch_audit(args)

    if args.command == "merge-audit":
        return _run_merge_audit(args)

    if args.command == "merge-record":
        return _run_merge_record(args)

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
