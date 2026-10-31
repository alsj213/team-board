"""``cli gate`` 的集成测试：真子进程 + tmp_path，验 rc 三档与落盘口径。

子命令一律用 ``sys.executable -c`` 起 —— 这里验的是**包装器**（去 ``--``、跑子进程、
落盘、只回摘要），不是某个具体检查器，所以不依赖仓库里的 pytest / ruff 可执行文件。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from team_board import cli

CARD = "t_eaa81611"

#: 仓库 ``src/`` —— 起真 CLI 子进程时用它拼 ``PYTHONPATH``（不依赖安装态）
SRC = Path(__file__).resolve().parents[2] / "src"


def run(
    capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str, str]:
    """直调 ``cli.main``，返回 ``(rc, stdout, stderr)``。"""
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def log_of(out: str) -> Path:
    """从摘要里取 ``log:`` 那行的路径（必须恰好一行）。"""
    lines = [line for line in out.splitlines() if line.startswith("log: ")]
    assert len(lines) == 1, out
    return Path(lines[0][len("log: ") :])


def test_rc0_writes_full_log_and_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rc0：子命令打印 ``12 passed in 0.30s`` 退出 0。"""
    log_dir = tmp_path / "g"
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "pytest",
        "--card",
        CARD,
        "--log-dir",
        str(log_dir),
        "--",
        sys.executable,
        "-c",
        "print('12 passed in 0.30s')",
    )

    assert err == ""
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "gate: pytest"
    assert lines[1] == f"card: {CARD}"
    assert lines[2] == "exit: 0"
    assert lines[3] == "counts: passed=12 failed=0 error=0 skipped=0"

    path = log_of(out)
    assert path.is_absolute()
    assert path == log_dir / f"pytest-{CARD}.log"
    assert path.read_text(encoding="utf-8") == "12 passed in 0.30s\n"


def test_rc1_is_transparent_and_log_still_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rc1：子命令的失败码透传，失败行进摘要，日志照写。"""
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "pytest",
        "--card",
        CARD,
        "--log-dir",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        "import sys; print('FAILED tests/x.py::t'); sys.exit(1)",
    )

    assert err == ""
    assert code == 1
    assert "exit: 1" in out.splitlines()
    assert "  FAILED tests/x.py::t" in out.splitlines()

    path = log_of(out)
    assert path.exists()
    assert "FAILED tests/x.py::t" in path.read_text(encoding="utf-8")


def test_child_exit_2_is_transparent_like_any_other_rc(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """子命令自己 ``sys.exit(2)``：rc2 透传，但走的是正常路径 —— 日志与摘要照写、stderr 空。

    与 ``test_rc2_when_child_cannot_start`` 的 rc2 区分：那是 (a) gate 自身错误
    （stderr 恰一行 ``错误: …``、stdout 空、不写日志）；这里是 (b) 子命令的退出码
    （stdout 有摘要、日志照写、stderr 空）。
    """
    log_dir = tmp_path / "g"
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "pytest",
        "--card",
        CARD,
        "--log-dir",
        str(log_dir),
        "--",
        sys.executable,
        "-c",
        "import sys; print('ERROR boom'); sys.exit(2)",
    )

    assert err == ""
    assert code == 2
    lines = out.splitlines()
    assert lines[0] == "gate: pytest"
    assert lines[2] == "exit: 2"
    assert "  ERROR boom" in lines

    path = log_of(out)
    assert path == log_dir / f"pytest-{CARD}.log"
    assert path.read_text(encoding="utf-8") == "ERROR boom\n"


def test_child_stderr_is_merged_into_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """落盘是全量：stderr 合并进 stdout，按写入顺序同一个文件。"""
    code, out, _err = run(
        capsys,
        "gate",
        "--gate",
        "mixed",
        "--card",
        CARD,
        "--log-dir",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        "import sys; print('out-line'); print('err-line', file=sys.stderr); sys.exit(3)",
    )

    assert code == 3
    text = log_of(out).read_text(encoding="utf-8")
    assert "out-line" in text
    assert "err-line" in text


def test_signalled_child_becomes_128_plus_signal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """被信号杀：负 rc 换成 shell 惯用码（SIGTERM → 143），摘要里不带负数。"""
    code, out, _err = run(
        capsys,
        "gate",
        "--gate",
        "killed",
        "--card",
        CARD,
        "--log-dir",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
    )

    assert code == 143
    assert "exit: 143" in out.splitlines()


def test_rc2_when_child_cannot_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rc2 起不来：stderr 恰一行、stdout 空、不建日志目录、不写日志。"""
    log_dir = tmp_path / "g"
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "probe",
        "--card",
        CARD,
        "--log-dir",
        str(log_dir),
        "--",
        "/nonexistent-binary",
    )

    assert code == 2
    assert out == ""
    assert err.startswith("错误: 启动失败")
    assert err.count("\n") == 1
    assert not log_dir.exists()


def test_empty_command_after_double_dash_is_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--`` 之后没有命令：rc2、不写日志、stdout 空。"""
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "probe",
        "--card",
        CARD,
        "--log-dir",
        str(tmp_path),
        "--",
    )

    assert code == 2
    assert out == ""
    assert err == "错误: 缺少要执行的命令（-- 之后至少一个）\n"
    assert not (tmp_path / f"probe-{CARD}.log").exists()


def test_missing_card_is_rc2_from_argparse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺 ``--card``：argparse 级 rc2（沿用既有机制），stdout 空。"""
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "gate",
                "--gate",
                "probe",
                "--log-dir",
                str(tmp_path),
                "--",
                sys.executable,
                "-c",
                "print('never')",
            ]
        )

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--card" in captured.err


