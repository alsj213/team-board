"""``cli verify-batch`` 的集成测试（E3）：真 ``cli.main``、真临时 git 仓、真子进程检查。

对应本卡（E3）验收标准 1–5 条：

1. :class:`TestDryRun` —— ``--dry-run`` 不建 worktree（``git worktree list`` 行数不变）、
   不写账本（``--log-dir`` 里一个文件都没有），打印压缩率行 + 每组一行 ``plan:``；
2. :class:`TestRunPass` —— 真跑 2 unit / 1 组 → rc0，摘要含 ``batch:`` / ``group: exit:`` /
   ``unit: … verdict=PASS`` / ``counts:`` / ``log:`` / ``batch-json:``，且原始输出只进日志；
3. :class:`TestRc1` / :class:`TestRc2` —— 有 FAIL → rc1 且该 unit 的 FAIL 行在 stdout；
   坏 plan（units 空 / pr=0 / 13 位毫秒）→ rc2、stderr 恰一行 ``错误: …``、stdout 0 字节；
4. :class:`TestSolo` —— 单 unit plan → ``degraded: … 单 unit 走逐卡流程``，rc 与逐卡一致
   （检查全过 rc0 / 自检失败 rc1）；
5. :class:`TestHelp` —— ``--help`` 与 ``verify-batch --help`` 均 rc0。

仓里每次用例都用 ``tmp_path`` 新建的仓，不碰本仓；``GIT_CONFIG_GLOBAL=os.devnull`` 挡住宿主
全局配置（与 ``test_verifybatch_run.py`` 同一套）。子命令一律用 ``sys.executable -c`` 起，
不依赖仓库里的 pytest / ruff 可执行文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from team_board import cli

CARD_A = "t_aaaaaaaa"
CARD_B = "t_bbbbbbbb"

_ENV = {
    key: value
    for key, value in os.environ.items()
    if key
    not in {
        "PYTHONPATH",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_PARAMETERS",
        "PYTHONIOENCODING",
        "GIT_CONFIG_GLOBAL",
    }
}
_ENV.update(
    {
        "GIT_AUTHOR_NAME": "verifybatch-test",
        "GIT_AUTHOR_EMAIL": "verifybatch@example.com",
        "GIT_COMMITTER_NAME": "verifybatch-test",
        "GIT_COMMITTER_EMAIL": "verifybatch@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
)


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """跑一条 git 命令（**不断言**退出码）。"""

    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=dict(_ENV),
        check=False,
    )


def must_git(cwd: Path, *args: str) -> str:
    proc = git(cwd, *args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def worktree_count(repo: Path) -> int:
    """``git worktree list`` 的行数（主工作树 + 每个附加 worktree 各一行）。"""

    return len(must_git(repo, "worktree", "list").splitlines())


def commit_file(repo: Path, name: str, body: str, message: str) -> str:
    """写文件 → add → commit，返回新提交的 40 位 sha。"""

    (repo / name).write_text(body, encoding="utf-8")
    must_git(repo, "add", "-A")
    must_git(repo, "commit", "-q", "-m", message)
    return must_git(repo, "rev-parse", "HEAD").strip()


class Repo:
    """一个临时仓：``base`` → ``first``（改 a.txt）→ ``twin``（同 tree、不同提交 = §7 的双胞胎 head）。"""

    def __init__(self, root: Path) -> None:
        self.path = root / "repo"
        self.path.mkdir()
        must_git(self.path, "init", "-q", "-b", "main")
        self.base = commit_file(self.path, "a.txt", "one\n", "base")
        self.first = commit_file(self.path, "a.txt", "two\n", "change a")
        # 同 tree、不同 sha：分组键是 ``<head>^{tree}``，因此与 first 必同组且 kind=patch
        self.twin = must_git(
            self.path,
            "commit-tree",
            f"{self.first}^{{tree}}",
            "-p",
            self.base,
            "-m",
            "twin of change a",
        ).strip()


@pytest.fixture(name="repo")
def repo_fixture(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def check_ok(marker: str) -> list[str]:
    """一条恒 PASS 的检查：打印 ``marker`` 后退出 0。"""

    return [sys.executable, "-c", f"print({marker!r})"]


def check_fail(rc: int, marker: str) -> list[str]:
    """一条恒 FAIL 的检查：打印 ``marker`` 后以 ``rc`` 退出。"""

    return [sys.executable, "-c", f"print({marker!r}); raise SystemExit({rc})"]


def write_plan(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def plan_for(
    repo: Repo,
    *,
    units: list[dict[str, object]],
    group_checks: list[list[str]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"base": repo.base, "units": units}
    payload["group_checks"] = group_checks if group_checks is not None else []
    return payload


def run_cli(
    capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str, str]:
    """直调 ``cli.main``，返回 ``(rc, stdout, stderr)``。"""

    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def lines_with(out: str, prefix: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(prefix)]


def test_dry_run_creates_nothing_and_prints_compression(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """验收 1：``--dry-run`` 不建 worktree、不写账本，打印压缩率 + 每组 ``plan:`` 行。"""

    log_dir = tmp_path / "evidence"
    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            units=[
                {"card": CARD_A, "pr": 53, "head": repo.first},
                # 同 tree、不同 sha ⇒ 同 patch 双胞胎 ⇒ 与上一行同一组（2 unit / 1 组 = 下降 50.0%）
                {"card": CARD_B, "pr": 57, "head": repo.twin},
            ],
        ),
    )
    before = worktree_count(repo.path)

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(log_dir),
        "--dry-run",
    )

    assert (code, err) == (0, ""), err
    lines = out.splitlines()
    assert lines[0] == "若批量，今天 2 次验证可压成 1 次（下降 50.0%）", out
    plan_lines = lines_with(out, "plan: ")
    assert len(plan_lines) == 1, out
    # plan: b-xxxxxxxx kind=patch units=2 heads=<short>,<short>
    fields = plan_lines[0].split()
    assert fields[0] == "plan:"
    assert fields[1].startswith("b-") and len(fields[1]) == len("b-") + 8
    assert fields[2] == "kind=patch"
    assert fields[3] == "units=2"
    heads = fields[4]
    assert heads.startswith("heads=")
    assert heads[len("heads=") :] == ",".join([repo.first[:7], repo.twin[:7]])

    # 不建 worktree、不写账本（整个 log-dir 都没被创建）
    assert worktree_count(repo.path) == before
    assert not log_dir.exists()


def test_dry_run_is_deterministic(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§0 D6：同一份 plan / 仓库状态，两次 ``--dry-run`` 逐字相同。"""

    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            units=[
                {"card": CARD_A, "pr": 53, "head": repo.first},
                {"card": CARD_B, "pr": 57, "head": repo.twin},
            ],
        ),
    )
    first = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(tmp_path / "evidence"),
        "--dry-run",
    )
    second = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(tmp_path / "evidence"),
        "--dry-run",
    )
    assert first == second


