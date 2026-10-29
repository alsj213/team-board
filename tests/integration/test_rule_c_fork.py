"""规则 C-2 的 fork 点集成测试：真 git 仓里验「merge 提交不再假红」与归因一致性。

本文件对应返工卡 ``t_991ae40f`` 的两处 spec 级修正（改动 A / 改动 B）：

- A：卡分支先 ``git merge --no-ff main`` 再落第一个卡提交时，fork 点必须沿
  first-parent 跳过 merge 提交，判据是「从 main 拉出来的分支」→ 绿；
- B：``forked_from_cards`` 的归因源是全仓卡分支，因此全量模式与 ``--branch``
  模式（CI 恒走这条）给出同一句文案。

刻意**不改** ``tests/integration/test_cli_branch_audit.py``（归 ``t_f3a4fb31``），
``tests/unit/test_branches.py`` 既有用例也一条不动。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from team_board import branches, cli
from team_board.branches import AuditError, _parent_count

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "audit-test",
    "GIT_AUTHOR_EMAIL": "audit@example.com",
    "GIT_COMMITTER_NAME": "audit-test",
    "GIT_COMMITTER_EMAIL": "audit@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}

#: C-2 归因文案（渲染层固定写法，本卡一个字都不许改）
_FROM_CARD = "（它属于卡 t_bbbbbbbb 的分支）"


def git(repo: Path, *args: str) -> str:
    """在 ``repo`` 里跑 git，失败即测试失败。"""

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
    """写一个文件并提交（不动工作区其它内容）。"""

    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """一个 main 上只有一条初始提交的仓。"""

    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit_file(path, "README.md", "chore: init", "hello\n")
    return path


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """跑 CLI，返回 ``(rc, stdout, stderr)``。"""

    rc = cli.main(["branch-audit", *args])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ------------------------------------------------- 改动 A：merge 提交不再假红


def test_merge_no_ff_before_first_card_commit_is_green(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """分支先 merge --no-ff main、再落第一个卡提交 → 应判绿。"""

    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, "m2.txt", "chore: main 前进")
    git(repo, "checkout", "-q", "wt/t_aaaaaaaa")
    git(repo, "merge", "--no-ff", "-m", "Merge branch 'main' into wt/t_aaaaaaaa", "main")
    commit_file(repo, "w.txt", "feat(t_aaaaaaaa): 第一个卡提交")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert err == ""
    assert rc == 0, out
    assert "分支结构偏差（0 处）" in out
    assert out.rstrip().endswith("结论：无偏差")


# ------------------------------------- 改动 B：full 与 --branch 归因文案一致


def test_forked_from_other_card_named_in_both_modes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """从别的卡分支拉出：全量与 ``--branch`` 都必须点名 t_bbbbbbbb。"""

    git(repo, "checkout", "-q", "-b", "wt/t_bbbbbbbb")
    commit_file(repo, "b.txt", "feat(t_bbbbbbbb): B 的活")
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "a.txt", "feat(t_aaaaaaaa): A 的活")
    git(repo, "checkout", "-q", "main")

    rc_full, out_full, err_full = run(["--repo", str(repo)], capsys)

    assert err_full == ""
    assert rc_full == 1
    assert _FROM_CARD in out_full

    rc_one, out_one, err_one = run(
        ["--repo", str(repo), "--branch", "wt/t_aaaaaaaa"], capsys
    )

    assert err_one == ""
    assert rc_one == 1
    assert _FROM_CARD in out_one
    assert "（不属于任何卡分支）" not in out_one


# ------------------------------------------------------------- 回归锁（三条）


def test_isolated_history_still_red(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """孤立历史仍判红（没有父提交）。"""

    git(repo, "checkout", "-q", "--orphan", "wt/t_cccccccc")
    git(repo, "rm", "-rfq", ".")
    commit_file(repo, "c.txt", "feat(t_cccccccc): 孤立历史")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert err == ""
    assert rc == 1
    assert "没有父提交（孤立历史）" in out


def test_fork_from_main_is_green(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """从 main 正常拉出仍判绿。"""

    git(repo, "checkout", "-q", "-b", "wt/t_dddddddd")
    commit_file(repo, "d.txt", "feat(t_dddddddd): 从 main 正常拉出")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert err == ""
    assert rc == 0, out
    assert "分支结构偏差（0 处）" in out


def test_branch_mode_c1_still_whole_repo(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--branch`` 模式下 C-1 仍扫全仓：仓里有 ``wt/t_aaaaaaaa-fix`` 就点名它。"""

    git(repo, "checkout", "-q", "-b", "wt/t_bbbbbbbb")
    commit_file(repo, "b.txt", "feat(t_bbbbbbbb): B 的活")
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "wt/t_aaaaaaaa-fix")

    rc, out, err = run(["--repo", str(repo), "--branch", "wt/t_bbbbbbbb"], capsys)

    assert err == ""
    assert rc == 1
    assert "wt/t_aaaaaaaa-fix" in out
    assert "有第二条分支 wt/t_aaaaaaaa-fix" in out


