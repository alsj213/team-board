"""``reviewhead`` 指纹层 · 调用面钉死（qa 缺陷视角打回的三条 + 多基 + rev 形态）。

这一层的问题不是算法，而是**调用面没钉死**：git 子进程的环境、颜色、rev 形态、分叉点
个数都交给外部决定，于是同一个内容在不同环境里能算出不同的指纹（或者算的是另一个仓）。
这里用真 ``git`` 在 ``tmp_path`` 造仓，逐条锁住：

1. ``GIT_DIR`` 污染（A 仓的值出现在环境里，``-C`` 指向 B 仓）→ 必须算 B 仓的值；
2. ``color.ui=always`` → 仍 ``kind="patch"``、``value`` 仍是 40 hex（两次 diff 都带
   ``--no-color``；对照组证明不带时确实会上色）；
3. criss-cross 多基 → ``merge-base --all`` 候选 2 个，fail-closed 抛 ``PatchIdError``；
4. ``-`` 开头的 base_ref / head_rev / sha → ``PatchIdError``（不许被 git 当选项吃）；
5. PATH 里没有 git → ``fingerprint_of_commit`` 抛 ``GitMissingError``（不许吞成 ``None``）。

全部不碰网络。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from team_board.reviewhead import (
    PATCH_ID_RE,
    GitMissingError,
    PatchIdError,
    fingerprint,
    fingerprint_of_commit,
)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fingerprint-test",
    "GIT_AUTHOR_EMAIL": "fingerprint@example.com",
    "GIT_COMMITTER_NAME": "fingerprint-test",
    "GIT_COMMITTER_EMAIL": "fingerprint@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


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
    return proc.stdout.strip()


def write(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(tmp_path: Path, name: str, text: str) -> Path:
    """``main`` 上放一个文件、``topic`` 上把它改成 ``text`` 的仓。"""

    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "f.txt", "base\n")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "topic")
    write(repo, "f.txt", text)
    commit(repo, f"{name}: topic 内容")
    git(repo, "checkout", "-q", "main")
    return repo


def test_git_dir_pollution_does_not_redirect_to_the_other_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 仓的 ``GIT_DIR`` 在环境里，算 B 仓必须得 B 的值（不是 A 的、也不是报错）。"""

    repo_a = make_repo(tmp_path, "a", "base\naaaa\n")
    repo_b = make_repo(tmp_path, "b", "base\nbbbb\n")

    fp_a = fingerprint(repo_a, "main", "topic")
    fp_b = fingerprint(repo_b, "main", "topic")
    assert fp_a.kind == "patch" and fp_b.kind == "patch"
    assert fp_a.value != fp_b.value  # 两个仓的内容不同，指纹必须不同

    # 污染：环境里指着 A 仓的 GIT_DIR，调用方却要算 B 仓
    monkeypatch.setenv("GIT_DIR", str(repo_a / ".git"))
    polluted = fingerprint(repo_b, "main", "topic")

    assert polluted.value == fp_b.value  # 算的还是 B
    assert polluted.value != fp_a.value  # 没有被搬到 A 上去算
    assert polluted.paths == fp_b.paths == ("f.txt",)


def test_color_ui_always_still_yields_a_patch_fingerprint(tmp_path: Path) -> None:
    """``color.ui=always`` → 仍 ``kind="patch"`` 且 ``value`` 是 40 hex（无 ANSI 残渣）。"""

    repo = make_repo(tmp_path, "colored", "base\ncolored\n")
    git(repo, "config", "color.ui", "always")
    git(repo, "config", "color.diff", "always")

    # 对照组：同一份 diff 不带 --no-color 时确实上色 —— 证明本用例不是空转
    colored_numstat = subprocess.run(
        ["git", "-C", str(repo), "diff", "--numstat", "-U0", "main", "topic"],
        capture_output=True,
        env={**_GIT_ENV, "GIT_CONFIG_GLOBAL": os.devnull},
        check=True,
    ).stdout
    assert b"\x1b[" in colored_numstat

    result = fingerprint(repo, "main", "topic")

    assert result.kind == "patch", result
    assert result.value is not None
    assert PATCH_ID_RE.fullmatch(result.value) is not None, result.value
    assert "\x1b" not in result.value
    assert result.paths == ("f.txt",)


def test_criss_cross_multiple_merge_bases_fail_closed(tmp_path: Path) -> None:
    """criss-cross：``merge-base --all`` 有 2 个候选 → ``PatchIdError``，消息带候选数。"""

    repo = tmp_path / "criss-cross"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "base.txt", "base\n")
    commit(repo, "base")

    git(repo, "checkout", "-q", "-b", "left")
    write(repo, "l.txt", "left\n")
    left_1 = commit(repo, "L1")

    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "right")
    write(repo, "r.txt", "right\n")
    right_1 = commit(repo, "R1")

    # 双向互合 → 两个分叉点都不再是祖先，criss-cross
    git(repo, "checkout", "-q", "left")
    git(repo, "merge", "-q", "--no-edit", "right")
    git(repo, "checkout", "-q", "right")
    git(repo, "merge", "-q", "--no-edit", left_1)

    candidates = git(repo, "merge-base", "--all", "left", "right").splitlines()
    assert sorted(candidates) == sorted([left_1, right_1])  # 前提成立：确实是 2 个候选

    with pytest.raises(PatchIdError, match="候选 merge base") as excinfo:
        fingerprint(repo, "left", "right")
    assert "2 个" in str(excinfo.value), str(excinfo.value)


def test_dash_prefixed_revs_are_rejected_before_git(tmp_path: Path) -> None:
    """``-`` 开头的 base_ref / head_rev / sha → ``PatchIdError``（不许交给 git 当选项）。"""

    repo = make_repo(tmp_path, "dash", "base\ndash\n")

    with pytest.raises(PatchIdError, match="base_ref"):
        fingerprint(repo, "--all", "topic")
    with pytest.raises(PatchIdError, match="head_rev"):
        fingerprint(repo, "main", "-n1")
    with pytest.raises(PatchIdError, match="提交 sha"):
        fingerprint_of_commit(repo, "main", "-" + "a" * 39)

    # 正常 rev 不受影响（防止把校验写宽成一律拒绝）
    assert fingerprint(repo, "main", "topic").kind == "patch"


def test_missing_git_raises_git_missing_error_not_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH 里没有 git → ``fingerprint_of_commit`` 抛 ``GitMissingError``，不许吞成 ``None``。"""

    repo = make_repo(tmp_path, "nogit", "base\nnogit\n")
    sha = git(repo, "rev-parse", "topic")
    assert len(sha) == 40

    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    with pytest.raises(GitMissingError, match="找不到 git"):
        fingerprint_of_commit(repo, "main", sha)
