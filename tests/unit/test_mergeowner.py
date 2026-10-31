"""``mergeowner`` 的纯函数单测：抽取、豁免、五类判定、渲染、rc 与三个 parse。

这里只测纯函数（不连库、不调 gh、不碰 stdout）；CLI 的 rc 三态与降级日志在
``tests/integration/test_cli_merge_owner_audit.py``（子卡 E2）。

12 条用例对应契约 v1（t_6fca5f99 冻结）的清单；每条内部覆盖多个边界值
（``0`` / 负数 / 13 位毫秒 / 天文数字 / 空串），不再拆成更多函数。
"""

from __future__ import annotations

import pytest

from team_board import mergeowner as mo
from team_board.mergeowner import GhResult, MergeOwnerError

CARD = "t_0123abcd"
OTHER_A = "t_0badf00d"
OTHER_B = "t_0badf00e"


class TestExtract:
    def test_two_forms(self) -> None:
        """``PR #n`` 与 URL 的 ``/pull/n`` 都命中；``PR#52`` / ``PR # 52`` 不命中。"""
        assert mo.extract_prs("PR #52") == (52,)
        assert mo.extract_prs("PR #52（说明）") == (52,)
        assert mo.extract_prs("见 https://github.com/x/y/pull/52 一栏") == (52,)
        assert mo.extract_prs("PR#52") == ()
        assert mo.extract_prs("PR # 52") == ()
        assert mo.extract_prs("PR  #52") == ()

    def test_dedupe_ascending_and_non_hits(self) -> None:
        """跨入参去重、返回升序；``0`` / 10 位 / 13 位毫秒 / ``None`` / 空串不命中。"""
        assert mo.extract_prs("PR #53", "PR #50, PR #53") == (50, 53)
        assert mo.extract_prs("PR #9 /pull/7", "PR #9") == (7, 9)
        for text in (
            "PR #0",
            "https://github.com/x/y/pull/0",
            "PR #1234567890",
            "PR #1793160000000",
            "/pull/1793160000000",
            None,
            "",
        ):
            assert mo.extract_prs(text) == ()


class TestExempt:
    def test_only_anchored_line_counts(self) -> None:
        """顶格命中；缩进、尾随空格、尾随文字、``None`` 都不算。"""
        assert mo.exempt_prs("PR-MERGE-EXEMPT: #52") == frozenset({52})
        assert mo.exempt_prs("目标：甲\nPR-MERGE-EXEMPT: #52\n结尾") == frozenset({52})
        assert mo.exempt_prs("PR-MERGE-EXEMPT: #52\nPR-MERGE-EXEMPT: #7") == frozenset(
            {52, 7}
        )
        assert mo.exempt_prs("  PR-MERGE-EXEMPT: #52") == frozenset()
        assert mo.exempt_prs("PR-MERGE-EXEMPT: #52 ") == frozenset()
        assert mo.exempt_prs("PR-MERGE-EXEMPT: #52（临时）") == frozenset()
        assert mo.exempt_prs(None) == frozenset()


class TestJudge:
    def test_exempt_wins_over_gh_and_gh_states(self) -> None:
        """优先级 1→2：豁免压过 gh 报错；``MERGED`` / ``CLOSED`` 原样落。"""
        report = mo.judge(
            CARD,
            "running",
            [51, 12, 50],
            exempt=frozenset({12}),
            gh={
                12: GhResult(error="gh rc=1"),
                50: GhResult(status="MERGED"),
                51: GhResult(status="CLOSED"),
            },
        )
        assert report.card == CARD
        assert report.status == "running"
        assert [(v.pr, v.kind, v.detail) for v in report.verdicts] == [
            (12, mo.EXEMPT, ""),
            (50, mo.MERGED, ""),
            (51, mo.CLOSED, ""),
        ]

    def test_claimed_by_other_open_card(self) -> None:
        """优先级 3：另一张未结卡认领 → ``CLAIMED``，detail 是卡号升序逗号分隔。"""
        report = mo.judge(
            CARD,
            "running",
            [53],
            gh={53: GhResult(status="OPEN")},
            others=[
                (OTHER_B, "todo", "PR-CLAIM: #53"),
                (OTHER_A, "running", "PR-CLAIM: #53\nPR-CLAIM: #53"),
            ],
        )
        assert report.verdicts[0].kind == mo.CLAIMED
        assert report.verdicts[0].detail == f"{OTHER_A},{OTHER_B}"

    def test_unowned_ignores_self_done_archived_and_ref(self) -> None:
        """优先级 4：本卡自己认领不算；已结 / 归档 / 只引用也不算。"""
        report = mo.judge(
            CARD,
            "running",
            [53],
            gh={53: GhResult(status="OPEN")},
            others=[
                (CARD, "running", "PR-CLAIM: #53"),
                ("t_11111111", "done", "PR-CLAIM: #53"),
                ("t_22222222", "archived", "PR-CLAIM: #53"),
                ("t_33333333", "running", "PR-REF: #53"),
            ],
        )
        assert report.verdicts[0].kind == mo.UNOWNED
        assert report.verdicts[0].detail == ""

    def test_warn_beats_claim_and_missing_entry_is_warn(self) -> None:
        """优先级 5：取数失败 → ``WARN``（即使有卡认领），detail 是那句话。"""
        report = mo.judge(
            CARD,
            "running",
            [55],
            gh={55: GhResult(error="gh 超时 20s")},
            others=[(OTHER_A, "running", "PR-CLAIM: #55")],
        )
        assert (report.verdicts[0].kind, report.verdicts[0].detail) == (
            mo.WARN,
            "gh 超时 20s",
        )
        # gh 字典里根本没有这个 PR（CLI 没取到）→ 同样按取数失败算
        missing = mo.judge(CARD, "running", [7], gh={})
        assert missing.verdicts[0].kind == mo.WARN
        assert missing.verdicts[0].detail


