"""``scripts/ci-commit-identity.sh`` 的集成测试：真 bash 子进程 + ``tmp_path`` 里的真 git 仓库。

覆盖卡面要求的三块判定：

1. **镜像只认 main**：临时看板库的 ``card<TAB>建卡 assignee`` 表来自
   ``git show origin/main:.team-board/cards.tsv``；工作树里那份改成什么都**不影响结论**
   （否则 PR 能在自己被审的树里放宽判据② = 自证）。
2. **未知卡回落默认白名单** ``{engineer, qa}`` 而不是 rc2 —— 不再把别的卡的 PR 锁死，
   但红线仍咬得住（pm 当 author 照样 rc1）。
3. **rc2 三段**：参数个数 / 空 branch / 非 git 仓库 / 坏 rev → 一律 2 + ``错误: ``，
   不冒 traceback、不静默放过。

另外隐含一条：被稽核仓库是 ``tmp_path`` 里造的空仓库（没有 ``src/``、没有 ``scripts/``），
脚本仍跑本树的 ``make-audit-board.py`` / ``team_board.cli`` / ``bots.tsv`` —— 这正是
CI 里「audit must run this build」+ 本地「拿新构建稽核别的 worktree」两个场景。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

#: 本机 git 全局/系统配置一律置空：测试造的仓库与调用者的身份配置无关。
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_DATE": "2026-10-29T12:00:00+08:00",
    "GIT_COMMITTER_DATE": "2026-10-29T12:00:00+08:00",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "TZ": "Asia/Shanghai",
}

#: 身份表：与仓库里那份同构（本脚本只从「脚本所在的树」取它）
BOTS_TEXT = (
    "# bot 身份表：profile<TAB>name<TAB>email\n"
    "pm\tpm\tpm@team-board.local\n"
    "engineer\tengineer\tengineer@team-board.local\n"
    "qa\tqa\tqa@team-board.local\n"
    "ops\tops\tops@team-board.local\n"
    "hatch\thatch\thatch@team-board.local\n"
    "chief\tchief\tchief@team-board.local\n"
)

ENGINEER = ("engineer", "engineer@team-board.local")
PM = ("pm", "pm@team-board.local")
CHIEF = ("chief", "chief@team-board.local")

#: 本次测试稽核的卡（不存在于任何真实看板库 —— 只在本文件的临时仓库里出现）
CARD = "t_1234abcd"
#: 另一张卡：用来构造「镜像表里有别的卡、没有本卡」
OTHER_CARD = "t_87654321"

#: 被测试的脚本（取本仓的，不是临时仓库的）
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci-commit-identity.sh"

#: 未知卡回落时脚本必须打印的那一行（卡面原文）
FALLBACK_LINE = f"卡 {CARD} 不在 main 的镜像表 → author 白名单取默认 {{engineer, qa}}"


def git(repo: Path, *args: str) -> str:
    """在 ``repo`` 里跑 git，非零退出即测试失败。"""

    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=False,
    )
    assert proc.returncode == 0, (args, proc.stderr)
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


def make_repo(tmp_path: Path, *, cards_on_main: dict[str, str] | None = None) -> Path:
    """造一个 main 上有 ``.team-board/bots.tsv``（+ 可选 cards.tsv）的仓库。

    ``refs/remotes/origin/main`` 指向 main 的初始提交，所以卡分支的判定区间从这里开始。
    ``cards_on_main=None`` → main 上**没有** cards.tsv（模拟这张表还没合进 main）。
    """

    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    board = path / ".team-board"
    board.mkdir()
    (board / "bots.tsv").write_text(BOTS_TEXT, encoding="utf-8")
    if cards_on_main is not None:
        lines = ["# 卡 → 建卡时 assignee（main 上的镜像）"]
        lines += [f"{card}\t{assignee}" for card, assignee in cards_on_main.items()]
        (board / "cards.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    git(path, "add", "-A")
    commit_as(path, "chore: init", author=PM)
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return path


def run_script(
    tmp_path: Path, argv: list[str], *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """跑被测试的脚本（真子进程）。

    ``TB_CI_CARDS_FILE`` / ``TB_CI_BOARD_DB`` 指到本用例的临时目录：默认值
    ``/tmp/cards-main.tsv`` 是卡面口径，跑测试时不能与别的用例互相踩。
    """

    env = {
        **_GIT_ENV,
        "PYTHON": sys.executable,
        "TB_CI_CARDS_FILE": str(tmp_path / "cards-main.tsv"),
        "TB_CI_BOARD_DB": str(tmp_path / "commit-audit-board.db"),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def run_card(
    tmp_path: Path,
    repo: Path,
    branch: str,
    rev: str = "HEAD",
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``run_script`` 的常用形状：``<repo> <branch> <rev>``。"""

    return run_script(tmp_path, [str(repo), branch, rev], extra_env=extra_env)


