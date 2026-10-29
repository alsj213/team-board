"""cli merge-record 的集成测试：生成认账 body + ``merge_guard`` 合并命令、``--check-file`` 预检。

覆盖：生成模式 stdout 前 5 行形状 + 4 行注释段、第 9 行是**可直接照抄执行**的
``merge_guard`` 命令（R12 ① 的唯一入口；``--method merge`` / ``--delete-branch`` /
认账四参数齐、``--match-head-commit`` 不进模板）、该行 ``shlex.split`` 后喂
``merge_guard.main()`` 能走到「源分支不在远端」而不是撞 argparse、``--body-out``
内容与 stdout 的 body 段逐字节一致（末尾恰好一个换行）、``--subject`` 覆盖标题且经
``shlex.quote`` 后能逐字还原、``--body-out`` 相对路径打印成绝对路径、``--branch`` /
``--body-out`` 缺任一项 rc2、``--branch`` 形态非法（空串 / 含空白 / ``refs/`` 前缀）
rc2、``--subject`` 含换行 rc2、打印行的 **shell 安全性**（``--branch`` 含 ``;`` / ``$``
时经 ``bash -c`` 仍恰好一条命令；``--repo`` 含空格时 ``cd`` / ``PYTHONPATH`` / ``--repo``
三处都加引号）、预检绿 rc0 / 缺字段 rc1 / 格式非法 rc1 / 字段与参数不一致 rc1、rc2 一组
（``--pr`` 取值域含 0 / 负 / 空串 / 13 位毫秒 / 天文数字 / 超上限 / ``+1`` / ``1_0``，
``--by`` 与 ``--card`` 形态，生成模式缺参数，``--check-file`` 不存在 / 是目录 /
空文件 / 非 UTF-8 / 与 ``--body-out`` 同给，``--body-out`` 父目录不存在）、stdout
编码表示不了中文的 rc2，以及**文档守卫**：``README.md`` 与
``docs/merge-accountability.md`` 里每条 ``merge-record …`` 生成命令抠出来真跑一遍
必须 rc0（文档漂移即 CI 红）。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from team_board import cli
from team_board import merge_guard as mg

CARD = "t_ad5e7ff6"
BY = "pm/run137"
PR = "15"
#: 该 PR 的 head 分支（生成模式必填；形态取 ``wt/t_<卡号>``）
BRANCH = "wt/t_bbeab29f"

#: 未给 ``--subject`` 时的默认标题（``merges.build_message`` 的契约）
DEFAULT_TITLE = f"Merge PR #{PR}（{CARD}）"

#: 仓库根（文档守卫要读 README.md / docs）
REPO_ROOT = Path(__file__).resolve().parents[2]


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """跑 ``cli.main(["merge-record", ...])``，返回 ``(rc, stdout, stderr)``。"""

    rc = cli.main(["merge-record", *args])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def generate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, str, str, Path]:
    """生成模式跑一遍（``--branch`` / ``--body-out`` / ``--repo`` 都给）。

    返回 ``(rc, out, err, body 文件)``。
    """

    body_path = tmp_path / "merge-body.txt"
    rc, out, err = run(
        [
            "--card", CARD,
            "--by", BY,
            "--pr", PR,
            "--branch", BRANCH,
            "--repo", str(tmp_path),
            "--body-out", str(body_path),
            *extra,
        ],
        capsys,
    )
    return rc, out, err, body_path


def body_segment(out: str) -> str:
    """从 stdout 里切出 body 段（前 5 行：标题 / 空行 / 三行 trailer）。"""

    return "\n".join(out.splitlines()[:5]) + "\n"


def printed_command(out: str) -> str:
    """取 stdout 第 9 行（打印出来的那条 ``merge_guard`` 合并命令原文）。"""

    return out.splitlines()[8]


def printed_argv(out: str) -> list[str]:
    """把打印行拆成 ``merge_guard`` 的 argv：去掉 ``cd <repo> && `` 与 ``PYTHONPATH=…/src``。"""

    line = printed_command(out)
    assert line.startswith("cd "), line
    before, sep, after = line.partition(" && ")
    assert sep and before.startswith("cd "), line
    parts = shlex.split(after)
    assert parts[0].startswith("PYTHONPATH="), parts
    assert parts[1:4] == ["python3", "-m", "team_board.merge_guard"], parts
    return parts[4:]


def flag_value(argv: list[str], flag: str) -> str:
    """取 ``--flag VALUE`` 的 VALUE。"""

    return argv[argv.index(flag) + 1]


def shim_python3(tmp_path: Path) -> Path:
    """造一个只会把 argv 打出来的 ``python3`` 替身，放在 ``PATH`` 最前面。

    「替换进 shell 后是不是仍然只有一条命令」必须真让 shell 跑，光 ``shlex.split``
    只证明参数能切开；本工具打印的那行以 ``cd … && PYTHONPATH=… python3 -m …`` 开头，
    把 ``python3`` 换成替身就能在不联网、不碰 git/gh 的前提下看 shell 到底发出了几次调用。
    """

    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text(
        "#!/bin/sh\n"
        "printf 'CALL\\n'\n"
        'for arg in "$@"; do printf \'ARG %s\\n\' "$arg"; done\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim_dir


def run_line_in_bash(line: str, shim_dir: Path) -> tuple[int, list[str], str, str]:
    """把打印行原样交给 ``bash -c`` 跑（``python3`` = 替身），返回 ``(rc, argv, out, err)``。

    判据是**严格**的：替身的 stdout 只许是「一条 ``CALL`` + 每行 ``ARG …``」。注入成功时
    shell 会额外跑 ``echo …``，多出来的输出落在这里 ⇒ 断言直接炸；命令被拆断则替身收到的
    argv 少一截、``--branch`` 不再逐字还原。
    """

    bash = shutil.which("bash")
    assert bash is not None, "本机 PATH 里没有 bash，做不了真 shell 校验"
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(shim_dir), env.get("PATH", "")])

    proc = subprocess.run(
        [bash, "-c", line], capture_output=True, text=True, env=env, check=False
    )

    lines = proc.stdout.splitlines()
    assert lines[:1] == ["CALL"], (proc.returncode, proc.stdout, proc.stderr)
    for extra in lines[1:]:
        assert extra.startswith("ARG "), (proc.stdout, proc.stderr)
    return proc.returncode, [ln[4:] for ln in lines[1:]], proc.stdout, proc.stderr


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
    """stdout = body 段 + 空行 + 注释段（4 行：3 条 ``# `` 注释 + 1 条 merge_guard 命令）。"""

    rc, out, err, body_path = generate(tmp_path, capsys)

    assert rc == 0, (rc, out, err)
    assert err == ""
    lines = out.splitlines()
    assert len(lines) == 10, lines
    assert lines[0] == DEFAULT_TITLE
    assert lines[1] == ""
    assert lines[2] == f"merged-by: {BY}"
    assert lines[3] == f"merge-card: {CARD}"
    assert lines[4] == f"merge-pr: #{PR}"
    assert lines[5] == ""
    assert lines[6].startswith("# 卡面认账块：")
    assert f"merge-audit --card {CARD} --require" in lines[6]
    assert lines[7] == (
        "# 合并命令（本工具不执行；R12 ① 唯一入口 = merge_guard，缺 --delete-branch 会被拒）："
    )
    repo = os.path.realpath(str(tmp_path))
    body = os.path.realpath(str(body_path))
    assert lines[8] == (
        f"cd {repo} && PYTHONPATH={repo}/src python3 -m team_board.merge_guard"
        f" --pr {PR} --branch {BRANCH} --method merge --delete-branch"
        f" --card {CARD} --by {BY} --subject {shlex.quote(DEFAULT_TITLE)}"
        f" --body-file {body} --repo {repo}"
    )
    assert lines[9] == f"# body 文件：{body}（已写入）"
    # 本工具只打印、不执行；旧的裸 gh pr merge 形态不许再出现
    assert "gh pr merge" not in out
    assert out.count("team_board.merge_guard") == 1


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


