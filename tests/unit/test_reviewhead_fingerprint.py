"""``reviewhead`` 指纹层单测：内容场景（rebase / 行号位移 / 换文件 / 真改 / 缩进 / 二进制 / 空）。

这一层不碰 gh、不碰网络：全部用真 ``git`` 在 ``tmp_path`` 里造仓，「main 动了」「改了
文件」「改了内容」都由真提交产生，判断只走 :func:`fingerprint`。

锁的几件事：

- ``-U0`` + 丢 ``@@`` / ``index`` 行是硬要求：行号位移（纯 rebase、上游在别处插行）
  必须同指纹；带上下文的 ``-U3`` diff 会整体变（下面 ContextShift 那条用例把两边的
  diff 都算出来做对照）；
- **空白 / 缩进是内容**：只改缩进必须换指纹（``git patch-id`` 会归一化空白 → 漏判，
  这正是 E3 打回的那条 P1）；
- 换文件 / 真改内容必须换指纹（这两件是「内容真的变了」）；
- 二进制、空 diff 判不了 / 没有内容，分别返回 ``kind="binary"`` / ``"empty"``，
  不许折成「内容没变」。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from team_board.reviewhead import (
    FINGERPRINT_ALGO,
    Fingerprint,
    PatchIdError,
    UnrelatedHistoryError,
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


def raw_diff(repo: Path, base: str, head: str, *diff_args: str) -> bytes:
    """``git diff <diff_args or -U0> --no-color <base> <head>`` 的原始字节。"""

    return subprocess.run(
        ["git", "-C", str(repo), "diff", *(diff_args or ("-U0",)), "--no-color", base, head],
        capture_output=True,
        env=_GIT_ENV,
        check=True,
    ).stdout


def digest(repo: Path, base: str, head: str, *diff_args: str) -> str:
    """独立算内容指纹：``sha256(内容行)[:40]``（与被测实现同口径，但另写一遍）。"""

    lines = [
        line
        for line in raw_diff(repo, base, head, *diff_args).split(b"\n")
        if not line.startswith(b"@@") and not line.startswith(b"index ")
    ]
    return hashlib.sha256(b"\n".join(lines)).hexdigest()[:40]


def patch_id(repo: Path, base: str, head: str, *diff_args: str) -> str:
    """旧算法 ``git diff | git patch-id --stable``（只用于对照它漏判空白 / 缩进）。"""

    out = subprocess.run(
        ["git", "-C", str(repo), "patch-id", "--stable"],
        input=raw_diff(repo, base, head, *diff_args),
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
    # 对照：同一例用默认 -U3，diff 里的上下文行一起变 —— 这就是必须 -U0 的理由
    u3_before = digest(repo, before.merge_base, old_head, "-U3")
    u3_after = digest(repo, "main", "feat", "-U3")
    assert digest(repo, before.merge_base, old_head) == before.value
    assert digest(repo, "main", "feat") == after.value
    assert u3_before != u3_after


def test_indent_only_change_changes_fingerprint(tmp_path: Path) -> None:
    """**只改缩进**（4 空格 → 8 空格）必须换指纹 —— 旧 ``patch-id`` 会给同一个 id。

    这是 E3 打回的那条 P1 的单测版：``git patch-id`` 归一化行首/行尾与连续空白，于是
    「只改缩进」能复用旧证据；而 Python 里缩进能改语义 → 闸门 fail-open。
    """

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    write(repo, "code.py", "def f():\n    return 1\n")
    old_sha = commit(repo, "feat: add code.py")

    before = fingerprint(repo, "main", "feat")
    assert before.kind == "patch"

    # 只动缩进：别的字节一个不改
    write(repo, "code.py", "def f():\n        return 1\n")
    commit(repo, "feat: 只改缩进")

    after = fingerprint(repo, "main", "feat")
    assert after.kind == "patch"
    assert before.value is not None and after.value is not None
    assert after.value != before.value, "只改缩进必须换指纹（否则 fail-open）"
    # 对照：旧算法（patch-id）对同一对 diff 给出同一个 id —— 证明这条用例抓的就是那个缺陷
    assert patch_id(repo, before.merge_base, old_sha) == patch_id(repo, "main", "feat")


@pytest.mark.parametrize(
    "mutated",
    ("        x = 1\n", "x  =  1\n", "x = 1 \n", "x\t= 1\n"),
    ids=("indent", "inner-double-space", "trailing-space", "tab-instead-of-space"),
)
def test_any_whitespace_byte_change_changes_fingerprint(
    tmp_path: Path, mutated: str
) -> None:
    """**任意一个空白字节的改动**都必须换指纹（行首 / 行中 / 行尾 / 制表符）。

    E3 卡面点名的两个实测样例就是 ``        x = 1`` 与 ``x  =  1``：旧 ``patch-id`` 对它们
    都返回同一个 id，于是「改空白」能复用旧证据。缩进那条用例只覆盖行首，这里把行中、
    行尾、制表符一并锁住 —— 判据是「内容行逐字节进哈希」，不是「只有缩进才算内容」。
    """

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    write(repo, "code.txt", "x = 1\n")
    old_sha = commit(repo, "feat: add code.txt")

    before = fingerprint(repo, "main", "feat")
    assert before.kind == "patch"
    assert before.value is not None

    write(repo, "code.txt", mutated)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--amend", "--no-edit")
    after = fingerprint(repo, "main", "feat")
    assert after.kind == "patch"
    assert after.value is not None
    assert after.value != before.value, f"空白改动必须换指纹：{mutated!r}"
    # 对照：旧算法（patch-id）对同一对 diff 给出同一个 id —— 证明这条用例抓的就是那个缺陷
    assert patch_id(repo, before.merge_base, old_sha) == patch_id(repo, "main", "feat")


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
    """空 diff（空提交）→ kind="empty"；取不到的 sha → None；无共同祖先 → 专门的异常。"""

    assert FINGERPRINT_ALGO == "diff-sha256-U0-v1"

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
    # 「rev 本身不成立」/ 无共同祖先：两者都抛（UnrelatedHistoryError 是 PatchIdError 子类，
    # 但 fingerprint_of_commit 会把后者折成 None —— 见下一条断言）
    with pytest.raises(PatchIdError):
        fingerprint(repo, "main", "no-such-rev")

    orphan = _orphan_commit(repo)
    with pytest.raises(UnrelatedHistoryError, match="没有共同祖先"):
        fingerprint(repo, "main", orphan)
    assert fingerprint_of_commit(repo, "main", orphan) is None


def _orphan_commit(repo: Path) -> str:
    """造一条与本仓库主线无共同祖先的提交，返回它的 sha（``merge-base`` 必然失败）。"""

    git(repo, "checkout", "-q", "--orphan", "orphan")
    git(repo, "rm", "-q", "-r", "-f", "--cached", ".")
    write(repo, "only.py", "x = 1\n")
    sha = commit(repo, "orphan root")
    git(repo, "checkout", "-q", "feat")
    return sha
