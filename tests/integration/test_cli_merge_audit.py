"""cli merge-audit 的集成测试：在 tmp_path 里造真 git 仓库，验 rc 与关键文案。

覆盖：判定段 rc0/rc1（缺字段、格式非法）、历史段只回显不判定、
基线文件（默认路径 / 缺失 / 非 UTF-8 / 空 / 多行 / 格式非法）、
``--baseline`` 与 ``--baseline-file`` 互斥、``--rev`` / ``--card`` 参数校验、
``--require`` 三种结局、判据①（``merged-by`` == 卡 assignee）的红绿与 rc2、
``PATH`` 里没有 git、stdout 编码表示不了中文。

判据① 用 ``tmp_path`` 里现造的**临时看板库**（``board`` fixture），一律不碰真库。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from team_board import cli

#: 临时看板库的表结构（与真库 tasks 的列一致，判据① 只读 assignee）
_TASKS_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    assignee TEXT,
    status TEXT,
    created_at INTEGER,
    started_at INTEGER,
    completed_at INTEGER
);
"""

#: 临时看板库里的卡 → assignee。``t_99999999`` 故意造出来但**没有任何合并**，
#: 用来区分「卡存在但没合并（rc1 缺认账字段）」与「卡不存在（rc2）」。
BOARD_CARDS = {
    "t_ad5e7ff6": "pm",
    "t_11112222": "pm",
    "t_aaaaaaaa": "pm",
    "t_99999999": "pm",
}


