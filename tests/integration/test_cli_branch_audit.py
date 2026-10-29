"""cli branch-audit 的集成测试：在 tmp_path 里造真 git 仓库，验 rc 与关键文案。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from team_board import cli

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "audit-test",
    "GIT_AUTHOR_EMAIL": "audit@example.com",
    "GIT_COMMITTER_NAME": "audit-test",
    "GIT_COMMITTER_EMAIL": "audit@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
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
    rc = cli.main(["branch-audit", *args])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ------------------------------------------------------- 场景 A：跨卡提交


def test_scenario_a_foreign_card_is_red(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w2.txt", "feat: t_bbbbbbbb 的活跑到 A 分支")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert rc == 1
    assert err == ""
    assert "t_aaaaaaaa" in out  # 分支卡号
    assert "t_bbbbbbbb" in out  # 对方卡号
    assert "跨卡提交" in out
    assert out.rstrip().endswith(
        "（提交归属 1 处 / checkout 0 处 / 分支结构 0 处）"
    )


def test_scenario_a_foreign_card_from_path_evidence(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(
        repo,
        "evidence/t_406be0e8-audit.log",
        "chore: 落证据",
    )
    git(repo, "checkout", "-q", "main")

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 1
    assert "t_406be0e8" in out
    assert "证据：文件路径 evidence/t_406be0e8-audit.log" in out


# ---------------------------------------------------- 场景 A'：未声明卡号


def test_scenario_a_prime_no_card_id_is_red(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w.txt", "chore: 顺手改点东西")
    git(repo, "checkout", "-q", "main")

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 1
    assert "未声明卡号" in out
    assert "t_aaaaaaaa" in out


# ------------------------------------------------- 场景 B：checkout 偏移


def test_scenario_b_main_checkout_on_card_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w.txt", "feat(t_aaaaaaaa): 本卡的活")
    git(repo, "checkout", "-q", "wt/t_aaaaaaaa")  # 主 checkout 停在卡分支

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 1
    assert str(repo.resolve()) in out
    assert "wt/t_aaaaaaaa" in out
    assert "（应为 main）" in out


def test_scenario_b_worktree_dirname_mismatch(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w.txt", "feat(t_aaaaaaaa): 本卡的活")
    git(repo, "checkout", "-q", "main")
    wrong = tmp_path / "wrongdir"
    git(repo, "worktree", "add", "-q", str(wrong), "wt/t_aaaaaaaa")

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 1
    assert str(wrong.resolve()) in out
    assert "当前在 wt/t_aaaaaaaa" in out
    assert "（应为 wt/wrongdir）" in out


def test_scenario_b_detached_worktree_is_red(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w.txt", "feat(t_aaaaaaaa): 本卡的活")
    git(repo, "checkout", "-q", "main")
    detached = tmp_path / "detwt"
    git(repo, "worktree", "add", "-q", "--detach", str(detached), "wt/t_aaaaaaaa")

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 1
    assert "detached@" in out
    assert str(detached.resolve()) in out


def test_scenario_b_other_branch_worktree_is_red(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    git(repo, "checkout", "-q", "-b", "pm/ci-check")
    git(repo, "checkout", "-q", "main")
    other = tmp_path / "pmci"
    git(repo, "worktree", "add", "-q", str(other), "pm/ci-check")

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 1
    assert "pm/ci-check" in out


# ------------------------------------------------------------ 绿色基线


def test_green_repo_returns_zero(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w.txt", "feat(t_aaaaaaaa): 本卡的活")
    git(repo, "checkout", "-q", "main")
    good = tmp_path / "t_aaaaaaaa"  # 目录名 == 卡号
    git(repo, "worktree", "add", "-q", str(good), "wt/t_aaaaaaaa")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert rc == 0
    assert err == ""
    assert "结论：无偏差" in out
    assert "t_aaaaaaaa" in out


def test_only_non_card_branches_are_skipped(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/ci-setup")
    commit_file(repo, "ci.yml", "chore: 没有卡号的 setup 提交")
    git(repo, "checkout", "-q", "main")

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 0
    assert "检查的卡分支: （无）" in out


# -------------------------------------------------------------- 豁免表


def test_allowlist_hit_suppresses_and_echoes_reason(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w2.txt", "feat: t_bbbbbbbb 的活跑到 A 分支")
    short = git(repo, "rev-parse", "--short=7", "wt/t_aaaaaaaa").strip()
    git(repo, "checkout", "-q", "main")
    allow = repo / ".team-board" / "branch-audit-allow.txt"
    allow.parent.mkdir(parents=True, exist_ok=True)
    allow.write_text(
        "# <分支卡号> <对方卡号或-> <提交sha7..40> <理由，必填>\n"
        f"t_aaaaaaaa t_bbbbbbbb {short} 同卡族返工已确认\n",
        encoding="utf-8",
    )

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 0
    assert "豁免（1 条）" in out
    assert "同卡族返工已确认" in out
    assert "结论：无偏差" in out


def test_allowlist_other_sha_does_not_suppress(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w2.txt", "feat: t_bbbbbbbb 的活跑到 A 分支")
    git(repo, "checkout", "-q", "main")
    allow = repo / ".team-board" / "branch-audit-allow.txt"
    allow.parent.mkdir(parents=True, exist_ok=True)
    allow.write_text(
        "t_aaaaaaaa t_bbbbbbbb deadbee 同卡族返工\n", encoding="utf-8"
    )

    rc, out, _ = run(["--repo", str(repo)], capsys)

    assert rc == 1
    assert "豁免（0 条）" in out


def test_allowlist_illegal_line_returns_two(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    allow = tmp_path / "bad-allow.txt"
    allow.write_text("t_aaaaaaaa 52a2f93\n", encoding="utf-8")

    rc, out, err = run(
        ["--repo", str(repo), "--allow-file", str(allow)], capsys
    )

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "Traceback" not in err


# --------------------------------------------------------------- rc2 边界


@pytest.mark.parametrize(
    "args",
    [
        ["--repo", ""],
        ["--repo", "/nonexistent-audit-path"],
        ["--repo", "/tmp"],
        ["--repo", "{repo}", "--main-branch", "nope"],
        ["--repo", "{repo}", "--branch", ""],
        ["--repo", "{repo}", "--branch", "nosuchbranch"],
    ],
)
def test_bad_arguments_return_two(
    repo: Path, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    resolved = [a.replace("{repo}", str(repo)) for a in args]
    rc, out, err = run(resolved, capsys)
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "Traceback" not in err


def test_repo_pointing_at_file_returns_two(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file_path = repo / "README.md"
    rc, out, err = run(["--repo", str(file_path)], capsys)
    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")


def test_unknown_flag_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["branch-audit", "--bogus"])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bogus" in captured.err


# --------------------------------------- 环境/参数级错误一律 rc2（必修 1）
# 判据来自卡面：AuditError 语义 → rc2、stderr `错误: `、stdout 空、无 Traceback。
# rc1 是「发现偏差」的专用码，崩溃绝不能和真偏差同码（CI 分不出红的原因）。


def test_allow_file_not_utf8_returns_two(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """豁免表指到二进制文件（非 UTF-8）→ rc2，不许 UnicodeDecodeError 冒出来。"""

    binary = tmp_path / "binary-allow.bin"
    binary.write_bytes(b"\xa0\xa1\xff\xfe not utf-8 \x80")

    rc, out, err = run(["--repo", str(repo), "--allow-file", str(binary)], capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "UTF-8" in err
    assert "Traceback" not in err


def test_allow_file_is_directory_returns_two(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    directory = tmp_path / "allow-dir"
    directory.mkdir()

    rc, out, err = run(["--repo", str(repo), "--allow-file", str(directory)], capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "Traceback" not in err


def test_missing_git_executable_returns_two(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PATH`` 里没有 git → rc2，不许 FileNotFoundError 冒出来。"""

    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "git" in err
    assert "Traceback" not in err


