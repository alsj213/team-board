"""branches 模块的纯单元测试：卡号正则、豁免表解析、渲染。不碰真实 git。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from team_board import branches
from team_board.branches import (
    AllowEntry,
    CheckoutDrift,
    Commit,
    Finding,
    Report,
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
    assert text.endswith("结论：发现 3 处偏差（提交归属 2 处 / checkout 1 处）")


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
