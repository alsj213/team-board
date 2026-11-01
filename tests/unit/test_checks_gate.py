"""``merge_guard`` 的 **CI 全绿闸门**（``gh pr checks``，``NO_CHECKS`` 不算绿）单测。

判据来源是卡 ``t_1a042fbb`` 的验收标准 1–4 与契约 v1 · E6 段，逐条对应：

1. 四档分类各一条用例（``classify`` 是纯函数，不跑子进程）：``rc=0`` → PASS；
   ``rc=1`` + ``no checks reported on the 'wt/x' branch`` → ``NO_CHECKS``；
   ``rc=1`` + ``pytest (3.12) fail`` → ``FAIL``；``rc=127`` + 空输出 → ``UNAVAILABLE``。
2. 三种非 PASS 都**拒绝合并**：rc2、stdout 0 字节、stderr 恰一行，分别逐字含
   ``NO_CHECKS`` / ``CI 未通过`` / ``判不了``。
3. ``NO_CHECKS`` 的文案点明「不算绿（R6）」，且任何路径下都不写成通过（stdout 里
   不许出现 ``CI | PASS``）。
4. 拒绝发生在 ``gh pr merge`` **之前**：fake runner 断言 ``gh pr merge`` 调用 0 次。
5. 全量 pytest / ruff 由交卡命令覆盖（本文件本身不跑 pytest）。

**假 runner 的口径**（与 ``test_merge_guard_evidence_gate.py`` 同一套）：夹具、看板库、
``merge_guard.main`` 的调用方式直接从那个模块复用（``tests.unit`` 是真包，
``tests/__init__.py`` 与 ``tests/unit/__init__.py`` 都在），本文件只加一层
``ChecksRunner`` —— 在它的 ``_gh`` 前面插一条 ``gh pr checks`` 分支，其余（预检 / 合并 /
``git``）走真夹具。这样测的是**整条** ``_run_merge``（认领 → 证据 → CI → 双问 → rebase →
合并），不是拼出来的行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from team_board import checks
from team_board import merge_guard as mg
from tests.unit.test_merge_guard_evidence_gate import (
    BRANCH,
    HEAD_OID,
    PR,
    FakeRunner,
    cli_argv,
    run_main,
)

#: 本仓 R6 案例里的真实形态（``gh pr checks`` 在没跑过 workflow 的分支上打这句、rc=1）
NO_CHECKS_OUT = f"no checks reported on the '{BRANCH}' branch"
#: ``gh pr checks`` 真跑过且失败时的典型末行（假输出只在本文件里当夹具用）
FAIL_OUT = "pytest (3.12)\tfail\t11s\thttps://example.invalid/run/1"
#: ``gh pr checks`` 全绿时的典型输出（末行会进 ``CI | PASS`` 的最后一格）
PASS_OUT = (
    "commit-identity\tpass\t5s\thttps://example.invalid/run/2\n"
    "pytest (3.12)\tpass\t41s\thttps://example.invalid/run/2"
)


class ChecksRunner(FakeRunner):
    """``FakeRunner`` + 一条 ``gh pr checks`` 分支（rc / 两股输出可分别指定）。

    ``checks_stderr`` 单独留口：真 gh 把 ``no checks reported`` 这句错误提示打在 stderr，
    只塞 stdout 的桩测不出「两股输出合并再分类」这件事。
    """

    def __init__(
        self,
        *,
        checks_rc: int = 0,
        checks_stdout: str = PASS_OUT,
        checks_stderr: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.checks_rc = checks_rc
        self.checks_stdout = checks_stdout
        self.checks_stderr = checks_stderr

    def __call__(
        self, argv: tuple[str, ...], *, cwd: str, timeout: float
    ) -> mg.CommandResult:
        argv = tuple(argv)
        if argv[:3] == ("gh", "pr", "checks"):
            self.calls.append(argv)
            return mg.CommandResult(
                argv=argv,
                returncode=self.checks_rc,
                stdout=self.checks_stdout,
                stderr=self.checks_stderr,
            )
        return super().__call__(argv, cwd=cwd, timeout=timeout)


def checks_calls(runner: FakeRunner) -> list[tuple[str, ...]]:
    """所有 ``gh pr checks`` 调用（判「这一步到底发没发」）。"""

    return [argv for argv in runner.calls if argv[:3] == ("gh", "pr", "checks")]


# ---------------------------------------------------------------- 验收 1：四档分类


@pytest.mark.parametrize(
    ("rc", "output", "expected"),
    [
        (0, "", checks.CHECK_PASS),
        (0, "pytest (3.12)\tpass\t41s", checks.CHECK_PASS),
        (1, NO_CHECKS_OUT, checks.CHECK_NO_CHECKS),
        (1, FAIL_OUT, checks.CHECK_FAIL),
        (127, "", checks.CHECK_UNAVAILABLE),
    ],
)
def test_classify_four_verdicts(rc: int, output: str, expected: str) -> None:
    """四档各一条：rc=0 全绿 / rc=1+no checks reported = NO_CHECKS / rc=1 其它 = FAIL /
    其它 rc = UNAVAILABLE（判不了）。"""

    assert checks.classify(rc, output) == expected


def test_classify_pass_ignores_output() -> None:
    """rc=0 不看输出内容：gh 自己保证 rc0 即全 pass（解析表格会把排版当契约）。"""

    assert checks.classify(0, "随便什么\n第二行") == checks.CHECK_PASS


def test_classify_no_checks_marker_is_case_insensitive() -> None:
    """标记匹配不看大小写：判定的是「gh 说了这句话」，不是猜它的排版。"""

    assert checks.classify(1, "No Checks Reported on the 'wt/x' branch") == checks.CHECK_NO_CHECKS


def test_classify_fail_wins_over_pending() -> None:
    """rc=1 但输出里没有那句标记 = 确有检查项没过（不许读成 NO_CHECKS）。"""

    assert checks.classify(1, "pytest (3.12)\tpending\t0s") == checks.CHECK_FAIL


def test_key_line_takes_last_non_empty_line() -> None:
    """``key_line`` 取最后一个非空行（汇总行在最末），空输出给占位符。"""

    assert checks.key_line("a\n\n b \n\n") == "b"
    assert checks.key_line("") == checks.NO_OUTPUT
    assert checks.key_line("\n\n\t\n") == checks.NO_OUTPUT


def test_key_line_strips_control_chars_and_truncates() -> None:
    """去控制字符（``\\x00`` / ``\\x1b`` 之类留在行内）+ 截断 200 字符（一行证据不许带整段输出）。

    ``\\r`` 由 ``str.splitlines`` 直接当行界处理（进度条重画：留下的是最后一帧），
    所以「``列1\\r列2``」取 ``列2`` —— 这也是 gh 在非 TTY 下输出 ``\\r`` 的真实意图。
    """

    assert checks.key_line("列1\r列2") == "列2"
    assert checks.key_line("a\x00b\x1bc") == "abc"
    assert len(checks.key_line("x" * 500)) == checks.KEY_LINE_LIMIT


def test_no_checks_marker_matches_real_gh_wording() -> None:
    """标记逐字对齐 R6 案例里 gh 的原文（``no checks reported on the '<branch>' branch``）。"""

    assert checks.NO_CHECKS_MARKER in NO_CHECKS_OUT


# ------------------------------------------------- 验收 1/2/4：三种拒绝 + 不合并


def _rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner: ChecksRunner,
) -> tuple[int, str, str]:
    """跑一次完整合并：``run_main`` 复用证据闸门那套夹具，只换 runner。"""

    return run_main(tmp_path, monkeypatch, capsys, runner=runner)


@pytest.mark.parametrize(
    ("checks_rc", "checks_stdout", "checks_stderr", "literal"),
    [
        (1, "", NO_CHECKS_OUT, "NO_CHECKS"),
        (1, FAIL_OUT, "", "CI 未通过"),
        (127, "", "gh: command not found", "判不了"),
    ],
)
def test_three_non_pass_verdicts_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    checks_rc: int,
    checks_stdout: str,
    checks_stderr: str,
    literal: str,
) -> None:
    """验收 2 + 4：三种非 PASS 一律 rc2、stdout 0 字节、stderr 恰一行、`gh pr merge` 0 次。"""

    runner = ChecksRunner(
        checks_rc=checks_rc, checks_stdout=checks_stdout, checks_stderr=checks_stderr
    )
    rc, out, err = _rejected(tmp_path, monkeypatch, capsys, runner)

    assert rc == 2
    assert out == ""  # stdout 0 字节：「半截证据行」不许被读成「已经走到合并」
    lines = err.splitlines()
    assert len(lines) == 1, err
    assert lines[0].startswith("错误: ")
    assert literal in lines[0]
    assert runner.merge_calls() == []  # 验收 4：拒绝发生在 gh pr merge 之前
    assert len(checks_calls(runner)) == 1  # 取数只发一次


def test_no_checks_message_says_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """验收 3：NO_CHECKS 的文案点明「不算绿（R6）」，任何路径下都不写成通过。"""

    runner = ChecksRunner(checks_rc=1, checks_stdout="", checks_stderr=NO_CHECKS_OUT)
    rc, out, err = _rejected(tmp_path, monkeypatch, capsys, runner)

    assert rc == 2
    assert out == ""
    assert "不算绿（R6）" in err
    assert "NO_CHECKS" in err
    # 「不写成通过」：stdout / stderr 里都不许出现 CI 的 PASS 行或「通过」二字
    assert "CI | PASS" not in out + err
    assert "通过" not in err.replace("不算绿", "").replace("拒绝合并", "")


def test_no_checks_read_from_stderr_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """真 gh 把那句话打在 stderr：只看 stdout 会把它读成 FAIL，文案就指错方向。"""

    runner = ChecksRunner(checks_rc=1, checks_stdout="", checks_stderr=NO_CHECKS_OUT)
    rc, _, err = _rejected(tmp_path, monkeypatch, capsys, runner)

    assert rc == 2
    assert "NO_CHECKS" in err


# ------------------------------------------------------ 验收 1：PASS 行与位置


def test_ci_pass_line_sits_between_evidence_and_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """全绿：``CI | PASS | gh pr checks <n> -R <slug> | rc=0 | <输出末行>`` 落在
    证据状态机之后、双问绑定之前，整条合并走通。"""

    runner = ChecksRunner(
        checks_rc=0, checks_stdout=PASS_OUT, checks_stderr="", ls_remote=(0, 2)
    )
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, runner=runner)

    assert (rc, err) == (0, "")
    lines = out.splitlines()
    evidence = next(i for i, line in enumerate(lines) if line.startswith("证据状态机 | PASS |"))
    ci = next(i for i, line in enumerate(lines) if line.startswith("CI | PASS |"))
    review = next(i for i, line in enumerate(lines) if line.startswith("双问绑定 | PASS |"))
    merge = next(i for i, line in enumerate(lines) if line.startswith("合并 | PASS |"))
    assert evidence < ci < review < merge
    assert lines[ci] == (
        f"CI | PASS | gh pr checks {PR} -R alsj213/team-board | rc=0 | "
        "pytest (3.12)\tpass\t41s\thttps://example.invalid/run/2"
    )
    assert runner.merge_calls() != []  # 全绿时合并照常发生


def test_verify_only_never_calls_gh_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """契约 C：``--verify-only`` 一次 gh 都不调（它不合并，也就不存在 CI 闸门这一步）。"""

    runner = ChecksRunner()
    _rc, _, _ = run_main(tmp_path, monkeypatch, capsys, runner=runner, verify_only=True)

    assert checks_calls(runner) == []


# ------------------------------------------------------------ 取数命令与接线细节


def test_checks_argv_pins_repo() -> None:
    """取数命令钉住 ``-R``（与预检 / 合并同一个 slug，「看 A 的 CI、合 B」不成立）。"""

    assert mg.checks_argv(7, "alsj213/team-board") == (
        "gh",
        "pr",
        "checks",
        "7",
        "-R",
        "alsj213/team-board",
    )
    assert " ".join(mg.checks_argv(PR, "alsj213/team-board")) == (
        f"gh pr checks {PR} -R alsj213/team-board"
    )


def test_ci_rejection_short_circuits_after_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CI 拒绝时闸门链就停在 CI 这一步：gh 调用只有预检 + 取 CI，没有后续步骤。"""

    runner = ChecksRunner(checks_rc=1, checks_stdout=FAIL_OUT, checks_stderr="")
    rc, out, err = _rejected(tmp_path, monkeypatch, capsys, runner)

    assert (rc, out) == (2, "")
    assert "CI 未通过" in err
    gh_calls = [argv for argv in runner.calls if argv[0] == "gh"]
    assert [argv[2] for argv in gh_calls] == ["view", "checks"]


def test_head_sha_still_pinned_in_merge_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CI 全绿不改变「合并钉 head sha」：合进去的仍是预检看到的那条提交。"""

    runner = ChecksRunner(checks_rc=0, checks_stdout=PASS_OUT, checks_stderr="")
    rc, _, _ = run_main(tmp_path, monkeypatch, capsys, runner=runner)

    assert rc == 0
    merge = runner.merge_calls()
    assert len(merge) == 1
    assert f"--match-head-commit {HEAD_OID}" in " ".join(merge[0])
    assert mg.STEP_CHECKS == "CI"


def test_cli_argv_helper_is_the_gate_entry(tmp_path: Path) -> None:
    """``cli_argv`` 走的是合并模式（带认账四参数）—— 本文件的拒绝用例都是真合并路径。"""

    argv = cli_argv(tmp_path)
    assert "--card" in argv and "--verify-only" not in argv
