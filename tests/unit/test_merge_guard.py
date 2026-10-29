"""merge_guard 的纯逻辑单测：判定、轮询、参数校验、输出格式（不联网、不调 gh）。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable

import pytest

from team_board import merge_guard as mg


class FakeClock:
    """可控时钟：``sleep`` 只推进自己的时间，测试不真睡。"""

    def __init__(self) -> None:
        self.now_value = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.now_value

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now_value += seconds


def make_probe(*rcs: int) -> tuple[Callable[[], mg.CommandResult], list[int]]:
    """按顺序返回给定退出码的假探针（最后一个值会重复使用）。"""

    queue = list(rcs)
    calls: list[int] = []

    def probe() -> mg.CommandResult:
        rc = queue.pop(0) if len(queue) > 1 else queue[0]
        calls.append(rc)
        return mg.CommandResult(
            argv=("git", "-C", ".", "ls-remote"),
            returncode=rc,
            stdout="c5768d4\trefs/heads/wt/t_bbeab29f\n" if rc == 0 else "",
            stderr="fatal: boom\n" if rc not in (0, 2) else "",
        )

    return probe, calls


# ------------------------------------------------------- classify_probe_rc

@pytest.mark.parametrize(
    ("rc", "expected"),
    [
        (0, mg.OUT_PRESENT),  # ref 存在 → 分支仍在远端
        (2, mg.OUT_DELETED),  # --exit-code 的「没有这个 ref」
        (128, mg.PROBE_ERROR),
        (124, mg.PROBE_ERROR),
        (1, mg.PROBE_ERROR),
        (255, mg.PROBE_ERROR),
    ],
)
def test_classify_probe_rc(rc: int, expected: str) -> None:
    assert mg.classify_probe_rc(rc) == expected


# ------------------------------------------------------ poll_branch_deleted

def test_poll_deleted_on_first_probe() -> None:
    probe, calls = make_probe(2)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=10, interval=2, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_DELETED
    assert result.probes == 1
    assert result.returncode == 2
    assert calls == [2]
    assert clock.slept == []  # 第一次就判到删除，不该再睡


def test_poll_present_until_timeout() -> None:
    probe, calls = make_probe(0)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=5, interval=2, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_PRESENT
    assert result.returncode == 0
    assert "refs/heads/wt/t_bbeab29f" in result.key
    assert len(calls) == result.probes == 4  # t=0,2,4,5
    assert sum(clock.slept) <= 5  # 总等待不超过 --timeout
    assert clock.slept == [2.0, 2.0, 1.0]  # 最后一次只睡剩余时间


def test_poll_interval_longer_than_timeout_waits_only_timeout() -> None:
    """函数层：间隔比总预算还长时只睡剩余时间。

    CLI 层另有硬约束 ``interval <= timeout``（``--interval`` 直接 rc2，见集成测试）；
    这里保留轮询器自身的「别睡过总预算」行为。
    """

    probe, _ = make_probe(0)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=3, interval=10, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_PRESENT
    assert clock.slept == [3.0]


def test_poll_error_then_deleted_is_deleted() -> None:
    probe, calls = make_probe(128, 2)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=10, interval=1, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_DELETED
    assert calls == [128, 2]
    assert clock.slept == [1.0]


def test_poll_only_errors_is_undetermined() -> None:
    probe, calls = make_probe(128)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=2, interval=1, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_UNDETERMINED
    assert result.returncode == 128
    assert "128" in result.reason
    assert "通信错误" in result.reason
    assert len(calls) == result.probes == 3  # t=0,1,2
    assert sum(clock.slept) <= 2


def test_poll_single_shot_timeout() -> None:
    """timeout=1 / interval=1：不真睡超过 1 秒，最多两次探针。"""

    probe, calls = make_probe(0)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=1, interval=1, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_PRESENT
    assert len(calls) == 2
    assert sum(clock.slept) <= 1


# ------------------------------------------------------- parse_positive_int

@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "1_000", "", " ", " 12", "12 ", "abc", "1.5", "+5", "١٢٣", "0x10", "１２"],
)
def test_parse_positive_int_rejects(raw: str) -> None:
    with pytest.raises(mg.MergeGuardError):
        mg.parse_positive_int("--timeout", raw)


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("60", 60), ("007", 7)])
def test_parse_positive_int_accepts(raw: str, expected: int) -> None:
    assert mg.parse_positive_int("--timeout", raw) == expected


def test_parse_pr_upper_bound() -> None:
    assert mg.parse_positive_int("--pr", "999999999", maximum=mg.MAX_PR) == 999_999_999
    with pytest.raises(mg.MergeGuardError):
        mg.parse_positive_int("--pr", "1000000000", maximum=mg.MAX_PR)
    with pytest.raises(mg.MergeGuardError):
        # 13 位毫秒时间戳是典型的误用
        mg.parse_positive_int("--pr", "1793160000000", maximum=mg.MAX_PR)


# --------------------------------------------------------- 主分支与安全

@pytest.mark.parametrize("branch", ["main", "master"])
def test_main_branch_rejected(branch: str) -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.check_branch_is_not_main(branch)
    assert "主分支" in str(excinfo.value)


@pytest.mark.parametrize("branch", ["wt/t_bbeab29f", "feature-x", "Main", "main2"])
def test_non_main_branch_accepted(branch: str) -> None:
    mg.check_branch_is_not_main(branch)  # 不抛异常即通过


# ------------------------------------------------------------ parse_pr_view

def _view_proc(payload: str, rc: int = 0) -> mg.CommandResult:
    return mg.CommandResult(argv=("gh", "pr", "view"), returncode=rc, stdout=payload, stderr="")


def test_parse_pr_view_ok() -> None:
    payload = json.dumps(
        {
            "number": 9,
            "state": "OPEN",
            "headRefName": "wt/t_f43fa954",
            "baseRefName": "main",
        }
    )
    info = mg.parse_pr_view(_view_proc(payload), 9)

    assert info.number == 9
    assert info.state == "OPEN"
    assert info.head_ref == "wt/t_f43fa954"
    assert info.base_ref == "main"


def test_parse_pr_view_not_json() -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.parse_pr_view(_view_proc("no such PR"), 99)
    assert "不是 JSON" in str(excinfo.value)


def test_parse_pr_view_json_not_object() -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.parse_pr_view(_view_proc("[1, 2]"), 99)
    assert "不是 JSON 对象" in str(excinfo.value)


def test_parse_pr_view_missing_field() -> None:
    payload = json.dumps({"number": 9, "state": "OPEN", "headRefName": "wt/t_x"})
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.parse_pr_view(_view_proc(payload), 9)
    assert "baseRefName" in str(excinfo.value)


def test_parse_pr_view_number_not_int() -> None:
    payload = json.dumps(
        {"number": True, "state": "OPEN", "headRefName": "x", "baseRefName": "main"}
    )
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.parse_pr_view(_view_proc(payload), 9)
    assert "number 不是整数" in str(excinfo.value)


# -------------------------------------------------------- 命令原文与输出格式

def test_ls_remote_argv_is_exact() -> None:
    assert mg.ls_remote_argv(".", "origin", "wt/t_bbeab29f") == (
        "git",
        "-C",
        ".",
        "ls-remote",
        "--exit-code",
        "--refs",
        "origin",
        "refs/heads/wt/t_bbeab29f",
    )


def test_format_step_pass() -> None:
    line = mg.format_step(
        mg.STEP_PREFLIGHT,
        ok=True,
        command="gh pr view 9 --json number",
        rc=0,
        key="state=OPEN",
    )
    assert line == "预检 | PASS | gh pr view 9 --json number | rc=0 | state=OPEN"


def test_step_branch_present_label() -> None:
    """新增的「分支存在」步骤标签是独立常量，不蹭「预检」/「分支删除」。"""

    assert mg.STEP_BRANCH_PRESENT == "分支存在"
    assert mg.STEP_BRANCH_PRESENT != mg.STEP_PREFLIGHT
    assert mg.STEP_BRANCH_PRESENT != mg.STEP_DELETE


def test_format_step_fail() -> None:
    line = mg.format_step(
        mg.STEP_DELETE, ok=False, command="git ls-remote", rc=0, key="sha refs/heads/x"
    )
    assert line == "分支删除 | FAIL | git ls-remote | rc=0 | sha refs/heads/x"


def test_key_line_prefers_stdout_then_stderr() -> None:
    both = mg.CommandResult(
        argv=("gh",), returncode=1, stdout="out-a\nout-b\n", stderr="err-a\n"
    )
    assert mg.key_line(both) == "out-b"

    stderr_only = mg.CommandResult(
        argv=("gh",), returncode=1, stdout="  \n", stderr="err-a\nerr-b\n"
    )
    assert mg.key_line(stderr_only) == "err-b"

    empty = mg.CommandResult(argv=("gh",), returncode=0, stdout="", stderr="")
    assert mg.key_line(empty) == "(无输出)"


def test_command_text() -> None:
    proc = mg.CommandResult(
        argv=("gh", "pr", "merge", "9", "--merge", "--delete-branch"),
        returncode=0,
        stdout="",
        stderr="",
    )
    assert proc.command == "gh pr merge 9 --merge --delete-branch"


# ------------------------------------------------- 取值域上界（MAX_* 常量）

def test_duration_bounds_are_constants() -> None:
    """上限写成模块常量，不散在函数体里。"""

    assert mg.MAX_TIMEOUT == 3600
    assert mg.MAX_INTERVAL == 3600
    assert mg.MAX_COMMAND_TIMEOUT == 3600
    assert mg.DEFAULT_COMMAND_TIMEOUT == 60


@pytest.mark.parametrize(
    ("flag", "raw", "maximum"),
    [
        ("--timeout", "3600", mg.MAX_TIMEOUT),
        ("--interval", "3600", mg.MAX_INTERVAL),
        ("--command-timeout", "3600", mg.MAX_COMMAND_TIMEOUT),
    ],
)
def test_duration_upper_bound_itself_accepted(flag: str, raw: str, maximum: int) -> None:
    assert mg.parse_positive_int(flag, raw, maximum=maximum) == 3600


@pytest.mark.parametrize(
    ("flag", "raw", "maximum"),
    [
        ("--timeout", "3601", mg.MAX_TIMEOUT),
        ("--timeout", "1793160000000", mg.MAX_TIMEOUT),  # 13 位毫秒
        ("--timeout", "9" * 400, mg.MAX_TIMEOUT),  # 天文数字：曾裸抛 OverflowError
        ("--interval", "3601", mg.MAX_INTERVAL),
        ("--interval", "1793160000000", mg.MAX_INTERVAL),
        ("--command-timeout", "3601", mg.MAX_COMMAND_TIMEOUT),
        ("--command-timeout", "9" * 400, mg.MAX_COMMAND_TIMEOUT),
    ],
)
def test_duration_upper_bound_rejected(flag: str, raw: str, maximum: int) -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.parse_positive_int(flag, raw, maximum=maximum)
    assert str(maximum) in str(excinfo.value)  # 说清上界


def _validated_ns(**overrides: str) -> argparse.Namespace:
    ns = mg.build_parser().parse_args(
        ["--pr", "9", "--branch", "wt/t_bbeab29f", "--method", "merge", "--delete-branch"]
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def test_validate_defaults_include_command_timeout() -> None:
    opts = mg.validate(_validated_ns())

    assert (opts.timeout, opts.interval, opts.command_timeout) == (60, 2, 60)


def test_methods_only_merge() -> None:
    """R13：合并方式只此一种，值域里不许留 squash / rebase。"""

    assert mg.METHODS == ("merge",)
    assert mg.DEFAULT_METHOD == "merge"


def test_method_default_is_merge_and_optional() -> None:
    """不传 ``--method`` 也能解析，取到的就是默认值 merge。"""

    ns = mg.build_parser().parse_args(
        ["--pr", "9", "--branch", "wt/t_bbeab29f", "--delete-branch"]
    )

    assert ns.method == mg.DEFAULT_METHOD == "merge"
    assert mg.validate(ns).method == "merge"


@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_parser_rejects_non_merge_method(method: str) -> None:
    """argparse 层就拒（``choices``），不是靠后面 validate 兜。"""

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.build_parser().parse_args(
            ["--pr", "9", "--branch", "wt/t_bbeab29f", "--method", method]
        )

    assert method in str(excinfo.value)
    assert "merge" in str(excinfo.value)


def test_validate_rejects_forged_method() -> None:
    """绕过 argparse 直接塞坏值（防御性保留）：validate 仍要拒。"""

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(method="squash"))

    assert "squash" in str(excinfo.value)


def test_validate_rejects_interval_greater_than_timeout() -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(timeout="3", interval="10"))
    assert "--interval" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("timeout", "3601"), ("interval", "3601"), ("command_timeout", "3601")],
)
def test_validate_rejects_duration_over_upper_bound(field: str, value: str) -> None:
    with pytest.raises(mg.MergeGuardError):
        mg.validate(_validated_ns(**{field: value}))


# --------------------------------------------- 合并前的「源分支必须存在」闸门

def _opts(**overrides: object) -> mg.Options:
    base: dict[str, object] = {
        "pr": 9,
        "branch": "wt/t_bbeab29f",
        "method": "merge",
        "repo": ".",
        "remote": "origin",
        "timeout": 60,
        "interval": 2,
        "command_timeout": 60,
        "verify_only": False,
    }
    base.update(overrides)
    return mg.Options(**base)  # type: ignore[arg-type]


def _fake_run(rc: int, *, stdout: str = "", stderr: str = "") -> Callable[..., mg.CommandResult]:
    def fake(argv: tuple[str, ...], *, cwd: str, timeout: float) -> mg.CommandResult:
        return mg.CommandResult(argv=argv, returncode=rc, stdout=stdout, stderr=stderr)

    return fake


def test_ensure_branch_present_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mg, "_run", _fake_run(0, stdout="c5768d4\trefs/heads/wt/t_bbeab29f\n")
    )

    proc = mg.ensure_branch_present(_opts())

    assert proc.returncode == 0
    assert proc.command == (
        "git -C . ls-remote --exit-code --refs origin refs/heads/wt/t_bbeab29f"
    )


@pytest.mark.parametrize(
    ("rc", "stderr"),
    [
        (2, ""),  # 没有这个 ref
        (128, "fatal: unable to access 'https://github.com/x/y.git'\n"),  # 通信错误
        (1, ""),
    ],
)
def test_ensure_branch_present_rejects(
    monkeypatch: pytest.MonkeyPatch, rc: int, stderr: str
) -> None:
    """非 0 一律「合并前源分支不存在」→ MergeGuardError（CLI 层即 rc2、零 gh 调用）。"""

    monkeypatch.setattr(mg, "_run", _fake_run(rc, stderr=stderr))

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.ensure_branch_present(_opts())

    message = str(excinfo.value)
    assert "不在" in message and "origin" in message
    assert "wt/t_bbeab29f" in message
    assert f"rc={rc}" in message
    assert "\n" not in message  # stderr 只许一行


# ------------------------------------------------ gh 非 0 时的 PR 状态反查

@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"state": "MERGED"}', "MERGED"),
        ('{"state": "OPEN"}', "OPEN"),
        ('{"state": "CLOSED"}', "CLOSED"),
    ],
)
def test_fetch_pr_state_parses(
    monkeypatch: pytest.MonkeyPatch, payload: str, expected: str
) -> None:
    monkeypatch.setattr(mg, "_run", _fake_run(0, stdout=payload))

    assert mg.fetch_pr_state(_opts()) == expected


@pytest.mark.parametrize(
    ("rc", "payload"),
    [
        (1, '{"state": "MERGED"}'),  # 反查本身失败
        (0, "not json"),  # 输出不是 JSON
        (0, '["MERGED"]'),  # 不是 JSON 对象
        (0, '{"number": 9}'),  # 没有 state
        (0, '{"state": 1}'),  # state 不是字符串
    ],
)
def test_fetch_pr_state_returns_none_on_garbage(
    monkeypatch: pytest.MonkeyPatch, rc: int, payload: str
) -> None:
    monkeypatch.setattr(mg, "_run", _fake_run(rc, stdout=payload))

    assert mg.fetch_pr_state(_opts()) is None


def test_fetch_pr_state_swallows_run_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """反查遇到环境错误（gh 不见了）返回 None，不许把它升级成新的失败源。"""

    def boom(argv: tuple[str, ...], *, cwd: str, timeout: float) -> mg.CommandResult:
        raise mg.MergeGuardError("找不到 gh 可执行文件")

    monkeypatch.setattr(mg, "_run", boom)

    assert mg.fetch_pr_state(_opts()) is None


# ------------------------------------------------------- 子命令超时（timeout=）

def test_run_passes_timeout_to_subprocess() -> None:
    """``_run`` 真的把 timeout 交给 subprocess：卡住的命令 0.5 秒就被打断。"""

    with pytest.raises(mg.CommandTimeout) as excinfo:
        mg._run(("sleep", "5"), cwd=".", timeout=0.5)
    assert "超时" in str(excinfo.value)


def test_run_completes_under_timeout() -> None:
    proc = mg._run(("true",), cwd=".", timeout=5)

    assert proc.returncode == 0


def test_make_probe_timeout_is_probe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针超时 → PROBE_ERROR（不是「分支还在」rc0，也不是「已删」rc2）。"""

    seen: list[float] = []

    def fake_run(argv: tuple[str, ...], *, cwd: str, timeout: float) -> mg.CommandResult:
        seen.append(timeout)
        raise mg.CommandTimeout("git 命令超时：1 秒内没有返回（见 --command-timeout）")

    monkeypatch.setattr(mg, "_run", fake_run)

    probe = mg.make_probe(".", "origin", "wt/t_bbeab29f", timeout=1)
    proc = probe()

    assert seen == [1]
    assert proc.returncode == mg.PROBE_TIMEOUT_RC
    assert mg.classify_probe_rc(proc.returncode) == mg.PROBE_ERROR
    assert "超时" in proc.stderr


