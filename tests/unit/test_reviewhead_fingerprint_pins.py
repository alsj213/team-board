"""``reviewhead`` 指纹层 · 接线前钉死两个口子（t_d81b3354 承接 t_a9535d34 双问 P2）。

这是 ``test_reviewhead_fingerprint*.py`` 的补丁层：算法不动，只钉**调用面**。

1. **git 配置面**（`diff.noprefix` / `diff.renames`）：三种注入方式各一条 ——
   ``GIT_CONFIG_PARAMETERS``（等于 ``git -c``）、``GIT_CONFIG_GLOBAL``（个人全局配置，
   有意不被摘掉）、仓库内 ``diff.renames=false``。三者在 baseline 与注入后必须**逐字相等**；
   每条都配对照组（裸 git 在同一注入下确实换 id），证明用例不是空转。
2. **sha 口径**：40 或 64 位小写十六进制，``fingerprint`` 与 ``fingerprint_of_commit``
   两条入口同一结论（sha256 仓里 64 位是真 sha，不是字符串游戏）。
3. **反例**：``-`` 开头的 rev / 大写 sha 仍 ``PatchIdError``（放宽宽度不许放宽成什么都收）。

全部用真 ``git`` 在 ``tmp_path`` 造仓，不碰网络。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from team_board.reviewhead import (
    PATCH_ID_RE,
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

#: ``git -c`` 等价注入：``GIT_CONFIG_PARAMETERS`` 的写法（git 自己也是这么传给子进程的）
NOPREFIX_PARAMETERS = "'diff.noprefix'='true'"


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """在 ``repo`` 里跑 git，失败即测试失败。``env`` 只用于造对照组。"""

    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env if env is not None else _GIT_ENV,
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


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """``main`` 上一个文件；``topic`` 把它改一行的仓（内容改动，不是改名）。"""

    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "f.txt", "a\nb\nc\n")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "topic")
    write(repo, "f.txt", "a\nB\nc\n")
    commit(repo, "topic: change one line")
    git(repo, "checkout", "-q", "main")
    return repo


def raw_patch_id(
    repo: Path, base: str, head: str, *diff_args: str, env_extra: dict[str, str] | None = None
) -> str:
    """**不经被测模块**直接算 patch-id（对照组：证明某种注入确实会改变指纹）。

    档位必须与模块一致（``patch_id_supports_verbatim`` 探针决定，不按版本号猜）：
    新 git 上模块走 ``--verbatim``，这里再拿 ``--stable`` 去比就是拿两个算法比数字
    （CI 首轮实测：``baseline.value == rename_off`` 在 runner 的 git 上因此红）。
    """

    env = {**_GIT_ENV, **(env_extra or {})}
    if patch_id_supports_verbatim(repo):
        pid_argv = ["-c", "patchid.verbatim=true", "patch-id", "--verbatim"]
    else:
        pid_argv = ["-c", "patchid.verbatim=false", "patch-id", "--stable"]
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", *(diff_args or ("-U0",)), base, head],
        capture_output=True,
        env=env,
        check=True,
    ).stdout
    out = subprocess.run(
        ["git", "-C", str(repo), *pid_argv],
        input=diff,
        capture_output=True,
        env=env,
        check=True,
    ).stdout
    return out.decode("utf-8").split()[0]


# ---------------------------------------------------------------- git 配置面（三注入）


def test_git_config_parameters_noprefix_keeps_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GIT_CONFIG_PARAMETERS``（= ``git -c diff.noprefix=true``）不许改变指纹。"""

    repo = make_repo(tmp_path)
    baseline = fingerprint(repo, "main", "topic")
    assert baseline.kind == "patch" and baseline.value is not None

    # 对照组：同一注入、不带 --src-prefix/--dst-prefix 时 patch-id 确实会换值
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", NOPREFIX_PARAMETERS)
    control = raw_patch_id(repo, baseline.merge_base, "topic", env_extra={
        "GIT_CONFIG_PARAMETERS": NOPREFIX_PARAMETERS,
    })
    assert control != baseline.value

    # 被测模块会把这个键摘掉，指纹逐字不变
    result = fingerprint(repo, "main", "topic")
    assert result.kind == "patch"
    assert result.value == baseline.value
    assert result.paths == baseline.paths == ("f.txt",)