def test_ascii_stdout_returns_two(
    repo: Path, tmp_path: Path
) -> None:
    """stdout 编码表示不了中文 → rc2（真子进程跑，环境变量才有意义）。

    ``LC_ALL=C`` + ``PYTHONIOENCODING=ascii`` 同时把 stdout/stderr 设成 ASCII：
    报告里的中文写不出去。这是环境级错误，不是「发现偏差」，必须 rc2、
    stdout 一个字节都不许落、stderr 里要有 ``错误: ``（UTF-8 字节直写）。
    """

    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "w2.txt", "feat: t_bbbbbbbb 的活跑到 A 分支")
    git(repo, "checkout", "-q", "main")
    src = Path(__file__).resolve().parents[2] / "src"
    env = {
        **os.environ,
        "PYTHONPATH": str(src),
        "PYTHONIOENCODING": "ascii",
        "LC_ALL": "C",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "team_board.cli", "branch-audit",
         "--repo", str(repo)],
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == b""
    assert proc.stderr.startswith("错误: ".encode())
    assert b"Traceback" not in proc.stderr


# ------------------- 必修 4：git 输出不是 UTF-8 也一律 rc2（不许 rc1 + traceback）
# 判据同必修 1：rc1 只表示「发现偏差」，解码失败是环境错误 → rc2 + `错误: ` +
# stdout 空 + 无 traceback。崩溃发生在第一条 git log，不兜住就整轮稽核中断。


