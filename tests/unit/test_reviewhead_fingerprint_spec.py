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

import pytest

from team_board.reviewhead import (
    FINGERPRINT_ALGO,
    FINGERPRINT_ALGO_VERBATIM,
    FINGERPRINT_LEGACY_CAVEAT,
    fingerprint,
    patch_id_supports_verbatim,
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


def test_whitespace_blind_under_legacy_algo(tmp_path: Path) -> None:
    """旧档（空白不敏感）下只改缩进 → 同 ``value``，且 ``algo`` / ``caveat`` 必须标出来。"""

    repo = whitespace_pair(tmp_path)
    eight = fingerprint(repo, "main", "ws8", verbatim=False)
    twelve = fingerprint(repo, "main", "ws12", verbatim=False)

    assert eight.kind == "patch" and twelve.kind == "patch"
    assert eight.algo == FINGERPRINT_ALGO
    assert twelve.algo == FINGERPRINT_ALGO
    assert eight.value is not None and eight.value == twelve.value
    # 盲区必须随指纹一起交出去：消费方不得据此复用证据
    assert eight.caveat == FINGERPRINT_LEGACY_CAVEAT
    assert twelve.caveat == FINGERPRINT_LEGACY_CAVEAT


@pytest.mark.skipif(not VERBATIM, reason="本机 git 无 patch-id --verbatim（探针 rc≠0）")
def test_whitespace_distinguishes_under_verbatim_algo(tmp_path: Path) -> None:
    """``--verbatim`` 档：只差缩进也必须换 ``value``（空白进哈希），``caveat`` 为空。"""

    repo = whitespace_pair(tmp_path)
    eight = fingerprint(repo, "main", "ws8", verbatim=True)
    twelve = fingerprint(repo, "main", "ws12", verbatim=True)

    assert eight.algo == FINGERPRINT_ALGO_VERBATIM
    assert twelve.algo == FINGERPRINT_ALGO_VERBATIM
    assert eight.value is not None and twelve.value is not None
    assert eight.value != twelve.value
    assert eight.caveat == ""


def test_default_algo_follows_probe_not_version(tmp_path: Path) -> None:
    """不显式给 ``verbatim`` 时走探针：``algo`` 必须与探针结论一致（本机 = 旧档）。"""

    repo = whitespace_pair(tmp_path)
    assert patch_id_supports_verbatim(repo) is VERBATIM
    # 探针结果缓存：同一 repo 第二次不再起子进程，结论不变
    assert patch_id_supports_verbatim(repo) is VERBATIM

    eight = fingerprint(repo, "main", "ws8")
    assert eight.algo == (FINGERPRINT_ALGO_VERBATIM if VERBATIM else FINGERPRINT_ALGO)
    assert eight.kind == "patch"


def test_probe_runs_once_per_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """探针每个 repo 只跑一次（进程内缓存）：两次 ``fingerprint`` 只能看到一条探针调用。"""

    import team_board.reviewhead as reviewhead

    repo = whitespace_pair(tmp_path)
    real_run_git = reviewhead._run_git
    probed: list[tuple[str, ...]] = []

    def spy(where, argv, **kwargs):
        if tuple(argv) == ("patch-id", "--verbatim"):
            probed.append(tuple(argv))
        return real_run_git(where, argv, **kwargs)

    monkeypatch.setattr(reviewhead, "_run_git", spy)
    reviewhead._VERBATIM_SUPPORT.pop(os.fspath(repo), None)

    first = fingerprint(repo, "main", "ws8")
    second = fingerprint(repo, "main", "ws12")

    assert len(probed) == 1
    assert first.algo == second.algo


@pytest.mark.skipif(VERBATIM, reason="本机 git 支持 --verbatim：这一侧只验「不支持」时的 fail-closed")
def test_forcing_verbatim_without_support_fails_closed(tmp_path: Path) -> None:
    """无 ``--verbatim`` 的 git 上强行要 verbatim 档 → ``PatchIdError``，不静默降级成旧档。"""

    from team_board.reviewhead import PatchIdError

    repo = whitespace_pair(tmp_path)
    with pytest.raises(PatchIdError):
        fingerprint(repo, "main", "ws8", verbatim=True)


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
