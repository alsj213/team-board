"""merge_guard 的集成测试：走 ``main(argv)``，gh / git 一律用临时目录里的假脚本。

**禁止联调**：测试里出现的 ``gh`` / ``git`` 全部是本目录 fixture 造的可执行假脚本，
塞在 ``PATH`` 最前面，调用记录写进 ``$FAKE_LOG``；既不打真实 GitHub，也不碰真远端。
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from team_board import merge_guard as mg

CARD_BRANCH = "wt/t_bbeab29f"

#: 假 gh 的 ``pr view`` 报出来的 head 提交（40 位十六进制）：合并 argv 必须带它
HEAD_OID = "4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a2"
#: ``gh pr view`` 要的字段（``headRefOid`` 是「预检钉到提交」的凭据）
VIEW_FIELDS = "number,state,headRefName,baseRefName,headRefOid"
VIEW_CALL = f"gh pr view 9 --json {VIEW_FIELDS}"
#: 合并命令原文：分支名 + 钉住的 head 提交，一条都不许少
MERGE_CALL = f"gh pr merge 9 --merge --delete-branch --match-head-commit {HEAD_OID}"

GH_STUB = """#!/bin/sh
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  # 合并之后（marker 存在）的反查走另一份固定输出：合并前必须报 OPEN，
  # 合并后要能报 MERGED（gh 非 0 时的 state 反查用例）
  if [ -n "$FAKE_MERGED_MARK" ] && [ -f "$FAKE_MERGED_MARK" ] && [ -n "$FAKE_VIEW_JSON_AFTER" ]; then
    printf '%s' "$FAKE_VIEW_JSON_AFTER"
    exit "${FAKE_VIEW_RC_AFTER:-0}"
  fi
  if [ -n "$FAKE_VIEW_SLEEP" ]; then sleep "$FAKE_VIEW_SLEEP"; fi
  printf '%s' "$FAKE_VIEW_JSON"
  exit "${FAKE_VIEW_RC:-0}"
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  if [ -n "$FAKE_MERGED_MARK" ]; then : > "$FAKE_MERGED_MARK"; fi
  if [ -n "$FAKE_MERGE_OUT" ]; then printf '%s\\n' "$FAKE_MERGE_OUT"; fi
  if [ -n "$FAKE_MERGE_ERR" ]; then printf '%s\\n' "$FAKE_MERGE_ERR" >&2; fi
  exit "${FAKE_MERGE_RC:-0}"
fi
printf 'fake gh: unexpected args: %s\\n' "$*" >&2
exit 9
"""

GIT_STUB = """#!/bin/sh
printf 'git %s\\n' "$*" >> "$FAKE_LOG"
n=0
if [ -n "$FAKE_GIT_COUNT" ] && [ -f "$FAKE_GIT_COUNT" ]; then n=$(cat "$FAKE_GIT_COUNT"); fi
n=$((n + 1))
if [ -n "$FAKE_GIT_COUNT" ]; then printf '%s' "$n" > "$FAKE_GIT_COUNT"; fi
# 第一次 ls-remote 可以是「合并前源分支存在」闸门，用独立的退出码
if [ "$n" = "1" ] && [ -n "$FAKE_LS_REMOTE_FIRST_RC" ]; then
  exit "$FAKE_LS_REMOTE_FIRST_RC"
fi
if [ -n "$FAKE_GIT_SLEEP" ]; then sleep "$FAKE_GIT_SLEEP"; fi
exit "${FAKE_LS_REMOTE_RC:-2}"
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@dataclass
class Stub:
    """一次性环境：假 gh / 假 git + 调用日志 + 空仓库目录。"""

    bindir: Path
    log: Path
    repo: Path
    setenv: Callable[[str, str], None]

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def gh_calls(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("gh ")]

    def git_calls(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("git ")]

    def view(
        self,
        *,
        state: str = "OPEN",
        head: str = CARD_BRANCH,
        base: str = "main",
        head_oid: str | None = HEAD_OID,
    ) -> None:
        payload: dict[str, object] = {
            "number": 9,
            "state": state,
            "headRefName": head,
            "baseRefName": base,
        }
        if head_oid is not None:  # None = 故意让假 gh 少报 headRefOid
            payload["headRefOid"] = head_oid
        self.setenv("FAKE_VIEW_JSON", json.dumps(payload))

    def view_after_merge(self, *, state: str = "MERGED", rc: int = 0) -> None:
        """合并之后（``gh pr merge`` 已经跑过）的 ``gh pr view --json state`` 输出。"""

        self.setenv("FAKE_VIEW_JSON_AFTER", json.dumps({"state": state}))
        self.setenv("FAKE_VIEW_RC_AFTER", str(rc))


@pytest.fixture()
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir()
    _write_exe(bindir / "gh", GH_STUB)
    _write_exe(bindir / "git", GIT_STUB)
    log = tmp_path / "calls.log"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_GIT_COUNT", str(tmp_path / "git.count"))
    monkeypatch.setenv("FAKE_MERGED_MARK", str(tmp_path / "merged.mark"))
    monkeypatch.setenv("FAKE_VIEW_RC", "0")
    monkeypatch.setenv("FAKE_MERGE_RC", "0")
    # 默认：合并前的「源分支存在」闸门通过（分支在），合并后的探针报「没有这个 ref」
    monkeypatch.setenv("FAKE_LS_REMOTE_FIRST_RC", "0")
    monkeypatch.setenv("FAKE_LS_REMOTE_RC", "2")
    out = Stub(bindir=bindir, log=log, repo=repo, setenv=monkeypatch.setenv)
    out.view()
    return out


