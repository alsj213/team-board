"""``scripts/install-card-template.sh`` 的行为测试：安装、``--check``、退出码。

判据全部落在子进程的 rc + stdout/stderr 上，脚本本身一行不 import ——
模板安装是给内核注入侧用的，跟 ``team_board`` 包无关。

隔离口径：每个用例都把 ``HERMES_KANBAN_HOME`` 指到 ``tmp_path``，默认目标路径
永远不碰真 home 下的 ``~/.hermes/templates/``。

rc 契约（脚本头写死，这里逐条钉住）：
    0  已装且两侧 sha256 一致 / --check 无漂移
    1  仅 --check：漂移或目标不存在
    2  参数或 IO 错误，且 stderr **恰好一行** `错误: …`、零 traceback
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

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_script(
    args: list[str],
    kanban_home: Path | None = None,
    *,
    env_override: dict[str, str] | None = None,
    env_drop: tuple[str, ...] = ("HERMES_KANBAN_HOME",),
    cwd: Path | None = None,
    prelude: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """跑脚本。

    ``kanban_home`` 非 None 时设 ``HERMES_KANBAN_HOME``；``env_drop`` 先从继承环境里
    删掉的键（默认删 ``HERMES_KANBAN_HOME``，避免宿主环境串味）；
    ``prelude`` 是包在同一个 bash 里、脚本之前的一段 shell（例 ``ulimit -f 0;``）。
    """
    env = {**os.environ}
    for key in env_drop:
        env.pop(key, None)
    if kanban_home is not None:
        env["HERMES_KANBAN_HOME"] = str(kanban_home)
    if env_override:
        env.update(env_override)

    if prelude is None:
        cmd = ["bash", str(SCRIPT), *args]
    else:
        cmd = ["bash", "-c", prelude + ' exec bash "$0" "$@"', str(SCRIPT), *args]

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=None if cwd is None else str(cwd),
        check=False,
        timeout=60,
    )


def stderr_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in proc.stderr.splitlines() if line.strip()]


def assert_single_error_line(proc: subprocess.CompletedProcess[str]) -> str:
    """rc2 契约：stderr 恰好一行且以 `错误: ` 开头、没有 traceback。"""
    lines = stderr_lines(proc)
    assert len(lines) == 1, f"stderr 应恰一行，实际 {len(lines)} 行: {proc.stderr!r}"
    assert lines[0].startswith("错误: "), proc.stderr
    assert "Traceback" not in proc.stderr
    return lines[0]


@pytest.fixture()
def kanban_home(tmp_path: Path) -> Path:
    """隔离的 ``HERMES_KANBAN_HOME``：默认目标落在它下面，不写真实 home。"""

    home = tmp_path / "kanban"
    home.mkdir()
    return home


# ---------------------------------------------------------------- 基础面


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
    assert_single_error_line(proc)


def test_unwritable_dest_rc2_single_stderr_line(tmp_path: Path, kanban_home: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    dest = blocker / "t.md"

    proc = run_script(["--dest", str(dest)], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert_single_error_line(proc)


def test_missing_dest_value_rc2(kanban_home: Path) -> None:
    proc = run_script(["--dest"], kanban_home)

    assert proc.returncode == 2
    assert_single_error_line(proc)


def test_empty_dest_value_rc2(kanban_home: Path) -> None:
    proc = run_script(["--dest", ""], kanban_home)

    assert proc.returncode == 2
    assert_single_error_line(proc)


def test_unknown_option_rc2(kanban_home: Path) -> None:
    proc = run_script(["--nope"], kanban_home)

    assert proc.returncode == 2
    assert_single_error_line(proc)


def test_dest_equals_form_works(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "t.md"

    proc = run_script([f"--dest={dest}"], kanban_home)

    assert proc.returncode == 0, proc.stderr
    assert dest.is_file()


# ------------------------------------------------- 返工 P2：非普通文件目标（1）


def test_fifo_target_rc2_without_hanging(tmp_path: Path, kanban_home: Path) -> None:
    """FIFO 目标：旧行为是把模板写进 FIFO 挂死（rc124），现在必须 rc2 单行。"""
    dest = tmp_path / "pipe"
    os.mkfifo(dest)

    proc = run_script(["--dest", str(dest)], kanban_home)  # 60s 超时兜底，挂死即判失败

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "FIFO" in assert_single_error_line(proc)
    assert os.path.exists(dest), "FIFO 本身不该被删"


def test_socket_target_rc2(tmp_path: Path, kanban_home: Path) -> None:
    import socket

    dest = tmp_path / "sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(dest))
    try:
        proc = run_script(["--dest", str(dest)], kanban_home)
    finally:
        sock.close()

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "socket" in assert_single_error_line(proc)


def test_directory_target_rc2_install_mode(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "adir"
    dest.mkdir()

    proc = run_script(["--dest", str(dest)], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "目录" in assert_single_error_line(proc)


# ------------------------------------------------------ 符号链接目标（2）


def test_symlink_target_rejected_and_linked_file_untouched(tmp_path: Path, kanban_home: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_bytes(b"ORIGINAL-IMPORTANT\n")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    proc = run_script(["--dest", str(link)], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "符号链接" in assert_single_error_line(proc)
    assert real.read_bytes() == b"ORIGINAL-IMPORTANT\n", "被链文件不许被改写"
    assert link.is_symlink()


def test_symlink_target_check_rc2_not_drift(tmp_path: Path, kanban_home: Path) -> None:
    """--check 不许跟随软链判漂移（旧行为：穿透读 → rc0/rc1 都说不清）。"""
    real = tmp_path / "real.md"
    real.write_bytes(SOURCE.read_bytes())  # 内容和源一模一样
    link = tmp_path / "link.md"
    link.symlink_to(real)

    proc = run_script(["--check", "--dest", str(link)], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "符号链接" in assert_single_error_line(proc)
    assert "一致：" not in proc.stdout


def test_dangling_symlink_target_rc2(tmp_path: Path, kanban_home: Path) -> None:
    link = tmp_path / "dangling.md"
    link.symlink_to(tmp_path / "nowhere.md")

    proc = run_script(["--dest", str(link)], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "符号链接" in assert_single_error_line(proc)
    assert not (tmp_path / "nowhere.md").exists(), "不许沿悬空软链把目标文件创建出来"


# ------------------------------------------- HERMES_KANBAN_HOME 空白串（3）


def test_blank_kanban_home_falls_back_to_home(tmp_path: Path) -> None:
    """空白串 HERMES_KANBAN_HOME：旧行为在 cwd 建名为空格的目录还报 rc0。"""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    proc = run_script(
        [],
        env_override={"HERMES_KANBAN_HOME": "   ", "HOME": str(home)},
        cwd=work,
    )

    assert proc.returncode == 0, proc.stderr
    assert (home / ".hermes" / "kanban" / "templates" / "card-template.md").is_file()
    assert list(work.iterdir()) == [], f"不该在 cwd 里建任何东西: {list(work.iterdir())}"


def test_kanban_home_is_stripped(tmp_path: Path) -> None:
    home = tmp_path / "kh"
    home.mkdir()

    proc = run_script([], env_override={"HERMES_KANBAN_HOME": f"  {home}  "})

    assert proc.returncode == 0, proc.stderr
    assert (home / "templates" / "card-template.md").is_file()


# --------------------------------------------------- rc2 stderr 单行（4）


@pytest.mark.skipif(IS_ROOT, reason="root 无视权限位，无法构造不可读文件")
def test_unreadable_target_check_rc2_single_line(tmp_path: Path, kanban_home: Path) -> None:
    dest = tmp_path / "locked.md"
    dest.write_bytes(b"x")
    dest.chmod(0o000)
    try:
        proc = run_script(["--check", "--dest", str(dest)], kanban_home)
    finally:
        dest.chmod(0o644)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    line = assert_single_error_line(proc)
    assert "sha256sum" not in line, f"sha256sum 的报错不许外泄: {line}"
    assert "Permission denied" not in proc.stderr


def test_dest_value_looking_like_option_rc2(kanban_home: Path) -> None:
    """`--dest --check` 旧行为把 dirname 喂成 `dirname --check`，stderr 3 行。"""
    proc = run_script(["--dest", "--check"], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "以 - 开头" in assert_single_error_line(proc)


def test_dest_with_newline_rc2(kanban_home: Path) -> None:
    proc = run_script(["--dest", "a\nb.md"], kanban_home)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "换行" in assert_single_error_line(proc)


# --------------------------------------------------------- HOME 未设（5）


def test_home_unset_without_dest_rc2_single_line() -> None:
    """HOME 未设：旧行为 set -u 炸 rc1（被当成漂移），现须 rc2 单行。"""
    proc = run_script([], env_drop=("HOME", "HERMES_KANBAN_HOME"))

    assert proc.returncode == 2, proc.stdout + proc.stderr
    line = assert_single_error_line(proc)
    assert "HOME" in line
    assert "unbound variable" not in proc.stderr


def test_home_empty_with_dest_still_works(tmp_path: Path, kanban_home: Path) -> None:
    """HOME 为空串 + 显式 --dest：不该拼出 /.hermes/... 也不该炸 set -u。"""
    dest = tmp_path / "t.md"

    proc = run_script(["--dest", str(dest)], kanban_home, env_override={"HOME": ""})

    assert proc.returncode == 0, proc.stderr
    assert dest.is_file()


# --------------------------------------------- 反斜杠等怪异文件名（6）


def test_backslash_dest_install_rc0_and_check_clean(tmp_path: Path, kanban_home: Path) -> None:
    """含反斜杠的目标：sha256sum 行首 `\\` 转义不许被当成哈希前缀。"""
    dest = tmp_path / "we\\ird.md"

    proc = run_script(["--dest", str(dest)], kanban_home)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert dest.read_bytes() == SOURCE.read_bytes()
    assert proc.stdout.count(sha256_of(SOURCE)) == 2

    chk = run_script(["--check", "--dest", str(dest)], kanban_home)
    assert chk.returncode == 0, chk.stdout + chk.stderr
    assert "漂移：" not in chk.stdout


# ------------------------------------------------- 原子写入不毁旧文件（7）


def test_failed_write_preserves_existing_target(tmp_path: Path, kanban_home: Path) -> None:
    """RLIMIT_FSIZE=0 让 cp 挂在 SIGXFSZ：旧模板必须一个字节不动（旧行为被截断成 0）。"""
    dest = tmp_path / "c1.md"
    dest.write_bytes(b"OLD-CONTENT-V1\n")

    proc = run_script(["--dest", str(dest)], kanban_home, prelude="ulimit -f 0;")

    assert proc.returncode == 2, f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert_single_error_line(proc)
    assert dest.read_bytes() == b"OLD-CONTENT-V1\n", "写失败不许截断旧模板"
    assert not list(tmp_path.glob("c1.md.tmp.*")), "临时文件必须被清掉"


def test_atomic_write_leaves_no_temp_files(tmp_path: Path, kanban_home: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    dest = work / "ok.md"

    proc = run_script(["--dest", str(dest)], kanban_home)

    assert proc.returncode == 0, proc.stderr
    assert [p.name for p in work.iterdir()] == ["ok.md"], list(work.iterdir())
