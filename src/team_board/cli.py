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
- 0：区间内合并提交都有认账三字段（``--require`` 时该卡有零问题的合并；给了
  ``--card`` 时该卡的 ``merged-by`` 与看板库 ``tasks.assignee`` 一致）
- 1：发现问题（缺字段 / 格式非法 / 重复字段 / 字段未落在 trailer 区 /
  ``merged-by 与卡 assignee 不符`` / ``--require`` 只有历史命中或两段都没命中）
- 2：参数或环境错误（``merges.MergeAuditError`` → stderr ``错误: …``、stdout 空；
  含仓库不是工作树、基线文件不存在 / 非 UTF-8 / 空 / 多行、看板库不可读或不是合法
  SQLite、库里没有 ``--card`` 指定的卡、``PATH`` 里没有 ``git``、git 输出不是
  UTF-8、stdout 编码表示不了中文）

``commit-audit`` 子命令沿用同一组数字但语义是「提交身份问题」：
- 0：区间内每个提交的 author 都在白名单里、committer 都是本 bot 且与 author 一致
  （区间为空也算通过：输出一行 ``区间内无提交``）
- 1：发现问题（``author 不在白名单`` / ``committer 不是本 bot`` /
  ``committer 与 author 不一致`` / ``身份表里没有该身份``）
- 2：参数或环境错误（``commits.CommitAuditError`` → stderr ``错误: …``、stdout 空；
  含卡号形态非法、仓库不是工作树、身份表缺失 / 格式错、看板库不可读或没有该卡的
  建卡事件（判据② 的 assignee 取建卡时，不取 ``tasks.assignee``）、
  ``--rev`` 解析不成提交、找不到主分支、``PATH`` 里没有 ``git``、git 输出不是
  UTF-8、stdout 编码表示不了中文）

``body-audit`` 子命令（只读，看每张卡 ``tasks.body`` 的字节分布）：
- 0：没有超限卡
- 1：有超限卡（``body`` 的 UTF-8 字节数 > ``bodylimit.BODY_LIMIT_BYTES``）
- 2：参数或环境错误（``db.BoardError`` / ``OSError`` / ``sqlite3.Error`` →
  stderr 恰一行 ``错误: …``、stdout 空、不冒 traceback；含 ``--db`` 不存在、
  不是合法 SQLite、打不开、读 ``tasks`` 失败、stdout 编码表示不了中文）

``push-guard`` 子命令（git pre-push 的判定本体，只读，不写任何东西）：
- 0：放行（stdout 恰一行 ``push-guard: 放行 N 条 ref（checkout=…，HEAD=…）``）；
  给 ``--check-install`` 时表示 pre-push 钩子已装好
- 1：拦下（stdout 空，stderr 每处一行以 ``拒绝：`` 开头）；给 ``--check-install``
  时表示钩子没装好（stderr 一行 ``未安装：<原因>；修复：git config core.hooksPath
  .githooks``）
- 2：参数或环境错误（``branches.AuditError`` / stdin 读失败 / stdout/stderr 编码
  表示不了中文 → stderr 一行 ``错误: …``、stdout 空、不冒 traceback；含 ``--repo``
  不存在或不是工作树、``--main-branch`` 为空或该分支不存在、stdin 每行字段数 ≠ 4、
  stdin 为空）