def test_probe_timeout_keeps_polling_then_undetermined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时记一次通信错误继续轮询；预算耗尽 → 无法判定（既不是 present 也不是 deleted）。"""

    def fake_run(argv: tuple[str, ...], *, cwd: str, timeout: float) -> mg.CommandResult:
        raise mg.CommandTimeout("git 命令超时")

    monkeypatch.setattr(mg, "_run", fake_run)

    clock = FakeClock()
    probe = mg.make_probe(".", "origin", "wt/t_bbeab29f", timeout=1)
    result = mg.poll_branch_deleted(
        probe, timeout=2, interval=1, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_UNDETERMINED
    assert result.probes > 1  # 超时没有中断轮询
    assert "通信错误" in result.reason


# --------------------------------------------- 合并后的异常必须保住已完成证据

def test_keep_evidence_wraps_unexpected_exception() -> None:
    lines = ["预检 | PASS | gh pr view 9 | rc=0 | state=OPEN"]

    with pytest.raises(mg.PartialFailure) as excinfo, mg._keep_evidence(lines):
        raise RuntimeError("boom\n第二行")

    assert excinfo.value.lines == lines
    assert "RuntimeError" in str(excinfo.value)
    assert "\n" not in str(excinfo.value)  # 单行：不许把 traceback 或多行塞进 stderr


def test_keep_evidence_passes_through_merge_guard_error_with_lines() -> None:
    lines = ["预检 | PASS | gh pr view 9 | rc=0 | state=OPEN"]

    with pytest.raises(mg.PartialFailure) as excinfo, mg._keep_evidence(lines):
        raise mg.MergeGuardError("合并已完成，但无法判定远端分支是否已删除")

    assert excinfo.value.lines == lines
    assert "无法判定" in str(excinfo.value)


def test_keep_evidence_no_exception_no_change() -> None:
    lines = ["预检 | PASS | x"]

    with mg._keep_evidence(lines):
        lines.append("合并 | PASS | y")

    assert lines == ["预检 | PASS | x", "合并 | PASS | y"]



# ------------------------ stdout 断管 / 关闭：不冒 traceback、不改真实退出码

class DeadStream:
    """写就抛的替身流；**没有 fileno**，所以测试不碰进程的 fd 1。"""

    def __init__(self, exc: BaseException, *, encoding: str | None = "utf-8") -> None:
        self.exc = exc
        self.encoding = encoding
        self.written: list[str] = []

    def write(self, text: str) -> None:
        raise self.exc

    def flush(self) -> None:
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        BrokenPipeError(32, "Broken pipe"),
        OSError(32, "Broken pipe"),
        OSError(9, "Bad file descriptor"),
        ValueError("I/O operation on closed file"),
    ],
    ids=["broken-pipe", "epipe", "ebadf", "closed-file"],
)
def test_write_stdout_swallows_dead_stream(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """写不出去不是运行结论 —— 不许抛，也不许让调用方改退出码。"""

    monkeypatch.setattr(mg.sys, "stdout", DeadStream(exc))

    mg._write_stdout(["分支删除 | PASS | git ls-remote | rc=2 | 分支已不在远端"])  # 不抛

    # 换成了 /dev/null 上的流：解释器退出时再 flush 也不会冒 "Exception ignored"
    assert mg.sys.stdout is not None
    assert getattr(mg.sys.stdout, "name", "") == mg.os.devnull


def test_write_stdout_none_stdout_redirects_fd1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``1>&-`` 时 ``sys.stdout is None``：把 fd 1 接到 /dev/null，然后静默返回。"""

    dup2_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(mg.os, "dup2", lambda fd, target: dup2_calls.append((fd, target)))
    monkeypatch.setattr(mg.sys, "stdout", None)

    mg._write_stdout(["结论：分支仍在远端"])

    assert [target for _fd, target in dup2_calls] == [1]  # fd 1 真的被重定向了
    assert mg.sys.stdout is not None
    assert getattr(mg.sys.stdout, "name", "") == mg.os.devnull


