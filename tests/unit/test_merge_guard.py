"""merge_guard 的纯逻辑单测：判定、轮询、参数校验、输出格式（不联网、不调 gh）。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from team_board import merge_guard as mg

#: 假 ``gh pr view`` 报出来的 head 提交（40 位十六进制）
HEAD_OID = "4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a2"

#: 认账四参数的默认取值（合并模式必填）
MERGE_CARD = "t_bbeab29f"
MERGE_BY = "pm/run200"
MERGE_SUBJECT = "Merge PR #9（t_bbeab29f）"
BODY_TEXT = f"merged-by: {MERGE_BY}\nmerge-card: {MERGE_CARD}\nmerge-pr: #9\n"

_BODY_PATH: str | None = None


def body_file() -> str:
    """造一份合法 body 文件并返回路径（模块级缓存：同一进程内只写一次）。"""

    global _BODY_PATH
    if _BODY_PATH is None:
        handle, name = tempfile.mkstemp(prefix="mg-body-", suffix=".txt")
        os.close(handle)
        Path(name).write_text(BODY_TEXT, encoding="utf-8")
        _BODY_PATH = name
    return _BODY_PATH


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
    # t=0,2,4 各一次；t=5 预算为 0 → 不再起新探针，按上一次 rc=0 判「分支还在」
    assert len(calls) == result.probes == 3
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
    assert len(calls) == result.probes == 2  # t=0,1；t=2 预算为 0 → 不再起新探针
    assert sum(clock.slept) <= 2


def test_poll_single_shot_timeout() -> None:
    """timeout=1 / interval=1：不真睡超过 1 秒，最多两次探针。"""

    probe, calls = make_probe(0)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=1, interval=1, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_PRESENT
    assert len(calls) == 1  # t=1 时预算已为 0 → 不再起第二个探针
    assert sum(clock.slept) <= 1


# ------------------- 总预算硬约束：--timeout 是「总」等待，不是「每次」等待
#
# 依据 README:313「总等待不超过 --timeout」。修之前单次探针只受 --command-timeout
# 约束：--timeout 1 --command-timeout 60 + 一次卡 3 秒的 git → 3.1 秒才返回。

def test_poll_budget_exhausted_skips_new_probe() -> None:
    """预算用完就不再起子进程（FakeClock 已推进到终点）。"""

    probe, calls = make_probe(0)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=2, interval=2, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_PRESENT  # 上一次 rc=0 已经是明确结论
    assert len(calls) == result.probes == 1  # t=2 时没再探一次
    assert clock.slept == [2.0]


def test_poll_budget_exhausted_without_conclusion_is_undetermined() -> None:
    """预算用完且没有明确结论 → 「无法判定」，理由里说清 --timeout 用完了。"""

    probe, calls = make_probe(128)
    clock = FakeClock()

    result = mg.poll_branch_deleted(
        probe, timeout=1, interval=1, sleep=clock.sleep, clock=clock.now
    )

    assert result.outcome == mg.OUT_UNDETERMINED
    assert len(calls) == result.probes == 1
    assert result.returncode == 128
    assert "总预算" in result.reason
    assert "128" in result.reason


def test_probe_with_limit_never_exceeds_command_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``with_limit`` 只取小：剩余预算再大也不越过 ``--command-timeout``。"""

    seen: list[float] = []

    def fake_run(argv: tuple[str, ...], *, cwd: str, timeout: float) -> mg.CommandResult:
        seen.append(timeout)
        raise mg.CommandTimeout("git 命令超时")

    monkeypatch.setattr(mg, "_run", fake_run)

    probe = mg.make_probe(".", "origin", "wt/x", timeout=1)
    probe.with_limit(60)()
    probe.with_limit(0.25)()

    assert seen == [1, 0.25]


