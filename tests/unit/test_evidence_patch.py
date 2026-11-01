"""``evidence patch``：给 worker 一条可直接命令替换的内容指纹入口。"""

from __future__ import annotations

import json

import pytest

from team_board import cli, reviewhead

PATCH = "c" * 40


def _fp(kind: str = "patch", value: str | None = PATCH) -> reviewhead.Fingerprint:
    return reviewhead.Fingerprint(
        kind=kind,
        value=value,
        merge_base="d" * 40,
        paths=(),
        algo=reviewhead.FINGERPRINT_ALGO,
    )


def test_prints_bare_value(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(reviewhead, "fingerprint", lambda *a, **k: _fp())
    rc = cli.main(["evidence", "patch", "--repo", ".", "--base", "origin/main", "--head", "HEAD"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == PATCH + "\n"
    assert err == ""


def test_json_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(reviewhead, "fingerprint", lambda *a, **k: _fp())
    rc = cli.main(
        ["evidence", "patch", "--repo", ".", "--base", "origin/main", "--head", "HEAD", "--json"]
    )
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert rc == 0
    assert payload["kind"] == "patch"
    assert payload["value"] == PATCH
    assert payload["algo"] == reviewhead.FINGERPRINT_ALGO
    assert err == ""


@pytest.mark.parametrize("kind", ["empty", "binary", "unfetchable"])
def test_unusable_kind_rc1(
    kind: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(reviewhead, "fingerprint", lambda *a, **k: _fp(kind=kind, value=None))
    rc = cli.main(["evidence", "patch", "--repo", ".", "--base", "origin/main", "--head", "HEAD"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert kind in err


def test_fingerprint_error_rc2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args, **kwargs):
        raise reviewhead.PatchIdError("no merge base")

    monkeypatch.setattr(reviewhead, "fingerprint", boom)
    rc = cli.main(["evidence", "patch", "--repo", ".", "--base", "origin/main", "--head", "HEAD"])
    out, err = capsys.readouterr()
    assert rc == 2
    assert out == ""
    assert "内容指纹" in err
