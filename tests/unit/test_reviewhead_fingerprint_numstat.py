"""``reviewhead`` 指纹层 · numstat 取段回归（qa 打回：patch 正文被当成 numstat 行）。

根因（git 2.34.1 实测）：``git diff --numstat -U0`` 的 stdout = numstat 行 + 空行 +
一整份 patch（``-U<n>`` 本身隐含 ``-p``；``--numstat -U0 --no-patch`` 会把 numstat
一起关掉，不可用）。patch 正文的删除行长成 ``-\\t5\\tghost`` / ``-\\t-\\tpayload``，
逐行按 numstat 正则筛就把它当成 numstat 行 —— 于是：

- 纯文本 diff 被报成 ``kind="binary"``（正文里出现 ``-\\t-\\tpayload``）；
- ``paths`` 混进正文碎片（``('f1.txt', 'ghost')``）。

这里锁的是「先按 ``diff --git`` 切出 numstat 段，再在段内逐行校验」，用真 git 造仓，
不碰网络。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from team_board.reviewhead import PatchIdError, _numstat_rows, fingerprint

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
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "f1.txt", "keep\n")
    commit(repo, "base")
    return repo


def test_deleted_lines_shaped_like_numstat_rows_are_not_paths(tmp_path: Path) -> None:
    """qa 复现 A+B：删除行形如 ``-\\t5\\tghost`` / ``-\\t-\\tpayload``，不许被当成 numstat。

    ``paths`` 只能是真文件名，``kind`` 只能是 patch（仓里连一个二进制 blob 都没有）。
    """

    repo = make_repo(tmp_path)
    write(repo, "f1.txt", "keep\n\t5\tghost\n\t-\tpayload\n")
    commit(repo, "main 里加入两行像 numstat 的内容")
    git(repo, "checkout", "-q", "-b", "topic")
    write(repo, "f1.txt", "keep\n")
    commit(repo, "删掉那两行")

    result = fingerprint(repo, "main", "topic")

    assert result.kind == "patch", result
    assert result.paths == ("f1.txt",), result
    assert result.value is not None and len(result.value) == 40


def test_binary_paths_do_not_include_patch_body_fragments(tmp_path: Path) -> None:
    """真二进制 + 文本混排：kind=binary 且 paths 只有真路径（正文碎片不许混进来）。"""

    repo = make_repo(tmp_path)
    (repo / "b.bin").write_bytes(b"\x00\x01\x02")
    commit(repo, "main 里放一个二进制")
    git(repo, "checkout", "-q", "-b", "topic")
    (repo / "b.bin").write_bytes(b"\x00\x01\x02\x03")
    write(repo, "f1.txt", "keep\n\t-\tpayload\n")
    commit(repo, "改二进制 + 加一行像 numstat 的内容")

    result = fingerprint(repo, "main", "topic")

    assert result.kind == "binary", result
    assert result.paths == ("b.bin", "f1.txt"), result


def test_multiple_text_files_paths_are_exact(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "topic")
    write(repo, "f1.txt", "keep\nnew\n")
    write(repo, "sub/f2.txt", "x\n")
    commit(repo, "两个文本文件")

    result = fingerprint(repo, "main", "topic")

    assert result.kind == "patch", result
    assert result.paths == ("f1.txt", "sub/f2.txt"), result


def test_numstat_rows_cut_at_first_diff_git_line() -> None:
    """切段口径本身：numstat 段之后的一切（含删形行）都不算行。"""

    raw = (
        "1\t0\tf1.txt\n"
        "-\t-\tb.bin\n"
        "\n"
        "diff --git a/f1.txt b/f1.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/f1.txt\n"
        "+++ b/f1.txt\n"
        "@@ -1,0 +2,1 @@\n"
        "-\t5\tghost\n"
        "-\t-\tpayload\n"
    )

    assert _numstat_rows(raw, "label") == ["1\t0\tf1.txt", "-\t-\tb.bin"]


def test_numstat_section_with_unparsable_line_fails_closed() -> None:
    """段内出现看不懂的行 → PatchIdError（不猜、不把 patch 正文当路径）。"""

    raw = "1\t0\tf1.txt\n垃圾行没有制表符\n"

    with pytest.raises(PatchIdError, match="解析不了"):
        _numstat_rows(raw, "label")


def test_numstat_section_without_rows_fails_closed() -> None:
    raw = "\ndiff --git a/f1.txt b/f1.txt\n--- a/f1.txt\n"

    with pytest.raises(PatchIdError, match="解析不出 numstat 行"):
        _numstat_rows(raw, "label")