# ------------------------------------------- _parent_count 的三种返回值（单测层）


def test_parent_count_root_normal_and_merge(repo: Path) -> None:
    """根提交 0 / 普通提交 1 / merge 提交 2。"""

    root = git(repo, "rev-parse", "HEAD").strip()
    assert _parent_count(repo, root) == 0

    commit_file(repo, "n.txt", "chore: 普通提交")
    normal = git(repo, "rev-parse", "HEAD").strip()
    assert _parent_count(repo, normal) == 1

    git(repo, "checkout", "-q", "-b", "side")
    commit_file(repo, "s.txt", "chore: side")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-m", "Merge branch 'side'", "side")
    merge = git(repo, "rev-parse", "HEAD").strip()
    assert _parent_count(repo, merge) == 2


# ============================================================================
# 返工轮（pm run 178 的 P1 / P2 + 64 层上限兜底）：以下只增不改，上面 6 条原样保留
# ============================================================================


def fork_line(out: str) -> str:
    """从报告里取出唯一的 C-2 偏差行（全量与 ``--branch`` 逐字比对用）。"""

    lines = [line for line in out.splitlines() if "的第一个提交" in line]
    assert len(lines) == 1, out
    return lines[0]


# ------------------------------- P1：主分支连续 merge 到 65 层，卡分支仍必须判绿


def test_main_with_65_merge_layers_still_green(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """主分支连续 65 层 ``merge --no-ff`` 后从 tip 拉卡分支 → 绿（rc=0）。

    修前这条是 rc=2（「连续 64 层都是 merge 提交」把整次稽核打成环境错误）：
    R13 规定合并统一走 merge commit，主分支每合一张 PR 就深一层，这是定时炸弹。
    """

    for index in range(65):
        git(repo, "checkout", "-q", "-b", f"side-{index}", "main")
        commit_file(repo, f"side-{index}.txt", f"chore: side {index}")
        git(repo, "checkout", "-q", "main")
        git(
            repo,
            "merge",
            "--no-ff",
            "-m",
            f"Merge branch 'side-{index}'",
            f"side-{index}",
        )
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "a.txt", "feat(t_aaaaaaaa): 第一个卡提交")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert err == ""
    assert rc == 0, out
    assert "分支结构偏差（0 处）" in out
    assert out.rstrip().endswith("结论：无偏差")


# --------------------- P2：合进来的是别的卡分支时不许跳（C-2 不得被打穿）


