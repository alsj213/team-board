"""``pushguard`` 模块的单元测试：解析、判定表（P-1…P-4）、不误拦、多命中不短路。

``judge`` 的判定依赖真 git 的 worktree 状态（HEAD / 主 checkout 定位），所以
这里用真 ``git`` 在 ``tmp_path`` 里造仓；解析类用例完全不碰 git。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from team_board import branches, pushguard
from team_board.pushguard import PushRef, Verdict, judge, parse_pre_push_lines

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "pushguard-test",
    "GIT_AUTHOR_EMAIL": "pushguard@example.com",
    "GIT_COMMITTER_NAME": "pushguard-test",
    "GIT_COMMITTER_EMAIL": "pushguard@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}

_ZERO = "0" * 40
_SHA = "a" * 40


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
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """主 checkout：main 上只有一条初始提交。"""

    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit_file(path, "README.md", "chore: init", "hello\n")
    return path


def add_worktree(repo: Path, parent: Path, dirname: str, branch: str) -> Path:
    """在 ``parent/dirname`` 建 worktree，检出 ``branch``。"""

    path = parent / dirname
    git(repo, "worktree", "add", "-q", str(path), "-b", branch)
    return path


def ref(local: str, remote: str | None = None, local_sha: str = _SHA) -> PushRef:
    """一条 pre-push 行；默认远端同本地、远端 sha 全零（新建分支）。"""

    return PushRef(
        local_ref=local,
        local_sha=local_sha,
        remote_ref=remote if remote is not None else local,
        remote_sha=_ZERO,
    )


def messages(verdicts: list[Verdict]) -> list[str]:
    return [verdict.message for verdict in verdicts]


# ------------------------------------------------------------ parse + 复用自检


def test_card_branch_re_is_branches_branch_re() -> None:
    """不许有第二份卡分支正则：同一个对象，pattern 也相等。"""

    assert pushguard.CARD_BRANCH_RE is branches.BRANCH_RE
    assert pushguard.CARD_BRANCH_RE.pattern == branches.BRANCH_RE.pattern


def test_parse_four_lines_ok() -> None:
    text = (
        "refs/heads/wt/t_aaaaaaaa abc refs/heads/wt/t_aaaaaaaa 000\n"
        "refs/heads/wt/t_bbbbbbbb def refs/heads/wt/t_bbbbbbbb 111"
    )
    refs = parse_pre_push_lines(text)
    assert len(refs) == 2
    assert refs[0] == PushRef(
        "refs/heads/wt/t_aaaaaaaa", "abc", "refs/heads/wt/t_aaaaaaaa", "000"
    )
    assert refs[1].remote_sha == "111"  # 末尾无换行也要收


def test_parse_skips_blank_lines() -> None:
    text = "\n\nrefs/heads/wt/t_aaaaaaaa abc refs/heads/wt/t_aaaaaaaa 000\n\n"
    assert len(parse_pre_push_lines(text)) == 1


@pytest.mark.parametrize(
    "line, fields",
    [
        ("refs/heads/x abc refs/heads/x", 3),
        ("refs/heads/x abc refs/heads/x 000 extra", 5),
        ("only-one", 1),
    ],
)
def test_parse_field_count_must_be_four(line: str, fields: int) -> None:
    with pytest.raises(branches.AuditError) as excinfo:
        parse_pre_push_lines(line + "\n")
    text = str(excinfo.value)
    assert "pre-push 输入格式非法" in text
    assert f"第 1 行只有 {fields} 个字段" in text


def test_parse_empty_is_error() -> None:
    with pytest.raises(branches.AuditError) as excinfo:
        parse_pre_push_lines("\n\n")
    assert str(excinfo.value) == "没有收到任何要推送的 ref（stdin 为空）"


def test_parse_line_number_counts_blank_lines() -> None:
    """行号按原始行算：第 1 行空、第 2 行空、坏行在第 3 行。"""

    with pytest.raises(branches.AuditError) as excinfo:
        parse_pre_push_lines("\n\na b c\n")
    assert "第 3 行只有 3 个字段" in str(excinfo.value)


# ------------------------------------------------------------ 放行基线 / 不误拦


def test_pass_baseline(repo: Path, tmp_path: Path) -> None:
    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    verdicts = judge(worktree, [ref("refs/heads/wt/t_aaaaaaaa")])
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.blocked is False
    assert verdict.message.startswith("push-guard: 放行 1 条 ref（checkout=")
    assert str(worktree) in verdict.message
    assert "HEAD=wt/t_aaaaaaaa" in verdict.message


def test_pass_does_not_block_tags_or_plain_branches(repo: Path, tmp_path: Path) -> None:
    """tag 与普通分支（pm/ci-check、wt/ci-setup）不在 P-3 拦。"""

    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    verdicts = judge(
        worktree,
        [
            ref("refs/tags/v1.0.0", "refs/tags/v1.0.0"),
            ref("refs/heads/pm/ci-check", "refs/heads/pm/ci-check"),
            ref("refs/heads/wt/ci-setup", "refs/heads/wt/ci-setup"),
        ],
    )
    assert len(verdicts) == 1
    assert verdicts[0].blocked is False
    assert "放行 3 条 ref" in verdicts[0].message


# ------------------------------------------------------------ P-1


def test_p1_main_checkout_on_card_branch(repo: Path, tmp_path: Path) -> None:
    git(repo, "checkout", "-q", "-b", "wt/t_cafebabe")
    verdicts = judge(repo, [ref("refs/heads/pm/ci-check")])
    assert verdicts[0].blocked is True
    assert verdicts[0].message.startswith(f"拒绝：主 checkout {repo} 停在 wt/t_cafebabe")
    assert "必须停 main" in verdicts[0].message


def test_p1_main_checkout_detached(repo: Path, tmp_path: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    verdicts = judge(repo, [ref("refs/heads/pm/ci-check")])
    assert verdicts[0].blocked is True
    assert "detached@" in verdicts[0].message
    assert "必须停 main" in verdicts[0].message


# ------------------------------------------------------------ P-2


def test_p2_main_checkout_must_not_push_card_branch(repo: Path) -> None:
    verdicts = judge(repo, [ref("refs/heads/wt/t_aaaaaaaa")])
    assert verdicts[0].blocked is True
    assert verdicts[0].message == (
        f"拒绝：主 checkout 不许推送卡分支 wt/t_aaaaaaaa；"
        f"做法：git -C {repo} worktree add /home/liu/tb-wt/t_aaaaaaaa "
        f"-b wt/t_aaaaaaaa origin/main"
    )


def test_p2_remote_side_only_counts(repo: Path) -> None:
    """本地 ref 是普通分支、远端目标是卡分支 → 一样拦（两侧都看）。"""

    verdicts = judge(
        repo,
        [ref("refs/heads/pm/ci-check", "refs/heads/wt/t_aaaaaaaa")],
    )
    assert verdicts[0].blocked is True
    assert "不许推送卡分支 wt/t_aaaaaaaa" in verdicts[0].message


# ------------------------------------------------------------ P-3


def test_p3a_worktree_dirname_mismatch(repo: Path, tmp_path: Path) -> None:
    worktree = add_worktree(repo, tmp_path, "wrongname", "wt/t_aaaaaaaa")
    verdicts = judge(worktree, [ref("refs/heads/wt/t_aaaaaaaa")])
    assert verdicts[0].blocked is True
    assert verdicts[0].message == (
        f"拒绝：worktree {worktree} 的目录名 wrongname 与所推卡分支 wt/t_aaaaaaaa "
        f"不一致（应为 t_aaaaaaaa）"
    )


def test_p3b_worktree_detached(repo: Path, tmp_path: Path) -> None:
    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    git(worktree, "checkout", "-q", "--detach")
    sha = git(worktree, "rev-parse", "HEAD").strip()
    verdicts = judge(worktree, [ref("refs/heads/wt/t_aaaaaaaa")])
    assert verdicts[0].blocked is True
    assert verdicts[0].message == (
        f"拒绝：worktree {worktree} 处于 detached@{sha[:7]}，无法归属到卡分支"
    )


def test_p3c_worktree_pushes_other_card(repo: Path, tmp_path: Path) -> None:
    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    verdicts = judge(worktree, [ref("refs/heads/wt/t_bbbbbbbb")])
    assert verdicts[0].blocked is True
    assert verdicts[0].message == (
        "拒绝：worktree 上的卡 t_aaaaaaaa 不许推送别的卡分支 wt/t_bbbbbbbb"
    )


def test_p3_does_not_apply_to_main_checkout(repo: Path) -> None:
    """主 checkout 推普通分支：P-3 不参与（否则会误拦）。"""

    verdicts = judge(repo, [ref("refs/heads/pm/ci-check")])
    assert verdicts == [
        Verdict(
            blocked=False,
            message=f"push-guard: 放行 1 条 ref（checkout={repo}，HEAD=main）",
        )
    ]


# ------------------------------------------------------------ P-4


@pytest.mark.parametrize("name", ["main", "master"])
def test_p4_protected_branch(repo: Path, tmp_path: Path, name: str) -> None:
    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    verdicts = judge(worktree, [ref(f"refs/heads/{name}")])
    assert verdicts[0].blocked is True
    assert verdicts[0].message == f"拒绝：不允许直接 push 到 {name}"
    assert len(verdicts) == 1  # 非删除不追加说明行


def test_p4_delete_remote_branch_adds_note(repo: Path, tmp_path: Path) -> None:
    worktree = add_worktree(repo, tmp_path, "t_aaaaaaaa", "wt/t_aaaaaaaa")
    verdicts = judge(
        worktree, [ref("(delete)", "refs/heads/main", local_sha=_ZERO)]
    )
    assert messages(verdicts) == [
        "拒绝：不允许直接 push 到 main",
        "（这是删除远端分支的操作，同样禁止）",
    ]
    assert verdicts[0].blocked is True
    assert verdicts[1].blocked is False


# ------------------------------------------------------------ 多命中不短路


def test_multi_hit_does_not_short_circuit(repo: Path) -> None:
    """主 checkout 停在卡分支 + 推 main：P-1 与 P-4 各一条，共 2 条（不短路）。"""

    git(repo, "checkout", "-q", "-b", "wt/t_cafebabe")
    verdicts = judge(repo, [ref("refs/heads/main")])
    assert len(verdicts) == 2
    assert messages(verdicts)[0].startswith("拒绝：主 checkout ")
    assert messages(verdicts)[1] == "拒绝：不允许直接 push 到 main"
    assert all(verdict.blocked for verdict in verdicts)


def test_multi_hit_p1_and_p2(repo: Path) -> None:
    """主 checkout 停在卡分支 + 推卡分支：P-1 与 P-2 各一条。"""

    git(repo, "checkout", "-q", "-b", "wt/t_cafebabe")
    verdicts = judge(repo, [ref("refs/heads/wt/t_aaaaaaaa")])
    assert len(verdicts) == 2
    assert messages(verdicts)[0].startswith("拒绝：主 checkout ")
    assert messages(verdicts)[1].startswith("拒绝：主 checkout 不许推送卡分支")


def test_multi_hit_same_card_both_sides_dedupes(repo: Path) -> None:
    """local_ref 与 remote_ref 是同一条卡分支 → P-2 只出一条。"""

    verdicts = judge(repo, [ref("refs/heads/wt/t_aaaaaaaa")])
    assert len(verdicts) == 1


# ------------------------------------------------------------ 参数 / 环境错误


def test_main_branch_empty_is_error(repo: Path) -> None:
    with pytest.raises(branches.AuditError) as excinfo:
        judge(repo, [ref("refs/heads/pm/ci-check")], main_branch="")
    assert "主分支名不能为空" in str(excinfo.value)


def test_main_branch_missing_is_error(repo: Path) -> None:
    with pytest.raises(branches.AuditError) as excinfo:
        judge(repo, [ref("refs/heads/pm/ci-check")], main_branch="nosuch")
    assert "主分支 nosuch 不存在" in str(excinfo.value)


def test_non_repo_is_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(branches.AuditError):
        judge(missing, [ref("refs/heads/pm/ci-check")])


def test_no_refs_is_error(repo: Path) -> None:
    with pytest.raises(branches.AuditError) as excinfo:
        judge(repo, [])
    assert "stdin 为空" in str(excinfo.value)