# ------------------------------------------------ 1. 镜像只认 main（自证通道关闭）


def test_mirror_comes_from_main_not_the_worktree(tmp_path: Path) -> None:
    """工作树里的 cards.tsv 把本卡写成 chief —— 判定仍按 main 的 engineer，chief 提交照红。

    如果脚本读了工作树那份，白名单会是 ``{chief, engineer, qa}`` → 这条提交变绿。
    """

    repo = make_repo(tmp_path, cards_on_main={CARD: "engineer"})
    assert not (repo / "scripts").exists(), "被稽核仓库不该自带 scripts/（构建取自脚本所在树）"
    git(repo, "checkout", "-q", "-b", f"wt/{CARD}")
    (repo / ".team-board" / "cards.tsv").write_text(f"{CARD}\tchief\n", encoding="utf-8")
    commit_as(repo, "feat: chief 提交", author=CHIEF)

    proc = run_card(tmp_path, repo, f"wt/{CARD}")

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "author 不在白名单" in proc.stdout
    assert "assignee=engineer（建卡时）" in proc.stdout
    assert "不在 main 的镜像表" not in proc.stdout, "卡在 main 的镜像表里，不该回落"


def test_creation_assignee_in_mirror_lets_that_profile_through(tmp_path: Path) -> None:
    """main 的镜像表说建卡 assignee=pm → pm 当 author 判绿（判据② 的原义）。"""

    repo = make_repo(tmp_path, cards_on_main={CARD: "pm", OTHER_CARD: "engineer"})
    git(repo, "checkout", "-q", "-b", f"wt/{CARD}")
    commit_as(repo, "feat: pm 提交", author=PM)

    proc = run_card(tmp_path, repo, f"wt/{CARD}")

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "assignee=pm（建卡时）" in proc.stdout
    assert "结论：无问题" in proc.stdout


# ------------------------------------- 2. 未知卡回落默认白名单 {engineer, qa}（不锁死）


def test_unknown_card_falls_back_to_default_whitelist(tmp_path: Path) -> None:
    """卡不在 main 的镜像表 → 打印回落行、追加 engineer、engineer 提交判绿（不是 rc2）。"""

    repo = make_repo(tmp_path, cards_on_main={OTHER_CARD: "pm"})
    git(repo, "checkout", "-q", "-b", f"wt/{CARD}")
    commit_as(repo, "feat: engineer 提交", author=ENGINEER)

    proc = run_card(tmp_path, repo, f"wt/{CARD}")

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert FALLBACK_LINE in proc.stdout
    assert "assignee=engineer（建卡时）" in proc.stdout
    assert "结论：无问题" in proc.stdout
    # 机制可见：副本里被追加了「<卡>\tengineer」，main 上那份表一个字没动
    mirror = (tmp_path / "cards-main.tsv").read_text(encoding="utf-8")
    assert f"{CARD}\tengineer" in mirror
    main_mirror = git(repo, "show", "origin/main:.team-board/cards.tsv")
    assert CARD not in main_mirror
    assert f"{OTHER_CARD}\tpm" in main_mirror


def test_unknown_card_still_red_for_pm_author(tmp_path: Path) -> None:
    """回落不等于放水：未知卡上 pm 当 author 仍 rc1（默认白名单只有 engineer / qa）。"""

    repo = make_repo(tmp_path, cards_on_main={OTHER_CARD: "engineer"})
    git(repo, "checkout", "-q", "-b", f"wt/{CARD}")
    commit_as(repo, "feat: pm 提交", author=PM)

    proc = run_card(tmp_path, repo, f"wt/{CARD}")

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert FALLBACK_LINE in proc.stdout
    assert "author 不在白名单" in proc.stdout
    assert "允许={engineer, qa}" in proc.stdout


