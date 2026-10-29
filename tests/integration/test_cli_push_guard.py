"""``cli push-guard`` 的集成测试：真 git 仓 + 真 ``subprocess`` 跑 CLI。

conftest 里没有把 ``src`` 装进环境，所以这里显式给子进程 ``PYTHONPATH=<repo>/src``，
跑的是 ``python -m team_board.cli push-guard``（与 pre-push 钩子将来调用的形态一致）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
_ZERO = "0" * 40

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "pushguard-cli-test",
    "GIT_AUTHOR_EMAIL": "pushguard-cli@example.com",
    "GIT_COMMITTER_NAME": "pushguard-cli-test",
    "GIT_COMMITTER_EMAIL": "pushguard-cli@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def commit_file(repo: Path, name: str, message: str, content: str = "x\n") -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit_file(path, "README.md", "chore: init", "hello\n")
    return path


def add_worktree(repo: Path, parent: Path, dirname: str, branch: str) -> Path:
    path = parent / dirname
    git(repo, "worktree", "add", "-q", str(path), "-b", branch)
    return path


def line(branch: str, sha: str = "a" * 40, remote_branch: str | None = None) -> str:
    remote = remote_branch if remote_branch is not None else branch
    return f"refs/heads/{branch} {sha} refs/heads/{remote} {_ZERO}"


def run(
    args: list[str], stdin_text: str | None = None
) -> tuple[int, str, str]:
    """真起一个进程跑 CLI（stdin 传 ``stdin_text``，不给则给空串）。"""

    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    proc = subprocess.run(
        [sys.executable, "-m", "team_board.cli", "push-guard", *args],
        input=stdin_text if stdin_text is not None else "",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def non_repo_dir(tmp_path: Path) -> Path:
    """一个确定「不在任何 git 仓库里」的目录。"""

    for root in (tmp_path, Path(tempfile.gettempdir()), Path("/tmp")):
        try:
            candidate = root / "t_4c08419a-nr"
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        proc = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            env=_GIT_ENV,
            check=False,
        )
        if proc.returncode != 0:
            return candidate
    raise AssertionError("找不到不在任何 git 仓库里的目录")


# ------------------------------------------------------------ 放行 / 拦截


def test_worktree_pushing_own_card_branch_is_allowed(
    repo: Path, tmp_path: Path
) -> None:
    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    rc, out, err = run(
        ["--repo", str(worktree)], line("wt/t_aaaaaaaa") + "\n"
    )
    assert rc == 0
    assert err == ""
    assert out.count("\n") == 1
    assert out.startswith("push-guard: 放行 1 条 ref（checkout=")
    assert "HEAD=wt/t_aaaaaaaa" in out


def test_main_checkout_pushing_card_branch_is_blocked(repo: Path) -> None:
    rc, out, err = run(["--repo", str(repo)], line("wt/t_aaaaaaaa") + "\n")
    assert rc == 1
    assert out == ""  # 拦截时 stdout 必须为空
    assert err.startswith("拒绝：主 checkout 不许推送卡分支 wt/t_aaaaaaaa；")
    assert f"git -C {repo} worktree add /home/liu/project/tb-wt/t_aaaaaaaa" in err


def test_main_checkout_on_card_branch_then_back_to_main(
    repo: Path, tmp_path: Path
) -> None:
    """同一命令 0 → 1 → 0：主 checkout 停在卡分支时拦、切回 main 后放行。"""

    # 先造一条「主 checkout 切得动」的卡分支（worktree 占着的那条 git 不许再检出）
    git(repo, "checkout", "-q", "-b", "wt/t_cafebabe")
    rc_blocked, out_blocked, err_blocked = run(
        ["--repo", str(repo)], line("wt/t_cafebabe") + "\n"
    )
    assert rc_blocked == 1
    assert out_blocked == ""
    assert err_blocked.startswith(f"拒绝：主 checkout {repo} 停在 wt/t_cafebabe")
    assert "必须停 main" in err_blocked

    git(repo, "checkout", "-q", "main")
    rc_ok, out_ok, _ = run(["--repo", str(repo)], line("pm/ci-check") + "\n")
    assert rc_ok == 0
    assert "放行 1 条 ref" in out_ok and "HEAD=main" in out_ok


def test_main_checkout_on_plain_branch_is_blocked_only_by_p1(repo: Path) -> None:
    """P-1 单独命中：主 checkout 停在 pm/ci-check、推同一条普通分支。"""

    git(repo, "checkout", "-q", "-b", "pm/ci-check")
    rc, out, err = run(["--repo", str(repo)], line("pm/ci-check") + "\n")
    assert rc == 1
    assert out == ""
    assert err.startswith("拒绝：主 checkout ")
    assert "必须停 main" in err


def test_push_to_main_is_blocked(repo: Path, tmp_path: Path) -> None:
    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    rc, out, err = run(["--repo", str(worktree)], line("main") + "\n")
    assert rc == 1
    assert out == ""
    assert err == "拒绝：不允许直接 push 到 main\n"


def test_delete_remote_main_is_blocked_with_extra_line(
    repo: Path, tmp_path: Path
) -> None:
    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    stdin_text = f"(delete) {_ZERO} refs/heads/main {_ZERO}\n"
    rc, out, err = run(["--repo", str(worktree)], stdin_text)
    assert rc == 1
    assert out == ""
    assert err == (
        "拒绝：不允许直接 push 到 main\n（这是删除远端分支的操作，同样禁止）\n"
    )


def test_worktree_dirname_mismatch_is_blocked(repo: Path, tmp_path: Path) -> None:
    worktree = add_worktree(repo, tmp_path, "wrongname", "wt/t_aaaaaaaa")
    rc, out, err = run(["--repo", str(worktree)], line("wt/t_aaaaaaaa") + "\n")
    assert rc == 1
    assert out == ""
    assert "的目录名 wrongname 与所推卡分支 wt/t_aaaaaaaa 不一致" in err


# ------------------------------------------------------------ --check-install


def test_check_install_unconfigured_is_rc1(repo: Path) -> None:
    rc, out, err = run(["--repo", str(repo), "--check-install"])
    assert rc == 1
    assert out == ""
    assert err.startswith("未安装：core.hooksPath 未设置；修复：git config core.hooksPath")


def test_check_install_configured_is_rc0(repo: Path) -> None:
    hooks = repo / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    git(repo, "config", "core.hooksPath", ".githooks")

    rc, out, err = run(["--repo", str(repo), "--check-install"])
    assert rc == 0
    assert err == ""
    assert out == f"已安装：core.hooksPath=.githooks（{hook}）\n"


def test_check_install_hook_not_executable_is_rc1(repo: Path) -> None:
    hooks = repo / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o644)
    git(repo, "config", "core.hooksPath", ".githooks")

    rc, out, err = run(["--repo", str(repo), "--check-install"])
    assert rc == 1
    assert out == ""
    assert "不可执行" in err


def test_check_install_ignores_stdin(repo: Path) -> None:
    """同给 stdin 也忽略：不读、不退化 rc2（仍是 rc1 未安装）。"""

    rc, out, err = run(["--repo", str(repo), "--check-install"], "x y\n")
    assert rc == 1
    assert out == ""
    assert err.startswith("未安装：")


# ------------------------------------------------------------ 边界（R3）


def test_repo_empty_is_rc2() -> None:
    rc, out, err = run(["--repo", ""], line("pm/ci-check") + "\n")
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1


def test_repo_nonexistent_is_rc2() -> None:
    rc, out, err = run(["--repo", "/nonexistent"], line("pm/ci-check") + "\n")
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")


def test_repo_not_a_git_dir_is_rc2(tmp_path: Path) -> None:
    rc, out, err = run(
        ["--repo", str(non_repo_dir(tmp_path))], line("pm/ci-check") + "\n"
    )
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")


def test_main_branch_empty_is_rc2(repo: Path) -> None:
    rc, out, err = run(["--repo", str(repo), "--main-branch", ""], line("pm/ci-check") + "\n")
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")


def test_main_branch_missing_is_rc2(repo: Path) -> None:
    rc, out, err = run(
        ["--repo", str(repo), "--main-branch", "nosuch"], line("pm/ci-check") + "\n"
    )
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")


def test_stdin_field_count_is_rc2(repo: Path) -> None:
    rc, out, err = run(["--repo", str(repo)], "x y\n")
    assert rc == 2
    assert out == ""
    assert err == "错误: pre-push 输入格式非法（第 1 行只有 2 个字段）\n"


def test_stdin_empty_is_rc2(repo: Path) -> None:
    rc, out, err = run(["--repo", str(repo)], "")
    assert rc == 2
    assert out == ""
    assert err == "错误: 没有收到任何要推送的 ref（stdin 为空）\n"