def test_two_units_one_group_rc0_summary(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """验收 2：真跑 2 unit / 1 组 → rc0，摘要六类行齐、原始输出只进日志。"""

    log_dir = tmp_path / "evidence"
    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            group_checks=[check_ok("GROUP-RAW-MARKER")],
            units=[
                {"card": CARD_A, "pr": 53, "head": repo.first, "checks": [check_ok("A-RAW")]},
                {"card": CARD_B, "pr": 57, "head": repo.twin, "checks": [check_ok("B-RAW")]},
            ],
        ),
    )
    before = worktree_count(repo.path)

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(log_dir),
    )

    assert (code, err) == (0, ""), err
    lines = out.splitlines()
    assert lines[0].startswith("batch: b-")
    assert "  kind=patch  " in lines[0]
    assert "  units=2" in lines[0]
    assert any(line.startswith("  group: exit:0  ") for line in lines), out
    assert any(
        line.startswith(f"  unit: {CARD_A} pr=53 verdict=PASS  failed_check=-")
        for line in lines
    ), out
    assert any(
        line.startswith(f"  unit: {CARD_B} pr=57 verdict=PASS  failed_check=-")
        for line in lines
    ), out
    assert "counts: groups=1 units=2 pass=2 fail=0" in lines, out
    # degraded 只给 solo 组，patch 组不许出现
    assert not lines_with(out, "degraded: "), out

    log_lines = lines_with(out, "log: ")
    assert len(log_lines) == 1, out
    log_path = Path(log_lines[0][len("log: ") :])
    assert log_path.is_absolute()
    assert log_path.parent == log_dir
    assert log_path.name.startswith("batch-b-") and log_path.name.endswith(".log")

    json_lines = lines_with(out, "batch-json: ")
    assert len(json_lines) == 1, out
    json_path = Path(json_lines[0][len("batch-json: ") :])
    assert json_path.is_absolute()
    assert json_path.parent == log_dir
    assert json_path.name.endswith(".json")

    # stdout 只有摘要：检查组与 unit 打印的原始 marker 一个字都不许上 stdout
    assert "GROUP-RAW-MARKER" not in out
    assert "A-RAW" not in out and "B-RAW" not in out
    log_text = log_path.read_text(encoding="utf-8")
    assert "GROUP-RAW-MARKER" in log_text
    assert "A-RAW" in log_text and "B-RAW" in log_text

    # 账本一份，字段照 §5，verdict 全 PASS
    ledger = json.loads(json_path.read_text(encoding="utf-8"))
    assert ledger["kind"] == "patch"
    assert ledger["base"] == repo.base
    assert ledger["group_rc"] == 0
    assert [unit["verdict"] for unit in ledger["units"]] == ["PASS", "PASS"]
    assert [unit["card"] for unit in ledger["units"]] == [CARD_A, CARD_B]

    # 跑完临时 worktree 已删（--keep-worktrees 没给）
    assert worktree_count(repo.path) == before


