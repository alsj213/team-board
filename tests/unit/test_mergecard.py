"""``mergecard`` 单测：实现卡 done 前置、幂等、正文首行认领与建卡 argv。"""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from typing import Any

import pytest

from team_board import mergecard

IMPL = "t_1234abcd"
PR = 90
HEAD = "a" * 40
SLUG = "owner/repo"


def pending(*rows: tuple[str, str, str, str, str]) -> list[tuple[str, str, str, str, str]]:
    return list(rows)


def plan(**overrides: Any) -> mergecard.MergeCardPlan:
    kwargs: dict[str, Any] = {
        "impl_card": IMPL,
        "impl_status": "done",
        "impl_title": "CI 闸门",
        "pr": PR,
        "pr_state": "OPEN",
        "head_oid": HEAD,
        "slug": SLUG,
        "pending_rows": pending(),
    }
    kwargs.update(overrides)
    return mergecard.plan(**kwargs)


class TestImplFromBranch:
    @pytest.mark.parametrize("branch", ["wt/t_1234abcd", "t_1234abcd", " wt/t_1234abcd "])
    def test_valid(self, branch: str) -> None:
        assert mergecard.impl_from_branch(branch) == IMPL

    @pytest.mark.parametrize("branch", ["main", "wt/t_1234ABC", "wt/t_1234abcd/extra", ""])
    def test_invalid(self, branch: str) -> None:
        with pytest.raises(mergecard.MergeCardError, match="不是卡分支"):
            mergecard.impl_from_branch(branch)


class TestPlan:
    def test_done_and_open_creates_plan(self) -> None:
        card = plan()
        assert card.should_create is True
        assert card.existing is None
        assert card.body.splitlines()[0] == f"PR-CLAIM: #{PR}"
        argv = mergecard.create_argv(card, "/tmp/b.md")
        assert argv[argv.index("--parent") + 1] == IMPL
        assert card.idempotency_key == f"merge-card-{IMPL}-{PR}"

    def test_impl_not_done_is_rejected(self) -> None:
        with pytest.raises(mergecard.MergeCardError, match="只有 done"):
            plan(impl_status="running")

    def test_pr_not_open_is_rejected(self) -> None:
        with pytest.raises(mergecard.MergeCardError, match="只有 OPEN"):
            plan(pr_state="MERGED")

    def test_bad_head_is_rejected(self) -> None:
        with pytest.raises(mergecard.MergeCardError, match="40 位"):
            plan(head_oid="abc")

    def test_existing_merge_card_is_idempotent(self) -> None:
        rows = pending(
            ("t_deadbeef", "ready", "repo", f"收口：合并 PR #{PR}（{IMPL}）", f"PR-CLAIM: #{PR}")
        )
        card = plan(pending_rows=rows)
        assert card.existing == "t_deadbeef"
        assert card.should_create is False

    def test_other_open_claim_is_rejected(self) -> None:
        rows = pending(
            ("t_deadbeef", "running", "engineer", "实现卡", f"PR-CLAIM: #{PR}")
        )
        with pytest.raises(mergecard.MergeCardError, match="仍被未结卡"):
            plan(pending_rows=rows)

    def test_custom_branch_and_title(self) -> None:
        card = plan(branch="wt/t_1234abcd", title="自定义")
        assert card.title == "自定义"
        assert "head=wt/t_1234abcd" in card.body


class TestCheckImplDone:
    def test_done_passes(self) -> None:
        mergecard.check_impl_done(IMPL, "done")  # 不抛就是通过

    @pytest.mark.parametrize("status", ["running", "ready", "blocked", ""])
    def test_not_done_raises(self, status: str) -> None:
        with pytest.raises(mergecard.MergeCardError, match="只有 done"):
            mergecard.check_impl_done(IMPL, status)

    def test_constant_is_done(self) -> None:
        assert mergecard.IMPL_DONE == "done"


class TestBodyCommands:
    """qa F1：正文里的两条命令必须**照抄即能跑**（不是裸 ``merge_guard``）。"""

    def commands(self, card: mergecard.MergeCardPlan) -> list[str]:
        return [ln for ln in card.body.splitlines() if ln.startswith("cd ")]

    def test_two_executable_commands(self) -> None:
        cmds = self.commands(plan())
        assert len(cmds) == 2
        record, merge = cmds
        assert " python3 -m team_board.cli merge-record" in record
        assert " python3 -m team_board.merge_guard" in merge
        assert "--delete-branch" in merge
        assert "--method merge" in merge

    def test_no_bare_merge_guard(self) -> None:
        for line in plan().body.splitlines():
            head = line.strip().split(" ")[0] if line.strip() else ""
            assert head not in {"merge_guard", "team-board"}, line

    def test_absolute_repo_and_body_file(self) -> None:
        cmds = self.commands(plan(repo="/home/liu/project/team-board"))
        for cmd in cmds:
            assert "cd /home/liu/project/team-board &&" in cmd
            assert "PYTHONPATH=/home/liu/project/team-board/src" in cmd
        record, merge = cmds
        assert f"--body-out /tmp/{IMPL}-merge-{PR}.body" in record
        assert f"--body-file /tmp/{IMPL}-merge-{PR}.body" in merge
        assert "--repo /home/liu/project/team-board" in merge

    def test_repo_path_with_space_is_quoted(self) -> None:
        merge = self.commands(plan(repo="/tmp/a b"))[1]
        assert merge.startswith("cd '/tmp/a b' && PYTHONPATH='/tmp/a b/src'")
        assert "--repo '/tmp/a b'" in merge

    def test_subject_is_quoted_single_token(self) -> None:
        merge = self.commands(plan(impl_title="fix: 空格 与 $HOME 都算标题"))[1]
        tokens = shlex.split(merge)
        subject = tokens[tokens.index("--subject") + 1]
        assert subject == f"Merge PR #{PR}（{IMPL}）：fix: 空格 与 $HOME 都算标题"
        assert len(merge.splitlines()) == 1

    def test_body_fits_2048_bytes(self) -> None:
        long_repo = "/home/liu/project/some-really-long-checkout-name/team-board"
        body = plan(repo=long_repo).body
        assert len(body.encode("utf-8")) <= 2048


class TestCreatedId:
    def test_json_id(self) -> None:
        assert mergecard.parse_created_id(json.dumps({"id": "t_abcd1234"})) == "t_abcd1234"

    def test_json_task_id(self) -> None:
        assert mergecard.parse_created_id(json.dumps({"task_id": "t_abcd1234"})) == "t_abcd1234"

    def test_text_fallback(self) -> None:
        assert mergecard.parse_created_id("Created t_abcd1234 (ready)") == "t_abcd1234"

    def test_unparseable(self) -> None:
        assert mergecard.parse_created_id("nope") is None


class FakeProc:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class TestCreate:
    def test_create_ok(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(argv: Sequence[str]):
            calls.append(tuple(argv))
            return FakeProc(out=json.dumps({"id": "t_abcd1234"}))

        result = mergecard.create_merge_card(plan(), body_path="/tmp/b.md", runner=runner)
        assert result.ok is True
        assert result.card_id == "t_abcd1234"
        assert calls[0][:5] == ("hermes", "kanban", "--board", "devteam", "create")

    def test_create_failure_has_no_id(self) -> None:
        result = mergecard.create_merge_card(
            plan(), body_path="/tmp/b.md", runner=lambda argv: FakeProc(rc=2, err="boom")
        )
        assert result.ok is False
        assert result.card_id is None
        assert "boom" in result.stderr
