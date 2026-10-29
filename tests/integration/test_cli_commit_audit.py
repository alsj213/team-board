"""commit-audit 的集成测试：纯逻辑（身份表解析 / 逐提交判定 / 渲染）与 CLI 用例。

覆盖：``email_localpart``、``load_bots`` 的正常与六类坏输入、判据② 红/绿（含
assignee 白名单、恒允许的 engineer / qa）、判据③ 红/绿（committer 不是本 bot、
committer 与 author 不一致、两条同时出现）、合并提交不参与、空区间 rc0、
``origin/<main>`` 回退到 ``refs/heads/<main>``、看板库只读取 assignee（含五类坏库）、
CLI 的 rc0 / rc1 / rc2 边界与 stdout 编码失败。

真 git 仓库都在 ``tmp_path`` 里造，不碰主仓，也不依赖本机的 git 身份配置。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from team_board import cli, commits
from team_board.model import DEFAULT_DB

#: 本机 git 全局/系统配置一律置空：测试造的仓库与调用者的身份配置无关，
#: 提交时间也固定，避免"同一份代码在不同机器上行为不同"。
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test-bot",
    "GIT_AUTHOR_EMAIL": "test-bot@example.com",
    "GIT_COMMITTER_NAME": "test-bot",
    "GIT_COMMITTER_EMAIL": "test-bot@example.com",
    "GIT_AUTHOR_DATE": "2026-10-28T12:00:00+08:00",
    "GIT_COMMITTER_DATE": "2026-10-28T12:00:00+08:00",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "TZ": "Asia/Shanghai",
}

#: 身份表：与仓库里那份同构（三行注释 + 六个 bot）
BOTS_TEXT = (
    "# bot 身份表：profile<TAB>name<TAB>email\n"
    "# 用途：commit-audit / commit-identity CI job 的唯一身份源。\n"
    "# 注意：GitHub 页面恒显示 alsj213。\n"
    "pm\tpm\tpm@team-board.local\n"
    "engineer\tengineer\tengineer@team-board.local\n"
    "qa\tqa\tqa@team-board.local\n"
    "ops\tops\tops@team-board.local\n"
    "hatch\thatch\thatch@team-board.local\n"
    "chief\tchief\tchief@team-board.local\n"
)

ENGINEER = ("engineer", "engineer@team-board.local")
PM = ("pm", "pm@team-board.local")
QA = ("qa", "qa@team-board.local")
OUTSIDER = ("outsider", "outsider@example.com")
ALSOUT = ("alsj213", "alsj213@example.com")

#: 本卡（看板库里 assignee=engineer）
CARD = "t_51f5fad0"
#: 看板库里 assignee=pm 的卡
CARD_PM = "t_aaaaaaaa"
#: 看板库里不存在的卡
CARD_MISSING = "t_99999999"


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


def commit_as(
    repo: Path,
    message: str,
    *,
    author: tuple[str, str],
    committer: tuple[str, str] | None = None,
) -> str:
    """用指定 author / committer 造一条空提交，返回 40 位 sha。"""

    committer = committer or author
    env = {
        **_GIT_ENV,
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": committer[0],
        "GIT_COMMITTER_EMAIL": committer[1],
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "--no-verify", "-q", "-m", message],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return git(repo, "rev-parse", "HEAD").strip()


def head(repo: Path) -> str:
    """当前 HEAD 的 40 位 sha。"""

    return git(repo, "rev-parse", "HEAD").strip()


def make_board(path: Path, cards: dict[str, str | None]) -> Path:
    """造一张最小看板库（只有 tasks 表），返回路径。"""

    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT)")
        conn.executemany(
            "INSERT INTO tasks (id, assignee) VALUES (?, ?)",
            list(cards.items()),
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """main 上有一条初始提交（pm 身份）的仓；``.team-board/bots.tsv`` 已就位，
    ``refs/remotes/origin/main`` 指向这条初始提交（区间从此开始为空）。"""

    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / ".team-board").mkdir()
    (path / ".team-board" / "bots.tsv").write_text(BOTS_TEXT, encoding="utf-8")
    git(path, "add", "-A")
    commit_as(path, "chore: init", author=PM)
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return path


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    """看板库：本卡 assignee=engineer，另一张卡 assignee=pm。"""

    return make_board(
        tmp_path / "board.db",
        {CARD: "engineer", CARD_PM: "pm", "t_bbbbbbbb": None},
    )


def run(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    """跑 ``cli.main(["commit-audit", ...])``，返回 ``(rc, stdout, stderr)``。"""

    rc = cli.main(["commit-audit", *args])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def audit(
    repo: Path,
    board: Path,
    *,
    card: str = CARD,
    rev: str = "HEAD",
    main_branch: str = "main",
) -> commits.CommitReport:
    """跑 ``commits.audit_commits``，默认拿本卡 + 临时看板库。"""

    return commits.audit_commits(
        repo, card=card, rev=rev, main_branch=main_branch, db_path=board
    )


# --------------------------------------------------------------- email_localpart


def test_email_localpart() -> None:
    """``@`` 之前那段；没有 ``@`` / 空串原样返回（匹配不上任何身份）。"""

    assert commits.email_localpart("engineer@team-board.local") == "engineer"
    assert commits.email_localpart("  pm@team-board.local ") == "pm"
    assert commits.email_localpart("a@b@c") == "a@b"
    assert commits.email_localpart("engineer") == "engineer"
    assert commits.email_localpart("") == ""


# --------------------------------------------------------------------- load_bots


def test_load_bots_reads_six_rows_and_skips_comments(repo: Path) -> None:
    """注释行与空行跳过，六个 profile 按出现顺序返回。"""

    bots = commits.load_bots(repo / ".team-board" / "bots.tsv")

    assert [bot.profile for bot in bots] == [
        "pm",
        "engineer",
        "qa",
        "ops",
        "hatch",
        "chief",
    ]
    assert bots[1].name == "engineer"
    assert bots[1].email == "engineer@team-board.local"


def test_load_bots_rejects_missing_file(tmp_path: Path) -> None:
    """缺文件 → CommitAuditError（不静默当成空表）。"""

    with pytest.raises(commits.CommitAuditError, match="身份表不存在"):
        commits.load_bots(tmp_path / "nope.tsv")


def test_load_bots_rejects_wrong_column_count(tmp_path: Path) -> None:
    """某行不是三列制表符分隔 → CommitAuditError 带行号。"""

    path = tmp_path / "bots.tsv"
    path.write_text("pm pm pm@team-board.local\n", encoding="utf-8")

    with pytest.raises(commits.CommitAuditError, match="第 1 行格式非法"):
        commits.load_bots(path)


def test_load_bots_rejects_duplicate_profile(tmp_path: Path) -> None:
    """profile 重复 → CommitAuditError 同时点名两行。"""

    path = tmp_path / "bots.tsv"
    path.write_text(
        "pm\tpm\tpm@team-board.local\npm\tpm2\tpm2@team-board.local\n",
        encoding="utf-8",
    )

    with pytest.raises(commits.CommitAuditError, match="profile 重复"):
        commits.load_bots(path)


def test_load_bots_rejects_empty_table(tmp_path: Path) -> None:
    """只有注释/空行 → CommitAuditError（读成空表等于豁免所有 committer）。"""

    path = tmp_path / "bots.tsv"
    path.write_text("# 只有注释\n\n", encoding="utf-8")

    with pytest.raises(commits.CommitAuditError, match="没有任何有效行"):
        commits.load_bots(path)


def test_load_bots_rejects_non_utf8(tmp_path: Path) -> None:
    """不是 UTF-8 文本 → CommitAuditError。"""

    path = tmp_path / "bots.tsv"
    path.write_bytes(b"pm\tpm\tpm@team-board.local\n\xff\xfe\n")

    with pytest.raises(commits.CommitAuditError, match="不是 UTF-8"):
        commits.load_bots(path)


def test_load_bots_rejects_empty_field(tmp_path: Path) -> None:
    """有字段为空（profile 空 / email 空）→ CommitAuditError。"""

    path = tmp_path / "bots.tsv"
    path.write_text("\tpm\tpm@team-board.local\n", encoding="utf-8")

    with pytest.raises(commits.CommitAuditError, match="有空字段"):
        commits.load_bots(path)


# ------------------------------------------------------------ card_assignee（看板）


def test_card_assignee_reads_board(repo: Path, board: Path) -> None:
    """读得到 assignee，且库文件字节在调用前后一致（只读，不写库）。"""

    before = board.read_bytes()

    assert commits.card_assignee(board, CARD) == "engineer"
    assert commits.card_assignee(board, CARD_PM) == "pm"
    assert board.read_bytes() == before


def test_card_assignee_missing_card(board: Path) -> None:
    """卡不存在 → CommitAuditError（不静默跳过判据②）。"""

    with pytest.raises(commits.CommitAuditError, match="没有卡 t_99999999"):
        commits.card_assignee(board, CARD_MISSING)


def test_card_assignee_empty_assignee(board: Path) -> None:
    """assignee 为 NULL → CommitAuditError（白名单不能退化成空集）。"""

    with pytest.raises(commits.CommitAuditError, match="assignee 为空"):
        commits.card_assignee(board, "t_bbbbbbbb")


def test_card_assignee_missing_db(tmp_path: Path) -> None:
    """库不存在 → CommitAuditError，且不会把文件创建出来。"""

    path = tmp_path / "nope.db"

    with pytest.raises(commits.CommitAuditError, match="数据源不存在"):
        commits.card_assignee(path, CARD)
    assert not path.exists()


def test_card_assignee_not_sqlite(tmp_path: Path) -> None:
    """不是 SQLite 库 → CommitAuditError。"""

    path = tmp_path / "board.db"
    path.write_text("not a database\n", encoding="utf-8")

    with pytest.raises(commits.CommitAuditError, match=r"读不了|不可读"):
        commits.card_assignee(path, CARD)


def test_card_assignee_without_tasks_table(tmp_path: Path) -> None:
    """库是合法 SQLite 但没有 tasks 表 → CommitAuditError。"""

    path = tmp_path / "board.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE other (id TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(commits.CommitAuditError, match=r"读不了 tasks\.assignee"):
        commits.card_assignee(path, CARD)


# ------------------------------------------------------- 判据②③：绿（单条提交）


def test_green_engineer_on_engineer_card(repo: Path, board: Path) -> None:
    """author=committer=engineer，卡 assignee=engineer → 零问题。"""

    sha = commit_as(repo, "feat: 一条干净提交", author=ENGINEER)

    report = audit(repo, board)

    assert report.ok is True
    assert report.problems == ()
    assert report.assignee == "engineer"
    assert [c.sha for c in report.commits] == [sha]
    assert report.commits[0].short == sha[:7]
    assert report.commits[0].author_name == "engineer"
    assert report.commits[0].committer_email == "engineer@team-board.local"


def test_green_pm_author_on_pm_card(repo: Path, board: Path) -> None:
    """卡 assignee=pm 时 author=pm 也在白名单里。"""

    commit_as(repo, "docs: pm 自己写文档", author=PM)

    report = audit(repo, board, card=CARD_PM)

    assert report.ok is True
    assert report.assignee == "pm"


def test_green_qa_author_on_any_card(repo: Path, board: Path) -> None:
    """qa 恒在白名单里（判据② 的 ``{assignee, engineer, qa}``）。"""

    commit_as(repo, "test: qa 补一条", author=QA)

    report = audit(repo, board)

    assert report.ok is True


def test_green_engineer_on_pm_card(repo: Path, board: Path) -> None:
    """engineer 在 pm 的卡上也合法（判据② 恒允许 engineer）。"""

    commit_as(repo, "fix: engineer 在 pm 卡上改", author=ENGINEER)

    report = audit(repo, board, card=CARD_PM)

    assert report.ok is True


# ------------------------------------------------------- 判据②③：红（单条提交）


def test_red_author_not_in_whitelist(repo: Path, board: Path) -> None:
    """author=outsider → ``author 不在白名单`` + ``身份表里没有该身份``，且不报不一致。"""

    sha = commit_as(repo, "feat: 假 author", author=OUTSIDER)

    report = audit(repo, board)

    assert report.ok is False
    kinds = [problem.kind for problem in report.problems]
    assert kinds == [
        commits.KIND_AUTHOR_NOT_ALLOWED,
        commits.KIND_IDENTITY_UNKNOWN,
        commits.KIND_COMMITTER_NOT_BOT,
    ]
    detail = report.problems[0].detail
    assert detail == "author=outsider <outsider@example.com>，允许={engineer, qa}"
    assert all(problem.sha == sha for problem in report.problems)


def test_red_author_whitelist_shows_assignee_first(repo: Path, board: Path) -> None:
    """白名单按「assignee 在前」渲染，重复的不重复列。"""

    commit_as(repo, "feat: 假 author", author=("outsider", "outsider@example.com"))

    report = audit(repo, board, card=CARD_PM)

    assert report.problems[0].detail.endswith("允许={pm, engineer, qa}")


def test_red_committer_not_bot_and_mismatch(repo: Path, board: Path) -> None:
    """committer=alsj213（不是本 bot）且与 author 不同 → 两条问题同时出现。"""

    commit_as(repo, "feat: 假 committer", author=ENGINEER, committer=ALSOUT)

    report = audit(repo, board)

    kinds = [problem.kind for problem in report.problems]
    assert kinds == [
        commits.KIND_COMMITTER_NOT_BOT,
        commits.KIND_COMMITTER_AUTHOR_MISMATCH,
    ]
    assert "alsj213@example.com" in report.problems[0].detail
    assert report.problems[1].detail == "author=engineer，committer=alsj213"


def test_red_committer_is_other_bot(repo: Path, board: Path) -> None:
    """committer=pm（是 bot）但与 author=engineer 不同 → 只报不一致。"""

    commit_as(repo, "feat: 借了别人的身份提交", author=ENGINEER, committer=PM)

    report = audit(repo, board)

    kinds = [problem.kind for problem in report.problems]
    assert kinds == [commits.KIND_COMMITTER_AUTHOR_MISMATCH]
    assert report.problems[0].detail == "author=engineer，committer=pm"


# ------------------------------------------------------------ 区间与合并提交口径


def test_merge_commits_are_ignored(repo: Path, board: Path) -> None:
    """``--no-merges``：分支上的提交计入，合并提交本身不计入。"""

    git(repo, "checkout", "-q", "-b", "side")
    side_sha = commit_as(repo, "feat: side 上的提交", author=ENGINEER)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "side", "-m", "Merge branch 'side'")
    merge_sha = head(repo)

    report = audit(repo, board)

    shas = [c.sha for c in report.commits]
    assert shas == [side_sha]
    assert merge_sha not in shas
    assert report.ok is True


def test_counts_multiple_commits(repo: Path, board: Path) -> None:
    """区间内多条提交逐条判：一条坏就整体 rc1（``ok`` 为 False）。"""

    good = commit_as(repo, "feat: 干净提交", author=ENGINEER)
    bad = commit_as(repo, "feat: 假 committer", author=ENGINEER, committer=ALSOUT)

    report = audit(repo, board)

    assert len(report.commits) == 2
    assert [c.sha for c in report.commits] == [bad, good]  # git log 新到旧
    assert report.ok is False
    assert {problem.sha for problem in report.problems} == {bad}


def test_empty_range_is_ok(repo: Path, board: Path) -> None:
    """``--rev origin/main`` → 区间为空、零问题、输出一行「区间内无提交」。"""

    report = audit(repo, board, rev="origin/main")

    assert report.commits == ()
    assert report.problems == ()
    assert report.ok is True
    assert commits.render_report(report) == "区间内无提交"


def test_base_falls_back_to_local_main(repo: Path, board: Path) -> None:
    """``origin/main`` 不存在时退回 ``refs/heads/main``（本地没 fetch 也能跑）。"""

    git(repo, "checkout", "-q", "-b", "wt/t_51f5fad0")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    sha = commit_as(repo, "feat: 没有 origin 也要判", author=ENGINEER)

    report = audit(repo, board)

    assert [c.sha for c in report.commits] == [sha]
    assert report.ok is True


def test_custom_main_branch_name(repo: Path, board: Path) -> None:
    """``main_branch="main"`` 之外的写法会找不到主分支 → CommitAuditError。"""

    commit_as(repo, "feat: 一条提交", author=ENGINEER)

    with pytest.raises(commits.CommitAuditError, match="找不到主分支"):
        audit(repo, board, main_branch="trunk")


def test_explicit_bots_path(repo: Path, board: Path, tmp_path: Path) -> None:
    """``bots_path`` 指到另一张表时以它为准（repo 里那份不被读）。"""

    other = tmp_path / "other-bots.tsv"
    other.write_text(
        "engineer\tengineer\tengineer@team-board.local\n", encoding="utf-8"
    )
    commit_as(repo, "feat: 只登记 engineer", author=ENGINEER)

    report = commits.audit_commits(repo, card=CARD, db_path=board, bots_path=other)

    assert report.ok is True


def test_explicit_bots_path_flags_unregistered_bot(
    repo: Path, board: Path, tmp_path: Path
) -> None:
    """表里没有 pm 时，pm 的提交被判「committer 不是本 bot」+「身份表里没有该身份」。"""

    other = tmp_path / "other-bots.tsv"
    other.write_text(
        "engineer\tengineer\tengineer@team-board.local\n", encoding="utf-8"
    )
    commit_as(repo, "docs: pm 提交", author=PM)

    report = commits.audit_commits(repo, card=CARD_PM, db_path=board, bots_path=other)

    kinds = [problem.kind for problem in report.problems]
    assert kinds == [
        commits.KIND_IDENTITY_UNKNOWN,
        commits.KIND_COMMITTER_NOT_BOT,
    ]


# ------------------------------------------------------------------ render_report


def test_render_report_structure(repo: Path, board: Path) -> None:
    """报告分段：标题 / 仓库 / 卡 / 区间 / 提交 / 问题 / 结论，末尾不带退出码。"""

    sha = commit_as(repo, "feat: 干净提交", author=ENGINEER)

    text = commits.render_report(audit(repo, board))
    lines = text.splitlines()

    assert lines[0] == "提交身份稽核（author 白名单 + committer 是本 bot）"
    assert lines[1] == f"仓库: {repo.resolve()}"
    assert lines[2] == f"卡: {CARD} assignee=engineer"
    assert lines[3] == "区间: origin/main..HEAD"
    assert lines[4] == "提交（1 条）:"
    assert lines[5] == f'  {sha[:7]} author=engineer committer=engineer "feat: 干净提交"'
    assert lines[6] == "问题（0 处）:"
    assert lines[7] == "结论：无问题"
    assert len(lines) == 8
    assert "rc=" not in text
    assert "退出码" not in text


def test_render_report_lists_problems(repo: Path, board: Path) -> None:
    """有问题时逐条编号，结论写条数。"""

    sha = commit_as(repo, "feat: 假 committer", author=ENGINEER, committer=ALSOUT)

    text = commits.render_report(audit(repo, board))

    assert "问题（2 处）:" in text
    assert f"  1. {sha[:7]} committer 不是本 bot：" in text
    assert f"  2. {sha[:7]} committer 与 author 不一致：" in text
    assert text.endswith("结论：发现 2 个问题")


# ------------------------------------------------------------------------- CLI


def test_cli_green(repo: Path, board: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """rc0 + 报告写 stdout，stderr 空。"""

    sha = commit_as(repo, "feat: 干净提交", author=ENGINEER)

    rc, out, err = run(
        ["--repo", str(repo), "--card", CARD, "--db", str(board)], capsys
    )

    assert rc == 0
    assert err == ""
    assert "结论：无问题" in out
    assert f"{sha[:7]} author=engineer committer=engineer" in out


def test_cli_red_pseudo_committer(
    repo: Path, board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rc1：假 committer（判据③ 红），stdout 里有那一行问题。"""

    commit_as(repo, "feat: 假 committer", author=ENGINEER, committer=ALSOUT)

    rc, out, err = run(
        ["--repo", str(repo), "--card", CARD, "--db", str(board)], capsys
    )

    assert rc == 1
    assert err == ""
    assert "committer 不是本 bot" in out
    assert "结论：发现 2 个问题" in out


