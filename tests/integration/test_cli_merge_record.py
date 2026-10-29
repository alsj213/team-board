"""cli merge-record 的集成测试：生成认账 body、``--body-out`` 落盘、``--check-file`` 预检。

覆盖：生成模式 stdout 前 5 行形状 + 4 行注释段、``--body-out`` 内容与 stdout 的
body 段逐字节一致（末尾恰好一个换行）、默认 body 路径**不写文件**、``--subject``
覆盖标题、预检绿 rc0 / 缺字段 rc1 / 格式非法 rc1 / 字段与参数不一致 rc1、
rc2 一组（``--pr`` 取值域含 0 / 负 / 空串 / 13 位毫秒 / 天文数字 / 超上限 /
``+1`` / ``1_0``，``--by`` 与 ``--card`` 形态，生成模式缺参数，``--check-file``
不存在 / 是目录 / 空文件 / 非 UTF-8 / 与 ``--body-out`` 同给，``--body-out``
父目录不存在）、stdout 编码表示不了中文的 rc2。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from team_board import cli

CARD = "t_ad5e7ff6"
BY = "pm/run137"
PR = "15"

#: 未给 ``--subject`` 时的默认标题（``merges.build_message`` 的契约）
DEFAULT_TITLE = f"Merge PR #{PR}（{CARD}）"


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """跑 ``cli.main(["merge-record", ...])``，返回 ``(rc, stdout, stderr)``。"""

    rc = cli.main(["merge-record", *args])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def generate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, str, str, Path]:
    """生成模式跑一遍（带 ``--body-out``），返回 ``(rc, out, err, body 文件)``。"""

    body_path = tmp_path / "merge-body.txt"
    rc, out, err = run(
        ["--card", CARD, "--by", BY, "--pr", PR, "--body-out", str(body_path), *extra],
        capsys,
    )
    return rc, out, err, body_path


def body_segment(out: str) -> str:
    """从 stdout 里切出 body 段（前 5 行：标题 / 空行 / 三行 trailer）。"""

    return "\n".join(out.splitlines()[:5]) + "\n"


@pytest.fixture()
def body_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """一份由本工具自己生成的、合法的 body 文件（预检用例的输入）。"""

    rc, out, err, body_path = generate(tmp_path, capsys)
    assert rc == 0, (rc, out, err)
    return body_path


# --------------------------------------------------------------- 生成模式：绿


def test_generate_prints_body_then_comment_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdout = body 段 + 空行 + 注释段（4 行：3 条 ``# `` 注释 + 1 条命令模板）。"""

    rc, out, err, body_path = generate(tmp_path, capsys)

    assert rc == 0, (rc, out, err)
    assert err == ""
    lines = out.splitlines()
    assert len(lines) == 10, lines
    assert lines[0] == DEFAULT_TITLE
    assert lines[1] == ""
    assert lines[2] == f"Merge-By: {BY}"
    assert lines[3] == f"Merge-Card: {CARD}"
    assert lines[4] == f"Merge-PR: #{PR}"
    assert lines[5] == ""
    assert lines[6].startswith("# 卡面认账块：")
    assert f"merge-audit --card {CARD} --require" in lines[6]
    assert lines[7] == "# 合并命令（本工具不执行）："
    assert lines[8] == (
        f'gh pr merge {PR} -R alsj213/team-board --merge --subject "{DEFAULT_TITLE}"'
        f" --body-file {body_path}"
    )
    assert lines[9] == f"# body 文件：{body_path}（已写入）"
    # 本工具只打印、不执行：stdout 里除了这条模板不该有别的命令
    assert out.count("gh pr merge ") == 1


def test_body_out_file_equals_body_segment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--body-out`` 写出的内容与 stdout 的 body 段逐字节一致，末尾恰好一个换行。"""

    rc, out, err, body_path = generate(tmp_path, capsys)

    assert rc == 0, (rc, out, err)
    written = body_path.read_bytes()
    assert written == body_segment(out).encode("utf-8")
    assert written.endswith(b"\n")
    assert not written.endswith(b"\n\n")
    assert written.decode("utf-8").splitlines()[0] == DEFAULT_TITLE


def test_subject_override_is_used_by_body_and_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--subject`` 同时进 body 首行与 ``gh pr merge --subject`` 模板。"""

    rc, out, err, body_path = generate(tmp_path, capsys, "--subject", "feat: 自定义标题")

    assert rc == 0, (rc, out, err)
    lines = out.splitlines()
    assert lines[0] == "feat: 自定义标题"
    assert '--subject "feat: 自定义标题"' in lines[8]
    assert body_path.read_text(encoding="utf-8").splitlines()[0] == "feat: 自定义标题"