``merge-record`` 子命令（合并**前**那一半）：
- 0：生成模式输出成功，或 ``--check-file`` 预检通过
- 1：``--check-file`` 预检发现认账问题（每处一行 ``问题: <kind>：<detail>``）
- 2：参数或环境错误（``merges.MergeAuditError`` → stderr ``错误: …``、stdout 空；
  含 ``--card`` / ``--by`` / ``--pr`` 形态非法、``--branch`` 形态非法（空串 / 含空白 /
  带 ``refs/`` 前缀，校验复用 :func:`team_board.merge_guard.check_branch_shape`，不复制
  正则）、生成模式缺必需参数（``--card`` / ``--by`` / ``--pr`` / ``--branch`` /
  ``--body-out`` 缺任一项）、``--subject`` 空 / 纯空白 / 含换行（校验复用
  :func:`team_board.merge_guard.check_subject`，不复制判据）、``--body-out`` /
  ``--repo`` 含换行（打印行会被截成两行，照抄即半条命令）、``--repo`` 含 ``:``
  （``PYTHONPATH=…`` 是冒号分隔的，单条路径里的冒号拆不开 → 照抄即
  ``No module named team_board``）、``--check-file`` 与
  ``--body-out`` 同给、``--body-out``
  父目录不存在、body 文件不存在 / 是目录 / 无权限 / 非 UTF-8 / 空、stdout 编码表示
  不了中文）