def test_cli_red_pseudo_author(
    repo: Path, board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rc1：假 author（判据② 红）。"""

    commit_as(repo, "feat: 假 author", author=OUTSIDER)

    rc, out, err = run(
        ["--repo", str(repo), "--card", CARD, "--db", str(board)], capsys
    )

    assert rc == 1
    assert err == ""
    assert "author 不在白名单" in out


def test_cli_empty_range(repo: Path, board: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """rc0 + 只输出一行「区间内无提交」。"""

    rc, out, err = run(
        ["--repo", str(repo), "--card", CARD, "--rev", "origin/main", "--db", str(board)],
        capsys,
    )

    assert rc == 0
    assert out == "区间内无提交\n"
    assert err == ""


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--card", ""], "卡号非法"),
        (["--card", "t_ZZZZZZZZ"], "卡号非法"),
        (["--card", "ad5e7ff6"], "卡号非法"),
        (["--card", CARD, "--rev", ""], "区间终点为空"),
        (["--card", CARD, "--rev", "nosuchrev"], "区间终点无法解析为提交"),
        (["--card", CARD, "--bots", "/nonexistent/bots.tsv"], "身份表不存在"),
        (["--card", CARD, "--db", "/nonexistent/board.db"], "数据源不存在"),
        (["--card", CARD_MISSING], "没有卡 t_99999999"),
        (["--card", CARD, "--main-branch", "trunk"], "找不到主分支"),
    ],
)
def test_cli_rc2_boundaries(
    repo: Path,
    board: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    needle: str,
) -> None:
    """rc2：stdout 空、stderr 以「错误: 」开头、文案指向具体原因。"""

    rc, out, err = run(["--repo", str(repo), "--db", str(board), *args], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: "), err
    assert needle in err


def test_cli_rc2_when_repo_is_not_git(
    tmp_path: Path, board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--repo`` 不是 git 仓库 → rc2。

    目录用 ``/tmp`` 下的临时目录：``tmp_path`` 落在 ``/home/liu`` 里，而本机
    ``/home/liu`` 本身是个 git 仓（dotfiles），嵌套目录会被 ``rev-parse
    --show-toplevel`` 认成那个外层仓 —— 那样测的就不是「不是仓库」这条分支了。
    """

    plain = Path(tempfile.mkdtemp(prefix="tb-not-a-repo-", dir="/tmp"))
    try:
        rc, out, err = run(
            ["--repo", str(plain), "--card", CARD, "--db", str(board)], capsys
        )

        assert rc == 2
        assert out == ""
        assert "不是 git 工作树" in err
    finally:
        shutil.rmtree(plain, ignore_errors=True)


def test_cli_rc2_when_bots_file_malformed(
    repo: Path, board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """身份表格式坏（两列）→ rc2，不是「没问题」。"""

    (repo / ".team-board" / "bots.tsv").write_text(
        "engineer engineer@team-board.local\n", encoding="utf-8"
    )
    commit_as(repo, "feat: 干净提交", author=ENGINEER)

    rc, out, err = run(["--repo", str(repo), "--card", CARD, "--db", str(board)], capsys)

    assert rc == 2
    assert out == ""
    assert "格式非法" in err


def test_cli_card_is_required(repo: Path, board: Path) -> None:
    """``--card`` 必填：不给由 argparse 拦成 rc2（SystemExit 2）。"""

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["commit-audit", "--repo", str(repo), "--db", str(board)])

    assert excinfo.value.code == 2


def test_parser_defaults() -> None:
    """默认值：repo ``.`` / rev ``HEAD`` / main ``main`` / db=DEFAULT_DB / bots=None。"""

    args = cli.build_parser().parse_args(["commit-audit", "--card", CARD])

    assert args.repo == "."
    assert args.rev == "HEAD"
    assert args.main_branch == "main"
    assert args.db == DEFAULT_DB
    assert args.bots is None


def test_ascii_stdout_returns_two(repo: Path, board: Path) -> None:
    """stdout 编码表示不了中文 → rc2（真子进程跑，环境变量才有意义）。"""

    commit_as(repo, "feat: 干净提交", author=ENGINEER)
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
            "commit-audit",
            "--repo",
            str(repo),
            "--card",
            CARD,
            "--db",
            str(board),
        ],
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 2
    assert proc.stdout == b""
    assert proc.stderr.decode("utf-8", "replace").startswith("错误: ")