def test_keep_worktrees_prints_path(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--keep-worktrees``：路径打进摘要，且该 worktree 真的留在 ``git worktree list`` 里。"""

    log_dir = tmp_path / "evidence"
    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            units=[{"card": CARD_A, "pr": 53, "head": repo.first}],
        ),
    )
    before = worktree_count(repo.path)

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(log_dir),
        "--keep-worktrees",
    )

    assert (code, err) == (0, ""), err
    worktree_lines = lines_with(out, "worktree: ")
    assert len(worktree_lines) == 1, out
    kept = Path(worktree_lines[0][len("worktree: ") :])
    assert kept.is_absolute() and kept.is_dir()
    assert worktree_count(repo.path) == before + 1
    try:
        assert str(kept) in must_git(repo.path, "worktree", "list")
    finally:
        # 自己收拾，免得临时目录挂在 tmp_path 与主仓 worktree 表里
        must_git(repo.path, "worktree", "remove", "--force", str(kept))


def test_any_fail_unit_gives_rc1_and_its_own_line(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """验收 3 前半：A 的自检过、B 的自检 rc=1 → rc1，且 stdout 里有 B 的 FAIL 行、A 仍 PASS。"""

    log_dir = tmp_path / "evidence"
    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            units=[
                {"card": CARD_A, "pr": 53, "head": repo.first, "checks": [check_ok("A-OK")]},
                {
                    "card": CARD_B,
                    "pr": 57,
                    "head": repo.first,
                    "checks": [check_fail(1, "B-BOOM")],
                },
            ],
        ),
    )

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(log_dir),
    )

    assert (code, err) == (1, ""), err
    lines = out.splitlines()
    assert any(
        line.startswith(f"  unit: {CARD_B} pr=57 verdict=FAIL  failed_check=unit:")
        for line in lines
    ), out
    assert any(
        line.startswith(f"  unit: {CARD_A} pr=53 verdict=PASS  failed_check=-")
        for line in lines
    ), out
    assert "counts: groups=1 units=2 pass=1 fail=1" in lines, out


def test_group_check_failure_fails_every_unit(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§0 D3：组级检查 rc≠0 → 组内每个 unit 都 FAIL，``failed_check`` 以 ``group:`` 开头。"""

    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            group_checks=[check_fail(2, "GROUP-BOOM")],
            units=[
                {"card": CARD_A, "pr": 53, "head": repo.first},
                {"card": CARD_B, "pr": 57, "head": repo.twin},
            ],
        ),
    )

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(tmp_path / "evidence"),
    )

    assert (code, err) == (1, ""), err
    fail_lines = [
        line
        for line in out.splitlines()
        if line.startswith("  unit: ") and "verdict=FAIL" in line
    ]
    assert len(fail_lines) == 2, out
    for card, pr in ((CARD_A, 53), (CARD_B, 57)):
        assert any(
            line.startswith(f"  unit: {card} pr={pr} verdict=FAIL  failed_check=group:")
            for line in fail_lines
        ), out
    assert "counts: groups=1 units=2 pass=0 fail=2" in out


def test_solo_degrades_and_passes(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """验收 4 前半：单 unit plan → ``degraded:`` 行，检查全过时 rc0（与逐卡一致）。"""

    log_dir = tmp_path / "evidence"
    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            group_checks=[check_ok("GROUP-OK")],
            units=[
                {"card": CARD_A, "pr": 53, "head": repo.first, "checks": [check_ok("A-OK")]}
            ],
        ),
    )

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(log_dir),
    )

    assert (code, err) == (0, ""), err
    lines = out.splitlines()
    assert "  kind=solo  " in lines[0], out
    degraded = lines_with(out, "degraded: ")
    assert len(degraded) == 1, out
    assert degraded[0].endswith(" 单 unit 走逐卡流程"), degraded
    assert degraded[0] == f"degraded: {lines[0].split()[1]} 单 unit 走逐卡流程"
    assert "counts: groups=1 units=1 pass=1 fail=0" in lines, out