``--subject`` 以 ``-`` 开头时（``-h`` / ``--fix …``），打印行改用
``--subject=<值>`` 形式：``shlex.quote('-h')`` 原样返回，``--subject -h`` 会被
argparse 当成 flag → 照抄即 rc2「expected one argument」；``--opt=value`` 形式的
值不参与选项解析。其余取值仍打 ``--subject <值>``（改动只落在这一行，不换命令形态）。
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import bodylimit, branches, commands, commits, db, merges, pushguard, web
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
    """建 CLI 前端：prog=team-board，共**十一个**子命令。

    - ``list`` / ``summary`` / ``member`` / ``export``：共享 ``--db`` / ``--now``
      （``summary`` 另有 ``--date``，``member`` 另有位置参数 ``name``）；
    - ``branch-audit``：稽核卡分支提交归属与 checkout 偏移（0 无偏差 / 1 有偏差 /
      2 参数或环境错误），吃 ``--repo`` / ``--main-branch`` / ``--branch`` /
      ``--allow-file``；
    - ``merge-audit``：稽核合并提交的认账三字段（0 无问题 / 1 有问题 / 2 参数或
      环境错误），吃 ``--repo`` / ``--baseline`` / ``--baseline-file`` / ``--rev`` /
      ``--card`` / ``--db`` / ``--require``（``--card`` + ``--db`` 时跑判据①）；
    - ``commit-audit``：稽核区间内提交的 author 白名单与 committer 身份（0 无问题 /
      1 有问题 / 2 参数或环境错误），吃 ``--repo`` / ``--card`` / ``--rev`` /
      ``--main-branch`` / ``--db`` / ``--bots``；
    - ``merge-record``：合并**前**生成认账 body 与一条可直接照抄的 ``merge_guard``
      合并命令（R12 ① 的唯一入口）或 ``--check-file`` 预检（0 成功/通过 / 1 预检有
      问题 / 2 参数或环境错误），吃 ``--card`` / ``--by`` / ``--pr`` / ``--branch`` /
      ``--subject`` / ``--body-out`` / ``--repo`` / ``--check-file``；**只打印**那条
      命令模板，不执行任何 git/gh 命令；
    - ``body-audit``：稽核全板 ``tasks.body`` 的 UTF-8 字节分布与超限清单（0 无超限 /
      1 有超限 / 2 参数或环境错误），只吃 ``--db`` / ``--all``；
    - ``push-guard``：git pre-push 的判定本体（0 放行 / 1 拦下 / 2 参数或环境
      错误），吃 ``--repo`` / ``--main-branch`` / ``--check-install``；不带
      ``--check-install`` 时从 stdin 读 pre-push 的 ref 行；
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
        help="只列这张卡的合并记录（同时过滤判定段与历史段）；给了它就同时判据①",
    )
    merge_cmd.add_argument(
        "--db",
        default=DEFAULT_DB,
        metavar="PATH",
        help=f"判据① 读 assignee 用的看板库（只读；默认 {DEFAULT_DB}）",
    )
    merge_cmd.add_argument(
        "--require",
        action="store_true",
        help="要求 --card 指定的卡有认账合并（必须与 --card 同用）",
    )

    commit_cmd = subparsers.add_parser(
        "commit-audit",
        help="稽核提交身份（author 白名单 + committer 是本 bot；0 无问题 / 1 有问题 / 2 参数或环境错误）",
    )
    commit_cmd.add_argument(
        "--repo",
        default=".",
        metavar="PATH",
        help="要稽核的 git 仓库路径（默认当前目录）",
    )
    commit_cmd.add_argument(
        "--card",
        required=True,
        metavar="CARD",
        help="卡号（必填；判据② 的白名单要读该卡建卡时的 assignee）",
    )
    commit_cmd.add_argument(
        "--rev",
        default="HEAD",
        metavar="REV",
        help="区间终点（默认 HEAD）",
    )
    commit_cmd.add_argument(
        "--main-branch",
        default="main",
        metavar="NAME",
        help="主分支名（区间起点 origin/<NAME>，不存在则退回 refs/heads/<NAME>；默认 main）",
    )
    commit_cmd.add_argument(
        "--db",
        default=DEFAULT_DB,
        metavar="PATH",
        help=f"看板 SQLite 路径，只读（默认 {DEFAULT_DB}）",
    )
    commit_cmd.add_argument(
        "--bots",
        default=None,
        metavar="PATH",
        help="bot 身份表路径（默认 <repo>/.team-board/bots.tsv）",
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
        help="合并提交标题（默认 Merge PR #<pr>（<card>））；空 / 纯空白 / 含换行 → rc2",
    )
    record_cmd.add_argument(
        "--branch",
        default=None,
        metavar="BRANCH",
        help=(
            "该 PR 的 head 分支（形如 wt/t_1234abcd；生成模式必给）。"
            "空串 / 含空白 / 带 refs/ 前缀 → rc2"
        ),
    )
    record_cmd.add_argument(
        "--body-out",
        default=None,
        metavar="PATH",
        help=(
            "把认账 body 原样写入该文件（末尾恰好一个换行）；父目录不存在 / 含换行 → rc2。"
            "生成模式必给（命令里的 --body-file 必须指向真实文件）"
        ),
    )
    record_cmd.add_argument(
        "--repo",
        default=".",
        metavar="PATH",
        help=(
            "仓库路径（默认 .）：只用来拼打印命令里的 cd / PYTHONPATH / --repo 三处，"
            "不校验它是不是 git 仓库（那是 merge_guard 的事）；含换行或冒号 → rc2"
            "（冒号会把 PYTHONPATH 拆成两条路径）"
        ),
    )
    record_cmd.add_argument(
        "--check-file",
        default=None,
        metavar="PATH",
        help="预检模式：校验该文件的认账三字段（不能与 --body-out 同给）",
    )

    body_cmd = subparsers.add_parser(
        "body-audit",
        help="稽核卡正文 body 的字节分布与超限清单（0 无超限 / 1 有超限 / 2 参数或环境错误）",
    )
    body_cmd.add_argument(
        "--db",
        default=DEFAULT_DB,
        metavar="PATH",
        help=f"看板 SQLite 路径，只读（默认 {DEFAULT_DB}）",
    )
    body_cmd.add_argument(
        "--all",
        action="store_true",
        help="连 done 卡一起算（默认只算未结卡：status ∉ {done, archived}）",
    )

    push_cmd = subparsers.add_parser(
        "push-guard",
        help=(
            "pre-push 判定：主 checkout 不许切/推卡分支、worktree 必须与卡分支一致"
            "（0 放行 / 1 拦下 / 2 参数或环境错误）"
        ),
    )
    push_cmd.add_argument(
        "--repo",
        default=".",
        metavar="PATH",
        help="要判定的 git 仓库路径（默认当前目录）",
    )
    push_cmd.add_argument(
        "--main-branch",
        default="main",
        metavar="NAME",
        help="主分支名（默认 main）",
    )
    push_cmd.add_argument(
        "--check-install",
        action="store_true",
        help="只检查 pre-push 钩子是否已装好（不读 stdin）",
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


def _write_stderr_line(line: str) -> None:
    """把**一行**文案（自动补换行）写到 stderr，在任何编码环境下都不崩。

    先问 stderr 的编码能不能表示整行：能就正常写；不能（``PYTHONIOENCODING=ascii``
    等）就按 UTF-8 字节直写 ``stderr.buffer``，避免 ``backslashreplace`` 把中文
    转成 ``\\u9519\\u8bef`` 之类（那会让下游 grep 不到前缀）。
    """

    text = f"{line}\n"
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        data = text.encode("utf-8")
        buffer = getattr(sys.stderr, "buffer", None)
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:  # pragma: no cover - 没有 buffer 的替身对象（极少见）
            sys.stderr.write(data.decode("utf-8", "backslashreplace"))
        return
    sys.stderr.write(text)
    sys.stderr.flush()


def _write_stderr_error(message: str) -> None:
    """把 ``错误: …`` 写到 stderr，**在任何编码环境下都不崩**。

    用户可能跑在 ``LC_ALL=C PYTHONIOENCODING=ascii`` 下：这条消息里有中文，
    直接 ``print(..., file=sys.stderr)`` 会 ``UnicodeEncodeError``（于是 rc1 +
    traceback，跟「发现偏差」同码），或者被 Python 默认的 ``backslashreplace``
    转成 ``\\u9519\\u8bef: …``（那就不是 ``错误: `` 了）。编码兜底交给
    :func:`_write_stderr_line`，前缀只在这里拼一次。
    """

    _write_stderr_line(f"错误: {message}")


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

    给了 ``--card`` 时会连判据①（``merged-by`` 的 profile == 该卡 assignee）一起跑，
    ``--db`` 只决定读哪个看板库取 assignee（只读，默认 ``DEFAULT_DB``）。
    """

    try:
        report = merges.audit(
            args.repo,
            baseline=args.baseline,
            baseline_file=args.baseline_file,
            rev=args.rev,
            only_card=args.card,
            require=args.require,
            db_path=args.db,
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


def _run_commit_audit(args: argparse.Namespace) -> int:
    """``commit-audit`` 子命令：CommitAuditError → stderr ``错误: …`` + rc2，不冒 traceback。

    报告整块生成后再一次性写出：stdout 编码表示不了中文（如
    ``PYTHONIOENCODING=ascii``）属于环境级错误 → rc2、stdout 空、stderr
    ``错误: …``，不许退化成 rc1 + traceback（rc1 是「发现问题」的专用码）。
    """

    try:
        report = commits.audit_commits(
            args.repo,
            card=args.card,
            rev=args.rev,
            main_branch=args.main_branch,
            db_path=args.db,
            bots_path=args.bots,
        )
    except commits.CommitAuditError as exc:
        _write_stderr_error(str(exc))
        return 2

    text = commits.render_report(report)
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
    """整块写 stdout；写不出去时**绝不冒 traceback**，且区分「没人要」与「环境坏」。

    ``TextIOWrapper`` 先整体编码再写出：编码失败时 stdout 里一个字节都没落，
    所以这里是「要么全出、要么不出」，码必须是 rc2（环境错误）而不是 rc1。

    下游提前关管道（`git push … | head -1`、调用方直接关掉 fd1）会让写 stdout 抛
    ``BrokenPipeError``。旧版只 catch ``UnicodeEncodeError``，于是异常一路冒到
    ``main`` 外面：stderr 出 traceback、进程以非 0 非 1/2 的码退出，``.githooks/pre-push``
    把「Traceback」当成「跑不起来」fail-closed —— **合法推送被挡**。现在这类「输出没人
    要了」不再算错误：判定结果本身是好的，照旧返回 ``True``（rc 仍是判定码），并把
    stdout 接到 ``/dev/null``，免得解释器退出时再 flush 一次又炸出
    ``Exception ignored … BrokenPipeError``。
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
    except (BrokenPipeError, OSError):
        # 管道对端已关 / 写不进去：输出目标没了，不是判定错误，也不是可修复的环境
        # 错误（报 rc2 会让钩子按「环境错误」误拦）。把 fd 挪到 /dev/null 后返回 True。
        _detach_stdout()
        return True
    return True


def _detach_stdout() -> None:
    """把 ``sys.stdout`` 底下那个 fd 接到 ``os.devnull``（失败就安静放弃）。

    只为了压掉「退出时最后一次 flush」的 ``Exception ignored … BrokenPipeError``：
    那行会进钩子的 stderr 捕获变量，而钩子按文本判「跑不起来」。没有 ``fileno``
    的替身对象（pytest ``capsys`` 一类）直接跳过。
    """

    try:
        fileno = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
    except (AttributeError, OSError, ValueError):
        return
    try:
        os.dup2(devnull, fileno)
    except OSError:  # pragma: no cover - 极端环境（fd 已被关掉）
        pass
    finally:
        os.close(devnull)


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
    body_out: str,
    branch: str,
    repo: str,
) -> int:
    """生成模式：打印「body 段 + 空行 + 4 行注释段」，并把 body 写进 ``--body-out``。

    注释段 4 行 = 3 行 ``# `` 注释 + 1 行**可直接照抄执行**的 ``merge_guard`` 合并命令
    （该行刻意不加 ``# ``，否则复制出来就不能直接执行）。命令形态对齐 R12 ①：唯一入口
    是 ``merge_guard``，``--method merge`` + ``--delete-branch`` 齐；“钉到提交”的
    ``--match-head-commit`` **不写进这一行**（由 merge_guard 自己从预检的
    ``headRefOid`` 拼），手写进去只会让命令多一个必填项。本函数**不执行**任何 git/gh
    命令。

    ``--subject`` / ``--branch`` / ``--body-file`` / ``--repo``（含 ``cd`` 与
    ``PYTHONPATH`` 两处拼接）**全部**过 :func:`shlex.quote`：标题里带 ``"`` / ``$`` /
    反引号时手写双引号会被 shell 改写，分支名带 ``;`` ``$`` ``|``（``git check-ref-format``
    认的合法分支名）会被 shell 拆成两条命令、退出码被后一条 rc0 覆盖，仓库路径带空格
    （``/mnt/c/Users/…`` 一类）会让 ``cd`` 报「too many arguments」。``--body-file``
    恒打印**绝对路径**：merge_guard 的 ``read_body_file`` 拒相对路径（进程 cwd 与
    ``--repo`` 两套 cwd 会解析成两个不同文件）。

    quote 之外还有三条「一行 / 一个 token」的约束（见 :func:`_subject_argument`、
    :func:`_check_single_line_for_command`、:func:`_check_repo_for_command`）：
    标题以 ``-`` 开头改打 ``--subject=<值>``，``--repo`` / ``--body-out`` 含换行与
    ``--repo`` 含冒号在 ``_run_merge_record`` 就拒（rc2），走到这里时都已是可打印值。
    """

    body = merges.build_message(card=card, by=by, pr=pr, subject=subject)
    title = subject if subject is not None else f"Merge PR #{pr}（{card}）"
    body_path = os.path.realpath(body_out)
    repo_path = os.path.realpath(repo)
    _write_body_file(body_path, body)
    repo_q = shlex.quote(repo_path)
    command = (
        f"cd {repo_q} && PYTHONPATH={shlex.quote(repo_path + '/src')}"
        " python3 -m team_board.merge_guard"
        f" --pr {pr} --branch {shlex.quote(branch)} --method merge --delete-branch"
        f" --card {card} --by {by} {_subject_argument(title)}"
        f" --body-file {shlex.quote(body_path)} --repo {repo_q}"
    )
    lines = [
        body.rstrip("\n"),
        "",
        f"# 卡面认账块：上面 body 段的三行原样贴卡；"
        f"合并完成后用 merge-audit --card {card} --require 回读合并提交 sha 补一行",
        "# 合并命令（本工具不执行；R12 ① 唯一入口 = merge_guard，缺 --delete-branch 会被拒）：",
        command,
        f"# body 文件：{body_path}（已写入）",
    ]
    return 0 if _write_stdout("\n".join(lines) + "\n") else 2


def _check_branch_for_command(branch: str) -> None:
    """生成模式的 ``--branch`` 形态校验：**复用** merge_guard 的那三条规则。

    不复制正则：``merge_guard.check_branch_shape`` 是「裸分支名」的唯一判据（空串 /
    含空白 / 带 ``refs/`` 前缀三种输入各自的错误文案都在那里）。这里只把它的
    ``MergeGuardError`` 折成本子命令既有的 ``MergeAuditError`` 口径（stderr
    ``错误: …`` + rc2）。惰性 import：``merge_guard`` 只依赖 ``team_board.merges``，
    无循环导入，但本项目只在真要拼命令时才加载它。
    """

    from . import merge_guard

    try:
        merge_guard.check_branch_shape(branch)
    except merge_guard.MergeGuardError as exc:
        raise merges.MergeAuditError(str(exc)) from exc


def _check_subject_for_command(subject: str) -> None:
    """生成模式的 ``--subject`` 形态校验：**复用** merge_guard 的 ``check_subject``。

    只多一条约束：含换行（``\\n`` / ``\\r``）时打印出来的命令会被切成两行 —— 照抄前半
    行是半条命令、照抄整段又会被 shell 当两条命令。``shlex.quote`` 只能保住参数字面，
    保不住「一行」。merge_guard 自己的 ``check_subject`` 本来就拒换行（同一判据），
    这里提前到生成阶段拒，省得先打印一条半条命令再让用户去撞 merge_guard 的 rc2。
    """

    from . import merge_guard

    try:
        merge_guard.check_subject(subject)
    except merge_guard.MergeGuardError as exc:
        raise merges.MergeAuditError(str(exc)) from exc


#: 打印行里「一行」这条硬约束的字符集：与 ``merge_guard.check_subject`` 拒的是同一组
#: （``\\n`` / ``\\r``）。这里不 import 它来判 ``--repo`` / ``--body-out``：那个函数还
#: 顺带拒空串与纯空白，而这两处「空值」各有自己的文案（``--body-out`` 的路径为空 /
#: ``--repo`` 的路径为空），套上去会把参数名说错。判据本身由
#: ``tests/integration/test_cli_merge_record.py`` 的对照用例锁住（同一批取值两边必须
#: 同判），改一边不改另一边会红。
_NEWLINE_CHARS = ("\n", "\r")


def _check_single_line_for_command(value: str, flag: str) -> None:
    """``--repo`` / ``--body-out`` 的值必须**单行**：含 ``\\n`` / ``\\r`` → rc2。

    这一行的末尾是 ``--repo …``，值里带换行时打印出来的命令被截成两行：前半行引号
    未闭合（``shlex.split`` 直接 ``No closing quotation``）、后半行是裸露的路径碎片。
    照抄前半行 = 半条命令；照抄整段 = shell 当两条命令，退出码被后一条覆盖。
    ``shlex.quote`` 保得住参数字面，保不住「一行」—— 与 ``--subject`` 的换行是同一类。
    """

    for index, char in enumerate(value):
        if char in _NEWLINE_CHARS:
            raise merges.MergeAuditError(
                f"{flag} {value!r} 不能含换行字符（第 {index + 1} 个字符是 {char!r}）："
                "打印出来的合并命令会被截成两行，照抄即半条命令（引号未闭合）"
            )


def _check_repo_for_command(repo: str) -> None:
    """``--repo`` 不能含 ``:``：``PYTHONPATH=…`` 是冒号分隔的，单条路径里的冒号拆不开。

    打印行的前半段是 ``cd <repo> && PYTHONPATH=<repo>/src python3 -m …``。
    ``PYTHONPATH`` 用冒号分隔多条路径，且**没有**转义机制 —— ``/tmp/a:b`` 会被 Python
    拆成 ``/tmp/a`` 与 ``b/src`` 两条，首条里没有 ``team_board``，照抄即 ``rc=1``
    「No module named team_board」。rc1 是「认账未达成」专用码，会被读成合并失败，
    所以宁可生成阶段拒（rc2，参数错误），不打印一条注定跑不通的命令。
    判据是打印形态决定的，与路径本身合不合法无关：``merge_guard --repo`` 自己吃 ``:``
    没问题，坏的是这一行里的 ``PYTHONPATH=``。调用方传进来的是 ``os.path.realpath``
    之后的值（打印行里写的就是它），所以「cwd 自带冒号、``--repo .``」这条路也拦得住。
    """

    if ":" in repo:
        raise merges.MergeAuditError(
            f"--repo {repo!r} 不能含冒号：打印行里的 PYTHONPATH=<repo>/src 是冒号分隔的，"
            "含冒号会被拆成两条路径 → 照抄即 rc1「No module named team_board」"
            "（rc1 会被读成合并失败）。请换一个不含冒号的仓库路径"
        )


def _subject_argument(subject: str) -> str:
    """把标题拼成打印行里的一个参数：以 ``-`` 开头时用 ``--subject=<quoted>`` 形式。

    ``shlex.quote('-h')`` 原样返回 ``-h``，于是 ``--subject -h`` 里的 ``-h`` 会被
    argparse 当成选项 → 照抄即 ``错误: argument --subject: expected one argument``（rc2）。
    argparse 认 ``--opt=value`` 形式，且 ``=`` 右边的值**不参与选项解析**，换行不受影响
    （换行已在 :func:`_check_subject_for_command` 拒掉）。其余取值保持
    ``--subject <quoted>``：现有断言与文档样例都是这个形态，不为一个边缘取值换形态。
    """

    quoted = shlex.quote(subject)
    if subject.startswith("-"):
        return f"--subject={quoted}"
    return f"--subject {quoted}"


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
                text = "通过：三字段齐（merged-by/merge-card/merge-pr）\n"
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
        if args.branch is None:
            raise merges.MergeAuditError(
                "生成合并命令必须给 --branch（该 PR 的 head 分支；"
                "R6 要求 headRefName == wt/<卡号>）"
            )
        _check_branch_for_command(args.branch)
        if args.subject is not None:
            _check_subject_for_command(args.subject)
        if args.body_out is None:
            raise merges.MergeAuditError(
                "生成合并命令必须给 --body-out（命令里的 --body-file 必须指向一个"
                "真实存在的文件，否则照抄即 rc2「--body-file 文件不存在」）"
            )
        _check_single_line_for_command(os.path.realpath(args.body_out), "--body-out")
        # 打印行里出现的是 realpath 结果（不是用户原样给的相对路径），所以判据也落在
        # 解析后的值上：cwd 本身带冒号 / 换行时，`--repo .` 拼出来的 PYTHONPATH 一样会坏。
        repo_abs = os.path.realpath(args.repo)
        _check_single_line_for_command(repo_abs, "--repo")
        _check_repo_for_command(repo_abs)
        return _record_generate(
            card=card,
            by=by,
            pr=pr,
            subject=args.subject,
            body_out=args.body_out,
            branch=args.branch,
            repo=args.repo,
        )
    except merges.MergeAuditError as exc:
        _write_stderr_error(str(exc))
        return 2


def _run_body_audit(args: argparse.Namespace) -> int:
    """``body-audit`` 子命令：读失败一律 stderr 一行 ``错误: …`` + rc2。

    三个码的分工：0 无超限 / 1 有超限 / 2 参数或环境错误。库不可用时 stdout
    **一个字节都不写**（报告整块在库读成功之后才拼），所以 rc2 与 rc1 不会混。
    """

    conn = None
    try:
        conn = db.connect(args.db)
        rows = db.iter_body_rows(conn, include_done=args.all)
    except (db.BoardError, OSError, sqlite3.Error) as exc:
        _write_stderr_error(str(exc))
        return 2
    finally:
        if conn is not None:
            conn.close()

    summary = bodylimit.summarize(rows)
    scope = bodylimit.SCOPE_ALL if args.all else bodylimit.SCOPE_PENDING
    text = bodylimit.render(summary, db_path=args.db, scope=scope)
    if not _write_stdout(text + "\n"):
        return 2
    return 1 if summary.counts["over"] else 0


def _push_guard_check_install(args: argparse.Namespace) -> int:
    """``push-guard --check-install``：钩子装好 rc0 / 没装好 rc1 / 参数环境错 rc2。

    判据（三个都要满足）：``core.hooksPath`` 恰好是 ``.githooks``、
    ``<repo>/.githooks/pre-push`` 存在、且可执行。**不读 stdin**（同给 stdin 也
    忽略，不退化 rc2）。
    """

    try:
        pushguard.require_repo(args.repo)
        root = Path(
            branches._must_git(
                args.repo, "rev-parse", "--show-toplevel", what="定位仓库失败"
            ).strip()
        ).resolve()
    except branches.AuditError as exc:
        _write_stderr_error(str(exc))
        return 2

    hook = root / ".githooks" / "pre-push"
    proc = branches._git(root, "config", "--get", "core.hooksPath")
    hooks_path = proc.stdout.strip() if proc.returncode == 0 else ""
    if hooks_path != ".githooks":
        reason = (
            "core.hooksPath 未设置" if hooks_path == "" else f"core.hooksPath={hooks_path}"
        )
    elif not hook.exists():
        reason = f"{hook} 不存在"
    elif not os.access(hook, os.X_OK):
        reason = f"{hook} 不可执行"
    else:
        if not _write_stdout(f"已安装：core.hooksPath=.githooks（{hook}）\n"):
            return 2
        return 0

    _write_stderr_line(f"未安装：{reason}；修复：git config core.hooksPath .githooks")
    return 1


def _run_push_guard(args: argparse.Namespace) -> int:
    """``push-guard`` 子命令：0 放行 / 1 拦下 / 2 参数或环境错误，不冒 traceback。

    - 不带 ``--check-install``：从 **stdin** 读全部内容（git 把
      ``<local_ref> <local_sha> <remote_ref> <remote_sha>`` 逐行喂给 pre-push
      钩子），解析后交给 :func:`pushguard.judge`；
    - 拦截时 stdout 必须为空（只往 stderr 写，每处一行以 ``拒绝：`` 开头），
      放行时 stdout 恰一行。
    """

    if args.check_install:
        return _push_guard_check_install(args)

    try:
        text = sys.stdin.read()
        refs = pushguard.parse_pre_push_lines(text)
        verdicts = pushguard.judge(args.repo, refs, main_branch=args.main_branch)
    except branches.AuditError as exc:
        _write_stderr_error(str(exc))
        return 2
    except (UnicodeDecodeError, OSError) as exc:
        # stdin 不是 UTF-8（严格解码失败）/ 读 stdin 出错：都是环境问题 → rc2
        _write_stderr_error(f"读取 stdin 失败: {exc}")
        return 2

    if any(verdict.blocked for verdict in verdicts):
        for verdict in verdicts:
            _write_stderr_line(verdict.message)
        return 1

    if not _write_stdout(verdicts[0].message + "\n"):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "branch-audit":
        return _run_branch_audit(args)

    if args.command == "merge-audit":
        return _run_merge_audit(args)

    if args.command == "commit-audit":
        return _run_commit_audit(args)
    if args.command == "merge-record":
        return _run_merge_record(args)

    if args.command == "body-audit":
        return _run_body_audit(args)

    if args.command == "push-guard":
        return _run_push_guard(args)

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