def test_silence_stdout_leaves_fd1_alone_for_fake_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """替身流（StringIO 之类）没有 fileno 1 → 不许动进程的 fd 1。"""

    dup2_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(mg.os, "dup2", lambda fd, target: dup2_calls.append((fd, target)))

    mg._silence_stdout(DeadStream(BrokenPipeError(32, "Broken pipe")))

    assert dup2_calls == []


def test_write_stdout_encoding_error_is_still_rc2(monkeypatch: pytest.MonkeyPatch) -> None:
    """编码表示不了中文仍是 rc2（原有契约不许被断管修复改掉）。"""

    bad = UnicodeEncodeError("ascii", "中文", 0, 1, "ordinal not in range(128)")
    monkeypatch.setattr(mg.sys, "stdout", DeadStream(bad, encoding="ascii"))

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg._write_stdout(["结论：分支仍在远端"])

    assert "UTF-8" in str(excinfo.value)


# ------------------------------ stderr 写不出去：rc2 不许因为写日志变成 rc1

def test_write_stderr_error_survives_missing_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``2>&-``（``sys.stderr is None``）时写 stderr 不许炸。"""

    monkeypatch.setattr(mg.sys, "stderr", None)

    mg._write_stderr_error("合并命令缺少 --delete-branch")  # 不抛
    mg._write_stderr_raw("gh: 原始输出\n")  # 不抛


@pytest.mark.parametrize(
    "exc",
    [BrokenPipeError(32, "Broken pipe"), OSError(9, "Bad file descriptor")],
    ids=["broken-pipe", "ebadf"],
)
def test_write_stderr_error_survives_broken_stream(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    monkeypatch.setattr(mg.sys, "stderr", DeadStream(exc))

    mg._write_stderr_error("内部错误：RuntimeError: boom")  # 不抛


# ------------------------------------------------- 信号：转成 rc2，不许被直接杀掉

def test_interrupted_message_format() -> None:
    """stderr 那一行必须是单行、写清信号名、并提醒「退出码不是 1」。"""

    text = str(mg.Interrupted("SIGTERM"))

    assert text == "收到信号 SIGTERM（退出码不是 1；合并是否已发生请人工核对）"
    assert "\n" not in text


def test_signal_name_maps_signum() -> None:
    import signal as signal_module

    assert mg.signal_name(signal_module.SIGTERM) == "SIGTERM"
    assert mg.signal_name(signal_module.SIGINT) == "SIGINT"


def test_handle_signals_restores_previous_handlers() -> None:
    """退出时恢复原 handler：不许污染宿主进程（否则 pytest 自己收不到 Ctrl-C）。"""

    import signal as signal_module

    before = {signum: signal_module.getsignal(signum) for signum in mg._interrupt_signals()}

    with mg.handle_signals():
        inside = [
            signal_module.getsignal(signum) for signum in mg._interrupt_signals()
        ]
        assert all(handler is not before[signum] for handler, signum in zip(
            inside, mg._interrupt_signals(), strict=True
        ))

    after = {signum: signal_module.getsignal(signum) for signum in before}
    assert after == before


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_handle_signals_turns_signal_into_interrupted(
    signame: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真发一次信号：处理期内的 SIGTERM / SIGINT 必须变成 Interrupted（不是 traceback）。"""

    import os
    import signal as signal_module

    signum = getattr(signal_module, signame, None)
    if signum is None:  # pragma: no cover - Windows
        pytest.skip(f"平台没有 {signame}")

    with pytest.raises(mg.Interrupted) as excinfo, mg.handle_signals():
        os.kill(os.getpid(), signum)

    assert excinfo.value.signame == signame
    assert "退出码不是 1" in str(excinfo.value)
