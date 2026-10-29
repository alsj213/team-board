"""合并闸门：合并命令必须带 ``--delete-branch``，合并后必须确认远端源分支真的消失。

为什么要有这个模块
------------------
历史合并（PR #2–#14）全是「合完就走」，源分支堆在远端：板上看不出哪些卡的分支已经
合过，稽核又把已合并的旧分支当成「卡还在途」。靠自觉不行，所以把两件事做成机器判定：

1. **入口**：命令行没有 ``--delete-branch`` 就拒绝（rc2），一个 gh 写操作都不发。
   本工具**不提供**不带删除的合并路径 —— 没有 ``--no-delete-branch`` /
   ``--keep-branch``，也没有 ``--admin`` / ``--auto`` / ``--force`` 这类绕过 PR
   检查的口子。
2. **出口**：合并后只认 ``git ls-remote --exit-code --refs`` 的退出码：``0`` =
   分支仍在远端、``2`` = 没有这个 ref（已删）、其它 = 判不了。分支还在就是 rc1，
   **绝不当成合并成功**；gh 自己的 stderr 警告（如 worktree 里
   ``failed to delete local branch``）不算失败，判定只看远端。

判定器不认识 PR 的「合并方式」（merge / squash / rebase）；本工具要求必传
``--method`` 但不设默认值，由调用方（合并卡）决定。

退出码
------
- ``0``：合并完成，且已确认远端源分支删除（``--verify-only`` 时为「分支已删除」）。
- ``1``：未达成 —— gh 合并失败 / 合并成功但源分支仍在远端 / ``--verify-only``
  时分支仍在远端或无法判定。
- ``2``：参数或环境错误：缺 ``--delete-branch``、``--pr`` / ``--timeout`` /
  ``--interval`` 不是 ≥1 的 ASCII 十进制整数（``--pr`` 上限 999999999）、
  ``--branch`` 是 ``main`` / ``master`` 或等于该 PR 的 ``baseRefName``、
  ``--branch`` 与 PR 的 ``headRefName`` 不一致、PR 不是 ``OPEN``、``PATH`` 里
  没有 ``gh`` / ``git``、gh/git 输出不是 UTF-8、stdout 编码表示不了中文、
  合并后通信持续失败导致无法判定。rc2 一律 stderr 一行 ``错误: …``、stdout 空、
  不冒 traceback（崩溃绝不落到 rc1 —— rc1 是「未达成」专用码）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

#: ``gh pr merge`` 接受的三种合并方式（本工具不给默认值，必须由调用方传）
METHODS: tuple[str, ...] = ("merge", "squash", "rebase")

#: 永远不许删的主分支名
MAIN_BRANCHES: frozenset[str] = frozenset({"main", "master"})

#: ``--pr`` 的上限：PR 号是小正整数；13 位毫秒时间戳这类误用必须被拒
MAX_PR = 999_999_999

STEP_PREFLIGHT = "预检"
STEP_MERGE = "合并"
STEP_DELETE = "分支删除"

OUT_DELETED = "deleted"
OUT_PRESENT = "present"
OUT_UNDETERMINED = "undetermined"

PROBE_PRESENT = OUT_PRESENT
PROBE_DELETED = OUT_DELETED
PROBE_ERROR = "error"

#: 缺 ``--delete-branch`` 时的固定文案（stderr 前面再加 ``错误: ``）
MISSING_DELETE_BRANCH = (
    "合并命令缺少 --delete-branch（合并卡必须带；本工具不提供不带删除的合并路径）"
)

#: 只认 ASCII 十进制：``int()`` 认空白、正负号、下划线与非 ASCII 数字
_DIGITS = re.compile(r"[0-9]+", re.ASCII)

_PR_VIEW_FIELDS = "number,state,headRefName,baseRefName"


class MergeGuardError(Exception):
    """参数 / 环境级错误，CLI 转 rc2（stderr ``错误: …``，stdout 空）。"""


@dataclass(frozen=True)
class CommandResult:
    """一次外部命令的原始结果。"""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def command(self) -> str:
        """命令原文（用于输出行里的 ``<命令原文>`` 段）。"""

        return " ".join(self.argv)


@dataclass(frozen=True)
class Preflight:
    """``gh pr view`` 里本工具关心的四个字段。"""

    number: int
    state: str
    head_ref: str
    base_ref: str


@dataclass(frozen=True)
class PollResult:
    """一次「远端分支是否已删」的轮询结果。"""

    outcome: str
    probes: int
    returncode: int
    key: str
    reason: str = ""


# --------------------------------------------------------------------- 输出

def _write_stderr_error(message: str) -> None:
    """把 ``错误: …`` 写到 stderr，**在任何编码环境下都不崩**。

    与 ``cli._write_stderr_error`` 同款做法（刻意不 import cli：本模块独立可跑）：
    先问 stderr 的编码能不能表示整行，能就正常写，不能就按 UTF-8 字节直写
    ``stderr.buffer``，避免 ``UnicodeEncodeError`` 变成 traceback。
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


