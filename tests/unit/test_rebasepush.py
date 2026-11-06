"""``rebasepush`` 单测：纯 rebase 的机械判定与 force-with-lease 代推。"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from team_board import commits, rebasepush, reviewhead

CARD = "t_1234abcd"
BRANCH = "wt/t_1234abcd"
OLD = "a" * 40
NEW = "b" * 40
PATCH = "c" * 40
BASE = "origin/main"


class FakeProc:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class FakeRunner:
    """按 git 子命令回放；push 成功后远端 head 自动变成 NEW。"""

    def __init__(
        self,
        *,
        dirty: str = "",
        ancestor_rc: int = 0,
        merges: str = "",
        push_rc: int = 0,
        old: str = OLD,
        new: str = NEW,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._dirty = dirty
        self._ancestor_rc = ancestor_rc
        self._merges = merges
        self._push_rc = push_rc
        self._old = old
        self._new = new
        self._pushed = False

    def __call__(self, argv: Sequence[str], cwd: str) -> Any:
        self.calls.append(tuple(argv))
        cmd = argv[1] if len(argv) > 1 else ""
        if cmd == "status":
            return FakeProc(0, self._dirty)
        if cmd == "fetch":
            return FakeProc(0)
        if cmd == "rev-parse":
            target = argv[-1]
            if target == "HEAD":
                return FakeProc(0, self._new + "\n")
            return FakeProc(0, (self._new if self._pushed else self._old) + "\n")
        if cmd == "merge-base":
            return FakeProc(self._ancestor_rc)
        if cmd == "rev-list":
            return FakeProc(0, self._merges)
        if cmd == "push":
            self._pushed = self._push_rc == 0
            return FakeProc(self._push_rc, "", "push rejected" if self._push_rc else "")
        raise AssertionError(f"unexpected argv: {argv}")

    @property
    def push_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == "push"]


def fingerprint(stub: dict[str, str] | None = None) -> Any:
    values = stub or {OLD: PATCH, NEW: PATCH}

    def _fp(repo: str | Any, base: str, rev: str) -> Any:
        return SimpleNamespace(kind="patch", value=values[rev])

    return _fp


def audit_ok(commits_count: int = 1) -> Any:
    def _audit(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(ok=True, commits=tuple(range(commits_count)), problems=())

    return _audit


def audit_bad() -> Any:
    def _audit(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(ok=False, commits=(), problems=("x",))

    return _audit


def make_plan(
    runner: FakeRunner | None = None,
    *,
    fp: Any = None,
    audit: Any = None,
    worktree: str = "/tmp",
) -> rebasepush.Plan:
    return rebasepush.plan(
        CARD,
        worktree,
        runner=runner or FakeRunner(),
        fingerprint=fp or fingerprint(),
        audit=audit or audit_ok(),
    )


@pytest.fixture(autouse=True)
def _exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """``plan`` 会校验 worktree 目录存在；单测不建真目录。"""

    monkeypatch.setattr(rebasepush.os.path, "isdir", lambda _p: True)
    monkeypatch.setattr(rebasepush.os.path, "realpath", lambda p: p)


class TestPlan:
    def test_bad_card(self) -> None:
        with pytest.raises(rebasepush.RebasePushError, match="卡号非法"):
            rebasepush.plan("t_BAD", ".", runner=FakeRunner())

    def test_dirty_worktree_is_rejected(self) -> None:
        with pytest.raises(rebasepush.RebasePushError, match="未提交"):
            make_plan(FakeRunner(dirty=" M src/x.py"))

    def test_not_ancestor_is_rejected(self) -> None:
        with pytest.raises(rebasepush.RebasePushError, match="不是 HEAD 的祖先"):
            make_plan(FakeRunner(ancestor_rc=1))

    def test_merge_commit_is_rejected(self) -> None:
        with pytest.raises(rebasepush.RebasePushError, match="merge commit"):
            make_plan(FakeRunner(merges="deadbee"))

    def test_patch_change_is_rejected(self) -> None:
        with pytest.raises(rebasepush.RebasePushError, match="不是纯 rebase"):
            make_plan(fp=fingerprint({OLD: PATCH, NEW: "d" * 40}))

    def test_non_patch_fingerprint_is_rejected(self) -> None:
        def _fp(repo: Any, base: str, rev: str) -> Any:
            return SimpleNamespace(kind="binary", value="")

        with pytest.raises(rebasepush.RebasePushError, match="不是可代推的 patch"):
            make_plan(fp=_fp)

    def test_identity_failure_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(commits, "render_report", lambda _r: "bad identity")
        with pytest.raises(rebasepush.RebasePushError, match="bad identity"):
            make_plan(audit=audit_bad())

    def test_ok_plan(self) -> None:
        result = make_plan()
        assert result.card == CARD
        assert result.branch == BRANCH
        assert result.old_head == OLD
        assert result.new_head == NEW
        assert result.patch == PATCH
        assert result.commit_count == 1
        assert result.needs_push is True

    def test_same_head_needs_no_push(self) -> None:
        result = make_plan(FakeRunner(old=NEW, new=NEW))
        assert result.needs_push is False


class TestApply:
    def test_push_uses_explicit_lease_and_verifies(self) -> None:
        runner = FakeRunner()
        result = make_plan(runner)
        rebasepush.apply(result, "/tmp", runner=runner)
        assert runner.push_calls == [
            (
                "git",
                "push",
                f"--force-with-lease=refs/heads/{BRANCH}:{OLD}",
                "origin",
                f"HEAD:refs/heads/{BRANCH}",
            )
        ]

    def test_push_failure_raises(self) -> None:
        runner = FakeRunner(push_rc=1)
        result = make_plan(runner)
        with pytest.raises(rebasepush.RebasePushError, match="force-with-lease 失败"):
            rebasepush.apply(result, "/tmp", runner=runner)

    def test_same_head_is_noop(self) -> None:
        runner = FakeRunner(old=NEW, new=NEW)
        result = make_plan(runner)
        rebasepush.apply(result, "/tmp", runner=runner)
        assert runner.push_calls == []


class TestMain:
    def test_dry_run(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            rebasepush,
            "plan",
            lambda *a, **k: rebasepush.Plan(CARD, BRANCH, OLD, NEW, PATCH, 1, BASE),
        )
        assert rebasepush.main(["--card", CARD, "--worktree", "/tmp"]) == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "patch=" in out

    def test_apply_prints_pushed(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            rebasepush,
            "plan",
            lambda *a, **k: rebasepush.Plan(CARD, BRANCH, OLD, NEW, PATCH, 1, BASE),
        )
        monkeypatch.setattr(rebasepush, "apply", lambda *a, **k: None)
        assert rebasepush.main(["--card", CARD, "--worktree", "/tmp", "--apply"]) == 0
        assert "PUSHED" in capsys.readouterr().out

    def test_failure_rc1(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: Any, **k: Any) -> Any:
            raise rebasepush.RebasePushError("nope")

        monkeypatch.setattr(rebasepush, "plan", boom)
        assert rebasepush.main(["--card", CARD]) == 1
        assert "rebasepush: FAIL" in capsys.readouterr().err


def test_fingerprint_error_is_wrapped() -> None:
    def _fp(repo: Any, base: str, rev: str) -> Any:
        raise reviewhead.PatchIdError("boom")

    with pytest.raises(rebasepush.RebasePushError, match="算 patch 指纹失败"):
        make_plan(fp=_fp)
