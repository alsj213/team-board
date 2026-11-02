"""``merge-card`` CLI 集成测试：done 前置、幂等、dry-run 与建卡调用。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from team_board import cli, mergecard

IMPL = "t_1234abcd"
PR = 90
HEAD = "a" * 40


def make_db(path: Path, rows: list[tuple[str, str, str, str, str]]) -> Path:
    """``rows`` 为 id/status/assignee/title/body。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, status TEXT, assignee TEXT, title TEXT, body TEXT)"
        )
        conn.executemany(
            "INSERT INTO tasks (id, status, assignee, title, body) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def done_board(tmp_path: Path) -> Path:
    return make_db(
        tmp_path / "done.db",
        [(IMPL, "done", "engineer", "CI 闸门", "正文")],
    )


@pytest.fixture(autouse=True)
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.train, "repo_slug", lambda repo, remote, run: "owner/repo")
    monkeypatch.setattr(
        cli,
        "_gh_pr_view",
        lambda repo, slug, pr: {"state": "OPEN", "head": HEAD, "title": "CI 闸门"},
    )


class TestPreconditions:
    def test_impl_running_is_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        board = make_db(tmp_path / "running.db", [(IMPL, "running", "engineer", "x", "")])
        code, out, err = run(
            capsys, "merge-card", "--impl-card", IMPL, "--pr", str(PR), "--db", str(board)
        )
        assert code == 1
        assert out == ""
        assert "只有 done" in err

    def test_existing_merge_card_is_idempotent(
        self, done_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 再插一张开放合并卡
        conn = sqlite3.connect(done_board)
        try:
            conn.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                ("t_deadbeef", "ready", "repo", f"收口：合并 PR #{PR}", f"PR-CLAIM: #{PR}"),
            )
            conn.commit()
        finally:
            conn.close()
        code, out, err = run(
            capsys, "merge-card", "--impl-card", IMPL, "--pr", str(PR), "--db", str(done_board)
        )
        assert (code, err) == (0, "")
        assert "已存在开放合并卡: t_deadbeef" in out


class TestDryRun:
    def test_dry_run_prints_plan(
        self, done_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(
            capsys,
            "merge-card",
            "--impl-card",
            IMPL,
            "--pr",
            str(PR),
            "--db",
            str(done_board),
            "--dry-run",
        )
        assert (code, err) == (0, "")
        assert "合并卡计划（dry-run）" in out
        assert f"PR-CLAIM: #{PR}" in out

    def test_dry_run_json(
        self, done_board: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run(
            capsys,
            "merge-card",
            "--impl-card",
            IMPL,
            "--pr",
            str(PR),
            "--db",
            str(done_board),
            "--dry-run",
            "--json",
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["dry_run"] is True
        assert payload["impl_card"] == IMPL
        assert payload["body"].startswith(f"PR-CLAIM: #{PR}")


class TestCreate:
    def test_create_calls_adapter(
        self,
        done_board: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_create(plan: mergecard.MergeCardPlan, *, body_path: str):
            captured["plan"] = plan
            captured["body_path"] = body_path
            captured["body"] = Path(body_path).read_text(encoding="utf-8")
            return mergecard.CreateResult(
                plan=plan, rc=0, card_id="t_abcd1234", stdout="", stderr=""
            )

        monkeypatch.setattr(mergecard, "create_merge_card", fake_create)
        code, out, err = run(
            capsys, "merge-card", "--impl-card", IMPL, "--pr", str(PR), "--db", str(done_board)
        )
        assert (code, err) == (0, "")
        assert "已建合并卡: t_abcd1234" in out
        assert captured["plan"].impl_card == IMPL
        assert captured["body"].splitlines()[0] == f"PR-CLAIM: #{PR}"

    def test_create_failure_is_rc2(
        self,
        done_board: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_create(plan: mergecard.MergeCardPlan, *, body_path: str):
            return mergecard.CreateResult(
                plan=plan, rc=2, card_id=None, stdout="", stderr="guard blocked"
            )

        monkeypatch.setattr(mergecard, "create_merge_card", fake_create)
        code, out, err = run(
            capsys, "merge-card", "--impl-card", IMPL, "--pr", str(PR), "--db", str(done_board)
        )
        assert code == 2
        assert out == ""
        assert "建合并卡失败" in err
