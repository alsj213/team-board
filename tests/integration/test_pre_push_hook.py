"""``.githooks/pre-push`` 的集成测试：真 ``git push`` 跑真钩子，一次性裸仓当远端。

钩子本体是薄壳（唯一判定源 = ``pushguard.py``），所以每个用例断言三件事：

1. ``git push`` 的**退出码**；
2. 钩子写进 stderr 的**文案**（拒绝理由 / fail-closed）；
3. **远端的实际状态**（``git -C <bare> for-each-ref``）—— 「拦在 push **之前**」
   只能靠裸仓 refs 前后都为空来证明，不能只看 git 的报错文案。

仓里 ``src`` 是指向本仓 ``src/`` 的符号链接（钩子把 ``$root/src`` 塞进
``PYTHONPATH``），因此用例 a–e 走的是真 ``push-guard``（真 ``pushguard.judge``）；
用例外 f 故意不给 ``src``，验证 fail-closed。

用例 g（``--no-verify`` 绕过）**不做机器拦截、也不真推**：那是客户端钩子的固有
剩余风险（``git push --no-verify`` 会让 git 直接跳过本钩子），权威判定仍是 CI 的
``branch-audit (卡分支/checkout)``。README 与 ``docs/push-guard.md`` 的「剩余风险」
清单第 1 条写的就是它，此处只留说明，不写用例。

用例外 h 是接线时踩到的真缺陷的回归钉子：``git push`` 在没有新提交时仍会调用钩子，
但 **stdin 一行都没有**（push-guard 判 rc2），钩子必须认这是「无事可拦」而不是
「环境错误」—— 否则每次空推都被拦。

用例 i–l 是 PR #41 第二视角双问打回后补的（返工卡 ``t_a875c1ed``），四个都是**环境层**
的误拦/误炸，直接跑钩子本体（不经 git，坏 ``TMPDIR`` / 改编码 / 换 ``src`` 都只影响
这一次进程）：

- i) 坏 ``TMPDIR`` × ``dash``：旧版在 ``if ! : >"$errfile"`` 处**致命退出 rc=2**，
  一句人话都没有（``:`` 是 POSIX 特殊内建，重定向失败在非交互 shell 里是致命错误）。
  现在钩子不落任何临时文件：空 stdin 静默放行，判定红照旧 rc=1 + 人话；
- j) 坏 ``TMPDIR`` × ``bash``：同一状态下旧版把「空 stdin 放行」判据（grep 临时文件）
  判死 → 零传输的 push 被拦。现在「有没有 ref」由钩子自己判；
- k) ``PYTHONIOENCODING=ascii``：push-guard 写 stdout 时编码失败 → rc2。旧版把它当
  「判定红」，还补了 ``--no-verify`` 绕过提示；现在环境错误单独一套文案；
- l) 缺 ``push-guard`` 子命令（版本偏斜，argparse ``invalid choice``）：同样归环境错误，
  且**不把 argparse usage 原样泄给用户**。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOKS = _REPO_ROOT / ".githooks"
_SRC = _REPO_ROOT / "src"

_ENV = {
    key: value
    for key, value in os.environ.items()
    if key
    not in {
        "PYTHONPATH",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_PARAMETERS",
        "PYTHONIOENCODING",
        "TMPDIR",
        "GIT_CONFIG_GLOBAL",
    }
}
_ENV.update(
    {
        "GIT_AUTHOR_NAME": "pre-push-hook-test",
        "GIT_AUTHOR_EMAIL": "pre-push-hook@example.com",
        "GIT_COMMITTER_NAME": "pre-push-hook-test",
        "GIT_COMMITTER_EMAIL": "pre-push-hook@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        # 宿主全局配置（commit.gpgsign / core.hooksPath）不许渗进用例
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
)


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """跑一条 git 命令（**不断言**退出码：有用例就是要看非 0）。"""

    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=_ENV,
        check=False,
    )


def must_git(cwd: Path, *args: str) -> str:
    proc = git(cwd, *args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def commit_file(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text("x\n", encoding="utf-8")
    must_git(repo, "add", "-A")
    must_git(repo, "commit", "-q", "-m", message)


def refs(bare: Path) -> tuple[str, ...]:
    """裸仓里现有的 ref 名（为空 = 什么都没推上去）。"""

    out = must_git(bare, "for-each-ref", "--format=%(refname)")
    return tuple(out.split())


def hooks_dir_for(tmp_path: Path) -> Path:
    """本仓 ``.githooks`` 的拷贝（保留可执行位），供 ``core.hooksPath`` 指向。"""

    target = tmp_path / "hooks"
    shutil.copytree(_HOOKS, target)
    return target


_HOOK = _HOOKS / "pre-push"
_REF_SHA = "1" * 40
_ZERO_SHA = "0" * 40


def ref_line(local_ref: str, remote_ref: str | None = None) -> str:
    """一行 pre-push stdin：``<local_ref> <sha> <remote_ref> <全零 sha>``。"""

    return f"{local_ref} {_REF_SHA} {remote_ref or local_ref} {_ZERO_SHA}\n"


def run_hook(
    cwd: Path,
    stdin_text: str,
    *,
    shell: str = "/bin/sh",
    **env_overrides: str,
) -> subprocess.CompletedProcess[str]:
    """直接跑真钩子本体（不经 git），给「环境层」用例用（坏 TMPDIR / 编码 / 版本偏斜）。

    ``cwd`` 决定钩子算出来的工作树根；``stdin_text`` 原样喂给钩子（等价于 git 给的
    ref 行）。只覆盖传入的变量，其余沿用 ``_ENV``，所以宿主机的 ``TMPDIR`` /
    ``PYTHONIOENCODING`` 不会渗进来 —— 用例断言的是钩子在这个环境下的行为。
    """

    return subprocess.run(
        [shell, str(_HOOK)],
        cwd=str(cwd),
        input=stdin_text,
        capture_output=True,
        text=True,
        env={**_ENV, **env_overrides},
        check=False,
    )


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    """一次性裸仓当远端（绝不用 origin，避免污染真仓库）。"""

    bare = tmp_path / "remote.git"
    assert git(tmp_path, "init", "-q", "--bare", str(bare)).returncode == 0
    return bare


@pytest.fixture()
def repo(tmp_path: Path, remote: Path) -> Path:
    """临时工作仓：``main`` 上一个提交、``src`` 符号链接、钩子已装。"""

    path = tmp_path / "repo"
    path.mkdir()
    assert git(path, "init", "-q", "-b", "main").returncode == 0
    os.symlink(_SRC, path / "src")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    must_git(path, "add", "-A")
    must_git(path, "commit", "-q", "-m", "chore: init")
    must_git(path, "config", "core.hooksPath", str(hooks_dir_for(tmp_path)))
    return path


def test_a_main_checkout_on_card_branch_is_blocked(repo: Path, remote: Path) -> None:
    """a) 主 checkout 停在卡分支 → 推卡分支当场被拦，裸仓为空（拦在 push 之前）。"""

    before = refs(remote)
    must_git(repo, "switch", "-q", "-c", "wt/t_deadbeef")
    try:
        proc = git(repo, "push", str(remote), "wt/t_deadbeef")
    finally:
        must_git(repo, "switch", "-q", "main")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "拒绝：主 checkout" in proc.stderr
    assert "wt/t_deadbeef" in proc.stderr
    assert before == ()
    assert refs(remote) == (), "钩子必须在传输之前中止：裸仓里不该出现任何 ref"


def test_b_card_worktree_push_is_allowed(tmp_path: Path, repo: Path, remote: Path) -> None:
    """b) 独立 worktree（目录名 = 卡号、HEAD = 卡分支）推同一分支 → 放行。"""

    worktree = tmp_path / "t_deadbeef"
    must_git(repo, "worktree", "add", "-q", "-b", "wt/t_deadbeef", str(worktree), "main")

    proc = git(worktree, "push", str(remote), "wt/t_deadbeef")

    assert proc.returncode == 0, proc.stderr
    assert "refs/heads/wt/t_deadbeef" in refs(remote)


def test_c_main_checkout_pushing_card_branch_is_blocked(repo: Path, remote: Path) -> None:
    """c) 主 checkout 停在 main、直接推卡分支 → 拦（P-2）。"""

    must_git(repo, "branch", "wt/t_deadbeef", "main")
    assert refs(remote) == ()

    proc = git(repo, "push", str(remote), "wt/t_deadbeef")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "拒绝：主 checkout 不许推送卡分支 wt/t_deadbeef" in proc.stderr
    assert refs(remote) == ()


def test_d_push_main_is_blocked(repo: Path, remote: Path) -> None:
    """d) 推 main → 拦（P-4，原有保护不丢）。"""

    proc = git(repo, "push", str(remote), "main")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "拒绝：不允许直接 push 到 main" in proc.stderr
    assert refs(remote) == ()


def test_e_push_tag_is_allowed(repo: Path, remote: Path) -> None:
    """e) 推 tag → 放行（不误拦）。"""

    must_git(repo, "tag", "v1")

    proc = git(repo, "push", str(remote), "refs/tags/v1")

    assert proc.returncode == 0, proc.stderr
    assert "refs/tags/v1" in refs(remote)


def test_f_missing_src_dir_fails_closed(tmp_path: Path, remote: Path) -> None:
    """f) 仓里没有 ``src/``（push-guard 跑不起来）→ fail-closed，不静默放行。

    推的是一条**普通非卡分支**：钩子正常工作时它会被放行，所以 rc≠0 只可能来自
    fail-closed 分支本身。
    """

    path = tmp_path / "nosrc"
    path.mkdir()
    assert git(path, "init", "-q", "-b", "main").returncode == 0
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    must_git(path, "add", "-A")
    must_git(path, "commit", "-q", "-m", "chore: init")
    must_git(path, "config", "core.hooksPath", str(hooks_dir_for(tmp_path)))
    must_git(path, "branch", "feat-x", "main")
    assert refs(remote) == ()

    proc = git(path, "push", str(remote), "feat-x")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "fail-closed" in proc.stderr
    assert "拒绝：不允许直接 push" not in proc.stderr
    assert refs(remote) == ()


def test_h_noop_push_has_empty_stdin_and_is_allowed(repo: Path, remote: Path) -> None:
    """h) 「已是最新」时再 push 一次：git 仍调钩子，但**一行 ref 都不喂** → 放行。

    这是接线时踩到的真缺陷（本用例就是回归钉子）：`push-guard` 对空 stdin 是 rc2
    （`没有收到任何要推送的 ref（stdin 为空）`），钩子若一律「rc≠0 就拦」，那么每次
    `git push` 在没有新提交时都会被拦 —— 没有 ref 要传，就没有可拦的东西。
    """

    must_git(repo, "branch", "feat-x", "main")
    first = git(repo, "push", str(remote), "feat-x")
    assert first.returncode == 0, first.stderr

    second = git(repo, "push", str(remote), "feat-x")

    assert second.returncode == 0, second.stdout + second.stderr
    assert "拒绝" not in second.stderr
    assert "refs/heads/feat-x" in refs(remote)


def test_hook_is_a_thin_wrapper_that_calls_push_guard() -> None:
    """钩子内容自查：确实调 ``team-board push-guard``，且没有在 shell 里重写判定。

    ``--check-install`` 只看 ``core.hooksPath`` + 文件存在 + 可执行，**不看内容**
    （给假绿的已知口径），所以「P-1…P-4 已生效」必须另配这一条断言。
    """

    text = (_HOOKS / "pre-push").read_text(encoding="utf-8")

    assert text.startswith("#!/bin/sh\n")
    assert "set -u" in text
    assert "team_board.cli push-guard" in text
    assert 'PYTHONPATH="$root/src' in text
    # 判定只许在 pushguard.py：shell 里不许再写一遍 main / master 判断
    assert "refs/heads/main" not in text
    assert "refs/heads/master" not in text
    # fail-closed 两句 + --no-verify 提示都得在
    assert "拿不到工作树根（fail-closed）" in text
    assert "push-guard 跑不起来（fail-closed" in text
    assert "--no-verify" in text
    # 不落临时文件（返工 t_a875c1ed ④：可预测文件名 + 预置符号链接 + 坏 TMPDIR 一起消失）
    assert "mktemp" not in text
    assert "pre-push-$$" not in text
    # 三档文案：环境错误单独一套，且 worktree 落点与现存约定一致
    assert "环境错误（rc=2）" in text
    assert "版本偏斜" in text
    assert "worktree add /home/liu/project/tb-wt/<卡号>" in text


def test_hook_runs_under_sh_syntax_check() -> None:
    """钩子是 ``#!/bin/sh`` 脚本：``sh -n`` 语法自检（避免只有 bash 才过的写法）。"""

    proc = subprocess.run(
        ["/bin/sh", "-n", str(_HOOKS / "pre-push")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr


# ------------------------------------------------- 环境层（返工 t_a875c1ed，用例 i–l）

_BAD_TMPDIR = "/nonexistent-xyz"


def test_i_bad_tmpdir_under_dash_keeps_hook_layer_codes(repo: Path) -> None:
    """i) 坏 ``TMPDIR`` × ``dash``：钩子层只有 0/1，判定红时有人话。

    旧版 `${TMPDIR:-/tmp}/pre-push-$$.err` + `if ! : >"$errfile"` 在 dash 下是**致命**
    错误：钩子直接以 rc=2 退出，一句 `[pre-push] …` 都没打印（重定向失败在非交互
    shell 里对特殊内建致命）。现在钩子不落临时文件，空 stdin 照样静默放行。
    """

    empty = run_hook(repo, "", TMPDIR=_BAD_TMPDIR)
    assert empty.returncode == 0, empty.stderr
    assert empty.stdout == ""
    assert empty.stderr == ""

    blocked = run_hook(repo, ref_line("refs/heads/main"), TMPDIR=_BAD_TMPDIR)
    assert blocked.returncode == 1, blocked.stderr
    assert "拒绝：不允许直接 push 到 main" in blocked.stderr
    assert "正确做法" in blocked.stderr


def test_j_bad_tmpdir_under_bash_empty_stdin_is_allowed(repo: Path) -> None:
    """j) 坏 ``TMPDIR`` × ``bash``：零传输的 push 不许被误拦（旧版 rc=1）。

    旧版在 errfile 建不出来时把它退化成 ``/dev/null``，于是「空 stdin 放行」那条
    ``grep`` 判据永远匹配不到 → 空 stdin 被判成「判定红」。现在「有没有 ref」由钩子
    自己判（stdin 整块收进变量），跟临时文件在不在无关。
    """

    if shutil.which("bash") is None:  # pragma: no cover - 本机有 bash
        pytest.skip("本机没有 bash")

    proc = run_hook(repo, "", shell="bash", TMPDIR=_BAD_TMPDIR)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_k_ascii_stdout_is_env_error_not_a_block(repo: Path) -> None:
    """k) ``PYTHONIOENCODING=ascii``：push-guard rc2（编码失败）走**环境错误**档。

    旧版把 rc2 与 rc1 混为一档，还补了 worktree add / ``--no-verify`` 两行 —— 误火时
    把绕过口子递给用户。现在环境错误单独一套文案，且 push-guard 自己的诊断照旧给到人。
    """

    proc = run_hook(repo, ref_line("refs/heads/feat-x"), PYTHONIOENCODING="ascii")

    assert proc.returncode == 1, proc.stderr
    assert "环境错误（rc=2）" in proc.stderr
    assert "无法表示输出里的中文" in proc.stderr  # push-guard 的原始诊断不许被吞掉
    assert "--no-verify" not in proc.stderr
    assert "正确做法" not in proc.stderr
    assert proc.stdout == ""


def test_l_missing_push_guard_subcommand_is_env_error(tmp_path: Path) -> None:
    """l) 版本偏斜：``src`` 里还没有 ``push-guard`` 子命令（argparse ``invalid choice``）。

    旧版把 argparse 的 usage 原样泄给用户，还当成「判定红」补绕过提示。现在归环境
    错误档：一句人话，不带 usage、不带 ``--no-verify``。
    """

    old_src = tmp_path / "old-src"
    package = old_src / "team_board"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import sys\n"
        "sys.stderr.write(\n"
        "    'usage: cli.py [-h] {list,summary}\\n'\n"
        "    \"cli.py: error: argument command: invalid choice: 'push-guard'\"\n"
        "    \" (choose from 'list', 'summary')\\n\"\n"
        ")\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )

    checkout = tmp_path / "old-checkout"
    checkout.mkdir()
    assert git(checkout, "init", "-q", "-b", "main").returncode == 0
    os.symlink(old_src, checkout / "src")
    (checkout / "a.txt").write_text("x\n", encoding="utf-8")
    must_git(checkout, "add", "-A")
    must_git(checkout, "commit", "-q", "-m", "chore: init")

    proc = run_hook(checkout, ref_line("refs/heads/feat-x"))

    assert proc.returncode == 1, proc.stderr
    assert "版本偏斜" in proc.stderr
    assert "usage:" not in proc.stderr
    assert "invalid choice" not in proc.stderr
    assert "--no-verify" not in proc.stderr

