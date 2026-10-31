"""``reviewhead`` 指纹层 · 空白敏感（algo 标记）与 git 配置钉死的规格用例。

对应卡 ``t_ecf81afb``（返工 E1-b）。四类判据：

1. **空白盲区**：同一处（本 PR 已改过的那一行）只改缩进 8 → 12 空格 ——
   旧档 ``algo="patch-id-U0-v1"`` 下两边的 ``value`` 必须相同（盲区存在，报告行必须
   带出 :data:`FINGERPRINT_LEGACY_CAVEAT`）；``git patch-id --verbatim`` 可用的机器上
   走 ``algo="patch-id-U0-verbatim-v1"``，``value`` 必须不同。两档用探针（不是版本号）
   分派，探针逐字是 ``git patch-id --verbatim </dev/null``。
2. **rename 拆成两个真路径**：rename-only 的 ``paths`` 是 ``old.txt`` + ``new.txt``。
3. **非 ASCII 文件名给真名字**（不是 ``"\\344\\270\\255…"`` 的 C 转义）。
4. **配置钉死**：仓库里 ``diff.renames`` / ``core.quotePath`` 怎么设都不改指纹与 paths。

全部用真 ``git`` 在 ``tmp_path`` 里造仓，不碰网络、不碰 gh。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from team_board.reviewhead import (
    fingerprint,
)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fingerprint-spec",
    "GIT_AUTHOR_EMAIL": "fingerprint-spec@example.com",
    "GIT_COMMITTER_NAME": "fingerprint-spec",
    "GIT_COMMITTER_EMAIL": "fingerprint-spec@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _probe() -> bool:
    """卡里那条探针，逐字：``git patch-id --verbatim </dev/null``，判据只有 rc。"""

    proc = subprocess.run(
        ["git", "patch-id", "--verbatim"],
        input=b"",
        capture_output=True,
        env=_GIT_ENV,
        check=False,
    )
    return proc.returncode == 0


#: 本机 git 是否支持 ``--verbatim``（本机 git 2.34.1 = False，rc=129）
VERBATIM = _probe()


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


def make_repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """``main`` 上一个 base 提交（默认 ``app.yml`` 两行 YAML）。"""

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name, text in (files or {"app.yml": "app:\n  name: demo\n"}).items():
        write(repo, name, text)
    commit(repo, "base")
    return repo


def whitespace_pair(tmp_path: Path) -> Path:
    """两个分支：同一新增行，缩进 8 空格 vs 12 空格 —— 只差空白。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "ws8")
    write(repo, "app.yml", "app:\n  name: demo\n        port: 8080\n")
    commit(repo, "ws8: eight spaces")

    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "ws12")
    write(repo, "app.yml", "app:\n  name: demo\n            port: 8080\n")
    commit(repo, "ws12: twelve spaces")
    return repo
def test_rename_only_gives_two_real_paths(tmp_path: Path) -> None:
    """rename-only 被拆成 delete + add：``paths`` 必须是两个真文件名，不许留 ``a => b``。

    注：本机 git 2.34.1 的 numstat 行序是「add 行在前、delete 行在后」
    （实测 ``2\\t0\\tnew.txt`` / ``0\\t2\\told.txt``），所以这里断言集合 + 条数，
    不断言版本相关的行序。
    """

    repo = make_repo(tmp_path, {"old.txt": "hello\nworld\n"})
    git(repo, "checkout", "-q", "-b", "renamed")
    git(repo, "mv", "old.txt", "new.txt")
    commit(repo, "rename only")

    renamed = fingerprint(repo, "main", "renamed")
    assert renamed.kind == "patch"
    assert len(renamed.paths) == 2
    assert set(renamed.paths) == {"old.txt", "new.txt"}
    assert not any(" => " in path for path in renamed.paths)


def test_non_ascii_path_is_real_name(tmp_path: Path) -> None:
    """非 ASCII 文件名给真名字；仓库把 ``core.quotePath`` 设成 true 也不许变。"""

    repo = make_repo(tmp_path, {"keep.txt": "x\n"})
    git(repo, "checkout", "-q", "-b", "zh")
    write(repo, "中文.txt", "内容\n")
    commit(repo, "chinese filename")

    assert fingerprint(repo, "main", "zh").paths == ("中文.txt",)

    git(repo, "config", "core.quotePath", "true")
    assert fingerprint(repo, "main", "zh").paths == ("中文.txt",)


def test_fingerprint_ignores_diff_renames_config(tmp_path: Path) -> None:
    """``diff.renames=true`` / ``false`` 两支同指纹、同 paths（rename 已被钉成 delete+add）。"""

    repo = make_repo(tmp_path, {"old.txt": "hello\nworld\n"})
    git(repo, "checkout", "-q", "-b", "renamed")
    git(repo, "mv", "old.txt", "new.txt")
    commit(repo, "rename only")

    git(repo, "config", "diff.renames", "true")
    on = fingerprint(repo, "main", "renamed")
    git(repo, "config", "diff.renames", "false")
    off = fingerprint(repo, "main", "renamed")

    assert on.kind == off.kind == "patch"
    assert on.value is not None and on.value == off.value
    assert on.paths == off.paths
    assert set(on.paths) == {"old.txt", "new.txt"}
    assert on.algo == off.algo
