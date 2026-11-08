"""``unblockevidence`` 单测：证据补齐后的合并卡解封。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from team_board import unblockevidence

CARD = "t_1234abcd"
IMPL = "t_abcd1234"
HEAD = "a" * 40
PR = 12


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


def body() -> str:
    return (
        f"PR-CLAIM: #{PR}\n"
        f"## 收口：合并 PR #{PR}（{IMPL}）\n"
        f"- PR https://github.com/x/y/pull/{PR}（base=main，"
        f"head=wt/{IMPL} @ {HEAD}）\n"
        f"- 实现卡 {IMPL} 已 done\n"
    )


def make_board(
    path: Path,
    *,
    reason: str = "合并卡证据闸门：t_abcd1234：缺证据；tier：无 tier 事件",
    with_tier: bool = True,
) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,"
            " payload TEXT, run_id INTEGER, created_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, '收口：合并 PR #12', ?, 'blocked')",
            (CARD, body()),
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, '实现卡', '', 'done')",
            (IMPL,),
        )
        for run_id, profile in ((1, "pm"), (2, "qa")):
            conn.execute("INSERT INTO task_runs VALUES (?, ?, ?)", (run_id, IMPL, profile))
        conn.execute(
            "INSERT INTO task_events VALUES (1, ?, 'blocked', ?, NULL, 100)",
            (CARD, json.dumps({"reason": reason}, ensure_ascii=False)),
        )
        events = []
        if with_tier:
            events.append(("tier", json.dumps(
                {"tier": "B", "reason": "常规改动", "head_sha": HEAD}), 1))
        events.extend([
            ("qa_pass", json.dumps({"head_sha": HEAD, "run_id": 2}), 2),
            ("review", json.dumps(
                {"kind": "defect", "head_sha": HEAD, "verdict": "pass"}), 1),
            ("review", json.dumps(
                {"kind": "regression", "head_sha": HEAD, "verdict": "pass"}), 2),
        ])
        for index, (kind, payload, run_id) in enumerate(events, start=2):
            conn.execute(
                "INSERT INTO task_events VALUES (?, ?, ?, ?, ?, 100)",
                (index, IMPL, kind, payload, run_id),
            )
        conn.commit()
    finally:
        conn.close()
    return path


class TestCollect:
    def test_collects_when_evidence_now_complete(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(make_board(tmp_path / "b.db")))
        try:
            got = unblockevidence.collect(conn)
        finally:
            conn.close()
        assert [c.card for c in got] == [CARD]
        assert got[0].impl_card == IMPL
        assert got[0].pr == PR
        assert got[0].head == HEAD
        assert got[0].verdict == "PASS"

    def test_skips_when_tier_still_missing(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(make_board(tmp_path / "b.db", with_tier=False)))
        try:
            assert unblockevidence.collect(conn) == ()
        finally:
            conn.close()

    def test_skips_pr_dependency_reason(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(make_board(
            tmp_path / "b.db", reason="E1 未合 main：PR #12 仍 OPEN")))
        try:
            assert unblockevidence.collect(conn) == ()
        finally:
            conn.close()

    def test_skips_non_evidence_reason(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(make_board(
            tmp_path / "b.db", reason="需要凭据：tier 未写")))
        try:
            assert unblockevidence.collect(conn) == ()
        finally:
            conn.close()


class TestUnblock:
    def test_calls_hermes(self) -> None:
        runner = FakeRunner()
        unblockevidence.unblock(CARD, reason="ok", runner=runner)
        assert runner.calls == [
            ("hermes", "kanban", "--board", "devteam", "unblock", CARD, "--reason", "ok")
        ]

    def test_failure_raises(self) -> None:
        runner = FakeRunner(rc=1)
        try:
            unblockevidence.unblock(CARD, reason="ok", runner=runner)
        except unblockevidence.UnblockEvidenceError as exc:
            assert "unblock" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected UnblockEvidenceError")