def test_git_output_non_utf8_returns_two_gbk_log_encoding(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``i18n.logOutputEncoding=GBK`` + 中文提交标题 → rc2；去掉该配置后 rc0。"""

    git(repo, "config", "i18n.logOutputEncoding", "GBK")
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "gbk.txt", "feat(t_aaaaaaaa): 中文标题的提交")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "UTF-8" in err
    assert "Traceback" not in err

    # 对照：同一棵树去掉 logOutputEncoding → 正常稽核，rc0
    git(repo, "config", "--unset", "i18n.logOutputEncoding")
    rc_ok, out_ok, err_ok = run(["--repo", str(repo)], capsys)

    assert rc_ok == 0, (rc_ok, out_ok, err_ok)
    assert err_ok == ""
    assert "结论：无偏差" in out_ok


def test_git_output_non_utf8_returns_two_raw_path_bytes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``core.quotePath=false`` + 文件名含非 UTF-8 字节 → rc2；quotePath 恢复后 rc0。"""

    git(repo, "config", "core.quotePath", "false")
    git(repo, "checkout", "-q", "-b", "wt/t_bbbbbbbb")
    weird_name = os.fsdecode(b"bad_\xff\xfe.txt")
    (repo / weird_name).write_bytes(b"x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat(t_bbbbbbbb): 非 UTF-8 文件名")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "UTF-8" in err
    assert "Traceback" not in err

    # 对照：quotePath 恢复成默认值 → 同一棵树正常稽核，rc0
    git(repo, "config", "core.quotePath", "true")
    rc_ok, out_ok, err_ok = run(["--repo", str(repo)], capsys)

    assert rc_ok == 0, (rc_ok, out_ok, err_ok)
    assert err_ok == ""
    assert "结论：无偏差" in out_ok


# ------------------------------------------------- --branch 只看一条分支


def test_branch_main_is_not_a_card_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = run(["--repo", str(repo), "--branch", "main"], capsys)

    assert rc == 0
    assert err == ""
    assert "不是卡分支，跳过提交归属检查" in out
    assert "结论：无偏差" in out


def test_branch_only_checks_the_requested_card_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 干净卡分支
    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "a.txt", "feat(t_aaaaaaaa): 本卡的活")
    git(repo, "checkout", "-q", "main")
    # 脏卡分支：跨卡提交
    git(repo, "checkout", "-q", "-b", "wt/t_dddddddd")
    commit_file(repo, "d.txt", "feat: t_eeeeeeee 的活")
    git(repo, "checkout", "-q", "main")

    rc_all, out_all, _ = run(["--repo", str(repo)], capsys)
    assert rc_all == 1
    assert "t_eeeeeeee" in out_all

    for spelling in ("wt/t_aaaaaaaa", "t_aaaaaaaa"):
        rc, out, _ = run(["--repo", str(repo), "--branch", spelling], capsys)
        assert rc == 0, out
        assert "t_eeeeeeee" not in out
        assert "检查的卡分支: t_aaaaaaaa" in out


# -------------------------- 必修 2：裸卡号指向「漏 wt/ 前缀」的分支不许假绿


def test_bare_card_branch_without_wt_prefix_returns_two(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """只有裸分支 ``refs/heads/t_aaaaaaaa``（漏 wt/ 前缀）时 ``--branch t_aaaaaaaa`` 必须 rc2。

    修前这里 rc0 + 「检查的卡分支: （无）」+「结论：无偏差」—— 零条 ref 静默空跑。
    CI 传的是 ``$HEAD_REF``，于是「PR 头分支漏写 wt/」这个最该抓的偏差形态会被
    门禁直接放行。卡面只许诺两种写法都指向**卡分支**，没许诺指向不存在的卡分支判绿。
    """

    git(repo, "checkout", "-q", "-b", "t_aaaaaaaa")
    commit_file(repo, "n.txt", "chore: 顺手改点东西（没写卡号）")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(
        ["--repo", str(repo), "--branch", "t_aaaaaaaa"], capsys
    )

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "wt/t_aaaaaaaa" in err
    assert "Traceback" not in err


def test_bare_card_branch_without_wt_prefix_rc2_even_with_wt_spelling(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """同理：显式写 ``--branch wt/t_aaaaaaaa`` 但该分支不存在时也不许静默空跑。"""

    git(repo, "checkout", "-q", "-b", "t_aaaaaaaa")
    commit_file(repo, "n.txt", "chore: 没写卡号")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(
        ["--repo", str(repo), "--branch", "wt/t_aaaaaaaa"], capsys
    )

    assert rc == 2
    assert out == ""
    assert "wt/t_aaaaaaaa" in err
    assert "Traceback" not in err


def test_branch_card_id_resolves_wt_branch_when_it_exists(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """裸卡号在 ``wt/t_xxx`` 存在时正常解析（修完别把正常路径也判死）。"""

    git(repo, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    commit_file(repo, "a.txt", "feat(t_aaaaaaaa): 本卡的活")
    git(repo, "checkout", "-q", "main")

    rc, out, err = run(["--repo", str(repo), "--branch", "t_aaaaaaaa"], capsys)

    assert rc == 0
    assert err == ""
    assert "检查的卡分支: t_aaaaaaaa" in out