def test_missing_mirror_on_main_is_treated_as_empty_table(tmp_path: Path) -> None:
    """main 上根本没有 cards.tsv → 按空表处理 + 回落，仍判（rc0），不许退化成 rc2。

    这正是 PR #28 合并前的情形：#28 自己新增这张表，main 上还没有。
    """

    repo = make_repo(tmp_path)  # cards_on_main=None
    git(repo, "checkout", "-q", "-b", f"wt/{CARD}")
    commit_as(repo, "feat: engineer 提交", author=ENGINEER)

    proc = run_card(tmp_path, repo, f"wt/{CARD}")

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "按空镜像表处理" in proc.stderr
    assert FALLBACK_LINE in proc.stdout


def test_committer_mismatch_is_still_red(tmp_path: Path) -> None:
    """committer 与 author 不一致这条判据不受回落影响（未知卡上照样 rc1）。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", f"wt/{CARD}")
    commit_as(repo, "feat: 作者与提交者不同", author=ENGINEER, committer=PM)

    proc = run_card(tmp_path, repo, f"wt/{CARD}")

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "committer 与 author 不一致" in proc.stdout


# ------------------------------------------------------------- 3. rc2（参数 / 环境错）


@pytest.mark.parametrize("argv", [[], ["only-one"]])
def test_rc2_on_wrong_argument_count(tmp_path: Path, argv: list[str]) -> None:
    """参数个数不是 3 → rc2 + 用法行。"""

    proc = run_script(tmp_path, argv)

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr.startswith("错误: 用法: ")


def test_rc2_on_empty_branch(tmp_path: Path) -> None:
    """空 branch → rc2（不猜成 HEAD）。"""

    repo = make_repo(tmp_path)

    proc = run_card(tmp_path, repo, "")

    assert proc.returncode == 2
    assert "branch 为空" in proc.stderr


@pytest.mark.parametrize(("rev", "needle"), [("", "rev 为空"), ("nosuchrev", "rev 解析不成提交")])
def test_rc2_on_bad_rev(tmp_path: Path, rev: str, needle: str) -> None:
    """空 rev / 解析不成提交 → rc2。"""

    repo = make_repo(tmp_path)

    proc = run_card(tmp_path, repo, f"wt/{CARD}", rev)

    assert proc.returncode == 2
    assert needle in proc.stderr


def test_rc2_on_non_git_repo(tmp_path: Path) -> None:
    """目录存在但不是 git 工作树 → rc2（用 GIT_CEILING_DIRECTORIES 封住向上发现父仓库）。"""

    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    proc = run_card(
        tmp_path, not_a_repo, f"wt/{CARD}", extra_env={"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    )

    assert proc.returncode == 2
    assert "不是 git 工作树" in proc.stderr


def test_rc2_when_path_is_not_the_worktree_root(tmp_path: Path) -> None:
    """给了仓库里的子目录（不是工作树根）→ rc2：否则会静默稽核到外面那棵仓库。"""

    repo = make_repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()

    proc = run_card(tmp_path, sub, f"wt/{CARD}")

    assert proc.returncode == 2
    assert "不是工作树的根" in proc.stderr


def test_rc2_on_missing_repo_path(tmp_path: Path) -> None:
    """repo-path 不存在 → rc2。"""

    proc = run_card(tmp_path, tmp_path / "nope", f"wt/{CARD}")

    assert proc.returncode == 2
    assert "不是目录" in proc.stderr


# ------------------------------------------------- 4. 非卡分支：跳过（rc0，不判）


@pytest.mark.parametrize("branch", ["main", "feature/x", "wt/T_1234ABCD", "wt/t_1234abcdx"])
def test_non_card_branch_is_skipped(tmp_path: Path, branch: str) -> None:
    """非 ``wt/<t_8hex>`` 分支 → rc0 + 跳过行（兜底归 branch-audit job）。"""

    repo = make_repo(tmp_path)
    if branch == "main":  # main 是初始分支，不新建
        commit_as(repo, "feat: 谁写都行", author=PM)
    else:
        git(repo, "checkout", "-q", "-b", branch)
        commit_as(repo, "feat: 谁写都行", author=PM)

    proc = run_card(tmp_path, repo, branch)

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "跳过" in proc.stdout
    assert "提交身份稽核" not in proc.stdout
