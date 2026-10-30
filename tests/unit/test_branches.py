"""branches 模块的纯单元测试：卡号正则、豁免表解析、渲染。不碰真实 git。"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from team_board import branches
from team_board.branches import (
    AllowEntry,
    BranchSetFinding,
    CheckoutDrift,
    Commit,
    Finding,
    ForkPointFinding,
    Report,
    _branch_set_findings,
    _make_findings,
    load_allowlist,
    render_report,
)

# ---------------------------------------------------------------- 卡号正则


@pytest.mark.parametrize(
    "text, expected",
    [
        ("t_12345678", ["t_12345678"]),
        ("feat(t_abcdef12): x", ["t_abcdef12"]),
        ("xt_12345678", []),  # 前面粘字母 → 不算
        ("t_1234567", []),  # 只有 7 位
        ("t_123456789", []),  # 9 位：后面粘 9
        ("t_12345678x", ["t_12345678"]),  # 后向只排除十六进制字符（按卡里给的正则原文）
        ("t_1234567g", []),  # g 不是十六进制
        ("t_ABCDEF12", []),  # 大写不算
        ("evidence/t_406be0e8-x.log", ["t_406be0e8"]),
        ("a t_11111111 b t_22222222", ["t_11111111", "t_22222222"]),
        ("t_00000000", ["t_00000000"]),
    ],
)
def test_card_re_boundaries(text: str, expected: list[str]) -> None:
    assert branches.CARD_RE.findall(text) == expected


def test_branch_re_requires_exact_card_branch() -> None:
    assert branches.BRANCH_RE.match("wt/t_abcdef12")
    assert branches.BRANCH_RE.match("wt/t_aaaaaaaa")
    for name in ("wt/t_abcdef1", "wt/t_abcdef123", "wt/ci-setup", "main", "t_abcdef12"):
        assert branches.BRANCH_RE.match(name) is None


# ---------------------------------------------------------------- 豁免表


def test_load_allowlist_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_allowlist(tmp_path / "nope.txt") == ()
    assert load_allowlist(None) == ()


def test_load_allowlist_legal_entries(tmp_path: Path) -> None:
    path = tmp_path / "allow.txt"
    path.write_text(
        "# 注释头\n"
        "\n"
        "t_aaaaaaaa t_bbbbbbbb 52a2f93 同卡族返工\n"
        "t_aaaaaaaa - 0d00aa5  无卡号的历史提交，已确认归属\n"
        f"t_aaaaaaaa t_cccccccc {'a' * 40} 全 sha 也接受\n",
        encoding="utf-8",
    )
    entries = load_allowlist(path)
    assert entries == (
        AllowEntry("t_aaaaaaaa", "t_bbbbbbbb", "52a2f93", "同卡族返工"),
        AllowEntry(
            "t_aaaaaaaa", None, "0d00aa5", "无卡号的历史提交，已确认归属"
        ),
        AllowEntry("t_aaaaaaaa", "t_cccccccc", "a" * 40, "全 sha 也接受"),
    )


def test_load_allowlist_comment_header_only_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "allow.txt"
    path.write_text(
        "# <分支卡号> <对方卡号或-> <提交sha7..40> <理由，必填>\n",
        encoding="utf-8",
    )
    assert load_allowlist(path) == ()


@pytest.mark.parametrize(
    "line",
    [
        "t_aaaaaaaa t_bbbbbbbb",  # 少字段
        "t_aaaaaaaa",  # 少字段
        "t_aaaaaaaa t_bbbbbbbb 52a2f93",  # 缺理由
        "t_aaaaaaaa t_bbbbbbbb 52a2f9 理由",  # sha 只有 6 位
        "t_aaaaaaaa t_bbbbbbbb " + "a" * 41 + " 理由",  # sha 太长
        "t_aaaaaaaa t_bbbbbbbb 52a2f9G 理由",  # 非十六进制
        "t_aaaaaaaa t_bbbbbbbb 52a2f9gg 理由",
        "t_aaaa t_bbbbbbbb 52a2f93 理由",  # 分支卡号格式不对
        "t_aaaaaaaa t_bbbb 52a2f93 理由",  # 对方卡号格式不对
        "t_aaaaaaaa t_bbbbbbbb 52a2f93 理由\nbadline 52a2f93",  # 第二行坏
    ],
)
def test_load_allowlist_illegal_raises(tmp_path: Path, line: str) -> None:
    path = tmp_path / "allow.txt"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(branches.AuditError):
        load_allowlist(path)


def test_load_allowlist_non_utf8_raises_audit_error(tmp_path: Path) -> None:
    """非 UTF-8 文本（如误指到二进制文件）→ AuditError，不是 UnicodeDecodeError。"""

    path = tmp_path / "allow.bin"
    path.write_bytes(b"\xa0\xff\xfe\x80")
    with pytest.raises(branches.AuditError) as excinfo:
        load_allowlist(path)
    assert "UTF-8" in str(excinfo.value)


# ---------------------------------------------------------------- 规则 A 判定


def _commit(subject: str, *paths: str) -> Commit:
    sha = "0123456789abcdef0123456789abcdef01234567"
    return Commit(sha=sha, short=sha[:7], subject=subject, paths=tuple(paths))


def test_make_findings_clean_commit_is_silent() -> None:
    commit = _commit("feat(t_aaaaaaaa): 本卡的活", "src/team_board/branches.py")
    assert _make_findings("t_aaaaaaaa", (commit,)) == []


def test_make_findings_foreign_card_from_subject() -> None:
    commit = _commit("feat: t_bbbbbbbb 的活跑到 A 分支")
    findings = _make_findings("t_aaaaaaaa", (commit,))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == branches.KIND_FOREIGN
    assert finding.branch_card == "t_aaaaaaaa"
    assert finding.foreign_card == "t_bbbbbbbb"
    assert finding.evidence == "提交标题"


def test_make_findings_foreign_card_from_path() -> None:
    commit = _commit("chore: 落证据", "evidence/t_406be0e8-audit.log")
    findings = _make_findings("t_aaaaaaaa", (commit,))
    assert len(findings) == 1
    assert findings[0].foreign_card == "t_406be0e8"
    assert findings[0].evidence == "文件路径 evidence/t_406be0e8-audit.log"


def test_make_findings_no_card_at_all() -> None:
    commit = _commit("chore: 顺手改点东西", "src/a.py")
    findings = _make_findings("t_aaaaaaaa", (commit,))
    assert len(findings) == 1
    assert findings[0].kind == branches.KIND_NO_CARD
    assert findings[0].foreign_card is None


def test_make_findings_multiple_foreign_cards_each_named() -> None:
    commit = _commit("merge work t_bbbbbbbb and t_cccccccc")
    findings = _make_findings("t_aaaaaaaa", (commit,))
    assert {f.foreign_card for f in findings} == {"t_bbbbbbbb", "t_cccccccc"}


def test_finding_is_immutable() -> None:
    commit = _commit("chore: x")
    finding = Finding(
        kind=branches.KIND_NO_CARD,
        branch_card="t_aaaaaaaa",
        foreign_card=None,
        commit=commit,
        evidence="提交标题",
    )
    with pytest.raises(FrozenInstanceError):
        finding.kind = "别的"  # type: ignore[misc]


# ---------------------------------------------------------------- 渲染


def _green_report() -> Report:
    return Report(
        repo="/tmp/repo",
        main_branch="main",
        checked_branches=("t_aaaaaaaa",),
        findings=(),
        drifts=(),
        suppressed=(),
    )


def test_render_report_green_ends_with_conclusion() -> None:
    text = render_report(_green_report())
    assert text.endswith("结论：无偏差")
    assert "t_aaaaaaaa" in text
    assert "（无）" in text


def test_render_report_ok_property() -> None:
    assert _green_report().ok is True
    red = Report(
        repo="/tmp/repo",
        main_branch="main",
        checked_branches=(),
        findings=(
            Finding(
                kind=branches.KIND_FOREIGN,
                branch_card="t_aaaaaaaa",
                foreign_card="t_bbbbbbbb",
                commit=_commit("feat: t_bbbbbbbb"),
                evidence="提交标题",
            ),
        ),
        drifts=(),
        suppressed=(),
    )
    assert red.ok is False


def test_render_report_red_names_both_cards_and_counts() -> None:
    report = Report(
        repo="/tmp/repo",
        main_branch="main",
        checked_branches=("t_aaaaaaaa",),
        findings=(
            Finding(
                kind=branches.KIND_FOREIGN,
                branch_card="t_aaaaaaaa",
                foreign_card="t_bbbbbbbb",
                commit=_commit("feat: t_bbbbbbbb 的活"),
                evidence="提交标题",
            ),
            Finding(
                kind=branches.KIND_NO_CARD,
                branch_card="t_aaaaaaaa",
                foreign_card=None,
                commit=_commit("chore: 无卡号"),
                evidence="提交标题",
            ),
        ),
        drifts=(
            CheckoutDrift(
                path="/tmp/other",
                actual="wt/t_aaaaaaaa",
                expected="wt/other",
            ),
        ),
        suppressed=(),
    )
    text = render_report(report)
    assert "t_aaaaaaaa" in text and "t_bbbbbbbb" in text
    assert "未声明卡号" in text
    assert "/tmp/other 当前在 wt/t_aaaaaaaa（应为 wt/other）" in text
    assert text.endswith(
        "结论：发现 3 处偏差（提交归属 2 处 / checkout 1 处 / 分支结构 0 处）"
    )


def test_render_report_echoes_allowlist_reason() -> None:
    entry = AllowEntry("t_aaaaaaaa", "t_bbbbbbbb", "52a2f93", "同卡族返工")
    finding = Finding(
        kind=branches.KIND_FOREIGN,
        branch_card="t_aaaaaaaa",
        foreign_card="t_bbbbbbbb",
        commit=_commit("feat: t_bbbbbbbb"),
        evidence="提交标题",
    )
    report = Report(
        repo="/tmp/repo",
        main_branch="main",
        checked_branches=("t_aaaaaaaa",),
        findings=(),
        drifts=(),
        suppressed=((entry, finding),),
    )
    text = render_report(report)
    assert "豁免" in text
    assert "同卡族返工" in text
    assert text.endswith("结论：无偏差")


def test_render_report_notes_line() -> None:
    report = Report(
        repo="/tmp/repo",
        main_branch="main",
        checked_branches=(),
        findings=(),
        drifts=(),
        suppressed=(),
        notes=("main 不是卡分支，跳过提交归属检查",),
    )
    text = render_report(report)
    assert "提示: main 不是卡分支，跳过提交归属检查" in text


# ---------------------------------------------------------------- 规则 C-1
# 分支名 → 该名字下的完整 ref（本地 + origin，已去重），由 audit() 的
# `for-each-ref refs/heads refs/remotes/origin` 整理而来。


def test_branch_set_clean_card_branch_is_silent() -> None:
    names: dict[str, tuple[str, ...]] = {
        "wt/t_aaaaaaaa": ("refs/heads/wt/t_aaaaaaaa",)
    }
    assert _branch_set_findings(names) == ()


def test_branch_set_second_branch_for_same_card() -> None:
    names: dict[str, tuple[str, ...]] = {
        "wt/t_aaaaaaaa": ("refs/heads/wt/t_aaaaaaaa",),
        "wt/t_aaaaaaaa-fix": ("refs/heads/wt/t_aaaaaaaa-fix",),
    }
    findings = _branch_set_findings(names)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.card == "t_aaaaaaaa"
    assert finding.branch == "wt/t_aaaaaaaa-fix"
    assert finding.refs == ("refs/heads/wt/t_aaaaaaaa-fix",)
    assert finding.evidence == (
        "分支名 wt/t_aaaaaaaa-fix 不在 wt/t_aaaaaaaa"
        "（卡分支只能叫 wt/t_aaaaaaaa）"
    )


def test_branch_set_bare_card_branch_without_prefix() -> None:
    names: dict[str, tuple[str, ...]] = {
        "t_aaaaaaaa": ("refs/heads/t_aaaaaaaa",)
    }
    findings = _branch_set_findings(names)
    assert len(findings) == 1
    assert findings[0].card == "t_aaaaaaaa"
    assert findings[0].branch == "t_aaaaaaaa"


def test_branch_set_names_without_card_are_skipped() -> None:
    names: dict[str, tuple[str, ...]] = {
        "main": ("refs/heads/main",),
        "pm/ci-check": ("refs/heads/pm/ci-check",),
        "wt/ci-setup": ("refs/heads/wt/ci-setup",),
        "HEAD": ("refs/remotes/origin/HEAD",),
        "wt/t_aaaaaaaa2": ("refs/heads/wt/t_aaaaaaaa2",),
    }
    assert _branch_set_findings(names) == ()


def test_branch_set_merges_local_and_origin_same_name() -> None:
    """同一个分支名的本地与 origin 两份属于同一条分支，不许判成两条。"""

    names: dict[str, tuple[str, ...]] = {
        "wt/t_aaaaaaaa": (
            "refs/heads/wt/t_aaaaaaaa",
            "refs/remotes/origin/wt/t_aaaaaaaa",
        ),
        "wt/t_bbbbbbbb": (
            "refs/heads/wt/t_bbbbbbbb",
            "refs/remotes/origin/wt/t_bbbbbbbb",
        ),
    }
    assert _branch_set_findings(names) == ()


# ------------------------------------------------- R11 证据分支命名空间（不占卡分支）


def test_evidence_branch_names_are_recognised() -> None:
    for name in (
        "evidence/t_aaaaaaaa",
        "evidence/t_aaaaaaaa-ci",
        "evidence/t_aaaaaaaa.perf_1",
        "evidence/t_589cc33a",
        "evidence/ci-logs",
    ):
        assert branches._is_evidence_branch(name) is True, name


def test_evidence_lookalikes_are_not_evidence_branches() -> None:
    """不属于 R11 命名空间的一律不算证据分支（不享受 C-1 豁免）。"""

    for name in (
        "evidence/a/t_aaaaaaaa",  # 多段
        "evidence/-x",  # 非字母数字开头
        "evidence/",  # 空名
        "evidence2/t_aaaaaaaa",  # 不是 evidence/ 前缀
        "wt/t_aaaaaaaa",
        "Evidence/t_aaaaaaaa",  # 大小写敏感
    ):
        assert branches._is_evidence_branch(name) is False, name


def test_branch_set_skips_evidence_branches() -> None:
    """R11 ②：证据分支不占卡分支命名空间，C-1 必须静默。

    这条是回归锁：C-1 全仓枚举、与 ``--branch`` 无关，一旦把
    ``evidence/t_<卡号>`` 判红，任何人按 R11 建证据分支都会让所有 PR 的
    branch-audit job 变红。
    """

    names: dict[str, tuple[str, ...]] = {
        "evidence/t_589cc33a": ("refs/heads/evidence/t_589cc33a",),
    }
    assert _branch_set_findings(names) == ()

    names = {
        "wt/t_aaaaaaaa": (
            "refs/heads/wt/t_aaaaaaaa",
            "refs/remotes/origin/wt/t_aaaaaaaa",
        ),
        "evidence/t_aaaaaaaa": ("refs/heads/evidence/t_aaaaaaaa",),
        "evidence/t_aaaaaaaa-ci": ("refs/heads/evidence/t_aaaaaaaa-ci",),
        "evidence/t_aaaaaaaa.perf_1": ("refs/heads/evidence/t_aaaaaaaa.perf_1",),
        "main": ("refs/heads/main",),
    }
    assert _branch_set_findings(names) == ()


def test_branch_set_still_flags_non_evidence_multi_segment_branch() -> None:
    """多段 ``evidence/a/<卡号>`` 不是 R11 证据分支 → C-1 仍判红。"""

    names: dict[str, tuple[str, ...]] = {
        "wt/t_aaaaaaaa": ("refs/heads/wt/t_aaaaaaaa",),
        "evidence/a/t_aaaaaaaa": ("refs/heads/evidence/a/t_aaaaaaaa",),
    }
    findings = _branch_set_findings(names)
    assert len(findings) == 1
    assert findings[0].card == "t_aaaaaaaa"
    assert findings[0].branch == "evidence/a/t_aaaaaaaa"
    assert findings[0].evidence == (
        "分支名 evidence/a/t_aaaaaaaa 不在 wt/t_aaaaaaaa"
        "（卡分支只能叫 wt/t_aaaaaaaa）"
    )


def test_branch_set_still_flags_evidence2_prefixed_branch() -> None:
    """``evidence2/<卡号>`` 不是 ``evidence/`` 前缀 → 照旧判红。"""

    names: dict[str, tuple[str, ...]] = {
        "evidence2/t_aaaaaaaa": ("refs/heads/evidence2/t_aaaaaaaa",),
    }
    findings = _branch_set_findings(names)
    assert len(findings) == 1
    assert findings[0].branch == "evidence2/t_aaaaaaaa"


def test_evidence_branch_without_card_is_silent() -> None:
    """``evidence/-x`` 不是证据分支，但也不含卡号 → C-1 本来就不报。"""

    names: dict[str, tuple[str, ...]] = {
        "evidence/-x": ("refs/heads/evidence/-x",),
    }
    assert branches._is_evidence_branch("evidence/-x") is False
    assert _branch_set_findings(names) == ()


def test_render_report_multi_branch_item() -> None:
    finding = BranchSetFinding(
        card="t_aaaaaaaa",
        branch="wt/t_aaaaaaaa-fix",
        refs=("refs/heads/wt/t_aaaaaaaa-fix",),
        evidence=(
            "分支名 wt/t_aaaaaaaa-fix 不在 wt/t_aaaaaaaa"
            "（卡分支只能叫 wt/t_aaaaaaaa）"
        ),
    )
    report = Report(
        repo="/tmp/repo",
        main_branch="main",
        checked_branches=("t_aaaaaaaa",),
        findings=(),
        drifts=(),
        suppressed=(),
        multi=(finding,),
    )
    text = render_report(report)
    assert "分支结构偏差（1 处）:" in text
    assert (
        "  1. 卡 t_aaaaaaaa 有第二条分支 wt/t_aaaaaaaa-fix"
        "（卡分支只能叫 wt/t_aaaaaaaa）；证据："
        "分支名 wt/t_aaaaaaaa-fix 不在 wt/t_aaaaaaaa"
        "（卡分支只能叫 wt/t_aaaaaaaa）"
    ) in text
    assert report.ok is False


# ---------------------------------------------------------------- 规则 C-2 渲染


def _report_with_forks(
    *forks: ForkPointFinding, multi: tuple[BranchSetFinding, ...] = ()
) -> Report:
    return Report(
        repo="/tmp/repo",
        main_branch="main",
        checked_branches=("t_aaaaaaaa",),
        findings=(),
        drifts=(),
        suppressed=(),
        multi=multi,
        forks=forks,
    )


def test_render_report_fork_without_parent() -> None:
    fork = ForkPointFinding(
        card="t_aaaaaaaa",
        branch="wt/t_aaaaaaaa",
        first_commit="a" * 40,
        fork_point=None,
        forked_from_cards=(),
    )
    text = render_report(_report_with_forks(fork))
    assert (
        "  1. 分支 wt/t_aaaaaaaa 的第一个提交 aaaaaaa 没有父提交（孤立历史）；"
        "证据：fork 点不在 main 上"
    ) in text
    assert text.endswith(
        "结论：发现 1 处偏差（提交归属 0 处 / checkout 0 处 / 分支结构 1 处）"
    )


def test_render_report_fork_on_other_card_branches() -> None:
    fork = ForkPointFinding(
        card="t_aaaaaaaa",
        branch="wt/t_aaaaaaaa",
        first_commit="a" * 40,
        fork_point="b" * 40,
        forked_from_cards=("t_bbbbbbbb", "t_cccccccc"),
    )
    text = render_report(_report_with_forks(fork))
    assert (
        "  1. 分支 wt/t_aaaaaaaa 的第一个提交 aaaaaaa 挂在 bbbbbbb 上，"
        "bbbbbbb 不在 main 上（它属于卡 t_bbbbbbbb、t_cccccccc 的分支）"
    ) in text


def test_render_report_fork_not_on_any_card_branch() -> None:
    fork = ForkPointFinding(
        card="t_aaaaaaaa",
        branch="wt/t_aaaaaaaa",
        first_commit="a" * 40,
        fork_point="b" * 40,
        forked_from_cards=(),
    )
    text = render_report(_report_with_forks(fork))
    assert (
        "  1. 分支 wt/t_aaaaaaaa 的第一个提交 aaaaaaa 挂在 bbbbbbb 上，"
        "bbbbbbb 不在 main 上（不属于任何卡分支）"
    ) in text


def test_render_report_multi_then_forks_numbering_is_continuous() -> None:
    multi = BranchSetFinding(
        card="t_aaaaaaaa",
        branch="wt/t_aaaaaaaa-fix",
        refs=("refs/heads/wt/t_aaaaaaaa-fix",),
        evidence="分支名 wt/t_aaaaaaaa-fix 不在 wt/t_aaaaaaaa"
        "（卡分支只能叫 wt/t_aaaaaaaa）",
    )
    fork = ForkPointFinding(
        card="t_dddddddd",
        branch="wt/t_dddddddd",
        first_commit="d" * 40,
        fork_point=None,
        forked_from_cards=(),
    )
    text = render_report(_report_with_forks(fork, multi=(multi,)))
    assert "分支结构偏差（2 处）:" in text
    assert "  1. 卡 t_aaaaaaaa 有第二条分支" in text
    assert "  2. 分支 wt/t_dddddddd 的第一个提交 ddddddd 没有父提交" in text


# ------------------------------ git 调用层的环境隔离（继承来的 GIT_DIR 不许劫持）
#
# 本文件其余部分不碰真 git；这一节是例外，且必须造真仓库：只有真 git 才验得出
# 「``git -C <path>`` 只切目录、不隔离 ``GIT_DIR``」。期望值来自卡 t_11ef8cf3：
# ``--repo`` 指向非仓库目录时必须报错（CLI 转 rc2），而不是去审 ``GIT_DIR`` 指的
# 那个仓库、报头却写 ``--repo`` 并给出假绿。

#: 会被子进程继承、把 git 指向别处的定位类变量。造仓库时一并剔掉：否则宿主自己
#: 带着 GIT_DIR 跑 pytest 时，fixture 仓库也会被造到别处去。
_LOCATION_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


def _clean_env() -> dict[str, str]:
    """造测试仓库用的环境：剔掉 git 定位类变量，固定作者/提交者。"""

    env = {
        key: value for key, value in os.environ.items() if key not in _LOCATION_ENV_KEYS
    }
    env.update(
        {
            "GIT_AUTHOR_NAME": "branches-env-test",
            "GIT_AUTHOR_EMAIL": "branches-env@example.com",
            "GIT_COMMITTER_NAME": "branches-env-test",
            "GIT_COMMITTER_EMAIL": "branches-env@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _real_git(repo: Path, *args: str) -> str:
    """在 ``repo`` 里跑 git，失败即测试失败。"""

    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _make_repo(path: Path) -> Path:
    """造一个 ``main`` 上有一条提交的真仓库，返回路径。"""

    path.mkdir(parents=True, exist_ok=True)
    _real_git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _real_git(path, "add", "-A")
    _real_git(path, "commit", "-q", "-m", "chore: init")
    return path


def _non_repo_dir(tmp_path: Path) -> Path:
    """一个确定「不在任何 git 仓库里」的目录。

    ``tmp_path`` 可能整个落在某个仓库里（本机 ``/home/liu`` 就是个人仓），那样
    ``rev-parse --show-toplevel`` 会成功，用例根本测不到「非仓库 → 报错」这一支。
    所以这里用 git 自己探一次，落在仓库里就退到系统临时目录再探。
    """

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
            env=_clean_env(),
            check=False,
        )
        if proc.returncode != 0:
            return candidate
    raise AssertionError("找不到不在任何 git 仓库里的目录，无法验证「非仓库 → 报错」")


def test_git_dir_pointing_elsewhere_cannot_make_non_repo_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用例 B：``GIT_DIR`` 指向真仓库 + ``--repo`` 指向非仓库 → ``AuditError``。"""

    other = _make_repo(tmp_path / "other")
    not_a_repo = _non_repo_dir(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    with pytest.raises(branches.AuditError) as excinfo:
        branches.audit(repo=not_a_repo)

    message = str(excinfo.value)
    assert "不是 git 工作树" in message
    assert str(not_a_repo) in message


def test_git_work_tree_pointing_elsewhere_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GIT_WORK_TREE`` 同样不许生效（与 ``GIT_DIR`` 成对设置才是完整劫持）。"""

    other = _make_repo(tmp_path / "other")
    not_a_repo = _non_repo_dir(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

    with pytest.raises(branches.AuditError):
        branches.audit(repo=not_a_repo)


def test_git_dir_cannot_supply_branches_from_another_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真仓对真仓：``GIT_DIR`` 指向 B 时，不许把 B 的卡分支算进 A 的稽核结果。

    修前 ``for-each-ref`` 会去读 B 的 refs —— 报头写 A，偏差来自 B。
    """

    target = _make_repo(tmp_path / "target")
    other = _make_repo(tmp_path / "other")
    _real_git(other, "checkout", "-q", "-b", "wt/t_aaaaaaaa")
    (other / "w.txt").write_text("x\n", encoding="utf-8")
    _real_git(other, "add", "-A")
    _real_git(other, "commit", "-q", "-m", "feat: t_bbbbbbbb 的活跑到 aaaaaaaa 分支")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    report = branches.audit(repo=target)

    assert Path(report.repo).resolve() == target.resolve()
    assert report.checked_branches == ()
    assert report.ok


def test_clean_env_still_audits_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用例 C/D：不设 ``GIT_DIR``、``PATH`` 正常时照常跑通（env 没把 PATH 抹掉）。"""

    repo = _make_repo(tmp_path / "repo")
    monkeypatch.delenv("GIT_DIR", raising=False)

    report = branches.audit(repo=repo)

    assert Path(report.repo).resolve() == repo.resolve()
    assert report.ok


# ---------------------------------------------------------------- 证据分支（R11）
# ``evidence/<单段名>`` 是独立命名空间：单列一段、不进 findings/drifts、不改 rc；
# 卡分支口径（BRANCH_RE）一字不放宽。


@pytest.mark.parametrize(
    "name, expected",
    [
        ("evidence/t_589cc33a", True),
        ("evidence/t_589cc33a-ci", True),
        ("evidence/perf-2026-10", True),
        ("evidence/A", True),
        ("evidence/", False),  # 空名
        ("evidence/a/b", False),  # 多段
        ("evidence/-x", False),  # 非字母数字开头
        ("evidence", False),
        ("wt/t_589cc33a", False),
        ("EV/t_589cc33a", False),
        ("evidenceX/t_589cc33a", False),
    ],
)
def test_evidence_re_requires_single_segment(name: str, expected: bool) -> None:
    assert branches._is_evidence_branch(name) is expected


def _report_with_evidence(names: tuple[str, ...]) -> Report:
    return Report(
        repo="/tmp/repo",
        main_branch="main",
        checked_branches=(),
        findings=(),
        drifts=(),
        suppressed=(),
        evidence_branches=names,
    )


def test_render_report_evidence_section_empty_follows_checked_line() -> None:
    lines = render_report(_report_with_evidence(())).splitlines()
    index = next(
        i for i, line in enumerate(lines) if line.startswith("检查的卡分支: ")
    )
    assert lines[index + 1] == "证据分支（0 条，不占卡分支命名空间、不计入提交归属检查）:"
    assert lines[index + 2] == "  （无）"


def test_render_report_evidence_section_lists_names_joined() -> None:
    text = render_report(
        _report_with_evidence(("evidence/t_589cc33a", "evidence/t_589cc33a-ci"))
    )

    assert "证据分支（2 条，不占卡分支命名空间、不计入提交归属检查）:" in text
    assert "  evidence/t_589cc33a、evidence/t_589cc33a-ci" in text
    # 证据分支不进任何偏差段，结论行不变
    assert text.endswith("结论：无偏差")


def test_report_evidence_branches_does_not_change_ok() -> None:
    """``ok`` 只看 findings / drifts / multi / forks —— 证据分支不加也不减。"""

    assert _report_with_evidence(("evidence/t_aaaaaaaa",)).ok is True


def test_audit_lists_evidence_branch_and_stays_green(tmp_path: Path) -> None:
    """真仓：证据分支上的无卡号提交不判红，且被单列出来。"""

    repo = _make_repo(tmp_path / "repo")
    _real_git(repo, "checkout", "-q", "-b", "evidence/t_deadbeef")
    (repo / "e.txt").write_text("x\n", encoding="utf-8")
    _real_git(repo, "add", "-A")
    _real_git(repo, "commit", "-q", "-m", "chore: 证据留档")
    _real_git(repo, "checkout", "-q", "main")

    report = branches.audit(repo=repo)

    assert report.evidence_branches == ("evidence/t_deadbeef",)
    assert report.checked_branches == ()  # 证据分支不进规则 A 的卡分支集合
    assert report.notes == ()
    assert report.ok


def test_audit_notes_odd_evidence_names_without_failing(tmp_path: Path) -> None:
    """``evidence/a/b``、``evidence/-x`` 按普通分支跳过：给提示、不进证据段、不改 rc。"""

    repo = _make_repo(tmp_path / "repo")
    for name in ("evidence/a/b", "evidence/-x"):
        _real_git(repo, "branch", name, "main")

    report = branches.audit(repo=repo)

    assert report.evidence_branches == ()
    assert report.ok
    joined = "\n".join(report.notes)
    for name in ("evidence/a/b", "evidence/-x"):
        assert (
            f"{name} 不符合证据分支命名（应为 evidence/<字母数字开头的单段名>），"
            f"按普通分支跳过"
        ) in joined


def test_evidence_names_dedupe_across_all_remotes(tmp_path: Path) -> None:
    """同名本地 + 各远端按名字去重成一条（覆盖 ``refs/remotes/*`` 不只 origin）。"""

    repo = _make_repo(tmp_path / "repo")
    sha = _real_git(repo, "rev-parse", "main").strip()
    _real_git(repo, "update-ref", "refs/remotes/origin/evidence/t_aaaaaaaa", sha)
    _real_git(repo, "update-ref", "refs/remotes/upstream/evidence/t_aaaaaaaa", sha)
    _real_git(repo, "update-ref", "refs/remotes/upstream/evidence/t_bbbbbbbb", sha)

    report = branches.audit(repo=repo)

    assert report.evidence_branches == (
        "evidence/t_aaaaaaaa",
        "evidence/t_bbbbbbbb",
    )
    assert report.ok

