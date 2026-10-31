"""``scripts/install-card-template.sh`` 的行为测试：安装、``--check``、退出码。

判据全部落在子进程的 rc + stdout/stderr 上，脚本本身一行不 import ——
模板安装是给内核注入侧用的，跟 ``team_board`` 包无关。

隔离口径：每个用例都把 ``HERMES_KANBAN_HOME`` 指到 ``tmp_path``，默认目标路径
永远不碰真 home 下的 ``~/.hermes/templates/``。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "install-card-template.sh"
SOURCE = REPO_ROOT / "docs" / "card-template.md"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_script(args: list[str], kanban_home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    env.pop("HERMES_KANBAN_HOME", None)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


@pytest.fixture()
def kanban_home(tmp_path: Path) -> Path:
    """隔离的 ``HERMES_KANBAN_HOME``：默认目标落在它下面，不写真实 home。"""

    home = tmp_path / "kanban"
    home.mkdir()
    return home


def test_script_and_source_exist() -> None:
    assert SCRIPT.is_file(), f"脚本不存在: {SCRIPT}"
    assert SOURCE.is_file(), f"源模板不存在: {SOURCE}"


def test_install_copies_and_prints_both_hashes(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "nested" / "t.md"

    proc = run_script(["--dest", str(dest)], kanban_home)

    assert proc.returncode == 0, proc.stderr
    assert dest.is_file()
    assert dest.read_bytes() == SOURCE.read_bytes()
    want = sha256_of(SOURCE)
    assert want in proc.stdout
    # 两侧各一条 `<hash>  <path>`，两条哈希相同
    hash_lines = [line for line in proc.stdout.splitlines() if len(line) > 64 and line[64:66] == "  "]
    assert len(hash_lines) == 2, proc.stdout
    assert {line[:64] for line in hash_lines} == {want}
    assert str(dest) in proc.stdout
    assert "已安装：" in proc.stdout


def test_install_creates_missing_parent_directory(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "a" / "b" / "c" / "t.md"
    assert not dest.parent.exists()

    proc = run_script(["--dest", str(dest)], kanban_home)

    assert proc.returncode == 0, proc.stderr
    assert dest.is_file()


def test_install_uses_hermes_kanban_home_default(kanban_home: Path) -> None:
    proc = run_script([], kanban_home)

    assert proc.returncode == 0, proc.stderr
    default_dest = kanban_home / "templates" / "card-template.md"
    assert default_dest.is_file()
    assert default_dest.read_bytes() == SOURCE.read_bytes()

    check = run_script(["--check"], kanban_home)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "一致：" in check.stdout


def test_default_falls_back_to_hermes_home(tmp_path: Path) -> None:
    """``HERMES_KANBAN_HOME`` 未设时，默认落点跟内核一致：``$HERMES_HOME/templates``（不是 ~/.hermes/kanban）。"""

    hermes = tmp_path / "hermes-root"
    hermes.mkdir()
    env = {**os.environ}
    env.pop("HERMES_KANBAN_HOME", None)
    env["HERMES_HOME"] = str(hermes)
    env["HOME"] = str(tmp_path / "home")

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    dest = hermes / "templates" / "card-template.md"
    assert dest.is_file()
    assert dest.read_bytes() == SOURCE.read_bytes()


def test_check_after_install_is_clean(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "t.md"
    assert run_script(["--dest", str(dest)], kanban_home).returncode == 0

    proc = run_script(["--check", "--dest", str(dest)], kanban_home)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "一致：" in proc.stdout
    assert "漂移：" not in proc.stdout


def test_check_reports_drift_with_rc1(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "t.md"
    assert run_script(["--dest", str(dest)], kanban_home).returncode == 0
    dest.write_bytes(dest.read_bytes() + b"x")

    proc = run_script(["--check", "--dest", str(dest)], kanban_home)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "漂移：" in proc.stdout
    assert proc.stderr == ""
    # --check 不写盘：漂移的内容还在，没被"修好"
    assert dest.read_bytes().endswith(b"x")


def test_check_on_missing_target_rc1_and_hints_install(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "missing" / "t.md"

    proc = run_script(["--check", "--dest", str(dest)], kanban_home)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "缺失：" in combined
    assert "先运行安装" in combined
    assert "--check" in combined, "提示里必须说明安装时不带 --check"
    # 不写盘：连父目录都不该被建出来
    assert not dest.parent.exists()


def test_check_on_directory_target_is_io_error(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "adir"
    dest.mkdir()

    proc = run_script(["--check", "--dest", str(dest)], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "错误: " in proc.stderr


def test_unwritable_dest_rc2_single_stderr_line(tmp_path: Path, kanban_home: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    dest = blocker / "t.md"

    proc = run_script(["--dest", str(dest)], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    stderr_lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert len(stderr_lines) == 1, proc.stderr
    assert stderr_lines[0].startswith("错误: ")
    assert "Traceback" not in proc.stderr


def test_missing_dest_value_rc2(kanban_home: Path) -> None:
    proc = run_script(["--dest"], kanban_home)

    assert proc.returncode == 2
    assert len([line for line in proc.stderr.splitlines() if line.strip()]) == 1
    assert proc.stderr.startswith("错误: ")


def test_empty_dest_value_rc2(kanban_home: Path) -> None:
    proc = run_script(["--dest", ""], kanban_home)

    assert proc.returncode == 2
    assert proc.stderr.startswith("错误: ")
    assert "Traceback" not in proc.stderr


def test_unknown_option_rc2(kanban_home: Path) -> None:
    proc = run_script(["--nope"], kanban_home)

    assert proc.returncode == 2
    assert proc.stderr.startswith("错误: ")
    assert len([line for line in proc.stderr.splitlines() if line.strip()]) == 1


def test_dest_equals_form_works(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "t.md"

    proc = run_script([f"--dest={dest}"], kanban_home)

    assert proc.returncode == 0, proc.stderr
    assert dest.is_file()
