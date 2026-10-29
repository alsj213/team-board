"""merge_guard 的集成测试：走 ``main(argv)``，gh / git 一律用临时目录里的假脚本。

**禁止联调**：测试里出现的 ``gh`` / ``git`` 全部是本目录 fixture 造的可执行假脚本，
塞在 ``PATH`` 最前面，调用记录写进 ``$FAKE_LOG``；既不打真实 GitHub，也不碰真远端。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from team_board import merge_guard as mg

CARD_BRANCH = "wt/t_bbeab29f"

GH_STUB = """#!/bin/sh
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  printf '%s' "$FAKE_VIEW_JSON"
  exit "${FAKE_VIEW_RC:-0}"
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  if [ -n "$FAKE_MERGE_OUT" ]; then printf '%s\\n' "$FAKE_MERGE_OUT"; fi
  if [ -n "$FAKE_MERGE_ERR" ]; then printf '%s\\n' "$FAKE_MERGE_ERR" >&2; fi
  exit "${FAKE_MERGE_RC:-0}"
fi
printf 'fake gh: unexpected args: %s\\n' "$*" >&2
exit 9
"""

GIT_STUB = """#!/bin/sh
printf 'git %s\\n' "$*" >> "$FAKE_LOG"
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

    def view(self, *, state: str = "OPEN", head: str = CARD_BRANCH, base: str = "main") -> None:
        self.setenv(
            "FAKE_VIEW_JSON",
            json.dumps(
                {"number": 9, "state": state, "headRefName": head, "baseRefName": base}
            ),
        )


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
    monkeypatch.setenv("FAKE_VIEW_RC", "0")
    monkeypatch.setenv("FAKE_MERGE_RC", "0")
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
    assert "gh pr merge 9 --merge --delete-branch" in stub.gh_calls()
    assert stub.git_calls()  # 确实做了远端判定


# ------------------------------------------ 3. 合并成功 + ls-remote rc2 → rc0

def test_merge_ok_and_branch_deleted_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "2")  # 没有这个 ref

    rc, out, err = run(args_for(stub, "--method", "squash"), capsys)

    assert rc == 0
    assert err == ""
    lines = out.splitlines()
    assert lines[0].startswith(
        "预检 | PASS | gh pr view 9 --json number,state,headRefName,baseRefName | rc=0 | "
    )
    assert lines[1].startswith("合并 | PASS | gh pr merge 9 --squash --delete-branch | rc=0 | ")
    assert lines[2].startswith(
        f"分支删除 | PASS | git -C {stub.repo} ls-remote --exit-code --refs "
        f"origin refs/heads/{CARD_BRANCH} | rc=2 | "
    )
    assert lines[-1] == f"结论：合并完成，源分支 {CARD_BRANCH} 已从 origin 删除"
    assert stub.gh_calls() == [
        "gh pr view 9 --json number,state,headRefName,baseRefName",
        "gh pr merge 9 --squash --delete-branch",
    ]


def test_gh_stderr_warning_is_not_a_failure(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 的 stderr 警告（worktree 里删不掉本地分支）不算失败，判定只看远端。"""

    stub.setenv("FAKE_MERGE_ERR", "failed to delete local branch wt/t_bbeab29f")
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, _err = run(args_for(stub), capsys)

    assert rc == 0
    assert "结论：合并完成" in out


# --------------------------------- 4. gh pr merge 失败 → rc1，且不跑删除验证

def test_merge_failure_rc1_and_no_verification(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_MERGE_RC", "1")
    stub.setenv("FAKE_MERGE_ERR", "pull request is not mergeable: required check failing")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 1
    assert stub.git_calls() == []  # 没有再跑删除验证
    assert "gh pr merge 9 --merge --delete-branch" in stub.gh_calls()
    assert "合并 | FAIL" in out
    assert "合并失败" in out
    assert "合并成功" not in out
    assert not [line for line in out.splitlines() if line.startswith("分支删除 | ")]
    assert "required check failing" in err  # gh 输出原文写出来了


# ------------------------------------------- 5. --verify-only（一次 gh 都不调）

def test_verify_only_branch_present_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "0")

    rc, out, err = run(args_for(stub, "--verify-only"), capsys)

    assert rc == 1
    assert err == ""
    assert out.splitlines()[-1] == "结论：分支仍在远端"
    assert out.splitlines()[0].startswith("分支删除 | FAIL | ")
    assert stub.gh_calls() == []


def test_verify_only_branch_deleted_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for(stub, "--verify-only"), capsys)

    assert rc == 0
    assert err == ""
    assert out.splitlines()[-1] == "结论：分支已删除"
    assert stub.gh_calls() == []


def test_verify_only_undetermined_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

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
            "--method",
            "rebase",
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
    assert "gh pr merge 999999999 --rebase --delete-branch" in stub.gh_calls()
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
    assert stub.gh_calls() == ["gh pr view 9 --json number,state,headRefName,baseRefName"]


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


def test_missing_git_after_merge_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并成功后 git 不可用 → 判不了，rc2（不许当绿，也不许当 rc1）。"""

    bindir = tmp_path / "gh-only-bin"
    bindir.mkdir()
    _write_exe(bindir / "gh", GH_STUB)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("PATH", str(bindir))  # 没有 git
    monkeypatch.setenv("FAKE_LOG", str(log))
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

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "git" in err
    assert "gh pr merge 9 --merge --delete-branch" in log.read_text(encoding="utf-8")


# ------------------------------- 合并后通信一直失败 → rc2（明确未通过，不当绿）

def test_merge_ok_but_undetermined_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""  # rc2 一律 stdout 空
    assert err.startswith("错误: ")
    assert "无法判定" in err


# --------------------------------------------------- 10. 回归：--help rc0

def test_help_rc0(capsys: pytest.CaptureFixture[str]) -> None:
    rc = mg.main(["--help"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "--delete-branch" in captured.out
    assert "--verify-only" in captured.out
    assert captured.err == ""