def test_without_body_out_does_not_write_default_file(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """没给 ``--body-out`` → 注释里报默认路径且标「未写入」，该文件不存在。"""

    default = Path(f"/tmp/{CARD}-merge-body.txt")
    default.unlink(missing_ok=True)

    rc, out, err = run(["--card", CARD, "--by", BY, "--pr", PR], capsys)

    assert rc == 0, (rc, out, err)
    assert f"# body 文件：{default}（未写入，给了 --body-out 才写）" in out
    assert f"--body-file {default}" in out
    assert not default.exists()


# ------------------------------------------------------------- 预检模式：绿


def test_check_file_green(
    body_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """三字段齐 → rc0 + 一行通过文案。"""

    rc, out, err = run(["--check-file", str(body_file)], capsys)

    assert rc == 0, (rc, out, err)
    assert err == ""
    assert out == "通过：三字段齐（Merge-By/Merge-Card/Merge-PR）\n"


def test_check_file_green_with_consistent_params(
    body_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--card/--by/--pr`` 与文件内容一致 → 仍 rc0。"""

    rc, out, err = run(
        ["--check-file", str(body_file), "--card", CARD, "--by", BY, "--pr", PR], capsys
    )

    assert rc == 0, (rc, out, err)
    assert out.startswith("通过：")


# ------------------------------------------------------------- 预检模式：红


def test_check_file_missing_field_is_red(
    body_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """删掉 ``Merge-PR`` 那行 → rc1 ``问题: 缺字段：Merge-PR``。"""

    broken = tmp_path / "missing-pr.txt"
    broken.write_text(
        "".join(line for line in body_file.read_text(encoding="utf-8").splitlines(True)
                if not line.startswith("Merge-PR: ")),
        encoding="utf-8",
    )

    rc, out, err = run(["--check-file", str(broken)], capsys)

    assert rc == 1, (rc, out, err)
    assert err == ""
    assert "问题: 缺字段：Merge-PR" in out


def test_check_file_bad_format_is_red(
    body_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``Merge-By: pm``（没有 ``/run<N>``）→ rc1 ``问题: 格式非法：Merge-By``。"""

    text = body_file.read_text(encoding="utf-8").replace(
        f"Merge-By: {BY}", "Merge-By: pm"
    )
    broken = tmp_path / "bad-format.txt"
    broken.write_text(text, encoding="utf-8")

    rc, out, err = run(["--check-file", str(broken)], capsys)

    assert rc == 1, (rc, out, err)
    assert '问题: 格式非法：Merge-By 的值 "pm" 不合法' in out


def test_check_file_param_mismatch_is_red(
    body_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """文件里是 ``t_ad5e7ff6``、参数要 ``t_11112222`` → rc1 ``字段与参数不一致``。"""

    rc, out, err = run(
        ["--check-file", str(body_file), "--card", "t_11112222"], capsys
    )

    assert rc == 1, (rc, out, err)
    assert "字段与参数不一致" in out
    assert "Merge-Card" in out


def test_check_file_misplaced_field_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """字段写在正文（不在末尾 trailer 块）→ rc1 ``字段未落在 trailer 区``。"""

    body = tmp_path / "misplaced.txt"
    body.write_text(
        f"{DEFAULT_TITLE}\n\nMerge-Card: {CARD}\n\n后续说明段落不属于 trailer 块\n",
        encoding="utf-8",
    )

    rc, out, err = run(["--check-file", str(body)], capsys)

    assert rc == 1, (rc, out, err)
    assert "字段未落在 trailer 区：Merge-Card" in out


# ------------------------------------------------------------------- rc2 一组


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        # --pr 取值域
        (["--card", CARD, "--by", BY, "--pr", "0"], "PR 号非法"),
        (["--card", CARD, "--by", BY, "--pr", "-1"], "PR 号非法"),
        (["--card", CARD, "--by", BY, "--pr", ""], "PR 号非法"),
        (["--card", CARD, "--by", BY, "--pr", "+1"], "PR 号非法"),
        (["--card", CARD, "--by", BY, "--pr", "1_0"], "PR 号非法"),
        (["--card", CARD, "--by", BY, "--pr", "1793160000000"], "超出上限"),
        (["--card", CARD, "--by", BY, "--pr", "99999999999999999999"], "超出上限"),
        (["--card", CARD, "--by", BY, "--pr", "1000000000"], "超出上限"),
        # --by 形态
        (["--card", CARD, "--by", "", "--pr", PR], "执行者标识非法"),
        (["--card", CARD, "--by", "pm", "--pr", PR], "执行者标识非法"),
        (["--card", CARD, "--by", "pm-run137", "--pr", PR], "执行者标识非法"),
        (["--card", CARD, "--by", "PM/run1", "--pr", PR], "执行者标识非法"),
        # --card 形态
        (["--card", "t_zzz", "--by", BY, "--pr", PR], "卡号非法"),
        (["--card", "t_ZZZZZZZZ", "--by", BY, "--pr", PR], "卡号非法"),
        (["--card", "t_ad5e7ff6x", "--by", BY, "--pr", PR], "卡号非法"),
        (["--card", "ad5e7ff6", "--by", BY, "--pr", PR], "卡号非法"),
        (["--card", "", "--by", BY, "--pr", PR], "卡号非法"),
        # 生成模式缺必需参数
        ([], "生成模式必须同时给"),
        (["--card", CARD], "生成模式必须同时给"),
        (["--card", CARD, "--by", BY], "生成模式必须同时给"),
        (["--by", BY, "--pr", PR], "生成模式必须同时给"),
    ],
)
def test_rc2_parameters(
    args: list[str], needle: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """参数级错误 → rc2、stdout 空、stderr 恰好一行 ``错误: …``、不冒 traceback。"""

    rc, out, err = run(args, capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert needle in err
    assert err.count("\n") == 1, repr(err)
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("kind", "needle"),
    [
        ("missing", "不存在"),
        ("directory", "不是普通文件"),
        ("empty", "是空文件"),
        ("non_utf8", "不是 UTF-8 文本"),
    ],
)
def test_rc2_check_file_bad_input(
    kind: str,
    needle: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--check-file`` 的五类坏输入（缺 / 目录 / 空 / 非 UTF-8 / 空路径）→ rc2。"""

    target = tmp_path / kind
    if kind == "directory":
        target.mkdir()
        path = str(target)
    elif kind == "empty":
        target.write_text("", encoding="utf-8")
        path = str(target)
    elif kind == "non_utf8":
        target.write_bytes(b"Merge-By: \xff\xfe\n")
        path = str(target)
    elif kind == "missing":
        path = str(tmp_path / "nope.txt")
    else:  # pragma: no cover - 参数表里没有这一档
        raise AssertionError(kind)

    rc, out, err = run(["--check-file", path], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert needle in err
    assert err.startswith("错误: ")
    assert "Traceback" not in err


def test_rc2_check_file_empty_path(capsys: pytest.CaptureFixture[str]) -> None:
    """``--check-file ""`` → rc2（空路径不是「没给」）。"""

    rc, out, err = run(["--check-file", ""], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "路径为空" in err


def test_rc2_check_file_with_body_out(
    body_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--check-file`` 与 ``--body-out`` 同给 → rc2，且不写文件。"""

    target = tmp_path / "never-written.txt"

    rc, out, err = run(
        ["--check-file", str(body_file), "--body-out", str(target)], capsys
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "不能同给" in err
    assert not target.exists()


def test_rc2_check_file_with_invalid_card(
    body_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """预检模式给了非法 ``--card`` → rc2（参数错误，不是 rc1）。"""

    rc, out, err = run(["--check-file", str(body_file), "--card", "t_zzz"], capsys)

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "卡号非法" in err


def test_rc2_body_out_parent_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--body-out`` 父目录不存在 → rc2、stdout 空、文件不被创建。"""

    target = tmp_path / "no-such-dir" / "body.txt"

    rc, out, err = run(
        ["--card", CARD, "--by", BY, "--pr", PR, "--body-out", str(target)], capsys
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert "父目录不存在" in err
    assert not target.exists()


def test_ascii_stdout_returns_two(tmp_path: Path) -> None:
    """stdout 编码表示不了中文 → rc2、stdout 空、stderr ``错误: `` 开头（真子进程）。"""

    src = Path(__file__).resolve().parents[2] / "src"
    env = {
        **os.environ,
        "PYTHONPATH": str(src),
        "PYTHONIOENCODING": "ascii",
        "LC_ALL": "C",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "team_board.cli",
            "merge-record",
            "--card",
            CARD,
            "--by",
            BY,
            "--pr",
            PR,
        ],
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == b""
    assert proc.stderr.startswith("错误: ".encode())
    assert b"Traceback" not in proc.stderr