def test_probe_timeout_clamped_to_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--timeout 1`` + 卡 3 秒的 git：总耗时 ≈ 1 秒（不是 command-timeout 的 60）。"""

    seen: list[float] = []

    def slow_run(argv: tuple[str, ...], *, cwd: str, timeout: float) -> mg.CommandResult:
        seen.append(timeout)
        time.sleep(timeout)  # 模拟子进程一直不返回，直到耗尽允许的超时
        raise mg.CommandTimeout(f"git 命令超时：{timeout:g} 秒内没有返回")

    monkeypatch.setattr(mg, "_run", slow_run)

    started = time.monotonic()
    result = mg.poll_branch_deleted(
        mg.make_probe(".", "origin", "wt/x", timeout=60), timeout=1, interval=1
    )
    elapsed = time.monotonic() - started

    assert len(seen) == 1
    assert 0 < seen[0] <= 1  # 被剩余预算夹住，不是 60（有微小时钟漂移）
    assert elapsed <= 1.5
    assert result.outcome == mg.OUT_UNDETERMINED
    assert result.probes == 1
    assert "总预算" in result.reason


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


# ------------------------------------------------ --branch 形态校验（P2-1 返工）
#
# 修前：validate 对 --branch 不做形态校验，--verify-only 把 ls-remote rc=2 一律读成
# 「已删除」→ 空串 / 带空白 / 带 refs/ 前缀三种输入都打「分支删除 | PASS」+ rc0。


@pytest.mark.parametrize(
    ("label", "branch", "rule"),
    [
        ("空串", "", mg.BRANCH_RULE_EMPTY),
        ("前导空格", " wt/t_bbeab29f", mg.BRANCH_RULE_WHITESPACE),
        ("尾随空格", "wt/t_bbeab29f ", mg.BRANCH_RULE_WHITESPACE),
        ("中间制表符", "wt/\tt_bbeab29f", mg.BRANCH_RULE_WHITESPACE),
        ("尾随换行", "wt/t_bbeab29f\n", mg.BRANCH_RULE_WHITESPACE),
        ("全角空格", "\u3000wt/t_bbeab29f", mg.BRANCH_RULE_WHITESPACE),
        ("refs/heads 前缀", "refs/heads/wt/t_bbeab29f", mg.BRANCH_RULE_REFS_PREFIX),
        ("refs/tags 前缀", "refs/tags/v1", mg.BRANCH_RULE_REFS_PREFIX),
        ("裸 refs/", "refs/", mg.BRANCH_RULE_REFS_PREFIX),
    ],
)
def test_check_branch_shape_rejects(label: str, branch: str, rule: str) -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.check_branch_shape(branch)

    message = str(excinfo.value)
    assert message.startswith("--branch "), label
    assert rule in message, label  # 说清是哪条规则
    assert "\n" not in message, label  # stderr 只许一行


@pytest.mark.parametrize(
    "branch",
    [
        "wt/t_bbeab29f",
        "feature-x",
        "main",  # 形态合法，由 check_branch_is_not_main 拒
        "Main",
        "main2",
        "refsfoo/x",  # 只有字面前缀 refs/ 才算
        "wt/refs/heads",  # 前缀在中间不算
    ],
)
def test_check_branch_shape_accepts(branch: str) -> None:
    mg.check_branch_shape(branch)  # 不抛异常即通过


@pytest.mark.parametrize("bad", ["", " wt/t_bbeab29f", "refs/heads/wt/t_bbeab29f"])
def test_validate_rejects_malformed_branch(bad: str) -> None:
    """validate 层也拒（不经 argparse 的伪造命名空间）：形态校验排在一切 gh / git 之前。"""

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(branch=bad))

    assert str(excinfo.value).startswith("--branch ")


def test_validate_accepts_legal_branch_unchanged() -> None:
    """合法分支名的行为不变（顺带钉住 --branch 原样进 Options，不做 trim）。"""

    assert mg.validate(_validated_ns()).branch == "wt/t_bbeab29f"


# ------------------------------------------- --verify-only 的步骤行（不写 PASS/FAIL）


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (mg.OUT_DELETED, "不存在"),
        (mg.OUT_PRESENT, "存在"),
        (mg.OUT_UNDETERMINED, "无法判定"),
    ],
)
def test_format_verify_step_never_says_pass(outcome: str, state: str) -> None:
    """``--verify-only`` 的步骤行只报 ref 现状：不许出现 PASS / FAIL，也不许宣称删除。"""

    line = mg.format_verify_step(
        outcome,
        command="git ls-remote --exit-code --refs origin refs/heads/wt/x",
        rc=2,
        key="分支已不在远端",
    )

    assert line.startswith(f"远端 ref | {state} | ")
    assert "PASS" not in line and "FAIL" not in line
    assert "删除" not in line.split(" | ")[0]
    assert mg.STEP_REMOTE_REF != mg.STEP_DELETE


def test_format_verify_step_unknown_outcome_is_undetermined() -> None:
    """未知 outcome 不许当成「不存在」（那会替没查过的 ref 打绿）。"""

    line = mg.format_verify_step("nonsense", command="git ls-remote", rc=1, key="k")

    assert line.startswith("远端 ref | 无法判定 | ")


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
            "headRefOid": HEAD_OID,
        }
    )
    info = mg.parse_pr_view(_view_proc(payload), 9)

    assert info.number == 9
    assert info.state == "OPEN"
    assert info.head_ref == "wt/t_f43fa954"
    assert info.base_ref == "main"
    assert info.head_oid == HEAD_OID  # 预检钉到提交：合并要用它 --match-head-commit


@pytest.mark.parametrize(
    "head_oid",
    [
        None,  # 字段缺失
        "",  # 空串
        "4f2a9c0",  # 7 位缩写
        "4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a",  # 39 位
        "4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a2f",  # 41 位
        "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",  # 非十六进制
        " 4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a2",  # 前导空格
    ],
    ids=["missing", "empty", "short", "39", "41", "non-hex", "leading-space"],
)
def test_parse_pr_view_rejects_bad_head_oid(head_oid: str | None) -> None:
    """没有合法的 head sha 就不许合并（rc2）：只比分支名约束不了「合的哪个提交」。"""

    payload: dict[str, object] = {
        "number": 9,
        "state": "OPEN",
        "headRefName": "wt/t_f43fa954",
        "baseRefName": "main",
    }
    if head_oid is not None:
        payload["headRefOid"] = head_oid

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.parse_pr_view(_view_proc(json.dumps(payload)), 9)

    assert "headRefOid" in str(excinfo.value)


def test_parse_pr_view_accepts_sha256_length() -> None:
    """SHA-256 仓库的 64 位 oid 也合法。"""

    oid = "a" * 64
    payload = json.dumps(
        {
            "number": 9,
            "state": "OPEN",
            "headRefName": "wt/t_x",
            "baseRefName": "main",
            "headRefOid": oid,
        }
    )

    assert mg.parse_pr_view(_view_proc(payload), 9).head_oid == oid


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
        {
            "number": True,
            "state": "OPEN",
            "headRefName": "x",
            "baseRefName": "main",
            "headRefOid": HEAD_OID,
        }
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


def _validated_ns(**overrides: object) -> argparse.Namespace:
    ns = mg.build_parser().parse_args(
        [
            "--pr",
            "9",
            "--branch",
            "wt/t_bbeab29f",
            "--method",
            "merge",
            "--delete-branch",
            "--card",
            MERGE_CARD,
            "--by",
            MERGE_BY,
            "--subject",
            MERGE_SUBJECT,
            "--body-file",
            body_file(),
        ]
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
        [
            "--pr",
            "9",
            "--branch",
            "wt/t_bbeab29f",
            "--delete-branch",
            "--card",
            MERGE_CARD,
            "--by",
            MERGE_BY,
            "--subject",
            MERGE_SUBJECT,
            "--body-file",
            body_file(),
        ]
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


# ------------------------------------- 认账四参数（--card / --by / --subject / --body-file）
#
# 缺口（pm 实跑，卡 t_bb6600d8）：``gh pr merge`` 不带 ``--subject`` / ``--body-file`` 时
# 用 gh 默认文案生成合并提交 message，**没有任何 trailer** —— 经 merge_guard 合出来的提交
# 必然过不了 ``merge-audit --require``（缺 merged-by / merge-card / merge-pr）。所以合并模式
# 多四个必填参数，全部原样转发；校验全部落在 ``validate``（即 ensure_branch_present 之前）。


def test_merge_argv_verbatim_order() -> None:
    """argv 逐字：三段语义（--merge/--delete-branch/--match-head-commit）原位，认账追加在后。"""

    argv = mg.merge_argv(9, "merge", HEAD_OID, MERGE_SUBJECT, "/tmp/body.txt")

    assert argv == (
        "gh",
        "pr",
        "merge",
        "9",
        "--merge",
        "--delete-branch",
        "--match-head-commit",
        HEAD_OID,
        "--subject",
        MERGE_SUBJECT,
        "--body-file",
        "/tmp/body.txt",
    )
    # 顺序也是契约：--subject / --body-file 必须在 --match-head-commit <sha> 之后
    assert argv.index("--match-head-commit") < argv.index("--subject") < argv.index("--body-file")


def test_validate_carries_accounting_into_options() -> None:
    opts = mg.validate(_validated_ns())

    assert opts.accounting is not None
    assert opts.accounting.card == MERGE_CARD
    assert opts.accounting.by == MERGE_BY
    assert opts.accounting.subject == MERGE_SUBJECT
    assert opts.accounting.body_file == body_file()


@pytest.mark.parametrize("field", ["card", "by", "subject", "body_file"])
def test_validate_rejects_missing_accounting_flag(field: str) -> None:
    """合并模式缺任一项 → rc2 档（MergeGuardError），错误里点名缺的是哪个。"""

    flag = "--" + field.replace("_", "-")
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(**{field: None}))

    assert flag in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_card",
    ["", "t_", "t_deadbee", "t_deadbeef0", "T_DEADBEEF", "deadbeef", "t_DEADBEEF", "t_deadbeez"],
)
def test_validate_rejects_bad_card_shape(bad_card: str) -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(card=bad_card))
    assert "--card" in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_by",
    ["", "pm", "pm/run", "pm/run-1", "PM/run200", "pm/200", "pm/run200x", "pm/run 200"],
)
def test_validate_rejects_bad_by_shape(bad_by: str) -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(by=bad_by))
    assert "--by" in str(excinfo.value)


@pytest.mark.parametrize(
    ("bad_subject", "hint"),
    [
        ("", "不能为空"),
        ("   ", "为空"),
        ("\t", "为空"),
        ("a\nb", "换行"),
        ("a\rb", "换行"),
        ("Merge PR #9\n", "换行"),
    ],
)
def test_validate_rejects_bad_subject(bad_subject: str, hint: str) -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(subject=bad_subject))
    assert "--subject" in str(excinfo.value)
    assert hint in str(excinfo.value)


def test_validate_accepts_subject_with_surrounding_spaces() -> None:
    """只拒「纯空白」与换行：首尾有空格但正文非空的标题照收（不静默 trim）。"""

    opts = mg.validate(_validated_ns(subject="  Merge PR #9（t_bbeab29f）  "))

    assert opts.accounting is not None
    assert opts.accounting.subject == "  Merge PR #9（t_bbeab29f）  "


def test_validate_rejects_empty_body_file_path() -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file=""))

    assert "--body-file" in str(excinfo.value)
    assert "路径为空" in str(excinfo.value)


def test_validate_rejects_relative_body_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对路径 → rc2（校验按进程 cwd 解析、``gh pr merge`` 按 ``--repo`` 解析）。

    这是 PR #30 的 P2-1：同一个相对路径在两处指向不同文件 —— 校验读到 caller cwd 的那份、
    gh 读 ``--repo`` 下的那份。轻则 gh 报读不到（rc1 冒充「未达成」），重则静默合进另一份
    body、合出没有 trailer 的提交。所以即便 caller cwd 里**真有一份合法 body**也必须拒。
    """

    (tmp_path / "body.txt").write_text(BODY_TEXT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file="body.txt"))

    assert "--body-file" in str(excinfo.value)
    assert "绝对路径" in str(excinfo.value)