def args_for(stub: Stub, *extra: str, delete_branch: bool = True) -> list[str]:
    """一条能走通的基本命令；``extra`` 追加在最后（覆盖同名参数）。"""

    base = [
        "--pr",
        "9",
        "--branch",
        CARD_BRANCH,
        "--method",
        "merge",
        "--repo",
        str(stub.repo),
        "--timeout",
        "1",
        "--interval",
        "1",
    ]
    if delete_branch:
        base.append("--delete-branch")
    return base + list(extra)


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = mg.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ------------------------------------------------ 1. 缺 --delete-branch 被拦

def test_missing_delete_branch_rc2_and_no_gh_call(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = run(args_for(stub, delete_branch=False), capsys)

    assert rc == 2
    assert out == ""  # stdout 必须空
    assert err.startswith("错误: 合并命令缺少 --delete-branch")
    assert err.count("\n") == 1  # stderr 只有一行
    assert "本工具不提供不带删除的合并路径" in err
    assert stub.calls() == []  # 一个 gh（乃至 git）写操作都没发


def test_no_way_to_turn_off_delete_branch(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """没有关掉删除的开关，也没有绕过 PR 检查的口子。"""

    for flag in ("--no-delete-branch", "--keep-branch", "--admin", "--auto", "--force"):
        rc, out, err = run(args_for(stub, flag), capsys)
        assert rc == 2, flag
        assert out == "", flag
        assert err.startswith("错误: "), flag
    assert stub.calls() == []


# --------------- 1b. 合并前源分支不存在：rc2、stdout 空、零 gh 调用（新闸门）

def test_merge_path_branch_never_existed_rc2_and_zero_gh_calls(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """「这个 ref 从来没存在过」不许被读成「已删除」：合并前就 rc2。

    依据（pm 实跑）：``--verify-only --branch wt/t_never_existed_zzz`` 修前打
    「结论：分支已删除」+ rc0。合并路径同理 —— 闸门必须在 ``gh pr view`` 之前。
    """

    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "2")  # 合并前就「没有这个 ref」

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""  # 合并还没发生 → 一个字的证据都不该有
    assert err.startswith("错误: 源分支 wt/t_bbeab29f 不在 origin 远端（rc=2）")
    assert err.count("\n") == 1  # stderr 恰一行
    assert stub.gh_calls() == []  # 零 gh 调用（写操作更是一个都没有）
    assert len(stub.git_calls()) == 1


def test_merge_path_branch_probe_error_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """合并前的探针通信错误（rc=128）同样是 rc2，不许当成「分支在」继续合并。"""

    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "128")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "rc=128" in err
    assert stub.gh_calls() == []


# ------ 1d. --branch 形态校验（P2-1）：空串 / 含空白 / refs/ 前缀 → rc2、零外部调用
#
# 修前（pm 实测）：--verify-only 对这三种输入都打「分支删除 | PASS」+ rc0。原因是
# ls-remote 的入参是 refs/heads/<--branch> 直接拼的：'' → refs/heads/、
# ' wt/x' → refs/heads/ wt/x、'refs/heads/wt/x' → refs/heads/refs/heads/wt/x，
# 三者在远端都不存在 → rc=2 → 被读成「已删除」。合并路径与 --verify-only 必须同一套校验。

MALFORMED_BRANCHES: list[tuple[str, str, str]] = [
    ("空串", "", mg.BRANCH_RULE_EMPTY),
    ("前导空格", f" {CARD_BRANCH}", mg.BRANCH_RULE_WHITESPACE),
    ("中间制表符", "wt/\tt_bbeab29f", mg.BRANCH_RULE_WHITESPACE),
    ("全角空格", f"　{CARD_BRANCH}", mg.BRANCH_RULE_WHITESPACE),
    ("尾随换行", f"{CARD_BRANCH}\n", mg.BRANCH_RULE_WHITESPACE),
    ("refs/ 前缀", f"refs/heads/{CARD_BRANCH}", mg.BRANCH_RULE_REFS_PREFIX),
]


@pytest.mark.parametrize(("label", "bad_branch", "rule"), MALFORMED_BRANCHES)
@pytest.mark.parametrize("verify_only", [False, True])
def test_malformed_branch_rc2_with_zero_calls(
    stub: Stub,
    capsys: pytest.CaptureFixture[str],
    label: str,
    bad_branch: str,
    rule: str,
    verify_only: bool,
) -> None:
    """畸形 ``--branch``：两条路径都 rc2、stdout 空、stderr 恰一行、零 gh / 零 git。"""

    extra = ["--verify-only"] if verify_only else []
    rc, out, err = run(args_for(stub, "--branch", bad_branch, *extra), capsys)

    assert rc == 2, label
    assert out == "", label  # 一个字的证据都没有（更不许有 PASS 行）
    assert err.startswith("错误: --branch "), label
    assert rule in err, label  # 说清是哪条规则
    assert err.count("\n") == 1, label  # stderr 严格一行
    assert stub.calls() == [], label  # 零 gh、零 git


# ------------------------------------- 2. 合并成功但分支仍在 → rc1（不算成功）

def test_merge_ok_but_branch_remains_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "0")  # ref 仍在

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 1
    assert err == ""
    assert "结论：合并成功但源分支仍在远端（合并不算完成）" in out
    assert "分支删除 | FAIL" in out
    assert out.splitlines()[0].startswith("分支存在 | PASS | git -C ")
    assert MERGE_CALL in stub.gh_calls()
    assert stub.git_calls()  # 确实做了远端判定


# ------------------------------------------ 3. 合并成功 + ls-remote rc2 → rc0

