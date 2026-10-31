"""``reviewhead`` 指纹层 · 接线前钉死两个口子（t_d81b3354 承接 t_a9535d34 双问 P2）。

这是 ``test_reviewhead_fingerprint*.py`` 的补丁层：算法不动，只钉**调用面**。

1. **git 配置面**（`diff.noprefix` / `diff.renames` / `diff.interHunkContext`）：注入方式
   各一条 —— ``GIT_CONFIG_PARAMETERS``（等于 ``git -c``）、``GIT_CONFIG_GLOBAL``（全局配置）、
   仓库内配置。baseline 与注入后必须**逐字相等**；每条都配对照组（裸 git 在同一注入下确实
   换 id），证明用例不是空转。
2. **sha 口径**：40 或 64 位小写十六进制，``fingerprint`` 的 base / head 两个位置与
   ``fingerprint_of_commit`` 同一结论（sha256 仓里 64 位是真 sha，不是字符串游戏）。
3. **反例**：``-`` 开头的 rev / 大写 sha 仍 ``PatchIdError``（放宽宽度不许放宽成什么都收）。
4. **配置载体**（第二视角 P2 · D1）：取对象那一路（``git fetch``）的 ``url.*.insteadOf``
   不管从 ``GIT_CONFIG_PARAMETERS`` 还是 ``GIT_CONFIG_GLOBAL`` 注入都要同样生效（旧版前者被
   摘掉 → fetch rc=128 → 静默 ``None``）；算指纹那一路两条载体则一起关掉。
5. **``diff.interHunkContext``**（D3）：仓库内配成非 0 时，被测模块仍给出与 baseline 相同的
   指纹（命令行 ``-c diff.interHunkContext=0`` 钉死）。

全部用真 ``git`` 在 ``tmp_path`` 造仓，不碰网络（``insteadOf`` 目标是本地路径，取不到时
用的是 ``file:///nonexistent`` 这种立即失败的 URL）。
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
    """``GIT_CONFIG_GLOBAL`` 里的 ``diff.noprefix`` 不许改变指纹。

    算指纹那一路把全局 / 系统配置指向空设备（见 ``_git_env``）：不是「留着但靠命令行压住」，
    而是根本不进口（删环境变量不够 —— git 会回落到 ``~/.gitconfig``）。
    """

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

    # 被测模块在算指纹这一路把全局配置整体关掉 → 注入根本到不了 diff
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
    # 被测模块的指纹是 sha256(内容行)[:40]，不是 patch-id；rename 这一维靠 -c/--no-renames 钉死：
    # 打开/关闭 diff.renames 后 fingerprint 都必须给出同一值。
    git(repo, "config", "diff.renames", "true")
    assert fingerprint(repo, "main", "topic").value == baseline.value
    git(repo, "config", "diff.renames", "false")
    assert fingerprint(repo, "main", "topic").value == baseline.value

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
    assert by_sha.value is not None and len(by_sha.value) == 40  # sha256(内容行) 取前 40 位
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


# ------------------------------------------- 第二视角 P2（D1 / D2 / D3，t_9f4673a2）

#: 取不到的远端 URL（``insteadOf`` 的目的地是本地源仓，测试不碰网络）
UNREACHABLE_REMOTE = "file:///nonexistent/repo.git"


def git_rc(repo: Path, *args: str) -> int:
    """跑 git 只取 rc（用于断言「这个对象不在」这类**必须失败**的调用）。"""

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=False,
    ).returncode


def _src_with_topic(tmp_path: Path) -> tuple[Path, str, str]:
    """源仓：``main`` 上一行文件、``topic`` 多一个提交。返回 (源仓, base sha, topic sha)。"""

    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    write(src, "x.txt", "one\n")
    commit(src, "base")
    git(src, "checkout", "-q", "-b", "topic")
    write(src, "x.txt", "one\ntwo\n")
    commit(src, "topic: second line")
    return src, git(src, "rev-parse", "main"), git(src, "rev-parse", "topic")


def _base_only_clone(tmp_path: Path, src: Path, name: str) -> Path:
    """只含 ``src`` 那个 base 提交的仓；远端 URL 取不到，只有 ``url.*.insteadOf`` 指得到源仓。"""

    dst = tmp_path / name
    dst.mkdir()
    git(dst, "init", "-q", "-b", "main")
    git(dst, "fetch", "-q", str(src), "main")
    git(dst, "reset", "-q", "--hard", "FETCH_HEAD")
    git(dst, "remote", "add", "origin", UNREACHABLE_REMOTE)
    return dst


def test_fetch_config_carriers_treated_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1：``url.*.insteadOf`` 从哪条载体注入，``fingerprint_of_commit`` 的 fetch 都同样生效。

    旧版摘 ``GIT_CONFIG_PARAMETERS`` 却保留 ``GIT_CONFIG_GLOBAL``：同一份 insteadOf 换个载体，
    一个 rc=0 算出指纹、另一个 fetch rc=128 后**静默返回 None**。现在取对象那一路两条载体
    一律保留（算指纹那一路一起关掉），所以两条载体给出同一个指纹。
    """

    src, base_sha, topic_sha = _src_with_topic(tmp_path)
    object_present = ("cat-file", "-e", f"{topic_sha}^{{commit}}")

    # 对照组：零载体 → 远端取不到 → 返回 None（不是异常），对象确实没进来（用例不是空转）
    unreachable = _base_only_clone(tmp_path, src, "dst-no-carrier")
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    assert git_rc(unreachable, *object_present) != 0
    assert fingerprint_of_commit(unreachable, base_sha, topic_sha) is None
    assert git_rc(unreachable, *object_present) != 0

    # 载体 A：``git -c`` 等价注入（GIT_CONFIG_PARAMETERS）—— 旧版在这一路返回 None
    via_params_repo = _base_only_clone(tmp_path, src, "dst-params")
    monkeypatch.setenv(
        "GIT_CONFIG_PARAMETERS",
        f"'url.file://{src}.insteadOf'='{UNREACHABLE_REMOTE}'",
    )
    via_params = fingerprint_of_commit(via_params_repo, base_sha, topic_sha)
    assert via_params is not None and via_params.kind == "patch"
    assert git_rc(via_params_repo, *object_present) == 0

    # 载体 B：全局配置（GIT_CONFIG_GLOBAL）
    via_global_repo = _base_only_clone(tmp_path, src, "dst-global")
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    cfg = tmp_path / "gitconfig-insteadof"
    cfg.write_text(
        f'[url "file://{src}"]\n\tinsteadOf = {UNREACHABLE_REMOTE}\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    via_global = fingerprint_of_commit(via_global_repo, base_sha, topic_sha)
    assert via_global is not None and via_global.kind == "patch"

    # 两条载体同口径：同内容同指纹（旧版这里 A 是 None，D1 就是这个不对称）
    assert via_global.value == via_params.value
    assert via_global.merge_base == via_params.merge_base == base_sha


def test_base_and_head_share_sha_shape_check(tmp_path: Path) -> None:
    """D2：看样子像 sha 的 rev 在 ``base_ref`` 与 ``head_rev`` 位置同一份口径。

    对照组：git 自己收大写对象名（``rev-parse`` / ``diff`` 全 rc=0），所以「大写 base 能算」
    是本 PR 之前的真行为 —— 现在两处一起 ``PatchIdError``，这是**有意的行为变更**。
    """

    repo = make_repo(tmp_path, "sha-shape-aligned")
    base = git(repo, "rev-parse", "main")
    head = git(repo, "rev-parse", "topic")

    # 对照组 1：git 侧收大写（不是对象不存在）
    assert git(repo, "rev-parse", "--verify", base.upper()) == base
    assert git(repo, "diff", "--numstat", base.upper(), "topic").startswith("1\t")

    # 对照组 2：小写裸 sha 两个位置都算得出，且与分支名同一份内容
    lower = fingerprint(repo, base, head)
    assert lower.kind == "patch" and lower.value is not None
    assert lower.value == fingerprint(repo, "main", "topic").value

    # 两处同口径：大写 40 / 64 位一律拒（不再「head 拒、base 放过」）
    with pytest.raises(PatchIdError, match="提交 sha"):
        fingerprint(repo, base.upper(), "topic")
    with pytest.raises(PatchIdError, match="提交 sha"):
        fingerprint(repo, "main", head.upper())
    with pytest.raises(PatchIdError, match="提交 sha"):
        fingerprint(repo, base.upper(), head)
    with pytest.raises(PatchIdError, match="提交 sha"):
        fingerprint_of_commit(repo, base.upper(), head)

    # 分支名不受影响（校验不许写宽成一律拒绝）
    assert fingerprint(repo, "main", "topic").kind == "patch"


def _two_hunk_repo(tmp_path: Path) -> Path:
    """两处改动隔 2 行不改的仓：``-U0`` 默认两个 ``@@``，``interHunkContext≥2`` 并成一个。"""

    repo = tmp_path / "ihc-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    lines = [f"line{i}" for i in range(1, 21)]
    write(repo, "f.txt", "\n".join(lines) + "\n")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "topic")
    lines[1] = "CHANGED2"
    lines[4] = "CHANGED5"
    write(repo, "f.txt", "\n".join(lines) + "\n")
    commit(repo, "topic: two changes two lines apart")
    git(repo, "checkout", "-q", "main")
    return repo


