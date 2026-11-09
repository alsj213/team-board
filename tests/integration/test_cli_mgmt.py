"""``mgmt`` 子命令：rc、输出与 --files-only 的写/不写边界。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from team_board import cli


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    home = tmp_path / "home"
    (root / "ops" / "scripts" / "ops").mkdir(parents=True)
    (root / "ops" / "systemd").mkdir()
    source = root / "ops" / "scripts" / "ops" / "a.sh"
    source.write_text("echo a\n", encoding="utf-8")
    source.chmod(0o755)
    (root / "ops" / "manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "source": "ops/scripts/ops/a.sh",
                        "target": "~/.hermes/profiles/ops/scripts/a.sh",
                        "mode": "0755",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return root, home


def run(
    capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_check_reports_missing_then_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, home = make_repo(tmp_path)
    code, out, err = run(
        capsys, "mgmt", "check", "--files-only", "--root", str(root), "--home", str(home)
    )
    assert (code, err) == (1, "")
    assert "target-missing" in out

    code, out, _ = run(
        capsys, "mgmt", "install", "--root", str(root), "--home", str(home)
    )
    assert (code, out.splitlines()[-1]) == (0, "未变 0 件，动作 1 条")

    code, out, err = run(
        capsys, "mgmt", "check", "--files-only", "--root", str(root), "--home", str(home)
    )
    assert (code, err) == (0, "")
    assert "结论: 通过" in out


def test_check_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, home = make_repo(tmp_path)
    code, out, _ = run(
        capsys,
        "mgmt",
        "check",
        "--files-only",
        "--json",
        "--root",
        str(root),
        "--home",
        str(home),
    )
    assert code == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["files"]["drifts"][0]["code"] == "target-missing"


def test_install_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, home = make_repo(tmp_path)
    code, out, err = run(
        capsys,
        "mgmt",
        "install",
        "--dry-run",
        "--root",
        str(root),
        "--home",
        str(home),
    )
    assert (code, err) == (0, "")
    assert "would-write" in out
    assert not (home / ".hermes/profiles/ops/scripts/a.sh").exists()


def test_capture_updates_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, home = make_repo(tmp_path)
    run(capsys, "mgmt", "install", "--root", str(root), "--home", str(home))
    target = home / ".hermes/profiles/ops/scripts/a.sh"
    target.write_text("echo live\n", encoding="utf-8")

    code, out, err = run(
        capsys, "mgmt", "capture", "--root", str(root), "--home", str(home)
    )
    assert (code, err) == (0, "")
    assert "updated: ops/scripts/ops/a.sh" in out
    assert (root / "ops/scripts/ops/a.sh").read_text(encoding="utf-8") == "echo live\n"


def test_bad_manifest_is_rc2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, home = make_repo(tmp_path)
    (root / "ops" / "manifest.json").write_text("{", encoding="utf-8")
    code, out, err = run(
        capsys, "mgmt", "check", "--files-only", "--root", str(root), "--home", str(home)
    )
    assert (code, out) == (2, "")
    assert err.startswith("错误: ")