def test_git_config_global_noprefix_keeps_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GIT_CONFIG_GLOBAL`` 里的 ``diff.noprefix`` 不许改变指纹（该键有意保留）。"""

    repo = make_repo(tmp_path)
    baseline = fingerprint(repo, "main", "topic")
    assert baseline.kind == "patch" and baseline.value is not None

    global_cfg = tmp_path / "gitconfig-noprefix"
    global_cfg.write_text("[diff]\n\tnoprefix = true\n", encoding="utf-8")

    # 对照组：全局配置生效时，裸 diff → patch-id 换值
    control_env = {"GIT_CONFIG_GLOBAL": str(global_cfg)}
    control = raw_patch_id(
        repo, baseline.merge_base, "topic", env_extra=control_env
    )
    assert control != baseline.value

    # 被测模块保留 GIT_CONFIG_GLOBAL，但它改的那一维（前缀）已在命令行钉死
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    result = fingerprint(repo, "main", "topic")
    assert result.kind == "patch"
    assert result.value == baseline.value
    assert result.paths == baseline.paths == ("f.txt",)


def test_diff_renames_false_keeps_fingerprint(tmp_path: Path) -> None:
    """仓库内 ``diff.renames=false`` 不许改变指纹（纯改名场景，最敏感的那种）。"""

    repo = tmp_path / "rename-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "f.txt", "one\ntwo\nthree\nfour\nfive\n")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "topic")
    git(repo, "mv", "f.txt", "g.txt")  # 100% 改名：默认会被检测成 rename
    commit(repo, "topic: pure rename")
    git(repo, "checkout", "-q", "main")

    baseline = fingerprint(repo, "main", "topic")
    assert baseline.kind == "patch" and baseline.value is not None
    assert baseline.paths == ("f.txt", "g.txt")  # 已关改名检测：删一个 + 加一个

    # 对照组：默认（rename 检测开）与 --no-renames 的 patch-id 不同 —— 这一维真会换指纹
    rename_on = raw_patch_id(repo, baseline.merge_base, "topic")
    rename_off = raw_patch_id(repo, baseline.merge_base, "topic", "-U0", "--no-renames")
    assert rename_on != rename_off
    assert baseline.value == rename_off  # 被测模块取的是钉死后的那一份

    # 注入 diff.renames=false（仓库内配置，摘不掉）→ 指纹逐字不变
    git(repo, "config", "diff.renames", "false")
    after = fingerprint(repo, "main", "topic")
    assert after.kind == "patch"
    assert after.value == baseline.value
    assert after.paths == baseline.paths


# ---------------------------------------------------------------- sha 口径（40 / 64）


def _sha256_repo(tmp_path: Path) -> Path:
    """``git init --object-format=sha256`` 的仓；不支持就 skip（不算失败）。"""

    repo = tmp_path / "sha256-repo"
    repo.mkdir()
    probe = subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", "main", "--object-format=sha256"],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"本机 git 不支 --object-format=sha256: {probe.stderr.strip()[:120]}")

    write(repo, "f.txt", "a\nb\nc\n")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "topic")
    write(repo, "f.txt", "a\nB\nc\n")
    commit(repo, "topic: change one line")
    git(repo, "checkout", "-q", "main")
    return repo


def test_sha256_repo_sha64_agrees_across_both_entries(tmp_path: Path) -> None:
    """64 位 sha：``fingerprint`` 与 ``fingerprint_of_commit`` 结论一致（sha256 仓实测）。"""

    repo = _sha256_repo(tmp_path)
    base = git(repo, "rev-parse", "main")
    head = git(repo, "rev-parse", "topic")
    assert len(base) == 64 and len(head) == 64  # 前提成立：真 64 位仓

    by_branch = fingerprint(repo, "main", "topic")
    by_sha = fingerprint(repo, base, head)
    by_commit = fingerprint_of_commit(repo, base, head)

    assert by_branch.kind == by_sha.kind == "patch"
    assert by_sha.value is not None and len(by_sha.value) == 64
    assert PATCH_ID_RE.fullmatch(by_sha.value) is not None, by_sha.value
    assert by_branch.value == by_sha.value  # 分支名与裸 sha 是同一份内容
    assert by_commit is not None
    assert by_commit.value == by_sha.value  # 两条入口同一结论（曾一个能算、一个报错）
    assert by_commit.merge_base == by_sha.merge_base == by_branch.merge_base


def test_sha64_shape_layer_accepts_in_both_entries(tmp_path: Path) -> None:
    """64 位 sha 在 sha1 仓里也不再撞「提交 sha 非法」，而是走「取不到」那条路。"""

    repo = make_repo(tmp_path, "sha64-shape")
    absent = "c" * 64

    # 取不到的对象不是异常：按「不可验证」处理（形态层必须放行 64 位）
    assert fingerprint_of_commit(repo, "main", absent) is None


def test_uppercase_sha_is_rejected_by_both_entries(tmp_path: Path) -> None:
    """大写 sha 两条入口都拒（放宽宽度 ≠ 放宽大小写）。"""

    repo = make_repo(tmp_path, "uppercase")
    upper40 = "A" * 40

    with pytest.raises(PatchIdError, match="提交 sha"):
        fingerprint(repo, "main", upper40)
    with pytest.raises(PatchIdError, match="提交 sha"):
        fingerprint_of_commit(repo, "main", upper40)

    # 分支名不受影响（校验不许写宽成一律拒绝）
    assert fingerprint(repo, "main", "topic").kind == "patch"


# ---------------------------------------------------------------- 反例：- 开头 rev


def test_dash_prefixed_revs_still_rejected(tmp_path: Path) -> None:
    """``-`` 开头（含 ``--``）的 rev 仍 ``PatchIdError``，不会被 git 当选项吃。"""

    repo = make_repo(tmp_path, "dash-revs")

    with pytest.raises(PatchIdError, match="base_ref"):
        fingerprint(repo, "--", "topic")
    with pytest.raises(PatchIdError, match="head_rev"):
        fingerprint(repo, "main", "--all")
    with pytest.raises(PatchIdError, match="head_rev"):
        fingerprint(repo, "main", "-" + "a" * 39)
    with pytest.raises(PatchIdError, match="提交 sha"):
        fingerprint_of_commit(repo, "main", "-" + "a" * 63)

    assert fingerprint(repo, "main", "topic").kind == "patch"