def test_subject_override_is_used_by_body_and_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--subject`` 同时进 body 首行与打印命令的 ``--subject``（``shlex`` 还原后逐字相等）。"""

    subject = 'feat: 含 "引号"、$HOME 与 `反引号` 的标题'
    rc, out, err, body_path = generate(tmp_path, capsys, "--subject", subject)

    assert rc == 0, (rc, out, err)
    lines = out.splitlines()
    assert lines[0] == subject
    assert flag_value(printed_argv(out), "--subject") == subject
    assert body_path.read_text(encoding="utf-8").splitlines()[0] == subject


def test_printed_command_is_merge_guard_and_runnable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """打印行 = R12 ① 的命令形态；``shlex.split`` 后喂 ``merge_guard.main()`` 不撞 argparse。

    这是本卡的核心理由：卡面/文档写「照抄它打印的命令」，那这一行就必须真被
    ``merge_guard`` 收下。这里把 ``merge_guard._run`` 换成「``git ls-remote`` 返回
    rc=2」，命令一路走到「合并前源分支必须先存在」的闸门（rc2），不联网、零 gh 调用。
    """

    rc, out, err, body_path = generate(tmp_path, capsys)

    assert rc == 0, (rc, out, err)
    body = os.path.realpath(str(body_path))
    repo = os.path.realpath(str(tmp_path))
    argv = printed_argv(out)
    assert argv == [
        "--pr", PR,
        "--branch", BRANCH,
        "--method", "merge",
        "--delete-branch",
        "--card", CARD,
        "--by", BY,
        "--subject", DEFAULT_TITLE,
        "--body-file", body,
        "--repo", repo,
    ]
    # 「钉到提交」由 merge_guard 自己从预检的 headRefOid 拼，不进模板
    assert "--match-head-commit" not in argv

    calls: list[tuple[str, ...]] = []

    def fake_run(argv_: tuple[str, ...], *, cwd: str, timeout: float) -> mg.CommandResult:
        calls.append(argv_)
        return mg.CommandResult(argv=argv_, returncode=2, stdout="", stderr="")

    monkeypatch.setattr(mg, "_run", fake_run)

    rc = mg.main(argv)
    captured = capsys.readouterr()

    assert rc == 2, (captured.out, captured.err)
    assert captured.out == ""
    assert "不在 origin 远端" in captured.err
    # 照抄不撞 argparse：认账四参数 + --branch 全被接住
    assert "the following arguments are required" not in captured.err
    # 闸门在 gh 之前：只有一次 git ls-remote，一个 gh 调用都没发
    assert len(calls) == 1, calls
    assert calls[0][0] == "git"


def test_relative_body_out_prints_absolute_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--body-out recv-body.txt``（相对路径）→ 打印行里的 ``--body-file`` 是绝对路径且存在。"""

    monkeypatch.chdir(tmp_path)

    rc, out, err = run(
        [
            "--card", CARD,
            "--by", BY,
            "--pr", PR,
            "--branch", BRANCH,
            "--repo", str(tmp_path),
            "--body-out", "recv-body.txt",
        ],
        capsys,
    )

    assert rc == 0, (rc, out, err)
    body = flag_value(printed_argv(out), "--body-file")
    assert os.path.isabs(body)
    assert body == os.path.realpath(str(tmp_path / "recv-body.txt"))
    assert Path(body).is_file()
    assert f"# body 文件：{body}（已写入）" in out