def test_merge_ok_and_branch_deleted_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "2")  # 没有这个 ref

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0
    assert err == ""
    lines = out.splitlines()
    assert lines[0].startswith(
        f"分支存在 | PASS | git -C {stub.repo} ls-remote --exit-code --refs "
        f"origin refs/heads/{CARD_BRANCH} | rc=0 | "
    )
    assert lines[1].startswith(f"预检 | PASS | {VIEW_CALL} | rc=0 | ")
    # 预检钉到提交：新增一行 head sha 证据（值是假 gh 报的 headRefOid）
    assert lines[2] == f"head sha | PASS | {HEAD_OID}"
    assert lines[3].startswith(f"合并 | PASS | {MERGE_CALL} | rc=0 | ")
    assert lines[4].startswith(
        f"分支删除 | PASS | git -C {stub.repo} ls-remote --exit-code --refs "
        f"origin refs/heads/{CARD_BRANCH} | rc=2 | "
    )
    assert lines[-1] == f"结论：合并完成，源分支 {CARD_BRANCH} 已从 origin 删除"
    assert stub.gh_calls() == [
        VIEW_CALL,
        MERGE_CALL,
    ]


# -------- 1c. 预检钉到提交（P2-3）：--match-head-commit + head sha 证据行
#
# 修之前：预检只比分支名，预检到合并之间被 force-push / 追加提交时，合的是预检从未
# 见过的提交，工具照样 PASS + rc0。现在 gh pr view 读 headRefOid、合并命令带
# --match-head-commit，预检通过后把 sha 记进证据。