def test_validate_accepts_absolute_body_file_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向保护：同一份文件给绝对路径 → 行为不变（原样进 ``Accounting``）。"""

    body = tmp_path / "body.txt"
    body.write_text(BODY_TEXT, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # cwd 与 body 所在目录不同，绝对路径照样可读

    opts = mg.validate(_validated_ns(body_file=str(body)))

    assert opts.accounting is not None
    # 交给 gh 的路径 == 校验用的路径（同一个绝对路径原文）
    assert opts.accounting.body_file == str(body)


def test_validate_rejects_missing_body_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file=str(missing)))

    assert "不存在" in str(excinfo.value)


def test_validate_rejects_body_file_directory(tmp_path: Path) -> None:
    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file=str(tmp_path)))

    assert "不是普通文件" in str(excinfo.value)


def test_validate_rejects_non_utf8_body_file(tmp_path: Path) -> None:
    path = tmp_path / "gbk.txt"
    path.write_bytes("Merge-Note: 合并\n".encode("gbk") + b"\xff\xfe\n")

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file=str(path)))

    assert "UTF-8" in str(excinfo.value)


def test_validate_rejects_empty_body_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file=str(path)))

    assert "空文件" in str(excinfo.value)


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ("merged-by: pm/run200\nmerge-card: t_bbeab29f\n", "缺字段"),  # 少 merge-pr
        (
            "merged-by: pm/run200\nmerge-card: t_bbeab29f\nmerge-pr: #9\nmerge-pr: #9\n",
            "重复字段",
        ),
        ("merged-by: PM/run200\nmerge-card: t_bbeab29f\nmerge-pr: #9\n", "格式非法"),
    ],
)
def test_validate_rejects_bad_body_content(tmp_path: Path, body: str, kind: str) -> None:
    """body 内容交给 ``merges.check_message`` 判（缺字段 / 重复 / 格式错）。"""

    path = tmp_path / "body.txt"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file=str(path)))

    assert kind in str(excinfo.value)


def test_validate_rejects_body_card_mismatch(tmp_path: Path) -> None:
    """``merge-card`` 与 ``--card`` 不一致：check_message 报「字段与参数不一致」。"""

    path = tmp_path / "body.txt"
    path.write_text(
        "merged-by: pm/run200\nmerge-card: t_deadbeef\nmerge-pr: #9\n", encoding="utf-8"
    )

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file=str(path)))

    assert "字段与参数不一致" in str(excinfo.value)


def test_validate_rejects_body_pr_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "body.txt"
    path.write_text(
        "merged-by: pm/run200\nmerge-card: t_bbeab29f\nmerge-pr: #10\n", encoding="utf-8"
    )

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(body_file=str(path)))

    assert "#10" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("card", MERGE_CARD), ("by", MERGE_BY), ("subject", MERGE_SUBJECT), ("body_file", "x.txt")],
)
def test_validate_verify_only_excludes_accounting(field: str, value: str) -> None:
    """``--verify-only`` 与认账四参数互斥（verify-only 不合并，给了 body 说明调用方误判）。"""

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.validate(_validated_ns(verify_only=True, **{field: value}))

    assert "互斥" in str(excinfo.value)


def test_validate_verify_only_alone_returns_no_accounting() -> None:
    """单独 ``--verify-only`` 行为不变：Options.accounting 是 None，且不要求四个参数。"""

    ns = mg.build_parser().parse_args(
        [
            "--pr",
            "9",
            "--branch",
            "wt/t_bbeab29f",
            "--delete-branch",
            "--repo",
            ".",
            "--verify-only",
        ]
    )

    opts = mg.validate(ns)

    assert opts.verify_only is True
    assert opts.accounting is None


# ------------------------------- 合并前 rebase 闸门：ref 名 / 三条 argv / rc 分类 / base 形态

def test_step_rebase_label_is_stable() -> None:
    """证据行的步骤标签下游要 grep，值钉死成 ``rebase``。"""

    assert mg.STEP_REBASE == "rebase"


def test_merge_guard_ref_namespace_is_private() -> None:
    """私有 ref 命名空间不在 ``refs/heads`` / ``refs/remotes`` 下（稽核看不见、push 不带）。"""

    assert mg.MERGE_GUARD_REF_PREFIX == "refs/merge-guard"
    assert not mg.MERGE_GUARD_REF_PREFIX.startswith("refs/heads")
    assert not mg.MERGE_GUARD_REF_PREFIX.startswith("refs/remotes")
    assert mg.rebase_base_ref(9) == "refs/merge-guard/9/base"
    assert mg.rebase_head_ref(9) == "refs/merge-guard/9/head"


def test_rebase_fetch_argv_verbatim() -> None:
    argv = mg.rebase_fetch_argv("/r", "origin", 9, "main", "wt/t_bbeab29f")

    assert argv == (
        "git",
        "-C",
        "/r",
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--quiet",
        "origin",
        "+refs/heads/main:refs/merge-guard/9/base",
        "+refs/heads/wt/t_bbeab29f:refs/merge-guard/9/head",
    )
    # 不许浅抓：浅历史下 merge-base --is-ancestor 会假红
    assert "--depth" not in argv


def test_rebase_fetch_argv_uses_base_ref_name_not_hardcoded_main() -> None:
    """base 来自 ``gh pr view`` 的 ``baseRefName``；``--remote`` 也不写死成 origin。"""

    argv = mg.rebase_fetch_argv("/r", "upstream", 9, "release/1.0", "wt/t_x")

    assert "+refs/heads/release/1.0:refs/merge-guard/9/base" in argv
    assert "upstream" in argv
    assert "+refs/heads/wt/t_x:refs/merge-guard/9/head" in argv


def test_rebase_rev_parse_argv_verbatim() -> None:
    assert mg.rebase_rev_parse_argv("/r", mg.rebase_head_ref(9)) == (
        "git",
        "-C",
        "/r",
        "rev-parse",
        "refs/merge-guard/9/head",
    )


def test_rebase_merge_base_argv_verbatim() -> None:
    assert mg.rebase_merge_base_argv("/r", 9) == (
        "git",
        "-C",
        "/r",
        "merge-base",
        "--is-ancestor",
        "refs/merge-guard/9/base",
        "refs/merge-guard/9/head",
    )


@pytest.mark.parametrize(
    ("rc", "expected"),
    [
        (0, mg.ANCESTOR_YES),
        (1, mg.ANCESTOR_NO),
        (128, mg.ANCESTOR_ERROR),  # 对象缺失 / 通信错误：判不了
        (255, mg.ANCESTOR_ERROR),
        (-1, mg.ANCESTOR_ERROR),  # 超时哨兵
    ],
)
def test_classify_merge_base_rc(rc: int, expected: str) -> None:
    assert mg.classify_merge_base_rc(rc) == expected


@pytest.mark.parametrize(
    ("base", "hint"),
    [
        ("", "不能是空串"),
        ("ma in", "不能含空白字符"),
        (" ma in", "不能含空白字符"),
        ("refs/heads/main", "不能带 refs/ 前缀"),
    ],
)
def test_check_base_ref_shape_rejects(base: str, hint: str) -> None:
    """畸形 ``baseRefName`` → ``MergeGuardError``，文案点名 baseRefName（不是 --branch）。"""

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.check_base_ref_shape(base)

    message = str(excinfo.value)
    assert "baseRefName" in message
    assert hint in message
    assert "--branch" not in message  # 复用 --branch 那套文案会造成误导


def test_check_base_ref_shape_accepts_plain_branch_names() -> None:
    for base in ("main", "master", "release/1.0", "wt/t_bbeab29f"):
        mg.check_base_ref_shape(base)  # 不抛

