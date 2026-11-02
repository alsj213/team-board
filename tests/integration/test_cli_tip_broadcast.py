"""``tip-broadcast`` CLI 的退出码与输出（逻辑在 test_tip.py，这里只锁 CLI 契约）。"""

from __future__ import annotations

import json

import pytest

from team_board import cli, tip

TIP = "a" * 40
HEAD = "b" * 40


def report(*, behind: bool, dry_run: bool = True, failed: bool = False) -> tip.BroadcastReport:
    pr = tip.PrTip(
        number=91,
        head_ref="wt/t_1234abcd",
        head_oid=HEAD,
        base_ref="main",
        draft=False,
        behind=behind,
        update_rc=1 if failed else (0 if not dry_run and behind else None),
        update_detail="boom" if failed else "",
    )
    return tip.BroadcastReport(tip=TIP, prs=(pr,), dry_run=dry_run)


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_dry_run_behind_is_rc1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.tip, "broadcast", lambda *a, **k: report(behind=True))
    code, out, err = run(capsys, "tip-broadcast")
    assert (code, err) == (1, "")
    assert "dry-run" in out


def test_dry_run_clean_is_rc0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.tip, "broadcast", lambda *a, **k: report(behind=False))
    code, out, err = run(capsys, "tip-broadcast")
    assert (code, err) == (0, "")
    assert "已包含 tip" in out


def test_apply_success_is_rc0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli.tip, "broadcast", lambda *a, **k: report(behind=True, dry_run=False)
    )
    code, out, err = run(capsys, "tip-broadcast", "--apply")
    assert (code, err) == (0, "")
    assert "已更新 1 条分支" in out


def test_apply_failure_is_rc1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli.tip, "broadcast", lambda *a, **k: report(behind=True, dry_run=False, failed=True)
    )
    code, out, err = run(capsys, "tip-broadcast", "--apply")
    assert (code, err) == (1, "")
    assert "1 条 update-branch 失败" in out


def test_json_output(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.tip, "broadcast", lambda *a, **k: report(behind=True))
    code, out, _ = run(capsys, "tip-broadcast", "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["tip"] == TIP
    assert payload["prs"][0]["behind"] is True


def test_tip_error_is_rc2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise tip.TipError("gh 挂了")

    monkeypatch.setattr(cli.tip, "broadcast", boom)
    code, out, err = run(capsys, "tip-broadcast")
    assert code == 2
    assert out == ""
    assert "gh 挂了" in err
