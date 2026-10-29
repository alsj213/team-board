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