# ------------------------------------------------- 生成模式：打印行的 shell 安全性

#: ``git check-ref-format "refs/heads/wt/t_x;echo\$IFS'INJ'"`` 认的合法分支名（P2-1 的触发值）
INJECT_BRANCH = "wt/t_x;echo$IFS'INJ'"


def test_branch_with_shell_metachars_stays_one_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--branch`` 含 ``;`` ``$`` 时打印行仍**恰好一条命令**，``--branch`` 逐字还原。

    不加引号时（修复前）shell 会把它切成 ``… --branch wt/t_x`` + ``echo $IFS'INJ' …``
    两条命令，stdout 出现 ``INJ``，且整行退出码被后一条的 rc0 覆盖 —— 合并失败却拿到 rc0。
    """

    rc, out, err = run(
        [
            "--card", CARD,
            "--by", BY,
            "--pr", PR,
            "--branch", INJECT_BRANCH,
            "--repo", str(tmp_path),
            "--body-out", str(tmp_path / "merge-body.txt"),
        ],
        capsys,
    )

    assert rc == 0, (rc, out, err)
    line = printed_command(out)
    assert "\n" not in line  # 一条命令 = 一行
    tokens = shlex.split(line)
    # 全行只有「cd … && …」那一个分隔符；注入的 ';' / '$' 必须被吃进引号、不能成为独立 token
    assert tokens.count("&&") == 1, tokens
    for operator in (";", "|", "||", "&", ">", ">>", "(", ")"):
        assert operator not in tokens, tokens
    argv = printed_argv(out)
    assert flag_value(argv, "--branch") == INJECT_BRANCH

    # 真交给 bash 跑：注入成立时 shell 会多跑一条 echo，替身 stdout 立刻多出非 ARG 行
    rc_bash, got, stdout, stderr = run_line_in_bash(line, shim_python3(tmp_path))
    assert rc_bash == 0, (stdout, stderr)
    assert got == ["-m", "team_board.merge_guard", *argv], (stdout, stderr)


def test_repo_with_space_is_quoted_and_roundtrips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--repo`` 路径含空格时 ``cd`` / ``PYTHONPATH`` / ``--repo`` 三处都加引号且逐字还原。

    不加引号时（修复前）照抄即 ``bash: cd: too many arguments`` / ``rc=1``
    （``/mnt/c/Users/…`` 一类路径是日常形态）。
    """

    repo = tmp_path / "probe with space"
    repo.mkdir()
    real = os.path.realpath(str(repo))

    rc, out, err = run(
        [
            "--card", CARD,
            "--by", BY,
            "--pr", PR,
            "--branch", BRANCH,
            "--repo", str(repo),
            "--body-out", str(tmp_path / "merge-body.txt"),
        ],
        capsys,
    )

    assert rc == 0, (rc, out, err)
    line = printed_command(out)
    assert line.startswith(
        f"cd {shlex.quote(real)} && PYTHONPATH={shlex.quote(real + '/src')} "
    ), line
    tokens = shlex.split(line)
    assert tokens.count("&&") == 1, tokens
    # PYTHONPATH 的整条路径是一个 token（含空格也不能被拆开），且逐字还原
    assert f"PYTHONPATH={real}/src" in tokens, tokens
    argv = printed_argv(out)
    assert flag_value(argv, "--repo") == real
    assert flag_value(argv, "--body-file") == os.path.realpath(
        str(tmp_path / "merge-body.txt")
    )

    rc_bash, got, stdout, stderr = run_line_in_bash(line, shim_python3(tmp_path))
    assert rc_bash == 0, (stdout, stderr)  # cd 的引号没写对时这里会是 rc 非 0
    assert got == ["-m", "team_board.merge_guard", *argv], (stdout, stderr)