def _write_stderr_raw(text: str) -> None:
    """把外部命令的输出原文写 stderr（合并失败时要看原文）。"""

    if not text:
        return
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        data = text.encode("utf-8")
        buffer = getattr(sys.stderr, "buffer", None)
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:  # pragma: no cover
            sys.stderr.write(data.decode("utf-8", "backslashreplace"))
        return
    sys.stderr.write(text)
    if not text.endswith("\n"):
        sys.stderr.write("\n")
    sys.stderr.flush()


def _write_stdout(lines: list[str]) -> None:
    """一次性写出所有输出行；编码表示不了中文 → ``MergeGuardError``（rc2）。"""

    text = "\n".join(lines) + "\n"
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError as exc:
        raise MergeGuardError(
            "当前 stdout 编码 "
            f"{getattr(sys.stdout, 'encoding', None)!r} 无法表示输出里的中文，"
            "请改用 UTF-8（例如 PYTHONIOENCODING=utf-8 / LC_ALL=C.UTF-8）"
        ) from exc


def key_line(proc: CommandResult) -> str:
    """取一行「关键输出」：先 stdout 后 stderr 的最后一条非空行。"""

    for text in (proc.stdout, proc.stderr):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return "(无输出)"


def format_step(step: str, *, ok: bool, command: str, rc: int, key: str) -> str:
    """``<步骤> | <PASS|FAIL> | <命令原文> | rc=<n> | <关键输出一行>``。"""

    status = "PASS" if ok else "FAIL"
    return f"{step} | {status} | {command} | rc={rc} | {key}"


# --------------------------------------------------------------- 外部命令

