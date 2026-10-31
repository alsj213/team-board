"""``reviewhead`` 指纹层单测：六个内容场景（rebase / 换文件 / 真改 / 上下文位移 / 二进制 / 空）。

这一层不碰 gh、不碰网络：全部用真 ``git`` 在 ``tmp_path`` 里造仓，「main 动了」「改了
文件」「改了内容」都由真提交产生，判断只走 :func:`fingerprint`。

锁的三件事：

- ``-U0`` 是硬要求：上下文位移（行号变、内容没变）必须同指纹，而同一例用默认
  ``-U3`` 会换 id（下面 ContextShift 那条用例把两个 id 都算出来做对照）；
- 换文件 / 真改内容必须换指纹（这两件是「内容真的变了」）；
- 二进制、空 diff 判不了 / 没有内容，分别返回 ``kind="binary"`` / ``"empty"``，
  不许折成「内容没变」。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from team_board.reviewhead import (
    FINGERPRINT_ALGO,
    Fingerprint,
    PatchIdError,
    fingerprint,
    fingerprint_of_commit,
    patch_id_supports_verbatim,
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


def make_repo(tmp_path: Path) -> Path:
    """``main`` 上两个文本文件（``f1.txt`` 七行、``f2.txt`` 三行）。"""

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "f1.txt", "l1\nl2\nl3\nl4\nl5\nl6\nl7\n")
    write(repo, "f2.txt", "a\nb\nc\n")
    commit(repo, "base")
    return repo


def patch_id(repo: Path, base: str, head: str, *diff_args: str) -> str:
    """不经被测模块直接算 ``patch-id``（只用于对照 ``-U0`` / ``-U3`` 的差别）。

    档位旗标必须与被测模块一致（:func:`patch_id_supports_verbatim`）：新 git 上
    ``fingerprint()`` 走 ``--verbatim``（空白敏感），这里再用 ``--stable`` 对比就是拿两套
    归一化比大小（CI 实测两者哈希不同）。``--verbatim`` 隐含 ``--stable``，但两者不能同时
    显式给（rc=129），所以是二选一。
    """

    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", *(diff_args or ("-U0",)), "--no-color", base, head],
        capture_output=True,
        env=_GIT_ENV,
        check=True,
    ).stdout
    mode = ("--verbatim",) if patch_id_supports_verbatim(repo) else ("--stable",)
    out = subprocess.run(
        ["git", "-C", str(repo), "patch-id", *mode],
        input=diff,
        capture_output=True,
        env=_GIT_ENV,
        check=True,
    ).stdout
    return out.decode("utf-8").split()[0]


def test_pure_rebase_keeps_fingerprint(tmp_path: Path) -> None:
    """纯 rebase（main 只动了另一个文件）→ 指纹不变；按旧 sha 也算得回同一个值。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    write(repo, "f1.txt", "l1\nl2\nL3\nl4\nl5\nl6\nl7\n")
    old_head = commit(repo, "feat: change one line")

    before = fingerprint(repo, "main", "feat")
    assert before.kind == "patch"
    assert before.value is not None and len(before.value) == 40

    # main 前进：改的是另一个文件（不碰 f1），所以 rebase 后 hunk 一字不差
    git(repo, "checkout", "-q", "main")
    write(repo, "f2.txt", "a\nb\nc\nd\n")
    commit(repo, "main: touch f2 only")
    new_main = git(repo, "rev-parse", "main")

    git(repo, "checkout", "-q", "feat")
    git(repo, "rebase", "-q", "main")

    after = fingerprint(repo, "main", "feat")
    assert after.kind == "patch"
    assert after.value == before.value
    assert after.merge_base == new_main  # merge base 确实换了，指纹没有
    # 旧 REVIEW-HEAD 兼容：按旧提交 sha 反算，内容相同 → 同指纹
    legacy = fingerprint_of_commit(repo, "main", old_head)
    assert isinstance(legacy, Fingerprint)
    assert legacy.value == before.value


def test_same_hunk_in_another_file_changes_fingerprint(tmp_path: Path) -> None:
    """同一段文本落到另一个文件 → 指纹必须变（换文件不能复用证据）。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "in-f1")
    write(repo, "f1.txt", "l1\nl2\nl3\nl4\nl5\nl6\nl7\nX\n")
    commit(repo, "add X at end of f1")

    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "in-f2")
    write(repo, "f2.txt", "a\nb\nc\nX\n")
    commit(repo, "add X at end of f2")

    f1_fp = fingerprint(repo, "main", "in-f1")
    f2_fp = fingerprint(repo, "main", "in-f2")
    assert f1_fp.paths == ("f1.txt",)
    assert f2_fp.paths == ("f2.txt",)
    assert f1_fp.value is not None and f2_fp.value is not None
    assert f1_fp.value != f2_fp.value


def test_real_content_change_changes_fingerprint(tmp_path: Path) -> None:
    """同文件同位置、内容真改了（X → Y）→ 指纹必须变。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "x")
    write(repo, "f1.txt", "l1\nl2\nl3\nl4\nl5\nl6\nl7\nX\n")
    commit(repo, "append X")

    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "y")
    write(repo, "f1.txt", "l1\nl2\nl3\nl4\nl5\nl6\nl7\nY\n")
    commit(repo, "append Y")

    assert fingerprint(repo, "main", "x").paths == ("f1.txt",)
    assert fingerprint(repo, "main", "x").value != fingerprint(repo, "main", "y").value