def make_board(path: Path, cards: dict[str, str] | None = None) -> Path:
    """在 ``path`` 造一个只有 ``tasks`` 表的看板库，返回路径。"""

    source = BOARD_CARDS if cards is None else cards
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_TASKS_SCHEMA)
        for card, assignee in source.items():
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status) VALUES (?, ?, ?, ?)",
                (card, card, assignee, "done"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    """判据① 用的临时看板库（默认含 ``BOARD_CARDS`` 四张卡）。"""

    return make_board(tmp_path / "board.db")


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "merge-audit-test",
    "GIT_AUTHOR_EMAIL": "merge-audit@example.com",
    "GIT_COMMITTER_NAME": "merge-audit-test",
    "GIT_COMMITTER_EMAIL": "merge-audit@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}

#: 三字段齐全的合并 message（判定段应当 rc0）
#:
#: 身份仍是 ``pm/run137``：本文件的既有用例锁的是**过渡期**语义（区间内没有任何
#: ``repo`` 合并 → R15 判据② 整段不生效），也用来锁判据①（profile == 卡 assignee）。
#: R15 的红样 / 绿样另见文件末尾 ``test_actor_*``（那里用 ``repo/runN`` 当分界）。
GOOD_MESSAGE = (
    "Merge PR #1\n"
    "\n"
    "merged-by: pm/run137\n"
    "merge-card: t_ad5e7ff6\n"
    "merge-pr: #1"
)

#: 与基线无关的另一个卡号（用于 ``--card`` 过滤与 ``--require`` 没命中）
OTHER_MESSAGE = (
    "Merge PR #2\n"
    "\n"
    "merged-by: pm/run138\n"
    "merge-card: t_11112222\n"
    "merge-pr: #2"
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
    for key in ("merged-by", "merge-card", "merge-pr"):
        assert f"{sha[:7]} 缺字段：{key} 缺失" in out
    assert "结论：发现 3 个问题" in out


def test_judged_segment_red_when_format_invalid(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """三字段都在但值非法（PR 前导零、profile 大写）→ rc1 格式非法。"""

    base = head(repo)
    sha = merge_in(
        repo,
        "Merge PR #7\n\nmerged-by: PM/run7\nmerge-card: t_ad5e7ff6\nmerge-pr: #007",
        branch="feature",
        name="feat.txt",
    )

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 1, (rc, out, err)
    assert err == ""
    assert f'{sha[:7]} 格式非法：merge-pr 的值 "#007" 不合法' in out
    assert f'{sha[:7]} 格式非法：merged-by 的值 "PM/run7" 不合法' in out


def test_field_outside_trailer_block_is_red(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """字段写在正文（不在末尾 trailer 块）→ rc1「字段未落在 trailer 区」。"""

    base = head(repo)
    message = "Merge PR #1\n\nmerge-card: t_ad5e7ff6\n\n后续说明段落不属于 trailer 块"
    sha = merge_in(repo, message, branch="feature", name="feat.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 1, (rc, out, err)
    assert f"{sha[:7]} 字段未落在 trailer 区：merge-card 出现在正文" in out


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
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
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
        [
            "--repo",
            str(repo),
            "--baseline",
            old,
            "--card",
            "t_ad5e7ff6",
            "--db",
            str(board),
        ],
        capsys,
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


# --------------------------------------- --baseline-file：base 侧基线（t_daba51a7）


def test_baseline_file_takes_value_from_ancestor(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--baseline-file`` 给 base 侧（祖先）提交 → rc0，且判定的是区间内的合并。"""

    base = head(repo)
    sha = merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    baseline = tmp_path / "baseline"
    baseline.write_text(base + "\n", encoding="utf-8")

    rc, out, err = run(
        ["--repo", str(repo), "--rev", sha, "--baseline-file", str(baseline)], capsys
    )

    assert rc == 0, (rc, out, err)
    assert err == ""
    assert f"基线: {base}" in out
    assert "判定（1 条）" in out
    assert f"{sha[:7]} t_ad5e7ff6 #1 by=pm/run137" in out
    assert "结论：无问题" in out


def test_rc2_when_explicit_baseline_file_missing(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """显式 ``--baseline-file`` 指向不存在的路径 → rc2（不静默当成「没有基线」）。"""

    missing = tmp_path / "no-such-dir" / "baseline"

    rc, out, err = run(["--repo", str(repo), "--baseline-file", str(missing)], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "基线文件不存在" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("via", ["flag", "file"])
def test_rc2_when_baseline_not_ancestor_of_rev(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    via: str,
) -> None:
    """基线不是被审 ``rev`` 的祖先（方向反了）→ rc2，拒绝「空区间 = 假绿」。

    旧行为：``git log <基线>..<rev>`` 退化成空区间，输出「判定（0 条）/ 结论：无问题」
    并 rc0，与「真的没有合并」不可区分（t_daba51a7 P2-3）。
    """

    base = head(repo)
    merged = merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    args = ["--repo", str(repo), "--rev", base]
    if via == "flag":
        args += ["--baseline", merged]
    else:
        baseline = tmp_path / "baseline"
        baseline.write_text(merged + "\n", encoding="utf-8")
        args += ["--baseline-file", str(baseline)]

    rc, out, err = run(args, capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "基线不是被审 rev 的祖先" in err
    assert merged[:7] in err or merged in err
    assert "Traceback" not in err


def test_baseline_file_wins_over_checked_out_baseline(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """判定只跟 ``--baseline-file`` 走，不跟工作区/检出树里的默认基线文件走。

    这就是 P2-4 的机器证明：被审方把自己那份基线改成「尖端」也缩不小判定区间。
    """

    base = head(repo)
    sha = merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    checked_out = repo / ".team-board" / "merge-audit-baseline"
    checked_out.parent.mkdir(parents=True)
    checked_out.write_text(sha + "\n", encoding="utf-8")  # 检出树里的那份 = 尖端

    # 不给 --baseline-file：读检出树里那份 → 区间空、0 条（旧漏洞的形状）
    rc_tree, out_tree, err_tree = run(["--repo", str(repo)], capsys)
    assert rc_tree == 0, (rc_tree, out_tree, err_tree)
    assert "判定（0 条）" in out_tree

    # 给 --baseline-file 指向 base 侧那份 → 区间恢复，照旧判 1 条
    baseline = tmp_path / "baseline"
    baseline.write_text(base + "\n", encoding="utf-8")
    rc_file, out_file, err_file = run(
        ["--repo", str(repo), "--baseline-file", str(baseline)], capsys
    )

    assert rc_file == 0, (rc_file, out_file, err_file)
    assert f"基线: {base}" in out_file
    assert "判定（1 条）" in out_file
    assert f"{sha[:7]} t_ad5e7ff6 #1 by=pm/run137" in out_file


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
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
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
            "--db",
            str(board),
            "--require",
        ],
        capsys,
    )

    assert rc == 0, (rc, out, err)
    assert "结论：无问题" in out


def test_require_red_when_only_history_hit(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
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
            "--db",
            str(board),
            "--require",
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    assert f"{old[:7]} 缺认账字段（历史，基线前）" in out
    assert "merged-by 无法判定" in out


def test_require_red_when_no_merge_at_all(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
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
            "--db",
            str(board),
            "--require",
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    assert "缺认账字段：历史与区间内都没有该卡的合并提交" in out


# ------------------------------------ 判据①：merged-by 必须等于该卡 assignee

#: ``merged-by`` 的 profile（engineer）与卡 ``t_ad5e7ff6`` 的 assignee（pm）不符
MISMATCH_MESSAGE = (
    "Merge PR #1\n"
    "\n"
    "merged-by: engineer/run9\n"
    "merge-card: t_ad5e7ff6\n"
    "merge-pr: #1"
)


def test_judgement_one_red_when_merged_by_differs(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
) -> None:
    """``merged-by=engineer/run9`` 而卡 assignee=pm → rc1 + 判据① FAIL + 不符问题。"""

    base = head(repo)
    sha = merge_in(repo, MISMATCH_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_ad5e7ff6",
            "--db",
            str(board),
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    assert err == ""
    assert (
        "判据①（merged-by == 卡 assignee）：卡 t_ad5e7ff6 assignee=pm，"
        "merged-by=engineer/run9 → FAIL"
    ) in out
    assert (
        f"{sha[:7]} merged-by 与卡 assignee 不符：merged-by=engineer/run9 assignee=pm"
    ) in out
    assert "结论：发现 1 个问题" in out


def test_judgement_one_green_when_merged_by_matches(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
) -> None:
    """``merged-by=pm/run137`` 与卡 assignee=pm 一致 → rc0 + 判据① PASS。"""

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
            "--db",
            str(board),
        ],
        capsys,
    )

    assert rc == 0, (rc, out, err)
    assert err == ""
    assert (
        "判据①（merged-by == 卡 assignee）：卡 t_ad5e7ff6 assignee=pm，"
        "merged-by=pm/run137 → PASS"
    ) in out
    assert "结论：无问题" in out


def test_judgement_one_absent_without_card(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
) -> None:
    """没给 ``--card`` → 整段判据① 不打印（也不读看板库）。"""

    base = head(repo)
    merge_in(repo, MISMATCH_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(
        ["--repo", str(repo), "--baseline", base, "--db", str(board)], capsys
    )

    assert rc == 0, (rc, out, err)  # trailer 三字段自身合法
    assert "判据①" not in out
    assert "assignee" not in out


def test_judgement_one_not_applied_to_history_segment(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
) -> None:
    """历史段（``source="legacy"``）没有 ``merged-by``，不参与判据① → 不适用。"""

    old = merge_in(
        repo,
        "Merge pull request #3 from alsj213/wt/t_aaaaaaaa",
        branch="old",
        name="old.txt",
    )
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

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
            "--db",
            str(board),
        ],
        capsys,
    )

    assert rc == 0, (rc, out, err)
    assert "区间内无该卡的 trailer 记录 → 不适用" in out
    assert f"{old[:7]} t_aaaaaaaa #3 by=无法判定" in out


def test_judgement_one_failure_also_breaks_require(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
) -> None:
    """判据① 不符的合并不算「零问题的认账合并」→ ``--require`` 也红。"""

    base = head(repo)
    merge_in(repo, MISMATCH_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_ad5e7ff6",
            "--db",
            str(board),
            "--require",
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    assert "merged-by 与卡 assignee 不符" in out
    assert "缺认账字段：历史与区间内都没有该卡的合并提交" in out


def test_rc2_when_card_missing_from_board(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
) -> None:
    """卡不在看板库里 → rc2（判据① 没有 assignee 可比，不静默跳过）。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_99999998",
            "--db",
            str(board),
        ],
        capsys,
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "里没有卡 t_99999998" in err
    assert "Traceback" not in err


def test_rc2_when_board_db_missing(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--db`` 指向不存在的库 → rc2，stderr 说明看板库不可读。"""

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
            "--db",
            str(tmp_path / "nonexistent.db"),
        ],
        capsys,
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "看板库不可读" in err
    assert "Traceback" not in err


def test_rc2_when_board_db_is_not_sqlite(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--db`` 指向非 SQLite 文件 → rc2（不当成「没有 assignee」放过）。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(b"not a sqlite database\n")

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_ad5e7ff6",
            "--db",
            str(bogus),
        ],
        capsys,
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "看板库不可读" in err
    assert "Traceback" not in err


def test_rc2_when_board_db_has_no_tasks_table(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """库合法但没有 ``tasks`` 表 → rc2（读失败要报，不许静默放过判据）。"""

    base = head(repo)
    merge_in(repo, GOOD_MESSAGE, branch="feature", name="feat.txt")
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_ad5e7ff6",
            "--db",
            str(empty),
        ],
        capsys,
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "tasks 表失败" in err


def test_old_uppercase_keys_are_rejected(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
) -> None:
    """大写旧键 ``Merge-By:`` 一律不认 → rc1，三个小写键都报缺字段。"""

    base = head(repo)
    message = (
        "Merge PR #1\n"
        "\n"
        "Merge-By: pm/run137\n"
        "Merge-Card: t_ad5e7ff6\n"
        "Merge-PR: #1"
    )
    sha = merge_in(repo, message, branch="feature", name="feat.txt")

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_ad5e7ff6",
            "--db",
            str(board),
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    for key in ("merged-by", "merge-card", "merge-pr"):
        assert f"{sha[:7]} 缺字段：{key} 缺失" in out
    assert "结论：发现 3 个问题" in out


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


# --------------------- 判据②（R15 合并权只归 repo bot）：切换点豁免与 rc
#
# 本文件上面的夹具保留 ``merged-by: pm/run137``：它们跑在**过渡期**语义下（区间内没有
# 任何 repo 合并 → 该判定整段不生效），用来锁既有行为不被 R15 改动波及。R15 本身的
# 红样 / 绿样 / 过渡期文案用下面这组 ``repo/runN`` 与 ``pm/runN`` 的对照。


def _message(pr: int, card: str, by: str) -> str:
    """一条三字段齐全的合并 message；``by`` 决定它是否合规。"""

    return (
        f"Merge PR #{pr}\n"
        "\n"
        f"merged-by: {by}\n"
        f"merge-card: {card}\n"
        f"merge-pr: #{pr}"
    )


def test_actor_red_after_switch_point(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """切换点之后的非 repo 合并 → rc1，文案给正确写法，并打出切换点 sha。"""

    base = head(repo)
    switch = merge_in(
        repo, _message(4, "t_ad5e7ff6", "repo/run1"), branch="one", name="one.txt"
    )
    bad = merge_in(
        repo, _message(5, "t_11112222", "pm/run9"), branch="two", name="two.txt"
    )

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 1, (rc, out, err)
    assert err == ""
    assert f"合并权切换点（首次 repo 合并）：{switch}" in out
    assert (
        f"{bad[:7]} merged-by 的 profile 不是 repo："
        "merged-by=pm/run9；合并权归 repo bot（R15），正确写法 repo/run<N>"
    ) in out
    assert "结论：发现 1 个问题" in out


def test_actor_green_before_switch_point(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """切换点之前的非 repo 合并不追溯（历史不追溯）→ rc0，切换点照旧渲染。"""

    base = head(repo)
    merge_in(repo, _message(5, "t_11112222", "pm/run9"), branch="one", name="one.txt")
    switch = merge_in(
        repo, _message(4, "t_ad5e7ff6", "repo/run1"), branch="two", name="two.txt"
    )

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 0, (rc, out, err)
    assert err == ""
    assert f"合并权切换点（首次 repo 合并）：{switch}" in out
    assert "合并权归 repo bot" not in out
    assert "结论：无问题" in out


def test_actor_transition_period_is_printed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """区间内没有 repo 合并 = 过渡期：该判定不生效，输出里明写（不静默假绿）。"""

    base = head(repo)
    merge_in(repo, _message(5, "t_11112222", "pm/run9"), branch="one", name="one.txt")

    rc, out, err = run(["--repo", str(repo), "--baseline", base], capsys)

    assert rc == 0, (rc, out, err)
    assert "合并权切换点：过渡期（区间内尚无 repo 合并，repo-only 判定未生效）" in out
    assert "合并权切换点（首次 repo 合并）" not in out


def test_actor_switch_point_does_not_drift_with_card_filter(
    repo: Path, capsys: pytest.CaptureFixture[str], board: Path
) -> None:
    """``--card`` 只过滤记录，不改分界：同一区间两种口径会让豁免范围随参数漂移。"""

    base = head(repo)
    switch = merge_in(
        repo, _message(4, "t_ad5e7ff6", "repo/run1"), branch="one", name="one.txt"
    )
    bad = merge_in(
        repo, _message(5, "t_11112222", "pm/run9"), branch="two", name="two.txt"
    )

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_11112222",
            "--db",
            str(board),
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    assert f"合并权切换点（首次 repo 合并）：{switch}" in out
    assert f"{bad[:7]} merged-by 的 profile 不是 repo" in out
    # 判据① 照旧：该卡 assignee=pm、merged-by=pm/run9 → PASS
    assert "merged-by=pm/run9 → PASS" in out


def test_actor_other_card_not_swallowed_by_card_filter(
    repo: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--card`` 指向一张干净的 repo 卡：别卡切换点之后的非 repo 合并仍判红。

    qa run470 的假绿：判据② 只遍历被 ``--card`` 过滤后的记录，别卡的非 repo 合并
    被吞成 rc0。覆盖面必须是**完整判定段**（切换点之前的不追溯）。
    """

    base = head(repo)
    before = merge_in(
        repo, _message(3, "t_aaaaaaaa", "pm/run3"), branch="zero", name="zero.txt"
    )
    switch = merge_in(
        repo, _message(4, "t_ad5e7ff6", "repo/run1"), branch="one", name="one.txt"
    )
    bad = merge_in(
        repo, _message(5, "t_11112222", "pm/run9"), branch="two", name="two.txt"
    )
    board_path = make_board(tmp_path / "board.db", {"t_ad5e7ff6": "repo"})

    rc, out, err = run(
        [
            "--repo",
            str(repo),
            "--baseline",
            base,
            "--card",
            "t_ad5e7ff6",
            "--db",
            str(board_path),
        ],
        capsys,
    )

    assert rc == 1, (rc, out, err)
    assert err == ""
    assert f"合并权切换点（首次 repo 合并）：{switch}" in out
    # 别卡在切换点之后的那条照样红
    assert f"{bad[:7]} merged-by 的 profile 不是 repo" in out
    # 切换点之前的不追溯
    assert f"{before[:7]} merged-by 的 profile 不是 repo" not in out
    assert "结论：发现 1 个问题" in out
    # 记录清单照旧只留该卡（只改判定覆盖面，不改 report.records 的过滤）
    assert "判定（1 条）" in out