def test_bad_card_shape_is_rc2_and_writes_no_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--card t_XYZ``：形态非法 → rc2，文案走 ``卡号形态非法``。"""
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "probe",
        "--card",
        "t_XYZ",
        "--log-dir",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        "print('never')",
    )

    assert code == 2
    assert out == ""
    assert err == "错误: 卡号形态非法: t_XYZ\n"
    assert not list(tmp_path.iterdir())


def test_bad_gate_shape_is_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """gate 名含 ``/`` 会拼出子目录（路径逃逸）：落盘前就拒，rc2。"""
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "a/b",
        "--card",
        CARD,
        "--log-dir",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        "print('never')",
    )

    assert code == 2
    assert out == ""
    assert err == "错误: gate 形态非法: a/b\n"
    assert not list(tmp_path.iterdir())


def test_full_output_lands_in_the_log_while_stdout_stays_a_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """500 行输出：日志 500 行；stdout 只有摘要（6 行固定 + 2 条失败行 = 8 行）。"""
    script = (
        "for i in range(498):\n"
        "    print(f'line {i}')\n"
        "print('ERROR boom-1')\n"
        "print('ERROR boom-2')\n"
    )
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "noisy",
        "--card",
        CARD,
        "--log-dir",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert err == ""
    assert code == 0
    text = log_of(out).read_text(encoding="utf-8")
    assert len(text.splitlines()) == 500

    lines = out.splitlines()
    assert len(lines) <= 8
    assert "line 0" not in out
    assert "  ERROR boom-1" in lines
    assert "  ERROR boom-2" in lines


def test_log_dir_is_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--log-dir`` 指向不存在的目录：建出来，日志落在里面。"""
    log_dir = tmp_path / "nested" / "g"
    assert not log_dir.exists()

    code, out, _err = run(
        capsys,
        "gate",
        "--gate",
        "probe",
        "--card",
        CARD,
        "--log-dir",
        str(log_dir),
        "--",
        sys.executable,
        "-c",
        "print('ok')",
    )

    assert code == 0
    assert log_dir.is_dir()
    assert log_of(out) == log_dir / f"probe-{CARD}.log"
    assert (log_dir / f"probe-{CARD}.log").read_text(encoding="utf-8") == "ok\n"


def test_default_log_dir_is_evidence_under_cwd(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不给 ``--log-dir``：默认 ``evidence``（相对 CWD）。"""
    monkeypatch.chdir(tmp_path)

    code, out, _err = run(
        capsys,
        "gate",
        "--gate",
        "probe",
        "--card",
        CARD,
        "--",
        sys.executable,
        "-c",
        "print('ok')",
    )

    assert code == 0
    assert log_of(out) == tmp_path / "evidence" / f"probe-{CARD}.log"
    assert (tmp_path / "evidence" / f"probe-{CARD}.log").exists()


def test_unwritable_log_dir_is_rc2_without_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """日志目录写不进去（这里用「父路径是文件」造必失败）：rc2、stdout 空、一行 stderr。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "probe",
        "--card",
        CARD,
        "--log-dir",
        str(blocker / "g"),
        "--",
        sys.executable,
        "-c",
        "print('ok')",
    )

    assert code == 2
    assert out == ""
    assert err.startswith("错误: 写日志失败")
    assert err.count("\n") == 1


def test_non_utf8_bytes_from_child_are_replaced_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """子进程吐 GBK 字节：按 utf-8 + ``errors="replace"`` 解码 —— rc 透传、不冒 traceback。

    解码不随 locale 漂移，所以非 UTF-8 字节只变成替换字符（U+FFFD），
    日志照写、``exit:`` 照常是子命令自己的 0。
    """

    script = (
        "import sys\n"
        "sys.stdout.buffer.write('中文'.encode('gbk'))\n"
        "sys.stdout.buffer.flush()\n"
    )
    code, out, err = run(
        capsys,
        "gate",
        "--gate",
        "gbk",
        "--card",
        CARD,
        "--log-dir",
        str(tmp_path),
        "--",
        sys.executable,
        "-c",
        script,
    )

    assert err == ""
    assert code == 0
    assert "exit: 0" in out.splitlines()
    assert "Traceback" not in out
    text = log_of(out).read_text(encoding="utf-8")
    assert "\ufffd" in text
    assert "中文" not in text


def test_child_reading_stdin_gets_immediate_eof(tmp_path: Path) -> None:
    """子命令读 stdin：调用方 stdin 一直开着也不能挂 —— 子进程拿 ``DEVNULL``，立刻 EOF。

    这里必须起**真 CLI 子进程**：把管道写端一直开着当它的 stdin，子进程若还继承
    这份 stdin，读 ``sys.stdin.read()`` 就会阻塞到 ``timeout``（用例失败而不是挂死）。
    """

    log_dir = tmp_path / "g"
    env = {key: value for key, value in os.environ.items() if key != "PYTHONIOENCODING"}
    env["PYTHONPATH"] = str(SRC)

    read_fd, write_fd = os.pipe()  # 写端刻意不关：继承它的读会阻塞
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "team_board.cli",
                "gate",
                "--gate",
                "stdin",
                "--card",
                CARD,
                "--log-dir",
                str(log_dir),
                "--",
                sys.executable,
                "-c",
                "import sys; print(len(sys.stdin.read()))",
            ],
            stdin=read_fd,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert proc.returncode == 0, proc.stderr
    assert "exit: 0" in proc.stdout.splitlines()
    assert (log_dir / f"stdin-{CARD}.log").read_text(encoding="utf-8") == "0\n"