def test_merge_pins_head_sha(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """``gh pr merge`` 必须收到 ``--match-head-commit <headRefOid>``，并留一行 head sha 证据。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0
    assert err == ""
    assert f"head sha | PASS | {HEAD_OID}" in out  # 预检钉到提交的证据行
    assert MERGE_CALL in stub.gh_calls()
    # 每一次合并调用都必须带 sha：一条不带 sha 的重试都不许有
    merge_calls = [call for call in stub.gh_calls() if call.startswith("gh pr merge")]
    assert merge_calls == [MERGE_CALL]


def test_view_missing_head_oid_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """假 gh 少报 ``headRefOid`` → rc2、stdout 空、stderr 一行、绝不合并。"""

    stub.view(head_oid=None)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "headRefOid" in err
    assert "gh pr merge" not in " ".join(stub.gh_calls())


@pytest.mark.parametrize(
    "head_oid",
    ["4f2a9c0", "zz" * 20, ""],
    ids=["short", "non-hex", "empty"],
)
def test_view_bad_head_oid_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], head_oid: str
) -> None:
    """``headRefOid`` 不是 40/64 位十六进制 → rc2（没有合法 sha 就不许合并）。"""

    stub.view(head_oid=head_oid)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "gh pr merge" not in " ".join(stub.gh_calls())


def test_old_gh_unknown_match_head_commit_rc2_no_downgrade(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """老版本 gh 报 ``unknown flag: --match-head-commit`` → rc2，**不许**降级合并。

    判据：stdout 空、stderr 恰一行、只发过一次合并调用（就是带 sha 那次）、
    合并后的删除验证一次都没跑。
    """

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.setenv("FAKE_MERGE_ERR", "unknown flag: --match-head-commit")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1：环境不支持，判不了
    assert out == ""  # 合并一定没发生 → 一个字的证据都不该有
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "--match-head-commit" in err
    assert "不提供不带 head sha 的合并路径" in err
    merge_calls = [call for call in stub.gh_calls() if call.startswith("gh pr merge")]
    assert merge_calls == [MERGE_CALL]  # 没有「不带 sha 再试一次」
    assert len(stub.git_calls()) == 1  # 只有合并前那次存在性探针，没有删除验证


# ------------------- 1d. --timeout 是总预算（P2-1）：单次卡住也不许击穿它
#
# 修之前：单次探针只受 --command-timeout 约束，--timeout 1 + 卡 3 秒的 git → 3.1s。

def test_verify_only_timeout_is_total_budget(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--timeout 1`` + 单次 probe 3 秒 → 总耗时 ≤ ~1.5s，结论「无法判定」rc1。"""

    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")  # 别让「第一次」跳过 sleep
    stub.setenv("FAKE_GIT_SLEEP", "3")
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    started = time.monotonic()
    rc, out, err = run(
        args_for(
            stub,
            "--verify-only",
            "--timeout",
            "1",
            "--interval",
            "1",
            "--command-timeout",
            "60",
        ),
        capsys,
    )
    elapsed = time.monotonic() - started

    assert rc == 1  # --verify-only 的「无法判定」按现有口径是 1
    assert err == ""
    assert elapsed <= 1.5  # 不许跑满 3 秒
    assert "结论：无法判定" in out
    assert "总预算" in out


def test_merge_path_timeout_is_total_budget_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """合并已发生后删除验证卡住 + ``--timeout 1`` → rc2（判不了），耗时 ≈ 1 秒。"""

    stub.setenv("FAKE_GIT_SLEEP", "3")
    stub.setenv("FAKE_LS_REMOTE_RC", "128")  # 第 2 次起的探针：睡满再报通信错误

    started = time.monotonic()
    rc, out, err = run(
        args_for(stub, "--timeout", "1", "--interval", "1", "--command-timeout", "60"),
        capsys,
    )
    elapsed = time.monotonic() - started

    assert rc == 2  # 合并已发生但判不了 → rc2，不是 1
    assert elapsed <= 1.5
    assert f"合并 | PASS | {MERGE_CALL}" in out  # 证据必须留住
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "无法判定" in err
    assert "总预算" in err


# ------------------------------- 3b. --method 只认 merge（R13）：默认值 + 拒绝两值

def test_method_defaults_to_merge_without_flag(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """**完全不传 ``--method``**：gh 收到的 argv 里必须是 ``--merge``。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    argv = [
        "--pr",
        "9",
        "--branch",
        CARD_BRANCH,
        "--repo",
        str(stub.repo),
        "--delete-branch",
        "--timeout",
        "1",
        "--interval",
        "1",
    ]
    assert "--method" not in argv  # 确保真的没传

    rc, out, err = run(argv, capsys)

    assert rc == 0
    assert err == ""
    assert f"合并 | PASS | {MERGE_CALL} | rc=0 | " in out
    assert stub.gh_calls() == [
        VIEW_CALL,
        MERGE_CALL,
    ]


@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_squash_and_rebase_rejected_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], method: str
) -> None:
    """R13：squash / rebase 一律 rc2、stdout 空、stderr 恰一行、**零 gh/git 调用**。"""

    rc, out, err = run(args_for(stub, "--method", method), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert method in err  # 说清实际收到的值
    assert stub.calls() == []  # 连「分支存在」预检都没跑


def test_gh_stderr_warning_is_not_a_failure(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 的 stderr 警告（worktree 里删不掉本地分支）不算失败，判定只看远端。"""

    stub.setenv("FAKE_MERGE_ERR", "failed to delete local branch wt/t_bbeab29f")
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, _err = run(args_for(stub), capsys)

    assert rc == 0
    assert "结论：合并完成" in out


# ------------------------------- 4. gh pr merge 非 0：先反查 state，再决定 rc
#
# gh 2.103.3 的 mergeRun 顺序是 merge() → deleteLocalBranch() → deleteRemoteBranch()：
# 删本地分支失败就 return 非 0，而那时 PR 已合、远端分支还没删。所以「gh 非 0」不能
# 一律当「合并失败」。

def test_merge_failure_rc1_and_no_verification(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 非 0 + state 不是 MERGED → rc1「合并失败」，且不跑删除验证。"""

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.setenv("FAKE_MERGE_ERR", "pull request is not mergeable: required check failing")
    stub.view_after_merge(state="OPEN")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 1
    # 只有合并前那一次「源分支存在」探针，合并后一次删除验证都没跑
    assert len(stub.git_calls()) == 1
    assert stub.git_calls()[0].startswith("git -C ")
    assert MERGE_CALL in stub.gh_calls()
    assert "gh pr view 9 --json state" in stub.gh_calls()  # 做了状态反查
    assert "合并 | FAIL" in out
    assert "合并失败" in out
    assert "PR 状态 OPEN" in out
    assert "合并成功" not in out
    assert not [line for line in out.splitlines() if line.startswith("分支删除 | ")]
    assert "required check failing" in err  # gh 输出原文写出来了


def test_merge_gh_nonzero_but_state_merged_then_verification_deleted_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 非 0 + state=MERGED + 远端分支已删 → rc0，**不许**说「合并失败」。

    真实触发：``gh pr merge --delete-branch`` 删本地分支失败（分支被别处 worktree
    / checkout 占着）→ 非 0 退出，但 PR 已经合并、远端分支还没删。
    """

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.setenv("FAKE_MERGE_ERR", "failed to delete local branch wt/t_bbeab29f")
    stub.view_after_merge(state="MERGED")
    stub.setenv("FAKE_LS_REMOTE_RC", "2")  # 远端已经没有这个 ref

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0
    assert "合并失败" not in out
    assert "gh pr view 9 --json state" in stub.gh_calls()
    assert f"合并 | PASS | {MERGE_CALL} | rc=1 | " in out
    assert "PR 状态已是 MERGED" in out
    assert "分支删除 | PASS | " in out
    assert out.splitlines()[-1] == f"结论：合并完成，源分支 {CARD_BRANCH} 已从 origin 删除"
    # gh 的退出码与输出原文都留了证
    assert "rc=1" in out
    assert "failed to delete local branch" in err


def test_merge_gh_nonzero_state_merged_branch_present_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 非 0 + state=MERGED + 分支仍在远端 → rc1（合并成功但没删干净）。"""

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.view_after_merge(state="MERGED")
    stub.setenv("FAKE_LS_REMOTE_RC", "0")

    rc, out, _err = run(args_for(stub), capsys)

    assert rc == 1
    assert "合并失败" not in out
    assert out.splitlines()[-1] == "结论：合并成功但源分支仍在远端（合并不算完成）"


def test_merge_gh_nonzero_state_undetermined_rc2_keeps_evidence(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 非 0 + MERGED + 删除验证判不了 → 走「合并已完成但无法判定」rc2。"""

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.view_after_merge(state="MERGED")
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1
    assert "合并失败" not in out
    assert out.splitlines()[0].startswith("分支存在 | PASS | ")
    assert f"合并 | PASS | {MERGE_CALL}" in out
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "无法判定" in err


def test_merge_gh_nonzero_state_query_fails_stays_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """反查本身失败 → 退回旧口径 rc1「合并失败」（反查不是新的失败源，不许变 rc2）。"""

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.view_after_merge(state="MERGED", rc=1)  # 反查命令自己失败

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 1
    assert "合并失败" in out
    assert "PR 状态 反查失败" in out
    assert err.count("\n") == 0 or "错误: " not in err


# ------------------------------------------- 5. --verify-only（一次 gh 都不调）

def test_verify_only_branch_present_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "0")

    rc, out, err = run(args_for(stub, "--verify-only"), capsys)

    assert rc == 1
    assert err == ""
    assert out.splitlines()[-1] == "结论：分支仍在远端"
    # 步骤行只报 ref 现状：--verify-only 没有「删除」这一步，不许打 PASS / FAIL
    assert out.splitlines()[0].startswith("远端 ref | 存在 | ")
    assert "PASS" not in out and "FAIL" not in out
    assert stub.gh_calls() == []


def test_verify_only_branch_deleted_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """远端没有这个 ref → rc0，但结论**只能说「没有」，不许说「分支已删除」**。

    因为 ``--verify-only`` 不知道这个分支是「删掉了」还是「从来没存在过」——
    后者正是修前把 ``wt/t_never_existed_zzz`` 读成 rc0「已删除」的那条路。

    步骤行同理：修前打 ``分支删除 | PASS | … | rc=2 | 分支已不在远端``，那一列会被
    抄进卡当「删除已验证」的证据，而这条路径一次删除都没验证过。
    """

    stub.setenv("FAKE_LS_REMOTE_RC", "2")
    # --verify-only 没有「合并前存在性」那道闸门：别让第一次调用被 FIRST_RC 抢走
    # （否则第一次报「分支在」，--timeout 1 的预算就只够一次探针，结论变成 rc1）
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")

    rc, out, err = run(args_for(stub, "--verify-only"), capsys)

    assert rc == 0  # rc 语义不变
    assert err == ""
    assert out.splitlines()[-1] == "结论：远端没有这个 ref（已删除，或从未存在过）"
    assert "分支已删除" not in out  # 旧措辞（会掩盖「从未存在过」）
    assert "或从未存在过" in out
    # 步骤行：只报「远端 ref 不存在」，既不写 PASS，也不写「分支删除」
    steps = [line for line in out.splitlines() if line.startswith("远端 ref | ")]
    assert len(steps) == 1
    assert steps[0].startswith("远端 ref | 不存在 | ")
    assert "rc=2" in steps[0]
    assert "PASS" not in out
    assert "分支删除" not in out
    assert stub.gh_calls() == []


def test_verify_only_legal_branch_rc2_never_claims_delete(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """合法 ``--branch`` + ``--verify-only`` + ``ls-remote rc=2``：输出里没有宣称删除的 PASS。

    这是本卡的验收判据②：``rc=2`` 只能说明「这个 remote 现在没有这个 ref」。
    同时钉住探针查的 ref **就是裸分支名拼出来的那一个** —— 不许出现
    ``refs/heads/refs/heads/…`` 或带空白的 ref（那才是修前「假绿」的来源）。
    """

    stub.setenv("FAKE_LS_REMOTE_RC", "2")
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")  # --verify-only 没有「合并前存在」闸门

    rc, out, err = run(args_for(stub, "--verify-only"), capsys)

    assert rc == 0
    assert err == ""
    for line in out.splitlines():
        if "PASS" in line:
            assert "删除" not in line, line  # 任何 PASS 行都不许宣称删除
    assert "分支删除" not in out
    assert "已删除 |" not in out  # 步骤行的状态字段只有 存在 / 不存在 / 无法判定
    assert stub.git_calls() == [
        f"git -C {stub.repo} ls-remote --exit-code --refs origin refs/heads/{CARD_BRANCH}"
    ]


def test_verify_only_undetermined_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "128")
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")  # 同上：第一次就该是 128，不是「分支在」

    rc, out, _err = run(args_for(stub, "--verify-only"), capsys)

    assert rc == 1
    assert out.splitlines()[-1].startswith("结论：无法判定（")


# --------------------------------------- 6. 参数取值域越界（全部 rc2、stdout 空）

@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--pr", "0"),
        ("--pr", "-1"),
        ("--pr", "1793160000000"),  # 13 位毫秒
        ("--pr", "1000000000"),
        ("--pr", "abc"),
        ("--pr", ""),
        ("--timeout", "0"),
        ("--timeout", "-1"),
        ("--timeout", "1_000"),
        ("--timeout", ""),
        ("--timeout", "abc"),
        ("--interval", "0"),
        ("--interval", "-5"),
        ("--interval", ""),
        ("--method", "fast-forward"),
        ("--method", ""),
    ],
)
def test_bad_values_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    rc, out, err = run(args_for(stub, flag, value), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert stub.calls() == []


def test_pr_boundary_accepted(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """上界 999999999 本身是合法值（不是越界）。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, _err = run(
        [
            "--pr",
            "999999999",
            "--branch",
            CARD_BRANCH,
            "--delete-branch",
            "--repo",
            str(stub.repo),
            "--timeout",
            "1",
            "--interval",
            "1",
        ],
        capsys,
    )

    assert rc == 0
    # 未传 --method → 默认 merge（gh 收到的是 --merge）
    assert (
        f"gh pr merge 999999999 --merge --delete-branch --match-head-commit {HEAD_OID}"
        in stub.gh_calls()
    )
    assert "结论：合并完成" in out


# ----------------------------------------- 7. 安全：不许删主分支 / base 分支

def test_branch_main_rejected(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--pr",
        "9",
        "--branch",
        "main",
        "--method",
        "merge",
        "--delete-branch",
        "--repo",
        str(stub.repo),
    ]

    rc, out, err = run(argv, capsys)

    assert rc == 2
    assert out == ""
    assert "主分支" in err
    assert stub.calls() == []  # 连 preflight 都没跑


def test_branch_master_rejected(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--pr",
        "9",
        "--branch",
        "master",
        "--method",
        "merge",
        "--delete-branch",
        "--repo",
        str(stub.repo),
    ]

    rc, out, err = run(argv, capsys)

    assert rc == 2
    assert out == ""
    assert "主分支" in err


def test_branch_equals_base_ref_rejected(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.view(head="release", base="release")

    rc, out, err = run(args_for(stub, "--branch", "release"), capsys)

    assert rc == 2
    assert out == ""
    assert "baseRefName" in err
    # 只跑了 preflight，没有发起合并
    assert stub.gh_calls() == [VIEW_CALL]


# ------------------------------- 8. --branch 与 PR headRefName 不一致 → rc2

def test_branch_mismatch_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    stub.view(head="wt/t_aaaaaaaa")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert "headRefName" in err
    assert "wt/t_aaaaaaaa" in err  # 说清实际值
    assert "gh pr merge" not in " ".join(stub.gh_calls())


def test_pr_not_open_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    stub.view(state="MERGED")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert "MERGED" in err
    assert "gh pr merge" not in " ".join(stub.gh_calls())


# ------------------------------------------- 9. PATH 里没有 gh → rc2

def test_missing_gh_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "git-only-bin"
    bindir.mkdir()
    _write_exe(bindir / "git", GIT_STUB)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("FAKE_GIT_COUNT", str(tmp_path / "git.count"))
    monkeypatch.setenv("FAKE_LS_REMOTE_FIRST_RC", "0")  # 分支存在闸门先过
    repo = tmp_path / "repo"
    repo.mkdir()

    rc = mg.main(
        [
            "--pr",
            "9",
            "--branch",
            CARD_BRANCH,
            "--method",
            "merge",
            "--delete-branch",
            "--repo",
            str(repo),
            "--timeout",
            "1",
            "--interval",
            "1",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert captured.err.startswith("错误: ")
    assert "gh" in captured.err


def test_missing_git_rc2_before_any_gh_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git`` 不可用 → 合并前的「源分支存在」闸门就 rc2：零 gh 调用、stdout 空。

    修前这一步不存在，git 缺失只能在**合并之后**才被发现（rc2 但 PR 已被合并）。
    现在往前挪到合并之前，这个风险整条消失。
    """

    bindir = tmp_path / "gh-only-bin"
    bindir.mkdir()
    _write_exe(bindir / "gh", GH_STUB)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("PATH", str(bindir))  # 没有 git
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_MERGED_MARK", str(tmp_path / "merged.mark"))
    monkeypatch.setenv("FAKE_VIEW_RC", "0")
    monkeypatch.setenv("FAKE_MERGE_RC", "0")
    monkeypatch.setenv(
        "FAKE_VIEW_JSON",
        json.dumps(
            {"number": 9, "state": "OPEN", "headRefName": CARD_BRANCH, "baseRefName": "main"}
        ),
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    rc, out, err = run(args_for(Stub(bindir, log, repo, monkeypatch.setenv)), capsys)

    assert rc == 2  # 不是 1
    assert out == ""  # 合并没发生 → 没有证据要保
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "git" in err
    assert not log.exists() or "gh " not in log.read_text(encoding="utf-8")


def test_git_vanishes_after_merge_keeps_evidence(
    stub: Stub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并成功后 git 突然不可用（容器重建 / PATH 被改）→ rc2，且必须保住已完成证据。

    合并前的「分支存在」闸门用的是同一次 ``_run``，所以这里只让**合并后的探针**
    失败（``make_probe`` 返回的探针抛 ``MergeGuardError``），复现「合并已发生、验证
    做不了」这一状态。
    """

    def make_broken_probe(*_args: object, **_kwargs: object) -> Callable[[], mg.CommandResult]:
        def probe() -> mg.CommandResult:
            raise mg.MergeGuardError("找不到 git 可执行文件（PATH 里没有 git），无法合并/验证")

        return probe

    monkeypatch.setattr(mg, "make_probe", make_broken_probe)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1
    lines = out.splitlines()
    assert lines[0].startswith("分支存在 | PASS | ")
    assert lines[1].startswith("预检 | PASS | ")
    assert lines[3].startswith(f"合并 | PASS | {MERGE_CALL} | ")
    assert "Traceback" not in out + err
    assert err.startswith("错误: ")
    assert err.count("\n") == 1  # stderr 只有一行
    assert "git" in err
    assert MERGE_CALL in stub.gh_calls()


# ------------------------------- 合并后通信一直失败 → rc2（明确未通过，不当绿）

def test_merge_ok_but_undetermined_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1：判不了就是未通过
    lines = out.splitlines()
    assert lines[0].startswith("分支存在 | PASS | ")  # 合并已发生 → 证据必须留下
    assert lines[1].startswith("预检 | PASS | ")
    assert lines[3].startswith(f"合并 | PASS | {MERGE_CALL}")
    assert "Traceback" not in out + err
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "无法判定" in err


# --------------------------------------------------- 10. 回归：--help rc0

def test_help_rc0(capsys: pytest.CaptureFixture[str]) -> None:
    rc = mg.main(["--help"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "--delete-branch" in captured.out
    assert "--verify-only" in captured.out
    assert "--command-timeout" in captured.out
    # R13：--method 值域只剩 merge，且默认值要写出来（不再要求调用方必传）
    assert "--method {merge}" in captured.out
    assert "默认 merge" in captured.out
    assert "squash" in captured.out  # 只出现在「一律拒绝」的说明里
    assert captured.err == ""


# --------------------- 11. 三个时长参数的取值域上界（max 3600，越界一律 rc2）

BOUNDARY_VALUES = [
    "0",
    "-1",
    "1793160000000",  # 13 位毫秒：修好之前它会让 poll 挂死（rc=124）
    "9" * 400,  # 天文数字：修好之前它会在 clock() + timeout 上裸抛 OverflowError
    "",
]


@pytest.mark.parametrize("flag", ["--timeout", "--interval", "--command-timeout"])
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_duration_flags_out_of_range_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    rc, out, err = run(args_for(stub, flag, value), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1  # stderr 恰一行
    assert "Traceback" not in err
    assert stub.calls() == []  # 越界在调 gh / git 之前就被拒


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "3601"),
        ("--interval", "3601"),
        ("--command-timeout", "3601"),
    ],
)
def test_duration_upper_bound_exceeded_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    rc, out, err = run(args_for(stub, flag, value), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "3600" in err  # 说清上界
    assert stub.calls() == []


def test_duration_upper_bound_itself_accepted(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """上界 3600 本身合法（不是越界）；假 git 第一次就说「已删」，不会真等。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(
        args_for(
            stub,
            "--timeout",
            "3600",
            "--interval",
            "3600",
            "--command-timeout",
            "3600",
        ),
        capsys,
    )

    assert rc == 0
    assert err == ""
    assert "结论：合并完成" in out


def test_interval_greater_than_timeout_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``interval <= timeout`` 是硬约束：轮询间隔比总预算还长等于没预算。"""

    rc, out, err = run(args_for(stub, "--timeout", "3", "--interval", "10"), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "--interval" in err
    assert stub.calls() == []


# ------------------- 12. 子命令超时（--command-timeout）：预检超时 → rc2

def test_preflight_timeout_rc2_and_no_merge(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``gh pr view`` 卡住 → rc2，且**不许**再发合并命令。"""

    stub.setenv("FAKE_VIEW_SLEEP", "5")

    rc, out, err = run(args_for(stub, "--command-timeout", "1"), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "超时" in err
    assert "Traceback" not in err
    assert "gh pr merge" not in " ".join(stub.gh_calls())


# ------------------- 13. 子命令超时：合并后的探针超时 → 不是 rc0

def test_probe_timeout_after_merge_is_not_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ls-remote`` 卡住不许当成「分支还在」（rc1）或「已删」（rc0）。

    超时按通信错误记一次、继续轮询，预算耗尽后判「无法判定」→ rc2（≠0，也≠1），
    且因为合并已发生，stdout 必须保住「预检 | PASS」「合并 | PASS」。
    """

    stub.setenv("FAKE_GIT_SLEEP", "5")

    rc, out, err = run(
        args_for(stub, "--timeout", "1", "--interval", "1", "--command-timeout", "1"),
        capsys,
    )

    assert rc != 0  # 绝不把超时读成「合并完成」
    assert rc == 2
    assert out.splitlines()[0].startswith("分支存在 | PASS | ")
    assert "预检 | PASS | " in out
    assert f"合并 | PASS | {MERGE_CALL}" in out
    assert "分支删除 | FAIL" in out  # 超时不算 PASS
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "无法判定" in err


# ---------- 14. 非 MergeGuardError 异常：不许落 rc1、不许丢已完成步骤证据

def test_unexpected_exception_after_merge_keeps_evidence(
    stub: Stub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> mg.PollResult:
        raise ValueError("模拟意外崩溃")

    monkeypatch.setattr(mg, "poll_branch_deleted", boom)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1
    lines = out.splitlines()
    assert lines[0].startswith("分支存在 | PASS | ")
    assert lines[1].startswith("预检 | PASS | ")
    assert lines[3].startswith(f"合并 | PASS | {MERGE_CALL} | ")
    assert "Traceback" not in out + err
    assert err.startswith("错误: ")
    assert err.count("\n") == 1  # 一行，没有 traceback
    assert "ValueError" in err
    assert "人工核对" in err


def test_unexpected_exception_before_merge_rc2_stdout_empty(
    stub: Stub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并之前的意外异常：rc2、stdout 空、stderr 一行（没有 traceback）。"""

    def boom(*_args: object, **_kwargs: object) -> mg.CommandResult:
        raise RuntimeError("模拟预检崩溃")

    monkeypatch.setattr(mg, "_run", boom)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert "Traceback" not in err
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "RuntimeError" in err


# --------------------------------- 12. 文档里的命令必须与 CLI 一致（可机器核对）

MERGE_POLICY = Path(__file__).resolve().parents[2] / "docs" / "merge-policy.md"


def _documented_merge_commands() -> list[list[str]]:
    """把 ``docs/merge-policy.md`` 里每行 ``python -m team_board.merge_guard …`` 拆成 argv。

    文档与 CLI 分叉过一次（文档缺 ``--branch`` → 照抄它的合并卡一律 rc2），所以这里
    不靠人眼对：把文档里的命令原文抠出来直接喂给 ``main()``。
    """

    argvs: list[list[str]] = []
    for line in MERGE_POLICY.read_text(encoding="utf-8").splitlines():
        at = line.find("python -m team_board.merge_guard")
        if at < 0:
            continue
        parts = shlex.split(line[at:].split(";")[0].strip())
        assert parts[:3] == ["python", "-m", "team_board.merge_guard"], line
        argvs.append(parts[3:])
    return argvs


def test_documented_commands_carry_required_branch() -> None:
    """条文 ② 与模板 ⑦ 两条命令都必须带 ``--branch wt/<卡号>``（``--branch`` 必填）。"""

    argvs = _documented_merge_commands()

    assert len(argvs) == 2  # 文档里只有这两条 merge_guard 命令；增删都得跟着改这里
    for argv in argvs:
        assert "--branch" in argv, argv
        assert "wt/<卡号>" in argv, argv


def test_documented_command_runs_to_preflight_and_merge(
    stub: Stub, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """照抄文档那条命令（占位符填掉）→ 跑通到预检并合并，而不是 rc2 于缺参数。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    for index, argv in enumerate(_documented_merge_commands()):
        # 每次跑都是一个新进程视角：换一份「第几次 git 调用」计数，
        # 让「分支存在」闸门重新走 FAKE_LS_REMOTE_FIRST_RC=0 那条
        stub.setenv("FAKE_GIT_COUNT", str(tmp_path / f"git-{index}.count"))
        filled = [arg.replace("<n>", "9").replace("<卡号>", "t_bbeab29f") for arg in argv]
        rc, out, err = run(
            [*filled, "--repo", str(stub.repo), "--timeout", "1", "--interval", "1"],
            capsys,
        )

        assert rc == 0, (filled, err)
        assert err == ""
        assert "分支存在 | PASS | " in out
        assert "预检 | PASS | " in out
        assert MERGE_CALL in stub.gh_calls()


# ---------------- 15. stdout 断管 / 关闭：rc 必须是本次运行的真实结论
#
# 断管不是运行结论：``… | head -0`` 与 ``1>&-`` 下不许冒 traceback，也不许把
# rc0/rc1 改成 120/141。这里两种形态各覆盖一遍。

class DeadStdout:
    """写就 EPIPE 的替身 stdout（没有 fileno：不碰进程的 fd 1）。"""

    encoding = "utf-8"

    def write(self, text: str) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.mark.parametrize(
    ("probe_rc", "expected"),
    [("0", 1), ("2", 0)],
    ids=["branch-present-rc1", "branch-deleted-rc0"],
)
def test_broken_pipe_keeps_true_rc(
    stub: Stub,
    monkeypatch: pytest.MonkeyPatch,
    probe_rc: str,
    expected: int,
) -> None:
    """合并路径上 stdout 断管：rc1 仍是 1、rc0 仍是 0，一条 traceback 都没有。"""

    stub.setenv("FAKE_LS_REMOTE_RC", probe_rc)
    monkeypatch.setattr(sys, "stdout", DeadStdout())

    rc = mg.main(args_for(stub))  # 不抛

    assert rc == expected
    assert len(stub.gh_calls()) == 2  # 预检 + 合并都照常发生
    assert MERGE_CALL in stub.gh_calls()


def test_broken_pipe_keeps_rc2_with_evidence(
    stub: Stub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """合并已发生 + 判不了 + stdout 断管：rc 仍 2（不是 1），stderr 一行仍在。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "128")
    monkeypatch.setattr(sys, "stdout", DeadStdout())

    rc = mg.main(args_for(stub))
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""  # 写不出来就是写不出来
    assert captured.err.startswith("错误: ")
    assert captured.err.count("\n") == 1
    assert "无法判定" in captured.err


def _spawn(argv: list[str], *, cwd: Path) -> subprocess.Popen[str]:
    """跑一个真子进程的 merge_guard（``python -m``），stdout/stderr 走管道。"""

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(mg.__file__).resolve().parents[1])
    return subprocess.Popen(
        [sys.executable, "-m", "team_board.merge_guard", *argv],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("等待前置条件超时（子进程没有走到预期位置）")


def test_closed_stdout_keeps_true_rc(
    stub: Stub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``1>&-``（真关掉 fd 1）：rc 仍是真实结论，stderr 照常可读。

    用 ``bash -c 'exec 1>&-; exec …'`` —— 这是 ``1>&-`` 的真实形态，不是
    ``>/dev/null``（那只是丢弃输出，fd 1 还开着，压根走不到这条分支）。
    """

    stub.setenv("PYTHONPATH", str(Path(mg.__file__).resolve().parents[1]))
    # ① 分支一直在远端 → --verify-only 轮询到超时 → 结论 1
    # ② 缺 --delete-branch → 结论 2（一个 gh / git 调用都不发）
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "0")
    stub.setenv("FAKE_LS_REMOTE_RC", "0")
    cases = [
        (args_for(stub, "--verify-only"), 1),
        (args_for(stub, delete_branch=False), 2),
    ]
    for index, (argv, expected) in enumerate(cases):
        stub.setenv("FAKE_GIT_COUNT", str(tmp_path / f"git-closed-{index}.count"))
        script = "exec 1>&-; exec " + shlex.join(
            [sys.executable, "-m", "team_board.merge_guard", *argv]
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            cwd=str(stub.repo),
            env=os.environ.copy(),  # 每次重建：setenv 改的是 os.environ
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        captured = proc.stderr
        assert proc.returncode == expected, (argv, proc.returncode, captured)
        assert "Traceback" not in captured
        assert captured.count("\n") <= 1  # 至多一行 错误: …（rc1 档干脆没有）


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT", "SIGHUP"])
def test_signal_after_merge_keeps_evidence_rc2(
    stub: Stub, tmp_path: Path, signame: str
) -> None:
    """合并已经发生、正在轮询删除验证时被信号杀掉：证据必须留在 stdout、rc=2。

    复现路径（假 gh / 假 git，绝不真合并）：``gh pr merge`` 成功 → 探针卡住 →
    外部发信号。旧行为是 0 字节 stdout + rc=143/130，下一轮会读成「没合并、可以重跑」。
    """

    signums: dict[str, int | None] = {
        name: getattr(signal, name, None) for name in ("SIGTERM", "SIGINT", "SIGHUP")
    }
    signum = signums[signame]
    if signum is None:  # pragma: no cover - Windows
        pytest.skip(f"平台没有 {signame}")

    stub.setenv("FAKE_GIT_SLEEP", "10")  # 合并后的探针卡住（合并前的闸门不睡）
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    proc = _spawn(
        args_for(stub, "--timeout", "30", "--interval", "1"), cwd=stub.repo
    )
    try:
        # git 第 2 次调用 = 合并后的轮询探针已启动 → 「合并 | PASS」那行已经攒好
        _wait_until(lambda: len(stub.git_calls()) >= 2)
        proc.send_signal(signum)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:  # pragma: no cover - 兜底，别留僵尸
            proc.kill()
            proc.communicate()

    assert proc.returncode == 2  # 不是 143 / 130 / 124 / 0
    assert f"合并 | PASS | {MERGE_CALL}" in out
    assert "分支存在 | PASS | " in out  # 合并前那一步也留着
    assert err.startswith(f"错误: 收到信号 {signame}（")
    assert err.count("\n") == 1  # 恰好一行
    assert "Traceback" not in out + err
    assert "合并是否已发生请人工核对" in err


def test_signal_before_merge_rc2_stdout_empty(stub: Stub) -> None:
    """还没走到合并就被杀：stdout 空、rc2、stderr 一行（没有证据要保，也不许落 rc1）。"""

    stub.setenv("FAKE_VIEW_SLEEP", "10")  # 预检的 gh pr view 卡住

    proc = _spawn(args_for(stub), cwd=stub.repo)
    try:
        _wait_until(lambda: any("gh pr view" in call for call in stub.gh_calls()))
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
            proc.communicate()

    assert proc.returncode == 2
    assert out == ""  # 合并没发生 → 没有证据可保
    assert err.startswith("错误: 收到信号 SIGTERM（")
    assert err.count("\n") == 1
    assert "gh pr merge" not in stub.gh_calls()  # 一个写操作都没发


def test_signals_restored_after_main_returns(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main()`` 返回后 handler 回到原样：不许把宿主进程的信号处理改掉。"""

    before = {signum: signal.getsignal(signum) for signum in mg._interrupt_signals()}

    rc, _out, _err = run(args_for(stub), capsys)

    assert rc == 0  # 默认 stub：分支已删 → 合并完成
    assert {signum: signal.getsignal(signum) for signum in before} == before
