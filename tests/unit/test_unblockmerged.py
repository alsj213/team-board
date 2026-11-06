"""``unblockmerged`` 单测：PR 提取、依赖判定、候选扫描与 dry-run/apply。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from team_board import unblockmerged

CARD = "t_1234abcd"


class FakeProc:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class FakeRunner:
    def __init__(self, states: dict[int, str], unblock_rc: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._states = states
        self._unblock_rc = unblock_rc

    def __call__(self, argv: Sequence[str], cwd: str) -> Any:
        self.calls.append(tuple(argv))
        if argv[0] == "gh":
            pr = int(argv[3])
            state = self._states.get(pr, "UNKNOWN")
            return FakeProc(0, json.dumps({"state": state}))
        if argv[0] == "hermes":
            return FakeProc(self._unblock_rc, "", "boom" if self._unblock_rc else "")
        raise AssertionError(f"unexpected argv: {argv}")

    @property
    def unblock_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == "hermes"]


def make_board(path: Path, rows: list[tuple[str, str, str, str]], events: list[tuple[str, str]]) -> Path:
    """rows=id/title/assignee/status；events=task_id/payload。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT,"
            " status TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)"
        )
        for card_id, title, assignee, status in rows:
            conn.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, 100, 100, NULL)",
                (card_id, title, assignee, status),
            )
        for index, (task_id, payload) in enumerate(events, start=1):
            conn.execute(
                "INSERT INTO task_events VALUES (?, ?, 'blocked', ?)",
                (index, task_id, payload),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def payload(reason: str) -> str:
    return json.dumps({"reason": reason, "kind": "needs_input"}, ensure_ascii=False)


class TestExtract:
    @pytest.mark.parametrize(
        ("text", "want"),
        [
            ("E1 未合 main：PR #108 仍 OPEN", (108,)),
            ("PR #12 和 pr #3、PR#5", (3, 5, 12)),
            ("没有 PR 号", ()),
            ("PR #0", (0,)),
            ("PR #1234567890", ()),
        ],
    )
    def test_extract(self, text: str, want: tuple[int, ...]) -> None:
        assert unblockmerged.extract_prs(text) == want


class TestMergeDependency:
    @pytest.mark.parametrize(
        "reason",
        [
            "E1 未合 main：PR #108 仍 OPEN，等合入后继续",
            "等 PR #5 合并后 rebase",
            "PR #9 已 MERGED，但卡还被依赖挡着",
        ],
    )
    def test_true(self, reason: str) -> None:
        assert unblockmerged.is_merge_dependency(reason) is True

    @pytest.mark.parametrize(
        "reason",
        [
            "没有 PR 号",
            "PR #5 需要老板批准",
            "需要凭据：PR #5 未合",
            "PR #5 只是提到，没有合并语义",
        ],
    )
    def test_false(self, reason: str) -> None:
        assert unblockmerged.is_merge_dependency(reason) is False


class TestCollect:
    def test_collects_only_merge_dependency_blocks(self, tmp_path: Path) -> None:
        board = make_board(
            tmp_path / "b.db",
            [(CARD, "x", "engineer", "blocked"), ("t_9999abcd", "y", "qa", "blocked")],
            [
                (CARD, payload("E1 未合 main：PR #108 仍 OPEN")),
                ("t_9999abcd", payload("需要凭据：PR #5 未合")),
            ],
        )
        conn = sqlite3.connect(str(board))
        try:
            got = unblockmerged.collect(conn)
        finally:
            conn.close()
        assert [c.card for c in got] == [CARD]
        assert got[0].prs == (108,)


class TestPrStates:
    def test_states(self) -> None:
        runner = FakeRunner({108: "MERGED", 111: "OPEN"})
        assert unblockmerged.pr_states((108, 111), runner=runner) == {
            108: "MERGED",
            111: "OPEN",
        }

    def test_gh_failure_is_unknown(self) -> None:
        class Bad:
            def __call__(self, argv: Sequence[str], cwd: str) -> Any:
                return FakeProc(1, "", "nope")

        assert unblockmerged.pr_states((1,), runner=Bad()) == {1: "UNKNOWN"}


class DummyConn:
    def close(self) -> None:
        pass


class TestMain:
    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        candidates: tuple[unblockmerged.Candidate, ...],
        states: dict[int, str],
    ) -> FakeRunner:
        runner = FakeRunner(states)
        monkeypatch.setattr(unblockmerged.db, "connect", lambda path: DummyConn())
        monkeypatch.setattr(unblockmerged, "collect", lambda conn: candidates)
        monkeypatch.setattr(
            unblockmerged, "pr_states", lambda prs, **k: {pr: states.get(pr, "UNKNOWN") for pr in prs}
        )
        monkeypatch.setattr(
            unblockmerged,
            "unblock",
            lambda card, **k: runner(("hermes", "kanban", "unblock", card), "."),
        )
        return runner

    def test_no_candidates(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(unblockmerged.db, "connect", lambda path: DummyConn())
        monkeypatch.setattr(unblockmerged, "collect", lambda conn: ())
        assert unblockmerged.main(["--db", "/tmp/x"]) == 0
        assert "0 张候选" in capsys.readouterr().out

    def test_dry_run(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        candidate = unblockmerged.Candidate(CARD, "PR #108 未合", (108,))
        runner = self._patch(monkeypatch, (candidate,), {108: "MERGED"})
        assert unblockmerged.main(["--db", "/tmp/x"]) == 0
        out = capsys.readouterr().out
        assert "would unblock" in out
        assert runner.unblock_calls == []

    def test_apply(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        candidate = unblockmerged.Candidate(CARD, "PR #108 未合", (108,))
        runner = self._patch(monkeypatch, (candidate,), {108: "MERGED"})
        assert unblockmerged.main(["--db", "/tmp/x", "--apply"]) == 0
        assert "unblocked" in capsys.readouterr().out
        assert runner.unblock_calls == [("hermes", "kanban", "unblock", CARD)]

    def test_open_pr_is_skipped(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        candidate = unblockmerged.Candidate(CARD, "PR #108 未合", (108,))
        self._patch(monkeypatch, (candidate,), {108: "OPEN"})
        assert unblockmerged.main(["--db", "/tmp/x", "--apply"]) == 0
        assert "skip" in capsys.readouterr().out