def test_context_shift_keeps_fingerprint_but_u3_would_not(tmp_path: Path) -> None:
    """上下文位移（行号 3 → 5、内容没变）→ -U0 同指纹；同一例 -U3 会换 id。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    write(repo, "f1.txt", "l1\nl2\nL3\nl4\nl5\nl6\nl7\n")
    old_head = commit(repo, "change l3")
    before = fingerprint(repo, "main", "feat")
    assert before.kind == "patch"

    # main 在同一个文件**上方**插两行：行号位移，且 rebase 不带冲突（改动点不重叠）
    git(repo, "checkout", "-q", "main")
    write(repo, "f1.txt", "p1\np2\nl1\nl2\nl3\nl4\nl5\nl6\nl7\n")
    commit(repo, "main: prepend two lines to f1")

    git(repo, "checkout", "-q", "feat")
    git(repo, "rebase", "-q", "main")
    after = fingerprint(repo, "main", "feat")

    assert before.value is not None and after.value is not None
    assert after.value == before.value  # -U0：内容没变就是没变
    # 对照：同一例用默认 -U3，patch-id 会因上下文位移而变 —— 这就是必须 -U0 的理由
    u3_before = patch_id(repo, before.merge_base, old_head, "-U3")
    u3_after = patch_id(repo, "main", "feat", "-U3")
    assert patch_id(repo, before.merge_base, old_head) == before.value
    assert patch_id(repo, "main", "feat") == after.value
    assert u3_before != u3_after


def test_binary_diff_is_binary_not_patch(tmp_path: Path) -> None:
    """二进制：两个不同 blob 的 patch-id 相同（判不了）→ 必须返回 kind="binary"。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "v2")
    (repo / "b.bin").write_bytes(bytes([0, 1, 2, 3]))
    commit(repo, "binary v2")

    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "v3")
    (repo / "b.bin").write_bytes(bytes([0, 9, 8, 7]))
    commit(repo, "binary v3")

    v2 = fingerprint(repo, "main", "v2")
    v3 = fingerprint(repo, "main", "v3")
    assert v2.kind == "binary" and v3.kind == "binary"
    assert v2.value is None and v3.value is None
    assert v2.paths == ("b.bin",) and v3.paths == ("b.bin",)
    # 对照：diff 正文对两个不同 blob 一模一样（只有 "Binary files … differ" 这一行），
    # 所以 patch-id 看不到内容差异 —— 上层不能靠它判「没变」，只能 fail-closed。
    # （不断言两者的 patch-id 相等：该行为随 git 版本变化，git 2.34.1 相等、CI 的
    #   runner 上新 git 不相等；判据落在「本模块一律返回 binary」上，版本无关。）
    for rev in ("v2", "v3"):
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "-U0", "--no-color", "main", rev],
            capture_output=True,
            env=_GIT_ENV,
            check=True,
        ).stdout
        # 正文里没有 NUL、只有 "Binary files … differ" 一行 —— 内容差异对 patch-id 不可见
        assert b"Binary files" in diff
        assert b"\x00" not in diff


def test_empty_diff_is_empty_and_unusable_revs_are_handled(tmp_path: Path) -> None:
    """空 diff（空提交）→ kind="empty"；取不到的 sha → None；未知 rev → PatchIdError。"""

    assert FINGERPRINT_ALGO == "patch-id-U0-v1"

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    git(repo, "commit", "-q", "--allow-empty", "-m", "no content change")

    empty = fingerprint(repo, "main", "feat")
    assert empty.kind == "empty"
    assert empty.value is None
    assert empty.paths == ()
    assert empty.merge_base == git(repo, "rev-parse", "main")

    # 取不到的对象不是异常：按「不可验证」处理
    assert fingerprint_of_commit(repo, "main", "b" * 40) is None
    # 而「rev 本身不成立」（不是共同祖先 / 不存在）是参数错，必须抛
    with pytest.raises(PatchIdError):
        fingerprint(repo, "main", "no-such-rev")
