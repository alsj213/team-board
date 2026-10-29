"""cli merge-audit 的集成测试：在 tmp_path 里造真 git 仓库，验 rc 与关键文案。

覆盖：判定段 rc0/rc1（缺字段、格式非法）、历史段只回显不判定、
基线文件（默认路径 / 缺失 / 非 UTF-8 / 空 / 多行 / 格式非法）、
``--baseline`` 与 ``--baseline-file`` 互斥、``--rev`` / ``--card`` 参数校验、
``--require`` 三种结局、``PATH`` 里没有 git、stdout 编码表示不了中文。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from team_board import cli

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "merge-audit-test",
    "GIT_AUTHOR_EMAIL": "merge-audit@example.com",
    "GIT_COMMITTER_NAME": "merge-audit-test",
    "GIT_COMMITTER_EMAIL": "merge-audit@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}

#: 三字段齐全的合并 message（判定段应当 rc0）
GOOD_MESSAGE = (
    "Merge PR #1\n"
    "\n"
    "Merge-By: pm/run137\n"
    "Merge-Card: t_ad5e7ff6\n"
    "Merge-PR: #1"
)

#: 与基线无关的另一个卡号（用于 ``--card`` 过滤与 ``--require`` 没命中）
OTHER_MESSAGE = (
    "Merge PR #2\n"
    "\n"
    "Merge-By: pm/run138\n"
    "Merge-Card: t_11112222\n"
    "Merge-PR: #2"
)


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
    """写一个文件并提交。"""

    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def head(repo: Path) -> str:
    """当前 HEAD 的 40 位 sha。"""

    return git(repo, "rev-parse", "HEAD").strip()


def merge_in(repo: Path, message: str, *, branch: str, name: str) -> str:
    """造一条 ``--no-ff`` 合并提交（两个 parent），返回合并提交 sha。"""

    git(repo, "checkout", "-q", "-b", branch)
    commit_file(repo, name, f"feat: {branch} 上的提交")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", branch, "-m", message)
    return head(repo)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """一个 main 上只有一条初始提交的仓。"""

    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit_file(path, "README.md", "chore: init", "hello\n")
    return path


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """跑 ``cli.main(["merge-audit", ...])``，返回 ``(rc, stdout, stderr)``。"""

    rc = cli.main(["merge-audit", *args])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def non_repo_dir(tmp_path: Path) -> Path:
    """一个确定「不在任何 git 仓库里」的目录（``tmp_path`` 可能整个落在某个仓库里）。"""

    for root in (tmp_path, Path(tempfile.gettempdir()), Path("/tmp")):
        try:
            candidate = root / "t_11ef8cf3-nr"
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:  # 该 root 不可写 / 不存在
            continue
        proc = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            env=_GIT_ENV,
            check=False,
        )
        if proc.returncode != 0:
            return candidate
    raise AssertionError("找不到不在任何 git 仓库里的目录，无法验证「非仓库 → 报错」")


# --------------------------------------------------- 判定段：绿（三字段齐全）


def test_judged_segment_green(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """区间内的合并提交带齐三字段 → rc0，输出回显卡号/PR/执行者。"""

    base = head(repo)
    sha = merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 0, (rc, out, err)
    assert err == ""
    assert "判定（1 条）" in out
    assert f"{sha[:7]} t_ad5e7ff6 #1 by=pm/run137" in out
    assert "结论：无问题" in out
    assert f"基线: {base}" in out


# ------------------------------------------------------ 判定段：红（缺字段）


def test_judged_segment_red_when_trailers_missing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有 trailer 块的 merge commit → rc1，三条缺字段都带真 sha。"""

    base = head(repo)
    sha = merge_in(repo, "Merge PR #1 无认账字段", branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 1, (rc, out, err)
    assert err == ""
    assert "问题（3 处）" in out
    for key in ("Merge-By", "Merge-Card", "Merge-PR"):
        assert f"{sha[:7]} 缺字段：{key} 缺失" in out
    assert "结论：发现 3 个问题" in out


def test_judged_segment_red_when_format_invalid(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """三字段都在但值非法（PR 前导零、profile 大写）→ rc1 格式非法。"""

    base = head(repo)
    sha = merge_in(
        repo,
        "Merge PR #7\n\nMerge-By: PM/run7\nMerge-Card: t_ad5e7ff6\nMerge-PR: #007",
        branch="feature",
        name="feat.txt",
    )

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 1, (rc, out, err)
    assert err == ""
    assert f'{sha[:7]} 格式非法：Merge-PR 的值 "#007" 不合法' in out
    assert f'{sha[:7]} 格式非法：Merge-By 的值 "PM/run7" 不合法' in out


def test_field_outside_trailer_block_is_red(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """字段写在正文（不在末尾 trailer 块）→ rc1「字段未落在 trailer 区」。"""

    base = head(repo)
    message = "Merge PR #1\n\nMerge-Card: t_ad5e7ff6\n\n后续说明段落不属于 trailer 块"
    sha = merge_in(repo, message, branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 1, (rc, out, err)
    assert f"{sha[:7]} 字段未落在 trailer 区：Merge-Card 出现在正文" in out


# ------------------------------------------- 历史段：只回显，不判定，rc 仍 0


def test_history_before_baseline_is_shown_and_green(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """基线及更早的合并进「历史（未判定）」段，不参与判定 → rc0。"""

    old = merge_in(
        repo,
        "Merge pull request #3 from alsj213/wt/t_aaaaaaaa",
        branch="old",
        name="old.txt",
    )
    new = merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", old], capsys)

    assert rc == 0, (rc, out, err)
    assert err == ""
    assert "判定（1 条）" in out
    assert f"{new[:7]} t_ad5e7ff6 #1 by=pm/run137" in out
    assert "历史（未判定，1 条）" in out
    assert f"{old[:7]} t_aaaaaaaa #3 by=无法判定" in out
    assert "问题（0 处）" in out


def test_history_keeps_unrecognized_merge(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """认不出卡号的历史合并（``Merge branch ...``）也要保留，不许静默丢条目。"""

    old = merge_in(repo, "Merge branch 'old' into main", branch="old", name="old.txt")
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", old], capsys)

    assert rc == 0, (rc, out, err)
    assert "历史（未判定，1 条）" in out
    assert f"{old[:7]} - - by=无法判定" in out


# ------------------------------------------------------------ --card 过滤


def test_card_filters_both_segments(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--card`` 同时过滤判定段与历史段，别卡的记录不出现。"""

    old = merge_in(
        repo,
        "Merge pull request #2 from alsj213/wt/t_11112222",
        branch="old",
        name="old.txt",
    )
    keep = merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    drop = merge_in(repo, OTHER_MESSAGE, branch="other", name="other.txt")

    rc, out, err = run(
        ["--repo", str(repo), "--baseline", old, "--card", "t_ad5e7ff6"], capsys
    )

    assert rc == 0, (rc, out, err)
    assert f"{keep[:7]} t_ad5e7ff6 #1" in out
    assert f"{drop[:7]} " not in out  # 别卡（同区间）被过滤掉
    assert "历史（未判定，0 条）" in out  # 历史里的 t_11112222 也被过滤掉
    assert "t_11112222" not in out


# ------------------------------------------------ 基线来源 / 基线文件校验（rc2）


def test_rc2_when_default_baseline_file_missing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``<repo>/.team-board/merge-audit-baseline`` 不存在 → rc2（不静默当无基线）。"""

    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "基线文件不存在" in err
    assert "Traceback" not in err


def test_default_baseline_file_is_used(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """默认基线文件存在且合法 → 不传 ``--baseline`` 也能跑，rc0。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    baseline_file = repo / ".team-board" / "merge-audit-baseline"
    baseline_file.parent.mkdir(parents=True)
    baseline_file.write_text(base + "\n", encoding="utf-8")

    rc, out, err = run(["--repo", str(repo)], capsys)

    assert rc == 0, (rc, out, err)
    assert f"基线: {base}" in out


@pytest.mark.parametrize(
    ("content", "keyword"),
    [
        (b"", "空文件"),
        (b"zzzzzzz\n", "7–40 位小写十六进制"),
        (b"abc\ndef\n", "含多行内容"),
        (b"bad_\xff\xfe.txt\n", "不是 UTF-8 文本"),
    ],
)
def test_rc2_when_baseline_file_malformed(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    content: bytes,
    keyword: str,
) -> None:
    """基线文件空 / 非 sha / 多行 / 非 UTF-8 → rc2，stdout 空、无 traceback。"""

    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    baseline = tmp_path / "baseline.txt"
    baseline.write_bytes(content)

    rc, out, err = run(
        ["--repo", str(repo), "--baseline-file", str(baseline)], capsys
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert keyword in err
    assert "Traceback" not in err


def test_rc2_when_both_baseline_flags_given(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--baseline`` 与 ``--baseline-file`` 同给 → rc2（基线来源必须唯一）。"""

    base = head(repo)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(base, encoding="utf-8")

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--baseline-file",
            str(baseline),
        ],
        capsys,
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "只能给一个" in err


def test_rc2_when_baseline_unresolvable(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--baseline nosuchrev`` → rc2。"""

    rc, out, err = run(["--repo", str(repo), "--baseline", "nosuchrev"], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "基线无法解析为提交" in err


# --------------------------------------------------------- --rev / --card 校验


@pytest.mark.parametrize("rev", ["nosuchrev", "", "HEAD^{tree}"])
def test_rc2_when_rev_invalid(
    repo: Path, capsys: pytest.CaptureFixture[str], rev: str
) -> None:
    """``--rev`` 解析不成提交（含空串、非 commit 对象）→ rc2。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", base, "--rev", rev], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "区间终点无法解析为提交" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("card", ["", "t_ZZZZZZZZ", "t_ad5e7ff6x", "ad5e7ff6"])
def test_rc2_when_card_invalid(
    repo: Path, capsys: pytest.CaptureFixture[str], card: str
) -> None:
    """``--card`` 形态非法（空串 / 大写 / 多字符 / 缺前缀）→ rc2。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(
        ["--repo", str(repo), "--baseline", base, "--card", card], capsys
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "卡号非法" in err


def test_rc2_when_require_without_card(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--require`` 必须与 ``--card`` 同用，否则 rc2。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", base, "--require"], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "--require 必须与 --card 同用" in err


# ------------------------------------------------------------- --require 三结局


def test_require_green_when_clean_trailer_in_range(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """区间内有该卡零问题的认账合并 → rc0。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_ad5e7ff6",
            "--require",
        ],
        capsys,
    )

    assert rc == 0, (rc, out, err)
    assert "结论：无问题" in out


def test_require_red_when_only_history_hit(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """只有历史段命中（基线前的合并）→ rc1「缺认账字段（历史，基线前）」。"""

    old = merge_in(
        repo,
        "Merge pull request #3 from alsj213/wt/t_aaaaaaaa",
        branch="old",
        name="old.txt",
    )

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            old,
            "--rev",
            "main",
            "--card",
            "t_aaaaaaaa",
            "--require",
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    assert f"{old[:7]} 缺认账字段（历史，基线前）" in out
    assert "Merge-By 无法判定" in out


def test_require_red_when_no_merge_at_all(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """历史与区间内都没有该卡的合并 → rc1「缺认账字段」。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_99999999",
            "--require",
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    assert "缺认账字段：历史与区间内都没有该卡的合并提交" in out


# -------------------------------------------------------- 仓库 / 环境级错误


@pytest.mark.parametrize("kind", ["missing", "not_a_repo", "empty"])
def test_rc2_when_repo_invalid(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path, kind: str
) -> None:
    """仓库路径不存在 / 不是 git 工作树 / 空串 → rc2。"""

    target = {
        "missing": str(tmp_path / "nope"),
        "not_a_repo": str(tmp_path),
        "empty": "",
    }[kind]

    rc, out, err = run(["--repo", target, "--baseline", head(repo)], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "Traceback" not in err


def test_rc2_when_git_missing(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PATH`` 里没有 git → rc2，不许 ``FileNotFoundError`` 冒出来。"""

    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")

    rc, out, err = run(["--repo", str(repo), "--baseline", "HEAD"], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "git" in err
    assert "Traceback" not in err


def test_rc2_when_inherited_git_dir_points_to_another_repo(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GIT_DIR`` 指向别处 + ``--repo`` 指向非仓库 → rc2、stdout 空、无 traceback。

    修前：``git -C <非仓库> rev-parse --show-toplevel`` 因继承 ``GIT_DIR`` 而成功，
    报头写 ``--repo``，实际审的是 ``GIT_DIR`` 那个仓 → rc0 假绿（卡 t_11ef8cf3）。
    """

    not_a_repo = non_repo_dir(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))

    rc, out, err = run(["--repo", str(not_a_repo), "--baseline", head(repo)], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: 不是 git 工作树")
    assert str(not_a_repo) in err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_rc2_when_git_output_not_utf8(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``i18n.logOutputEncoding=GBK`` + 中文 message → rc2；去掉该配置后 rc0。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    git(repo, "config", "i18n.logOutputEncoding", "GBK")
    merge_in(repo, "Merge PR #9 中文标题", branch="gbk", name="gbk.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "UTF-8" in err
    assert "Traceback" not in err

    git(repo, "config", "--unset", "i18n.logOutputEncoding")
    rc_ok, out_ok, err_ok = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc_ok == 1, (rc_ok, out_ok, err_ok)  # 第 9 条缺字段 → rc1，不再是 rc2
    assert err_ok == ""


def test_ascii_stdout_returns_two(repo: Path) -> None:
    """stdout 编码表示不了中文 → rc2（真子进程跑，环境变量才有意义）。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    src = Path(__file__).resolve().parents[2] / "src"
    env = {
        **os.environ,
        "PYTHONPATH": str(src),
        "PYTHONIOENCODING": "ascii",
        "LC_ALL": "C",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "team_board.cli",
            "merge-audit",
            "--repo",
            str(repo),
            "--baseline",
            base,
        ],
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == b""
    assert proc.stderr.startswith("错误: ".encode())
    assert b"Traceback" not in proc.stderr