class TestRender:
    def test_header_no_pr_and_four_conclusions(self) -> None:
        """无 PR 段 + 四种结论逐字 + ``EXEMPT`` / ``CLAIMED`` / ``CLOSED`` 行文案。"""
        no_pr = mo.judge(CARD, "running", [])
        assert mo.render(no_pr) == (
            f"卡 {CARD} [running]\n"
            "结论: 抽取到 0 个 PR 号 → 无合并归属需要（直接通过）"
        )

        all_ok = mo.judge(
            CARD,
            "running",
            [50, 51],
            exempt=frozenset({50}),
            gh={50: GhResult(status="MERGED"), 51: GhResult(status="OPEN")},
            others=[(OTHER_A, "running", "PR-CLAIM: #51")],
        )
        assert mo.render(all_ok) == "\n".join(
            [
                f"卡 {CARD} [running]",
                "PR #50 EXEMPT",
                f"PR #51 CLAIMED {OTHER_A}",
                "结论: 2 个 PR 全部有归属 → 可关",
            ]
        )

        unowned = mo.judge(
            CARD,
            "running",
            [50, 53, 55],
            gh={
                50: GhResult(status="CLOSED"),
                53: GhResult(status="OPEN"),
                55: GhResult(error="gh rc=1"),
            },
        )
        assert mo.render(unowned) == "\n".join(
            [
                f"卡 {CARD} [running]",
                "PR #50 CLOSED",
                "PR #53 UNOWNED",
                "PR #55 WARN gh rc=1",
                "结论: 3 个 PR / 通过 2 / 无归属 1 → "
                "先建收口卡（首行 PR-CLAIM: #<最小无归属号>）再关本卡",
            ]
        )

        degraded = mo.judge(
            CARD,
            "running",
            [50, 55],
            gh={
                50: GhResult(status="MERGED"),
                55: GhResult(error="gh 不在 PATH"),
            },
        )
        assert mo.render(degraded) == "\n".join(
            [
                f"卡 {CARD} [running]",
                "PR #50 MERGED",
                "PR #55 WARN gh 不在 PATH",
                "结论: gh 不可用（gh 不在 PATH）→ 降级放行（日志已写），晨报须列",
            ]
        )
        assert not mo.render(degraded).endswith("\n")


class TestExitCode:
    def test_three_states(self) -> None:
        """``UNOWNED`` → 1；无 PR / 全通过 / 降级 → 0；本模块永不返回 2。"""
        assert mo.exit_code(mo.judge(CARD, "running", [])) == 0
        assert (
            mo.exit_code(
                mo.judge(CARD, "running", [50], gh={50: GhResult(status="MERGED")})
            )
            == 0
        )
        assert (
            mo.exit_code(
                mo.judge(CARD, "running", [53], gh={53: GhResult(status="OPEN")})
            )
            == 1
        )
        mixed = mo.judge(
            CARD,
            "running",
            [53, 55],
            gh={53: GhResult(status="OPEN"), 55: GhResult(error="gh rc=1")},
        )
        assert mo.exit_code(mixed) == 1
        assert mo.exit_code(
            mo.judge(CARD, "running", [55], gh={55: GhResult(error="gh rc=1")})
        ) == 0


class TestParse:
    def test_parse_card(self) -> None:
        assert mo.parse_card(CARD) == CARD
        for bad in ("", "t_0123ABC", "t_0123abc", "t_0123abcde", "0123abcd", "t_0123abcd\n"):
            with pytest.raises(MergeOwnerError):
                mo.parse_card(bad)
        with pytest.raises(MergeOwnerError):
            mo.parse_card(None)  # type: ignore[arg-type]

    def test_parse_repo(self) -> None:
        assert mo.parse_repo("alsj213/team-board") == "alsj213/team-board"
        for bad in ("", "team-board", "a/b/c", "alsj213/team board", "alsj213/"):
            with pytest.raises(MergeOwnerError):
                mo.parse_repo(bad)
        with pytest.raises(MergeOwnerError):
            mo.parse_repo(None)  # type: ignore[arg-type]

    def test_parse_timeout(self) -> None:
        assert mo.parse_timeout("1") == mo.TIMEOUT_MIN
        assert mo.parse_timeout("600") == mo.TIMEOUT_MAX
        assert mo.parse_timeout("20") == mo.TIMEOUT_DEFAULT
        for bad in ("0", "-1", "601", "", "1793160000000", "99999999999999999999", " 20", "+20", "20s"):
            with pytest.raises(MergeOwnerError):
                mo.parse_timeout(bad)
        with pytest.raises(MergeOwnerError):
            mo.parse_timeout(20)  # type: ignore[arg-type]