def test_inter_hunk_context_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3：``diff.interHunkContext`` 非 0 不许改变指纹（命令行 ``-c …=0`` 钉死）。

    两条注入轴各配对照组：全局配置载体（算指纹那一路整体关掉，见 :func:`_git_env`）与
    **仓库内配置**（摘不掉，只能靠 ``-c diff.interHunkContext=0`` 压住）。
    """

    repo = _two_hunk_repo(tmp_path)
    baseline = fingerprint(repo, "main", "topic")
    assert baseline.kind == "patch" and baseline.value is not None

    # --- 轴 1：全局配置载体。对照组：裸 diff 在该配置下确实换 id
    cfg = tmp_path / "gitconfig-ihc"
    cfg.write_text("[diff]\n\tinterHunkContext = 2\n", encoding="utf-8")
    control_global = raw_patch_id(
        repo, baseline.merge_base, "topic", "-U0",
        env_extra={"GIT_CONFIG_GLOBAL": str(cfg)},
    )
    assert control_global != baseline.value
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    assert fingerprint(repo, "main", "topic").value == baseline.value
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

    # --- 轴 2：仓库内配置（摘不掉的那一维）。对照组：同一仓里裸 diff 已换 id
    git(repo, "config", "diff.interHunkContext", "2")
    control_local = raw_patch_id(repo, baseline.merge_base, "topic", "-U0")
    assert control_local != baseline.value

    after = fingerprint(repo, "main", "topic")
    assert after.kind == "patch"
    assert after.value == baseline.value
    assert after.paths == baseline.paths == ("f.txt",)