def test_solo_failing_check_gives_rc1(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """验收 4 后半：单 unit 的自检 rc≠0 → rc1（与逐卡验证同一码），``degraded:`` 照打。"""

    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            units=[
                {
                    "card": CARD_A,
                    "pr": 53,
                    "head": repo.first,
                    "checks": [check_fail(1, "A-BOOM")],
                }
            ],
        ),
    )

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(tmp_path / "evidence"),
    )

    assert (code, err) == (1, ""), err
    assert lines_with(out, "degraded: "), out
    assert any(
        line.startswith(f"  unit: {CARD_A} pr=53 verdict=FAIL  failed_check=unit:")
        for line in out.splitlines()
    ), out


#: §1 的三类坏 plan：``units`` 空 / ``pr=0`` / ``pr`` 是 13 位毫秒
BAD_PLANS = {
    "units-empty": {"base": "origin/main", "units": []},
    "pr-zero": {
        "base": "origin/main",
        "units": [{"card": CARD_A, "pr": 0, "head": "main"}],
    },
    "pr-thirteen-digit-ms": {
        "base": "origin/main",
        "units": [{"card": CARD_A, "pr": 1793160000000, "head": "main"}],
    },
}


@pytest.mark.parametrize("name", sorted(BAD_PLANS))
def test_bad_plan_is_rc2_one_error_line_and_empty_stdout(
    name: str,
    repo: Repo,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验收 3 后半：坏 plan → rc2、stderr 恰一行 ``错误: …``、stdout 0 字节、无 traceback。"""

    log_dir = tmp_path / "evidence"
    plan = write_plan(tmp_path / "plan.json", BAD_PLANS[name])

    # 真跑与 --dry-run 都是 rc2（`--dry-run` 的 plan 校验失败同样 rc2，§4）
    for extra in ((), ("--dry-run",)):
        code, out, err = run_cli(
            capsys,
            "verify-batch",
            "--plan",
            str(plan),
            "--repo",
            str(repo.path),
            "--log-dir",
            str(log_dir),
            *extra,
        )
        assert code == 2, (name, extra, out, err)
        assert out == "", (name, extra, out)
        assert err.startswith("错误: "), (name, extra, err)
        assert err.endswith("\n") and err.count("\n") == 1, (name, extra, err)
        assert "Traceback" not in err, (name, extra, err)

    # 一条都不许落盘
    assert not log_dir.exists()


def test_missing_plan_file_is_rc2(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """plan 文件不存在也是 rc2（``load_plan`` 的 ``OSError`` → ``VerifyBatchError``）。"""

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(tmp_path / "nope.json"),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(tmp_path / "evidence"),
    )

    assert (code, out) == (2, ""), out
    assert err.startswith("错误: ") and err.count("\n") == 1, err


def test_unknown_head_is_rc2(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``head`` 解析不成提交 → rc2（§3.1 整条命令失败，不静默跳过），stdout 空。"""

    plan = write_plan(
        tmp_path / "plan.json",
        plan_for(
            repo,
            units=[{"card": CARD_A, "pr": 53, "head": "deadbeefdeadbeef"}],
        ),
    )

    code, out, err = run_cli(
        capsys,
        "verify-batch",
        "--plan",
        str(plan),
        "--repo",
        str(repo.path),
        "--log-dir",
        str(tmp_path / "evidence"),
    )

    assert (code, out) == (2, ""), out
    assert err.startswith("错误: ") and err.count("\n") == 1, err


def test_plan_is_required(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--plan`` 必填：不给是 argparse 级 rc2（不是 traceback）。"""

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["verify-batch", "--repo", str(repo.path)])
    assert excinfo.value.code == 2


class TestHelp:
    """验收 5 前半：两个 ``--help`` 都 rc0。"""

    def test_top_level_help_rc0(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--help"])
        out = capsys.readouterr().out
        assert excinfo.value.code == 0
        assert "verify-batch" in out
        # 顶层 help 的条目里带 rc 三档说明（``help=`` 原文，argparse 会按宽度折行）
        assert "verify-batch        按 patch-id 分组跑全套检查" in out
        assert "0 全 PASS / 1 有 FAIL / 2" in out

    def test_subcommand_help_rc0(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["verify-batch", "--help"])
        out = capsys.readouterr().out
        assert excinfo.value.code == 0
        for option in ("--plan", "--repo", "--dry-run", "--log-dir", "--keep-worktrees"):
            assert option in out, (option, out)
        assert "usage: team-board verify-batch" in out