def test_merging_other_card_branch_before_first_commit_is_still_red(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """卡分支先 ``merge --no-ff`` 别的卡分支再落第一个卡提交 → 全量与 ``--branch`` 都红。

    修前这一跳会把 fork 点一步洗到 main 上（假绿），C-2 这道不可豁免的第二网消失。
    """

    git(repo, "checkout", "-q", "-b", "wt/t_bbbbbbbb")
    commit_file(repo, "b.txt", "feat(t_bbbbbbbb): B 的活")
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    git(repo, "merge", "--no-ff", "-m", "Merge branch 'wt/t_bbbbbbbb'", "wt/t_bbbbbbbb")
    commit_file(repo, "a.txt", "feat(t_aaaaaaaa): A 的活")
    git(repo, "checkout", "-q", "main")

    rc_full, out_full, err_full = run(["--repo", str(repo)], capsys)

    assert err_full == ""
    assert rc_full == 1
    assert "分支结构偏差（1 处）" in out_full

    rc_one, out_one, err_one = run(
        ["--repo", str(repo), "--branch", "wt/t_aaaaaaaa"], capsys
    )

    assert err_one == ""
    assert rc_one == 1
    assert "分支结构偏差（1 处）" in out_one
    # 同一事实两种模式同一句文案（归因源已是全仓卡分支）
    assert fork_line(out_full) == fork_line(out_one)


def test_fork_from_other_card_merge_tip_is_still_red_and_named(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """从「别的卡分支 tip（它自己 merge 过 main 的 merge 提交）」拉出 → 仍红且点名。

    非首父是 main 祖先，但跳到首父后落在别的卡的提交上 —— 必须判红并点名它。
    """

    git(repo, "checkout", "-q", "-b", "wt/t_bbbbbbbb")
    commit_file(repo, "b.txt", "feat(t_bbbbbbbb): B 的活")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, "m2.txt", "chore: main 前进")
    git(repo, "checkout", "-q", "wt/t_bbbbbbbb")
    git(repo, "merge", "--no-ff", "-m", "Merge branch 'main' into wt/t_bbbbbbbb", "main")
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "a.txt", "feat(t_aaaaaaaa): A 的活")
    git(repo, "checkout", "-q", "main")

    rc_full, out_full, err_full = run(["--repo", str(repo)], capsys)

    assert err_full == ""
    assert rc_full == 1
    assert "分支结构偏差（1 处）" in out_full
    assert _FROM_CARD in out_full

    rc_one, out_one, err_one = run(
        ["--repo", str(repo), "--branch", "wt/t_aaaaaaaa"], capsys
    )

    assert err_one == ""
    assert rc_one == 1
    assert _FROM_CARD in out_one
    assert fork_line(out_full) == fork_line(out_one)


# --------------------------------- 64 层上限作为兜底仍必须能测到（AuditError）


def test_merge_skip_cap_raises_audit_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跳过链无限长 → 命中 64 层上限，抛 ``AuditError``（CLI 转 rc2）。

    注：卡面写的是「monkeypatch 让 ``_parents`` 每次都返回 2 个都是 main 祖先的
    父提交」，但那样循环第一轮就会因为 ``cand = parents[0]`` 本身是 main 祖先而
    走「先判 main」的出口 break（P1 修好后的正常路径），永远走不到上限。因此这里
    只 monkeypatch ``_parents``：首父固定成一条**不在 main 上**的真实提交，非首父
    是 main tip —— 每层都「只是把 main 合进来」但链无限长，正是上限该拦的形状。
    """

    git(repo, "checkout", "-q", "-b", "wt/t_bbbbbbbb")
    commit_file(repo, "b.txt", "feat(t_bbbbbbbb): B 的活")
    off_main = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "main")
    # 卡分支先在 main 的旧 tip 上创建，再让 main 前进 —— 否则 `merge main` 是
    # 「Already up to date」，造不出 merge 提交，循环根本进不去。
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, "m2.txt", "chore: main 前进")
    git(repo, "checkout", "-q", "wt/t_aaaaaaaa")
    git(repo, "merge", "--no-ff", "-m", "Merge branch 'main' into wt/t_aaaaaaaa", "main")
    commit_file(repo, "a.txt", "feat(t_aaaaaaaa): 第一个卡提交")
    git(repo, "checkout", "-q", "main")
    main_tip = git(repo, "rev-parse", "refs/heads/main").strip()

    calls: list[str] = []

    def fake_parents(repo_arg: Path | str, sha: str) -> tuple[str, ...]:
        calls.append(sha)
        return (off_main, main_tip)

    monkeypatch.setattr(branches, "_parents", fake_parents)

    with pytest.raises(AuditError) as excinfo:
        branches.audit(repo)

    assert "连续 64 层" in str(excinfo.value)
    assert len(calls) == 64

