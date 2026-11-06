"""``cli._same_patch``：纯 rebase 时合并卡 sweep 的内容等价判定。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from team_board import cli, reviewhead

OLD = "a" * 40
NEW = "b" * 40
PATCH = "c" * 40


def fp(value: str | None, kind: str = "patch") -> Any:
    return SimpleNamespace(kind=kind, value=value)


def patch_fps(monkeypatch: pytest.MonkeyPatch, old: Any, new: Any) -> None:
    monkeypatch.setattr(reviewhead, "fingerprint_of_commit", lambda *a, **k: old)
    monkeypatch.setattr(reviewhead, "fingerprint", lambda *a, **k: new)


def test_same_patch_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fps(monkeypatch, fp(PATCH), fp(PATCH))
    assert cli._same_patch("/repo", OLD, NEW) == (True, PATCH)


def test_different_patch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fps(monkeypatch, fp(PATCH), fp("d" * 40))
    same, detail = cli._same_patch("/repo", OLD, NEW)
    assert same is False
    assert "patch" in detail


def test_missing_old_head_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fps(monkeypatch, None, fp(PATCH))
    same, detail = cli._same_patch("/repo", OLD, NEW)
    assert same is False
    assert "取不到对象" in detail


def test_non_patch_kind_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fps(monkeypatch, fp("", "binary"), fp(PATCH))
    same, detail = cli._same_patch("/repo", OLD, NEW)
    assert same is False
    assert "不是 patch" in detail


def test_fingerprint_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise reviewhead.PatchIdError("boom")

    monkeypatch.setattr(reviewhead, "fingerprint_of_commit", boom)
    same, detail = cli._same_patch("/repo", OLD, NEW)
    assert same is False
    assert "算 patch 指纹失败" in detail
