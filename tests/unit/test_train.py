"""``merge-train`` 的单测：队列校验、CI 归一、预检 / 更新 / 证据 / 合并的顺序。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from team_board import evidence, reviewhead, train

HEAD = "a" * 40
NEW_HEAD = "b" * 40
PATCH = "c" * 40
CARD = "t_1234abcd"
BRANCH = f"wt/{CARD}"
BY = "repo/run200"


def view(
    *,
    head: str = HEAD,
    checks: str = "SUCCESS",
    mergeable: str = "MERGEABLE",
    draft: bool = False,
    pr: int = 42,
    head_ref: str = BRANCH,
    base_ref: str = "main",
) -> dict[str, object]:
    rollup = [] if checks == "NONE" else [{"conclusion": checks}]
    return {
        "number": pr,
        "state": "OPEN",
        "isDraft": draft,
        "headRefName": head_ref,
        "baseRefName": base_ref,
        "headRefOid": head,
        "mergeable": mergeable,
        "statusCheckRollup": rollup,
    }


def result(argv: list[str] | tuple[str, ...], rc: int = 0, out: str = "", err: str = ""):
    return train.CommandResult(argv=tuple(argv), returncode=rc, stdout=out, stderr=err)


class FakeRun:
    """假 gh/git：按 argv 回放预设结果。"""

    def __init__(
        self,
        *,
        views: list[dict[str, object]],
        ancestor: bool = True,
        update_rc: int = 0,
    ) -> None:
        self.views = list(views)
        self.ancestor = ancestor
        self.update_rc = update_rc
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, cwd):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[:3] == ("git", "remote", "get-url"):
            return result(argv, out="git@github.com:owner/repo.git\n")
        if argv[:2] == ("git", "fetch"):
            return result(argv)
        if argv[:2] == ("git", "merge-base"):
            return result(argv, rc=0 if self.ancestor else 1)
        if argv[:3] == ("gh", "pr", "view"):
            payload = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            return result(argv, out=json.dumps(payload))
        if argv[:2] == ("gh", "api"):
            return result(argv, rc=self.update_rc)
        return result(argv, rc=1, err=f"unexpected: {argv}")


def make_trainer(
    tmp_path: Path,
    fake: FakeRun,
    *,
    dry_run: bool = False,
    update_branch: bool = True,
    merge_rc: int = 0,
):
    merged: list[tuple[str, ...]] = []

    def merge_run(argv, cwd, env):
        merged.append(tuple(argv))
        return result(argv, rc=merge_rc)

    body = tmp_path / "body.txt"
    body.write_text("body\n", encoding="utf-8")
    trainer = train.Trainer(
        train.TrainOptions(
            repo=str(tmp_path),
            db_path=str(tmp_path / "kanban.db"),
            dry_run=dry_run,
            update_branch=update_branch,
            timeout=10,
            interval=0,
        ),
        run=fake,
        fingerprint=lambda repo, base, head: reviewhead.Fingerprint(
            kind="patch", value=PATCH, merge_base="d" * 40, paths=(), algo=reviewhead.FINGERPRINT_ALGO
        ),
        judge=lambda card, pr, head, patch: evidence.Judgment(
            verdict=evidence.VERDICT_PASS, slots=(), missing=(), detail=""
        ),
        merge_run=merge_run,
        sleep=lambda _: None,
        clock=lambda: 0,
    )
    item = train.QueueItem(
        pr=42, card=CARD, branch=BRANCH, by=BY, base="main", body_file=str(body)
    )
    return trainer, item, merged


class TestQueue:
    def _write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "queue.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_array_and_wrapper(self, tmp_path: Path) -> None:
        row = {"pr": 42, "card": CARD, "branch": BRANCH, "by": BY}
        for payload in ([row], {"queue": [row]}):
            items = train.load_queue(self._write(tmp_path, payload))
            assert len(items) == 1
            assert items[0].pr == 42
            assert items[0].merge_subject == f"Merge PR #42（{CARD}）"

    def test_rejects_duplicate_pr(self, tmp_path: Path) -> None:
        row = {"pr": 42, "card": CARD, "branch": BRANCH, "by": BY}
        path = self._write(tmp_path, [row, {**row, "card": "t_ffffffff", "branch": "wt/t_ffffffff"}])
        with pytest.raises(train.TrainError, match="重复"):
            train.load_queue(path)

    def test_rejects_unknown_key(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            [{"pr": 42, "card": CARD, "branch": BRANCH, "by": BY, "extra": 1}],
        )
        with pytest.raises(train.TrainError, match="未知键"):
            train.load_queue(path)

    def test_rejects_non_repo_by(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path, [{"pr": 42, "card": CARD, "branch": BRANCH, "by": "pm/run1"}]
        )
        with pytest.raises(train.TrainError, match="repo/"):
            train.load_queue(path)

    def test_rejects_branch_card_mismatch(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            [{"pr": 42, "card": CARD, "branch": "wt/t_ffffffff", "by": BY}],
        )
        with pytest.raises(train.TrainError, match="branch"):
            train.load_queue(path)


class TestChecks:
    @pytest.mark.parametrize(
        ("rollup", "expected"),
        [
            ([{"conclusion": "SUCCESS"}], "green"),
            ([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}], "green"),
            ([{"conclusion": "FAILURE"}], "red"),
            ([{"conclusion": "SUCCESS"}, {"conclusion": "CANCELLED"}], "red"),
            ([{"status": "IN_PROGRESS"}], "pending"),
            ([{"conclusion": None}], "pending"),
            ([], "none"),
            (None, "none"),
            ([{"conclusion": "SOMETHING_NEW"}], "pending"),
        ],
    )
    def test_states(self, rollup: object, expected: str) -> None:
        assert train.checks_state(rollup) == expected

    def test_pr_view_rejects_bad_head(self) -> None:
        payload = view()
        payload["headRefOid"] = "XYZ"
        with pytest.raises(train.TrainError, match="headRefOid"):
            train.parse_pr_view(payload, 42)


class TestTrainer:
    def test_happy_path_merges(self, tmp_path: Path) -> None:
        fake = FakeRun(views=[view()])
        trainer, item, merged = make_trainer(tmp_path, fake)
        step = trainer.one(item)
        assert step.status == "merged"
        assert step.patch == PATCH
        assert len(merged) == 1
        assert not any(argv[:2] == ("gh", "api") for argv in fake.calls)

    def test_dry_run_does_not_merge(self, tmp_path: Path) -> None:
        fake = FakeRun(views=[view()])
        trainer, item, merged = make_trainer(tmp_path, fake, dry_run=True)
        step = trainer.one(item)
        assert step.status == "dry-run"
        assert merged == []

    def test_update_branch_then_merge(self, tmp_path: Path) -> None:
        fake = FakeRun(views=[view(head=HEAD), view(head=NEW_HEAD), view(head=NEW_HEAD)], ancestor=False)
        trainer, item, merged = make_trainer(tmp_path, fake)
        step = trainer.one(item)
        assert step.status == "merged"
        assert step.head == NEW_HEAD
        assert any(argv[:2] == ("gh", "api") for argv in fake.calls)
        assert len(merged) == 1

    def test_behind_without_update_flag_bounces(self, tmp_path: Path) -> None:
        fake = FakeRun(views=[view()], ancestor=False)
        trainer, item, _ = make_trainer(tmp_path, fake, update_branch=False)
        with pytest.raises(train.TrainError, match="update-branch"):
            trainer.one(item)

    def test_red_ci_bounces_before_merge(self, tmp_path: Path) -> None:
        fake = FakeRun(views=[view(checks="FAILURE")])
        trainer, item, merged = make_trainer(tmp_path, fake)
        with pytest.raises(train.TrainError, match="CI"):
            trainer.one(item)
        assert merged == []

    def test_run_train_continues_after_bounce(self, tmp_path: Path) -> None:
        fake = FakeRun(views=[view(checks="FAILURE"), view()], ancestor=True)
        trainer, item, _ = make_trainer(tmp_path, fake)
        # 第一条红，第二条绿；view() 会被 pop 一次给第一条，随后复用第二条
        results = train.run_train([item], train.TrainOptions(repo=str(tmp_path)), trainer=trainer)
        assert len(results) == 1
        assert results[0].status == "bounced"