def _run(argv: tuple[str, ...], *, cwd: str) -> CommandResult:
    """跑外部命令，不抛 CalledProcessError，交给调用方判 rc。

    ``PATH`` 里没有可执行文件、输出不是 UTF-8、其它 ``OSError`` → ``MergeGuardError``
    （rc2），不许冒 traceback，也不许落到 rc1。
    """

    try:
        # bandit B603/B607 假阳性：可执行文件名写死（"gh" / "git"，走 PATH），
        # 参数字符串以 argv 列表传入、shell=False，用户给的路径/分支名只作为独立
        # argv 元素，不经 shell 解释，无注入面
        proc = subprocess.run(  # nosec B603 B607
            list(argv),
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
    except FileNotFoundError as exc:
        raise MergeGuardError(
            f"找不到 {argv[0]} 可执行文件（PATH 里没有 {argv[0]}），无法合并/验证"
        ) from exc
    except UnicodeDecodeError as exc:
        raise MergeGuardError(
            f"{argv[0]} 输出不是 UTF-8，无法解析（第 {exc.start} 字节起解码失败）"
        ) from exc
    except OSError as exc:
        raise MergeGuardError(f"无法执行 {argv[0]}: {exc}") from exc
    return CommandResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def ls_remote_argv(repo: str, remote: str, branch: str) -> tuple[str, ...]:
    """判定「远端分支是否已删」的唯一命令。"""

    return (
        "git",
        "-C",
        repo,
        "ls-remote",
        "--exit-code",
        "--refs",
        remote,
        f"refs/heads/{branch}",
    )


def make_probe(repo: str, remote: str, branch: str) -> Callable[[], CommandResult]:
    """返回一个无参探针：每次调用跑一次 ``git ls-remote``。"""

    def probe() -> CommandResult:
        return _run(ls_remote_argv(repo, remote, branch), cwd=repo)

    return probe


def classify_probe_rc(rc: int) -> str:
    """``ls-remote --exit-code`` 的退出码 → 三种判定。

    - ``0``：ref 存在 → 分支仍在远端
    - ``2``：没有这个 ref → 已删（``--exit-code`` 的语义）
    - 其它：通信 / 仓库错误 → 判不了，记一次继续轮询
    """

    if rc == 0:
        return PROBE_PRESENT
    if rc == 2:
        return PROBE_DELETED
    return PROBE_ERROR


def poll_branch_deleted(
    probe: Callable[[], CommandResult],
    *,
    timeout: int,
    interval: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> PollResult:
    """轮询 ``probe`` 直到确认分支已删或超时；总等待不超过 ``timeout`` 秒。"""

    deadline = clock() + timeout
    probes = 0
    errors = 0
    while True:
        proc = probe()
        probes += 1
        kind = classify_probe_rc(proc.returncode)
        if kind == PROBE_DELETED:
            return PollResult(
                outcome=OUT_DELETED,
                probes=probes,
                returncode=proc.returncode,
                key="分支已不在远端",
            )
        if kind == PROBE_ERROR:
            errors += 1
        remaining = deadline - clock()
        if remaining <= 0:
            if kind == PROBE_PRESENT:
                return PollResult(
                    outcome=OUT_PRESENT,
                    probes=probes,
                    returncode=proc.returncode,
                    key=key_line(proc),
                )
            return PollResult(
                outcome=OUT_UNDETERMINED,
                probes=probes,
                returncode=proc.returncode,
                key=key_line(proc),
                reason=(
                    f"共 {probes} 次检查、其中 {errors} 次通信错误，"
                    f"最后一次退出码 {proc.returncode}"
                ),
            )
        sleep(min(float(interval), remaining))


# --------------------------------------------------------------- 参数校验

def parse_positive_int(flag: str, raw: str, *, maximum: int | None = None) -> int:
    """``flag`` 的值必须是 ≥1 的 ASCII 十进制整数（可带 ``maximum`` 上限）。"""

    if not _DIGITS.fullmatch(raw):
        raise MergeGuardError(
            f"{flag} 必须是 ≥1 的 ASCII 十进制整数，实际是 {raw!r}"
        )
    value = int(raw)
    if value < 1:
        raise MergeGuardError(f"{flag} 必须 ≥1，实际是 {raw!r}")
    if maximum is not None and value > maximum:
        raise MergeGuardError(
            f"{flag} 超出取值范围（1–{maximum}），实际是 {raw!r}"
        )
    return value


def check_branch_is_not_main(branch: str) -> None:
    """拒绝 ``main`` / ``master``（合并后删除会毁掉主分支）。"""

    if branch in MAIN_BRANCHES:
        raise MergeGuardError(
            f"--branch {branch} 是主分支，拒绝合并（合并后删除会毁掉主分支）"
        )


def parse_pr_view(proc: CommandResult, pr: int) -> Preflight:
    """解析 ``gh pr view --json number,state,headRefName,baseRefName``。"""

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MergeGuardError(
            f"gh pr view {pr} 的输出不是 JSON，无法解析: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise MergeGuardError(f"gh pr view {pr} 的输出不是 JSON 对象，无法解析")
    try:
        number = payload["number"]
        state = payload["state"]
        head_ref = payload["headRefName"]
        base_ref = payload["baseRefName"]
    except KeyError as exc:
        raise MergeGuardError(
            f"gh pr view {pr} 的输出缺少字段 {exc.args[0] if exc.args else '?'}"
        ) from exc
    if not isinstance(number, int) or isinstance(number, bool):
        raise MergeGuardError(f"gh pr view {pr} 的 number 不是整数: {number!r}")
    if not isinstance(state, str) or not isinstance(head_ref, str) or not isinstance(
        base_ref, str
    ):
        raise MergeGuardError(f"gh pr view {pr} 的 state/headRefName/baseRefName 不是字符串")
    return Preflight(number=number, state=state, head_ref=head_ref, base_ref=base_ref)


# ------------------------------------------------------------------ 命令行

class _Parser(argparse.ArgumentParser):
    """argparse 的报错也走 ``MergeGuardError``：rc2 只留一行 ``错误: …``。"""

    def error(self, message: str) -> NoReturn:
        raise MergeGuardError(message)


def build_parser() -> argparse.ArgumentParser:
    """本工具的命令行定义（没有关掉删除的开关，也没有绕过 PR 检查的口子）。"""

    parser = _Parser(
        prog="python3 -m team_board.merge_guard",
        description=(
            "合并闸门：合并命令必须带 --delete-branch，"
            "并在合并后用 git ls-remote 机器确认远端源分支真的消失。"
        ),
    )
    parser.add_argument(
        "--pr", required=True, metavar="N", help=f"PR 号（1–{MAX_PR} 的整数）"
    )
    parser.add_argument(
        "--branch",
        required=True,
        metavar="NAME",
        help="PR 的 head 分支名（必须与 PR 的 headRefName 逐字一致）",
    )
    parser.add_argument(
        "--method",
        required=True,
        choices=METHODS,
        help="合并方式（本工具不设默认值，由合并卡决定）",
    )
    parser.add_argument(
        "--delete-branch",
        action="store_true",
        help="必填：合并后删除远端源分支（缺了就拒绝，本工具没有不删的路径）",
    )
    parser.add_argument("--repo", default=".", metavar="PATH", help="仓库路径（默认 .）")
    parser.add_argument("--remote", default="origin", metavar="NAME", help="远端名（默认 origin）")
    parser.add_argument(
        "--timeout", default="60", metavar="SEC", help="删除验证的最长等待秒数（默认 60）"
    )
    parser.add_argument(
        "--interval", default="2", metavar="SEC", help="删除验证的轮询间隔秒数（默认 2）"
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="不合并，只验证远端分支是否已删（巡检 / 取红样例用）",
    )
    return parser


@dataclass(frozen=True)
class Options:
    """校验通过的参数集合。"""

    pr: int
    branch: str
    method: str
    repo: str
    remote: str
    timeout: int
    interval: int
    verify_only: bool


def _parse(argv: list[str] | None) -> argparse.Namespace:
    """argparse 解析（``--help`` 仍然是 argparse 自己那套，rc0）。"""

    return build_parser().parse_args(argv)


def validate(args: argparse.Namespace) -> Options:
    """把 argparse 的结果校验成 ``Options``；任何越界 → rc2。"""

    if not args.delete_branch:
        raise MergeGuardError(MISSING_DELETE_BRANCH)
    pr = parse_positive_int("--pr", args.pr, maximum=MAX_PR)
    branch = args.branch
    check_branch_is_not_main(branch)
    timeout = parse_positive_int("--timeout", args.timeout)
    interval = parse_positive_int("--interval", args.interval)
    repo = args.repo
    if not os.path.isdir(repo):
        raise MergeGuardError(f"仓库路径不存在或不是目录: {repo}")
    method = args.method
    if method not in METHODS:
        raise MergeGuardError(
            f"--method 只接受 {'/'.join(METHODS)}，实际是 {method!r}"
        )
    return Options(
        pr=pr,
        branch=branch,
        method=method,
        repo=repo,
        remote=args.remote,
        timeout=timeout,
        interval=interval,
        verify_only=bool(args.verify_only),
    )


# -------------------------------------------------------------------- 主流程

def _run_verify_only(opts: Options) -> tuple[list[str], int]:
    """``--verify-only``：只验证「远端分支是否已删」，一次 gh 都不调。"""

    result = poll_branch_deleted(
        make_probe(opts.repo, opts.remote, opts.branch),
        timeout=opts.timeout,
        interval=opts.interval,
    )
    lines = [
        format_step(
            STEP_DELETE,
            ok=result.outcome == OUT_DELETED,
            command=" ".join(ls_remote_argv(opts.repo, opts.remote, opts.branch)),
            rc=result.returncode,
            key=result.key,
        )
    ]
    if result.outcome == OUT_DELETED:
        lines.append("结论：分支已删除")
        return lines, 0
    if result.outcome == OUT_PRESENT:
        lines.append("结论：分支仍在远端")
        return lines, 1
    lines.append(f"结论：无法判定（{result.reason}）")
    return lines, 1


def _run_merge(opts: Options) -> tuple[list[str], int]:
    """预检 → 合并 → 删除验证。"""

    view_argv = (
        "gh",
        "pr",
        "view",
        str(opts.pr),
        "--json",
        _PR_VIEW_FIELDS,
    )
    view = _run(view_argv, cwd=opts.repo)
    if view.returncode != 0:
        raise MergeGuardError(
            f"gh pr view {opts.pr} 失败（rc={view.returncode}）: {key_line(view)}"
        )
    info = parse_pr_view(view, opts.pr)
    if info.state != "OPEN":
        raise MergeGuardError(
            f"PR {opts.pr} 的状态是 {info.state}，只有 OPEN 的 PR 能合并"
        )
    if info.head_ref != opts.branch:
        raise MergeGuardError(
            f"--branch {opts.branch} 与 PR {opts.pr} 的 headRefName "
            f"{info.head_ref} 不一致"
        )
    if opts.branch == info.base_ref:
        raise MergeGuardError(
            f"--branch {opts.branch} 等于 PR {opts.pr} 的 baseRefName，"
            "拒绝合并（合并后删除会毁掉主分支）"
        )

    lines = [
        format_step(
            STEP_PREFLIGHT,
            ok=True,
            command=view.command,
            rc=view.returncode,
            key=(
                f"state={info.state}, headRefName={info.head_ref}, "
                f"baseRefName={info.base_ref}"
            ),
        )
    ]

    merge_argv = (
        "gh",
        "pr",
        "merge",
        str(opts.pr),
        f"--{opts.method}",
        "--delete-branch",
    )
    merge = _run(merge_argv, cwd=opts.repo)
    if merge.returncode != 0:
        # gh 返回非 0：把 gh 的输出原文写出来，rc1，且不许再跑删除验证
        _write_stderr_raw(merge.stdout)
        _write_stderr_raw(merge.stderr)
        lines.append(
            format_step(
                STEP_MERGE,
                ok=False,
                command=merge.command,
                rc=merge.returncode,
                key=key_line(merge),
            )
        )
        lines.append(
            f"结论：合并失败（gh pr merge 退出码 {merge.returncode}，"
            "未做分支删除验证）"
        )
        return lines, 1

    lines.append(
        format_step(
            STEP_MERGE,
            ok=True,
            command=merge.command,
            rc=merge.returncode,
            key=key_line(merge),
        )
    )

    result = poll_branch_deleted(
        make_probe(opts.repo, opts.remote, opts.branch),
        timeout=opts.timeout,
        interval=opts.interval,
    )
    lines.append(
        format_step(
            STEP_DELETE,
            ok=result.outcome == OUT_DELETED,
            command=" ".join(ls_remote_argv(opts.repo, opts.remote, opts.branch)),
            rc=result.returncode,
            key=result.key,
        )
    )
    if result.outcome == OUT_DELETED:
        lines.append(
            f"结论：合并完成，源分支 {opts.branch} 已从 {opts.remote} 删除"
        )
        return lines, 0
    if result.outcome == OUT_PRESENT:
        lines.append("结论：合并成功但源分支仍在远端（合并不算完成）")
        return lines, 1
    # 合并已经发生，但「分支是否真的删了」判不了 —— 明确未通过，绝不当绿
    raise MergeGuardError(
        f"合并已完成，但无法判定远端分支 {opts.branch} 是否已删除"
        f"（{result.reason}）；请人工核对"
    )


def run(args: argparse.Namespace) -> tuple[list[str], int]:
    """校验参数并执行；返回（要打到 stdout 的行, 退出码）。"""

    opts = validate(args)
    if opts.verify_only:
        return _run_verify_only(opts)
    return _run_merge(opts)


def main(argv: list[str] | None = None) -> int:
    """``python -m team_board.merge_guard`` 入口。"""

    try:
        args = _parse(argv)
    except MergeGuardError as exc:
        _write_stderr_error(str(exc))
        return 2
    except SystemExit as exc:  # --help / --version：交给 argparse 的退出码
        code = exc.code
        return code if isinstance(code, int) else 2

    try:
        lines, rc = run(args)
    except MergeGuardError as exc:
        _write_stderr_error(str(exc))
        return 2

    try:
        _write_stdout(lines)
    except MergeGuardError as exc:
        _write_stderr_error(str(exc))
        return 2
    return rc


if __name__ == "__main__":  # pragma: no cover - 由 python -m 触发
    raise SystemExit(main())