# ------------------------------------------------------------- 生成模式：rc2


def test_without_branch_is_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺 ``--branch`` → rc2、stdout 0 字节、stderr 恰一行，且不写 body 文件。"""

    target = tmp_path / "never-written.txt"

    rc, out, err = run(
        ["--card", CARD, "--by", BY, "--pr", PR, "--body-out", str(target)], capsys
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "--branch" in err
    assert err.count("\n") == 1, repr(err)
    assert not target.exists()


@pytest.mark.parametrize("subject", ["第一行\n第二行", "带\r回车", "\n"])
def test_rc2_subject_with_newline(
    subject: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--subject`` 含换行 → rc2（复用 ``merge_guard.check_subject``），且不写 body 文件。

    含换行的标题经 ``shlex.quote`` 后仍是**带字面换行**的引号串 —— 打印行会被切成两行，
    照抄即半条命令。生成阶段就拒，省得先打出半条命令再让人去撞 merge_guard 的 rc2。
    """

    target = tmp_path / "never-written.txt"

    rc, out, err = run(
        [
            "--card", CARD,
            "--by", BY,
            "--pr", PR,
            "--branch", BRANCH,
            "--subject", subject,
            "--body-out", str(target),
        ],
        capsys,
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "换行" in err or "不能为空" in err
    assert err.count("\n") == 1, repr(err)
    assert not target.exists()


def test_without_body_out_is_rc2(capsys: pytest.CaptureFixture[str]) -> None:
    """缺 ``--body-out`` → rc2、stdout 0 字节、stderr 恰一行（``--body-file`` 必须真实存在）。"""

    rc, out, err = run(
        ["--card", CARD, "--by", BY, "--pr", PR, "--branch", BRANCH], capsys
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert "--body-out" in err
    assert err.count("\n") == 1, repr(err)


@pytest.mark.parametrize(
    ("branch", "needle"),
    [
        ("", "不能是空串"),
        (" wt/t_x", "不能含空白字符"),
        ("wt/t x", "不能含空白字符"),
        ("refs/heads/wt/t_x", "不能带 refs/ 前缀"),
    ],
)
def test_rc2_branch_shape(
    branch: str,
    needle: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--branch`` 形态非法（复用 ``merge_guard.check_branch_shape``）→ rc2、stdout 空、一行。"""

    target = tmp_path / "never-written.txt"

    rc, out, err = run(
        [
            "--card", CARD,
            "--by", BY,
            "--pr", PR,
            "--branch", branch,
            "--body-out", str(target),
        ],
        capsys,
    )

    assert rc == 2, (rc, out, err)
    assert out == ""
    assert err.startswith("错误: ")
    assert needle in err
    assert err.count("\n") == 1, repr(err)
    assert not target.exists()


# ------------------------------------------------------------- 预检模式：绿


def test_check_file_green(
    body_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """三字段齐 → rc0 + 一行通过文案。"""

    rc, out, err = run(["--check-file", str(body_file)], capsys)

    assert rc == 0, (rc, out, err)
    assert err == ""
    assert out == "通过：三字段齐（merged-by/merge-card/merge-pr）\n"


def test_check_file_green_with_consistent_params(
    body_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--card/--by/--pr`` 与文件内容一致 → 仍 rc0（预检模式不要求 ``--branch``）。"""

    rc, out, err = run(
        ["--check-file", str(body_file), "--card", CARD, "--by", BY, "--pr", PR], capsys
    )

    assert rc == 0, (rc, out, err)
    assert out.startswith("通过：")


# ------------------------------------------------------------- 预检模式：红


def test_check_file_missing_field_is_red(
    body_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """删掉 ``merge-pr`` 那行 → rc1 ``问题: 缺字段：merge-pr``。"""

    broken = tmp_path / "missing-pr.txt"
    broken.write_text(
        "".join(line for line in body_file.read_text(encoding="utf-8").splitlines(True)
                if not line.startswith("merge-pr: ")),
        encoding="utf-8",
    )

    rc, out, err = run(["--check-file", str(broken)], capsys)

    assert rc == 1, (rc, out, err)
    assert err == ""
    assert "问题: 缺字段：merge-pr" in out


def test_check_file_bad_format_is_red(
    body_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``merged-by: pm``（没有 ``/run<N>``）→ rc1 ``问题: 格式非法：merged-by``。"""

    text = body_file.read_text(encoding="utf-8").replace(
        f"merged-by: {BY}", "merged-by: pm"
    )
    broken = tmp_path / "bad-format.txt"
    broken.write_text(text, encoding="utf-8")

    rc, out, err = run(["--check-file", str(broken)], capsys)

    assert rc == 1, (rc, out, err)
    assert '问题: 格式非法：merged-by 的值 "pm" 不合法' in out


def test_check_file_param_mismatch_is_red(
    body_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """文件里是 ``t_ad5e7ff6``、参数要 ``t_11112222`` → rc1 ``字段与参数不一致``。"""

    rc, out, err = run(
        ["--check-file", str(body_file), "--card", "t_11112222"], capsys
    )

    assert rc == 1, (rc, out, err)
    assert "字段与参数不一致" in out
    assert "merge-card" in out


def test_check_file_misplaced_field_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """字段写在正文（不在末尾 trailer 块）→ rc1 ``字段未落在 trailer 区``。"""

    body = tmp_path / "misplaced.txt"
    body.write_text(
        f"{DEFAULT_TITLE}\n\nmerge-card: {CARD}\n\n后续说明段落不属于 trailer 块\n",
        encoding="utf-8",
    )

    rc, out, err = run(["--check-file", str(body)], capsys)

    assert rc == 1, (rc, out, err)
    assert "字段未落在 trailer 区：merge-card" in out


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
        target.write_bytes(b"merged-by: \xff\xfe\n")
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
        [
            "--card", CARD,
            "--by", BY,
            "--pr", PR,
            "--branch", BRANCH,
            "--body-out", str(target),
        ],
        capsys,
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
            "--branch",
            BRANCH,
            "--repo",
            str(tmp_path),
            "--body-out",
            str(tmp_path / "ascii-body.txt"),
        ],
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == b""
    assert proc.stderr.startswith("错误: ".encode())
    assert b"Traceback" not in proc.stderr


# ------------------------- 文档守卫：README / docs 里的 merge-record 生成命令必须真能跑
#
# merge-record 的生成模式契约改过两次（``--pr`` 取值域、现在的 ``--branch`` /
# ``--body-out`` 必填并改打印 merge_guard 命令），每次都有「文档里的命令照抄即 rc2」。
# 所以这里不靠人眼对：把两份文档里每条 ``merge-record …`` **生成**命令抠出来，占位符
# 换成型别合法的值，真跑 ``cli.main([...])``，必须 rc0。文档漂移 → CI 红。
#
# 两类不算生成命令（都跳过）：
# - ``--check-file`` 预检命令：它要的 body 文件在文档里并不存在；
# - 只提了一个参数名的片段（如「由 ``merge-record --body-out`` 生成」）：不是完整命令。

_DOC_FILES = ("README.md", "docs/merge-accountability.md")
_CARD_TOKEN = "t_bbeab29f"
_CONTINUATION = re.compile(r"\\\s*$")


def _logical_lines(path: Path) -> list[str]:
    """按 ``\\`` 续行把物理行拼成逻辑行（文档里的命令块都是这么排的）。"""

    logical: list[str] = []
    buffer = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        continued = _CONTINUATION.search(line)
        if continued is not None:
            buffer += line[: continued.start()]
            continue
        logical.append(buffer + line)
        buffer = ""
    if buffer:
        logical.append(buffer)
    return logical


def _documented_generate_commands() -> list[tuple[str, list[str]]]:
    """抠出两份文档里每条 ``merge-record`` 生成命令的 ``(文件, 参数列表)``。"""

    found: list[tuple[str, list[str]]] = []
    for name in _DOC_FILES:
        for line in _logical_lines(REPO_ROOT / name):
            at = line.find("merge-record")
            if at < 0:
                continue
            rest = line[at + len("merge-record"):]
            # 行内命令用反引号收尾；说明文字用破折号 / 分号 / 句号收尾
            for marker in ("`", "——", "；", "。"):
                cut = rest.find(marker)
                if cut >= 0:
                    rest = rest[:cut]
            rest = rest.strip()
            if not rest:
                continue
            args = shlex.split(rest)
            if "--check-file" in args or "--card" not in args:
                continue
            found.append((name, args))
    return found


def _fill_doc_placeholders(args: list[str], tmp_path: Path) -> list[str]:
    """把文档里的占位符换成型别合法的值；留下任何 ``<…>`` 都算文档 bug。"""

    body = str(tmp_path / "doc-body.txt")
    mapping = (
        ("pm/runNNN", "pm/run1"),
        ("pm/run<NNN>", "pm/run1"),
        ("<profile>/run<NNN>", "pm/run1"),
        ("t_xxxxxxx", _CARD_TOKEN),
        ("t_xxx", _CARD_TOKEN),
        ("<卡号>", _CARD_TOKEN),
        ("<profile>", "pm"),
        ("<NNN>", "1"),
        ("<body文件>", body),
        ("<body-file>", body),
        ("NN", PR),
    )
    filled: list[str] = []
    for arg in args:
        for token, value in mapping:
            arg = arg.replace(token, value)
        if arg.startswith("/tmp/"):
            arg = body
        assert "<" not in arg and ">" not in arg, f"文档命令里有没填的占位符：{arg}"
        filled.append(arg)
    return filled


def test_docs_merge_record_examples_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """两份文档里每条 ``merge-record`` 生成命令（占位符填掉）→ rc0，且打印 merge_guard。"""

    commands = _documented_generate_commands()

    # 文档增删样例都必须跟着改这里，否则守卫悄悄瞎掉
    assert len(commands) == 4, commands
    names = [name for name, _ in commands]
    assert names.count("README.md") == 2, commands
    assert names.count("docs/merge-accountability.md") == 2, commands

    for name, args in commands:
        filled = _fill_doc_placeholders(args, tmp_path)
        assert "--branch" in filled, (name, filled)
        assert "--body-out" in filled, (name, filled)
        rc, out, err = run(filled, capsys)
        assert rc == 0, (name, filled, err)
        assert err == ""
        assert "team_board.merge_guard" in out, (name, out)
