"""``tierhandback`` 单测：候选判据、tier 合法性、dry-run/apply。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from team_board import tierhandback

CARD = "t_1234abcd"
HEAD = "a" * 40


class FakeProc:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class FakeRunner:
    def __init__(self, rc: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._rc = rc

    def __call__(self, argv: Sequence[str], cwd: str) -> Any:
        self.calls.append(tuple(argv))
        return FakeProc(self._rc, "", "boom" if self._rc else "")


def make_board(
    path: Path,
    *,
    status: str = "review",
    assignee: str = "pm",
    current_run_id: int | None = None,
    body: str = "## 验收标准\nQA-SPEC: qa\n",
    events: list[tuple[str, str, int | None]] | None = None,
    runs: dict[int, str] | None = None,
) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT,"
            " assignee TEXT, status TEXT, current_run_id INTEGER,"
            " created_at INTEGER, started_at INTEGER, completed_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT,"
            " kind TEXT, payload TEXT, run_id INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, 'x', ?, ?, ?, ?, 100, 100, NULL)",
            (CARD, body, assignee, status, current_run_id),
        )
        for run_id, profile in (runs or {}).items():
            conn.execute("INSERT INTO task_runs VALUES (?, ?, ?)", (run_id, CARD, profile))
        for index, (kind, payload, event_run_id) in enumerate(events or [], start=1):
            conn.execute(
                "INSERT INTO task_events VALUES (?, ?, ?, ?, ?)",
                (index, CARD, kind, payload, event_run_id),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def tier_payload(tier: str = "B", head: str = HEAD) -> str:
    return json.dumps({"tier": tier, "reason": "常规改动", "head_sha": head},
                      ensure_ascii=False)


class TestCollect:
    def test_collects_tier_last_from_pm(self, tmp_path: Path) -> None:
        board = make_board(
            tmp_path / "b.db",
            events=[("review_requested", "{}", 1), ("tier", tier_payload(), 2)],
            runs={1: "qa", 2: "pm"},
        )
        conn = sqlite3.connect(str(board))
        try:
            got = tierhandback.collect(conn)
        finally:
            conn.close()
        assert [c.card for c in got] == [CARD]
        assert got[0].tier == "B"

    def test_skips_when_qa_requested_pm_after_tier(self, tmp_path: Path) -> None:
        board = make_board(
            tmp_path / "b.db",
            events=[
                ("tier", tier_payload(), 2),
                ("review_requested", "{\"reviewer\":\"pm\"}", 3),
            ],
            runs={2: "pm", 3: "qa"},
        )
        conn = sqlite3.connect(str(board))
        try:
            assert tierhandback.collect(conn) == ()
        finally:
            conn.close()

    def test_skips_active_run_or_wrong_owner(self, tmp_path: Path) -> None:
        cases: tuple[dict[str, Any], ...] = (
            {"current_run_id": 9},
            {"assignee": "qa"},
            {"status": "running"},
            {"body": "没有签署行\n"},
        )
        for index, kwargs in enumerate(cases):
            board = make_board(
                tmp_path / f"b{index}.db",
                events=[("tier", tier_payload(), 2)],
                runs={2: "pm"},
                **kwargs,
            )
            conn = sqlite3.connect(str(board))
            try:
                assert tierhandback.collect(conn) == (), kwargs
            finally:
                conn.close()

    def test_skips_invalid_tier_payload_or_role(self, tmp_path: Path) -> None:
        bad_payloads = [
            ("tier", json.dumps({"tier": "D", "reason": "x", "head_sha": HEAD}), 2),
            ("tier", json.dumps({"tier": "B", "head_sha": HEAD}), 2),
            ("tier", tier_payload(), 2),
        ]
        for index, (kind, payload, run_id) in enumerate(bad_payloads):
            profile = "engineer" if index == 2 else "pm"
            board = make_board(
                tmp_path / f"b{index}.db",
                events=[(kind, payload, run_id)],
                runs={run_id: profile},
            )
            conn = sqlite3.connect(str(board))
            try:
                assert tierhandback.collect(conn) == (), (kind, payload, profile)
            finally:
                conn.close()


class TestHandoff:
    def test_assigns_qa(self) -> None:
        runner = FakeRunner()
        tierhandback.handoff(CARD, runner=runner)
        assert runner.calls == [
            ("hermes", "kanban", "--board", "devteam", "assign", CARD, "qa")
        ]

    def test_failure_raises(self) -> None:
        runner = FakeRunner(rc=1)
        try:
            tierhandback.handoff(CARD, runner=runner)
        except tierhandback.TierHandbackError as exc:
            assert "assign" in str(exc)
        else:  # pragma: no cover - 失败分支必须抛
            raise AssertionError("expected TierHandbackError")
