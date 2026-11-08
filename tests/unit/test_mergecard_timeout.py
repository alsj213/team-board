"""子进程超时兜底：sweep 不因 gh/hermes 挂起而卡死 systemd oneshot。"""

from __future__ import annotations

import subprocess

import pytest

from team_board import cli, mergecard, train


def test_mergecard_create_timeout_becomes_rc124(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], mergecard.CREATE_TIMEOUT)

    monkeypatch.setattr(mergecard.subprocess, "run", boom)
    proc = mergecard._default_runner(("hermes", "kanban", "create", "x"))
    assert proc.returncode == 124
    assert "超时" in proc.stderr


def test_gh_pr_view_timeout_raises_train_error(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], cli.GH_PR_VIEW_TIMEOUT)

    monkeypatch.setattr(cli.subprocess, "run", boom)
    with pytest.raises(train.TrainError) as ei:
        cli._gh_pr_view("/repo", "o/r", 119)
    assert "gh pr view #119 超时" in str(ei.value)
