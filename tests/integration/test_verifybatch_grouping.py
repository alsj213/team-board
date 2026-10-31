"""``verifybatch`` 分组行为的集成测试：真临时 git 仓、真 ``worktree add``、真 ``cherry-pick``。

对应本卡（E1）验收标准第 1–3 条，以及第 4 条里需要仓库的那半：

1. :class:`TestSamePatchDifferentHead` —— **同 patch 不同 head**：``git worktree add --detach``
   从父提交开一个 detached worktree，把原提交 ``git cherry-pick`` 进去得到「双胞胎」，
   两个 head sha 不同而 ``patch_id`` 相等 → ``build_groups`` 返 **1 组 2 unit**、``kind="patch"``（§3）；
2. :class:`TestDeterminism` —— 同一 plan 连跑两次、以及把 plan 里的 unit 顺序颠倒，
   组顺序 / ``batch_id`` / 组内 unit 顺序逐字相同（§0 D6）；
3. :class:`TestDegenerate` —— unit 数 1 → ``kind="solo"``；``patch_id`` 为 ``None`` 的
   同 head 两个 unit → 独占 ``kind="head"`` 组（§3.3、§0 D5）；
4. :class:`TestOrdering` —— 组按 ``(kind, key)`` 升序（``head`` < ``patch`` < ``solo``）；
5. :class:`TestErrors` —— head 解析不出来 → ``VerifyBatchError``（§3.1 整条命令失败）。

仓里每次用例都用 ``tmp_path`` 新建的仓，不碰本仓；``GIT_CONFIG_GLOBAL=os.devnull`` 挡住
宿主全局配置（``commit.gpgsign`` / ``core.hooksPath`` 都不许渗进来），身份用环境变量给。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from team_board import verifybatch as vb
from team_board.verifybatch import Unit, VerifyBatchError

CARD_A = "t_aaaaaaaa"
CARD_B = "t_bbbbbbbb"
CARD_C = "t_cccccccc"
CARD_D = "t_dddddddd"
CARD_E = "t_eeeeeeee"

_ENV = {
    key: value
    for key, value in os.environ.items()
    if key
    not in {
        "PYTHONPATH",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_PARAMETERS",
        "PYTHONIOENCODING",
        "GIT_CONFIG_GLOBAL",
    }
}
_ENV.update(
    {
        "GIT_AUTHOR_NAME": "verifybatch-test",
        "GIT_AUTHOR_EMAIL": "verifybatch@example.com",
        "GIT_COMMITTER_NAME": "verifybatch-test",
        "GIT_COMMITTER_EMAIL": "verifybatch@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
)


def git(
    cwd: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """跑一条 git 命令（**不断言**退出码）；``extra_env`` 只覆盖本次调用。"""

    env = dict(_ENV)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def must_git(cwd: Path, *args: str, extra_env: dict[str, str] | None = None) -> str:
    proc = git(cwd, *args, extra_env=extra_env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def commit_file(repo: Path, name: str, body: str, message: str) -> str:
    """写文件 → add → commit，返回新提交的 40 位 sha。"""

    (repo / name).write_text(body, encoding="utf-8")
    must_git(repo, "add", "-A")
    must_git(repo, "commit", "-q", "-m", message)
    return must_git(repo, "rev-parse", "HEAD").strip()


class Repo:
    """一个临时仓 + 三条提交 / 一个孤儿根提交（``patch_id`` 算不出来的那个）。"""

    def __init__(self, root: Path) -> None:
        self.path = root / "repo"
        self.path.mkdir()
        must_git(self.path, "init", "-q", "-b", "main")
        self.base = commit_file(self.path, "a.txt", "one\n", "base")
        self.first = commit_file(self.path, "a.txt", "two\n", "change a")
        self.second = commit_file(self.path, "b.txt", "three\n", "change b")
        self.orphan = self._orphan()

    def _orphan(self) -> str:
        """无父提交的根提交：与主历史没有共同祖先 → ``merge-base`` 直接失败。"""

        tree = must_git(self.path, "rev-parse", f"{self.base}^{{tree}}").strip()
        return must_git(self.path, "commit-tree", tree, "-m", "orphan").strip()

    def twin(self, worktree_parent: Path, *, stamp: str = "2001-01-01T00:00:00 +0000") -> str:
        """``worktree add --detach base`` + ``cherry-pick first`` → 同内容、新 sha 的第二个 head。

        单条 cherry-pick 回**它自己的父提交**会得到同一个 commit 对象（tree / parent /
        author / message 全同 → sha 相同），所以这里把提交者时间换掉，才真正「同改动、
        新 sha」。``patch-id`` 不看提交元数据，因此两个 sha 的 patch-id 仍然相等。
        """

        worktree = worktree_parent / f"twin-{stamp[:4]}"
        must_git(self.path, "worktree", "add", "--detach", str(worktree), self.base)
        must_git(
            worktree,
            "cherry-pick",
            self.first,
            extra_env={"GIT_COMMITTER_DATE": stamp},
        )
        return must_git(worktree, "rev-parse", "HEAD").strip()


@pytest.fixture(name="repo")
def repo_fixture(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def unit(card: str, pr: int, head: str) -> Unit:
    return Unit(card=card, pr=pr, head=head)


def plan(base: str, units: tuple[Unit, ...]) -> vb.Plan:
    return vb.Plan(base=base, group_checks=(), units=units)


class TestResolveHead:
    def test_full_sha(self, repo: Repo) -> None:
        assert vb.resolve_head(repo.path, repo.first) == repo.first

    def test_short_and_branch_and_rev_suffix(self, repo: Repo) -> None:
        """短 sha、分支名、``HEAD``、``<sha>^`` 都按 git rev 展开成 40 位。"""
        assert vb.resolve_head(repo.path, repo.first[:7]) == repo.first
        assert vb.resolve_head(repo.path, "main") == repo.second
        assert vb.resolve_head(repo.path, "HEAD") == repo.second
        assert vb.resolve_head(repo.path, f"{repo.first}^") == repo.base

    @pytest.mark.parametrize("bad", ["deadbeef", "no-such-branch", "HEAD~99", "af45471"])
    def test_bad_rev_raises(self, repo: Repo, bad: str) -> None:
        with pytest.raises(VerifyBatchError):
            vb.resolve_head(repo.path, bad)

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_rev_raises(self, repo: Repo, bad: str) -> None:
        with pytest.raises(VerifyBatchError):
            vb.resolve_head(repo.path, bad)


class TestPatchId:
    def test_same_commit_same_patch(self, repo: Repo) -> None:
        first = vb.patch_id(repo.path, repo.base, repo.first)
        assert first is not None
        assert vb.patch_id(repo.path, repo.base, repo.first) == first

    def test_different_content_different_patch(self, repo: Repo) -> None:
        first = vb.patch_id(repo.path, repo.base, repo.first)
        second = vb.patch_id(repo.path, repo.base, repo.second)
        assert first != second

    def test_empty_diff_is_none(self, repo: Repo) -> None:
        """base 就是 head 自己 → diff 为空 → ``None``（§0 D5：比不出来）。"""
        assert vb.patch_id(repo.path, repo.first, repo.first) is None

    def test_no_merge_base_is_none(self, repo: Repo) -> None:
        assert vb.patch_id(repo.path, repo.base, repo.orphan) is None

    def test_unknown_base_is_none(self, repo: Repo) -> None:
        assert vb.patch_id(repo.path, "no-such-rev", repo.first) is None


class TestSamePatchDifferentHead:
    """验收第 1 条：``worktree add --detach`` + ``cherry-pick`` 造双胞胎 → 1 组 2 unit。"""

    def test_grouped_by_patch(self, repo: Repo, tmp_path: Path) -> None:
        twin = repo.twin(tmp_path)

        # 前提：两个 head sha 不同、patch-id 相等（缺一个前提，这条用例就没意义）
        assert twin != repo.first
        pid_first = vb.patch_id(repo.path, repo.base, repo.first)
        pid_twin = vb.patch_id(repo.path, repo.base, twin)
        assert pid_first is not None
        assert pid_first == pid_twin
        print(f"head_1={repo.first}")
        print(f"head_2={twin}")
        print(f"patch_id={pid_first}")

        groups = vb.build_groups(
            repo.path,
            plan(repo.base, (unit(CARD_A, 53, repo.first), unit(CARD_B, 57, twin))),
        )

        assert len(groups) == 1
        group = groups[0]
        assert group.kind == "patch"
        assert group.key == f"patch:{pid_first}"
        assert group.patch_id == pid_first
        assert [u.card for u in group.units] == [CARD_A, CARD_B]
        # §3.6：组级检查跑在排序后首 unit 的 head_sha 上
        assert group.head_sha == repo.first
        assert group.batch_id == vb.batch_id_for([repo.first, twin])
        print(f"group: kind={group.kind} key={group.key} units={len(group.units)} "
              f"batch_id={group.batch_id} head={group.head_sha[:7]}")

    def test_three_units_same_patch(self, repo: Repo, tmp_path: Path) -> None:
        """再来一个双胞胎（同改动、另一个 committer date）→ 仍 1 组 3 unit。"""

        twin_a = repo.twin(tmp_path)
        twin_b = repo.twin(tmp_path, stamp="2002-02-02T00:00:00 +0000")
        assert len({repo.first, twin_a, twin_b}) == 3

        groups = vb.build_groups(
            repo.path,
            plan(
                repo.base,
                (
                    unit(CARD_A, 53, repo.first),
                    unit(CARD_B, 57, twin_a),
                    unit(CARD_C, 59, twin_b),
                ),
            ),
        )
        assert [g.kind for g in groups] == ["patch"]
        assert len(groups[0].units) == 3
        assert groups[0].batch_id == vb.batch_id_for([repo.first, twin_a, twin_b])


class TestDeterminism:
    """验收第 2 条：同一 plan 连跑两次，组 / batch_id / 组内顺序逐字相同。"""

    def test_two_runs_identical(self, repo: Repo, tmp_path: Path) -> None:
        twin = repo.twin(tmp_path)
        the_plan = plan(
            repo.base,
            (unit(CARD_B, 57, twin), unit(CARD_A, 53, repo.first)),
        )
        first = vb.build_groups(repo.path, the_plan)
        second = vb.build_groups(repo.path, the_plan)
        assert first == second
        for group in first:
            print(f"run: kind={group.kind} key={group.key} batch_id={group.batch_id} "
                  f"units={[u.card for u in group.units]}")

    def test_plan_order_irrelevant(self, repo: Repo, tmp_path: Path) -> None:
        """plan 里 unit 顺序颠倒，组顺序、组内顺序、batch_id 都不变。"""

        twin = repo.twin(tmp_path)
        forward = vb.build_groups(
            repo.path,
            plan(repo.base, (unit(CARD_A, 53, repo.first), unit(CARD_B, 57, twin))),
        )
        backward = vb.build_groups(
            repo.path,
            plan(repo.base, (unit(CARD_B, 57, twin), unit(CARD_A, 53, repo.first))),
        )
        assert forward == backward
        assert [u.card for u in forward[0].units] == [CARD_A, CARD_B]


class TestDegenerate:
    """验收第 3 条：单 unit → solo；``patch_id`` 为 None 的同 head 组 → head。"""

    def test_single_unit_is_solo(self, repo: Repo) -> None:
        groups = vb.build_groups(repo.path, plan(repo.base, (unit(CARD_A, 53, repo.first),)))
        assert len(groups) == 1
        group = groups[0]
        assert group.kind == "solo"
        assert len(group.units) == 1
        # 组键仍是 patch:<pid>（比得出来就是 patch 键），只是 unit 不够凑批
        assert group.key == f"patch:{group.patch_id}"
        assert group.head_sha == repo.first

    def test_lone_unpatchable_unit_is_solo(self, repo: Repo) -> None:
        """``patch_id`` 为 None 且唯一 → 仍是 ``solo``（契约 §3.3 优先于卡面「独占 head」措辞）。"""

        groups = vb.build_groups(repo.path, plan(repo.base, (unit(CARD_A, 53, repo.orphan),)))
        assert len(groups) == 1
        assert groups[0].kind == "solo"
        assert groups[0].patch_id is None
        assert groups[0].key == f"head:{repo.orphan}"

    def test_same_head_two_units_is_head_group(self, repo: Repo) -> None:
        """``patch_id`` 为 None + 同 head 两个 unit → 独占一组 ``kind="head"``。"""

        # base 取 head 自己 → diff 为空 → patch_id 为 None
        groups = vb.build_groups(
            repo.path,
            plan(repo.first, (unit(CARD_A, 53, repo.first), unit(CARD_B, 57, repo.first))),
        )
        assert len(groups) == 1
        group = groups[0]
        assert group.kind == "head"
        assert group.patch_id is None
        assert group.key == f"head:{repo.first}"
        assert len(group.units) == 2
        assert group.head_sha == repo.first
        assert group.batch_id == vb.batch_id_for([repo.first, repo.first])

    def test_two_solo_groups_sorted_by_key(self, repo: Repo) -> None:
        groups = vb.build_groups(
            repo.path,
            plan(repo.base, (unit(CARD_A, 53, repo.first), unit(CARD_B, 57, repo.second))),
        )
        assert [g.kind for g in groups] == ["solo", "solo"]
        assert all(len(g.units) == 1 for g in groups)
        assert [g.key for g in groups] == sorted(g.key for g in groups)


class TestOrdering:
    """§3.5：组按 ``(kind, key)`` 升序 —— ``head`` < ``patch`` < ``solo``。"""

    def test_kind_ordering(self, repo: Repo, tmp_path: Path) -> None:
        twin = repo.twin(tmp_path)
        groups = vb.build_groups(
            repo.path,
            plan(
                repo.base,
                (
                    # solo：单 unit，比得出来
                    unit(CARD_E, 63, repo.second),
                    # patch：同 patch 两个 head
                    unit(CARD_A, 53, repo.first),
                    unit(CARD_B, 57, twin),
                    # head：孤儿根提交，patch 比不出来（merge-base 失败）
                    unit(CARD_C, 59, repo.orphan),
                    unit(CARD_D, 61, repo.orphan),
                ),
            ),
        )
        assert [g.kind for g in groups] == ["head", "patch", "solo"]
        assert [len(g.units) for g in groups] == [2, 2, 1]
        assert groups[0].patch_id is None
        assert groups[1].patch_id is not None
        print("order: " + ", ".join(f"{g.kind}:{len(g.units)}:{g.batch_id}" for g in groups))


class TestErrors:
    def test_unresolvable_head_fails_whole_call(self, repo: Repo) -> None:
        """§3.1：任一 head 解析失败 → 整条命令失败，不静默跳过该 unit。"""

        with pytest.raises(VerifyBatchError) as excinfo:
            vb.build_groups(
                repo.path,
                plan(repo.base, (unit(CARD_A, 53, repo.first), unit(CARD_B, 57, "nope"))),
            )
        assert "nope" in str(excinfo.value)

    def test_missing_repo_is_environment_error(self, tmp_path: Path) -> None:
        """仓库路径不存在 → git 报错 → ``VerifyBatchError``（CLI 层 rc2）。"""

        with pytest.raises(VerifyBatchError):
            vb.resolve_head(tmp_path / "not-a-repo", "HEAD")
