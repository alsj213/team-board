"""``verifybatch`` 执行层的集成测试（E2）：真临时 git 仓、真 ``worktree add``、真子进程检查。

对应本卡（E2）验收标准 1–5 条：

1. :class:`TestGroupFailureFailsEveryUnit` —— 组级检查失败 → 组内**每个** unit 都 FAIL 且
   ``failed_check="group:<argv>"``（§0 D3；证明不是「整批绿」，也不是「只打一个」）；
2. :class:`TestUnitFailureIsolated` —— 同组两 unit、只有 B 的自检 rc≠0 → A PASS / B FAIL，
   且 ``A.failed_check is None``（需求硬验收）；
3. :class:`TestLogAndSummary` —— ``evidence/batch-<batch_id>.log`` 含子进程原始输出，
   而 stdout 摘要一行原始输出都没有（比字节数）；
4. :class:`TestBatchJson` —— 每组一个账本、``sort_keys`` + ``ensure_ascii=False`` + 末尾换行，
   连跑两次除 ``created_at`` 外逐字相同（归一后真跑 ``diff``，贴 rc）；
5. :class:`TestWorktreeCleanup` —— 跑完 ``git worktree list`` 里没有临时路径；``keep_worktrees``
   时保留并打进摘要；另加 :class:`TestSoloDegraded`（§0 D4 的 solo 摘要行）。

仓里每次用例都用 ``tmp_path`` 新建的仓，不碰本仓；``GIT_CONFIG_GLOBAL=os.devnull`` 挡住宿主
全局配置，身份用环境变量给（与 ``test_verifybatch_grouping.py`` 同一套）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from team_board import verifybatch as vb
from team_board.verifybatch import Unit

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


def commit_file(repo: Path, name: str, body: str, message: str) -> str:
    """写文件 → add → commit，返回新提交的 40 位 sha。"""

    (repo / name).write_text(body, encoding="utf-8")
    must_git(repo, "add", "-A")
    must_git(repo, "commit", "-q", "-m", message)
    return must_git(repo, "rev-parse", "HEAD").strip()


class Repo:
    """一个临时仓：``base`` → ``first``（改 a.txt）→ ``second``（加 b.txt）。"""

    def __init__(self, root: Path) -> None:
        self.path = root / "repo"
        self.path.mkdir()
        must_git(self.path, "init", "-q", "-b", "main")
        self.base = commit_file(self.path, "a.txt", "one\n", "base")
        self.first = commit_file(self.path, "a.txt", "two\n", "change a")
        self.second = commit_file(self.path, "b.txt", "three\n", "change b")


@pytest.fixture(name="repo")
def repo_fixture(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def check_ok(marker: str) -> tuple[str, ...]:
    """一条恒 PASS 的检查：打印 ``marker`` 后退出 0。"""

    return (sys.executable, "-c", f"print({marker!r})")


def check_fail(rc: int, marker: str) -> tuple[str, ...]:
    """一条恒 FAIL 的检查：打印 ``marker`` 后以 ``rc`` 退出。"""

    return (sys.executable, "-c", f"print({marker!r}); raise SystemExit({rc})")


def check_noisy(stdout_marker: str, stderr_marker: str, lines: int = 200) -> tuple[str, ...]:
    """往 stdout 与 stderr 各写原始输出，再灌 ``lines`` 行填充：证明「日志有、摘要没有」。"""

    code = (
        "import sys;"
        f"print({stdout_marker!r});"
        f"sys.stderr.write({stderr_marker!r} + chr(10));"
        f"[print('padding line %d' % i) for i in range({lines})]"
    )
    return (sys.executable, "-c", code)


def groups_for(repo: Repo, units: tuple[Unit, ...], base: str | None = None):
    """走 E1 的真分组（同 head 的两个 unit 必然同 patch → 一组）。"""

    return vb.build_groups(
        repo.path,
        vb.Plan(base=base or repo.base, group_checks=(), units=units),
    )


def two_units(
    head: str, check_a: tuple[str, ...], check_b: tuple[str, ...]
) -> tuple[Unit, ...]:
    """同一 head 的两个 unit（同 patch 同代码 → 同组），各自带自己的自检。"""

    return (
        Unit(card=CARD_A, pr=53, head=head, checks=(check_a,)),
        Unit(card=CARD_B, pr=57, head=head, checks=(check_b,)),
    )


class TestGroupFailureFailsEveryUnit:
    """验收第 1 条：组级失败 → 组内每个 unit 都 FAIL，且归因到组级那条 argv。"""

    def test_every_unit_fails_and_all_group_checks_still_run(
        self, repo: Repo, tmp_path: Path
    ) -> None:
        broken = check_fail(2, "group broke")
        later = check_ok("second group check still ran")
        units = two_units(repo.first, check_ok("A ok"), check_ok("B ok"))
        groups = groups_for(repo, units)
        assert len(groups) == 1
        assert len(groups[0].units) == 2

        runs = vb.run_groups(
            repo.path,
            groups,
            log_dir=tmp_path / "evidence",
            group_checks=(broken, later),
        )
        run = runs[0]

        # 组级 rc 记第一条非 0
        assert run.group_rc == 2
        # 每个 unit 都 FAIL —— 不是整批绿，也不是只打第一个
        assert [result.verdict for result in run.results] == ["FAIL", "FAIL"]
        expected = f"group:{' '.join(broken)}"
        assert [result.failed_check for result in run.results] == [expected, expected]
        assert [result.rc for result in run.results] == [2, 2]
        # D3 的诚实归因：unit 自己的自检是绿的，但仍因组级失败而 FAIL
        assert all("A ok" not in (result.failed_check or "") for result in run.results)

        # 组级检查「后面的仍跑完」（便于诊断）：两条组级检查各留一个 header 行
        log_text = Path(run.log_path).read_text(encoding="utf-8")
        headers = [
            line
            for line in log_text.splitlines()
            if line.startswith("--- group: ") and "(cwd=" in line
        ]
        assert len(headers) == 2
        assert " ".join(broken) in log_text
        assert " ".join(later) in log_text
        assert "second group check still ran" in log_text

        summary = vb.render_summary(runs, batch_dir=tmp_path / "evidence")
        assert summary.count("verdict=FAIL") == 2
        assert "failed_check=-" not in summary
        print(f"group_rc={run.group_rc} expected_failed_check={expected}")
        print(summary)


class TestUnitFailureIsolated:
    """验收第 2 条（需求硬验收）：只有 B 的自检红 → A PASS / B FAIL。"""

    def test_only_failing_unit_is_marked(self, repo: Repo, tmp_path: Path) -> None:
        broken_b = check_fail(1, "B broken")
        units = two_units(repo.first, check_ok("A ok"), broken_b)
        groups = groups_for(repo, units)
        assert len(groups) == 1

        runs = vb.run_groups(
            repo.path, groups, log_dir=tmp_path / "evidence", group_checks=(check_ok("group ok"),)
        )
        run = runs[0]
        by_card = {result.card: result for result in run.results}

        assert run.group_rc == 0
        assert by_card[CARD_A].verdict == "PASS"
        assert by_card[CARD_A].failed_check is None
        assert by_card[CARD_A].rc == 0
        assert by_card[CARD_B].verdict == "FAIL"
        assert by_card[CARD_B].failed_check == f"unit:{' '.join(broken_b)}"
        assert by_card[CARD_B].rc == 1

        summary = vb.render_summary(runs, batch_dir=tmp_path / "evidence")
        assert "counts: groups=1 units=2 pass=1 fail=1" in summary
        assert f"  unit: {CARD_A} pr=53 verdict=PASS  failed_check=-" in summary
        print(summary)


class TestUnrunnableCheck:
    """一条跑不起来的命令（可执行文件不存在）算「这条检查失败」，不把整批打成 rc2。"""

    def test_missing_binary_is_a_failed_check(self, repo: Repo, tmp_path: Path) -> None:
        units = (
            Unit(card=CARD_A, pr=53, head=repo.first, checks=(("/nonexistent/checker",),)),
        )
        runs = vb.run_groups(repo.path, groups_for(repo, units), log_dir=tmp_path / "evidence")
        result = runs[0].results[0]
        assert result.verdict == "FAIL"
        assert result.rc == 127
        assert result.failed_check == "unit:/nonexistent/checker"
        assert "找不到可执行文件" in Path(runs[0].log_path).read_text(encoding="utf-8")


class TestLogAndSummary:
    """验收第 3 条：日志含子进程原始输出，stdout 摘要不含。"""

    def test_log_has_raw_output_summary_does_not(self, repo: Repo, tmp_path: Path) -> None:
        log_dir = tmp_path / "evidence"
        noisy = check_noisy("RAW-STDOUT-MARKER", "RAW-STDERR-MARKER")
        units = (Unit(card=CARD_A, pr=53, head=repo.first, checks=(noisy,)),)

        runs = vb.run_groups(repo.path, groups_for(repo, units), log_dir=log_dir)
        run = runs[0]
        log_file = Path(run.log_path)

        # 路径与命名：<log_dir>/batch-<batch_id>.log，绝对路径
        assert log_file.name == f"batch-{run.group.batch_id}.log"
        assert log_file.parent == log_dir.resolve()
        assert log_file.is_absolute()

        log_text = log_file.read_text(encoding="utf-8")
        assert "RAW-STDOUT-MARKER" in log_text
        assert "RAW-STDERR-MARKER" in log_text
        assert "--- unit t_aaaaaaaa: " in log_text

        summary = vb.render_summary(runs, batch_dir=log_dir)
        assert "RAW-STDOUT-MARKER" not in summary
        assert "RAW-STDERR-MARKER" not in summary
        log_bytes = log_file.stat().st_size
        summary_bytes = len(summary.encode("utf-8"))
        assert log_bytes > summary_bytes

        # 摘要该有的行一行不少（§4 的格式）
        for expected in (
            f"batch: {run.group.batch_id}  kind=solo  head={repo.first[:7]}  units=1",
            "  group: exit:0  失败行（0/0，≤20）:",
            f"  unit: {CARD_A} pr=53 verdict=PASS  failed_check=-",
            "counts: groups=1 units=1 pass=1 fail=0",
            f"log: {run.log_path}",
        ):
            assert expected in summary, expected
        assert f"batch-json: {log_dir.resolve() / f'batch-{run.group.batch_id}.json'}" in summary

        print(f"log_bytes={log_bytes} summary_bytes={summary_bytes}")
        print(f"log_name={log_file.name}")
        print(summary)


class TestBatchJson:
    """验收第 4 条：每组一个账本 + 固定序列化 + 两次跑除 created_at 外逐字相同。"""

    def test_format_and_determinism(self, repo: Repo, tmp_path: Path) -> None:
        batch_dir = tmp_path / "evidence"
        units = (
            Unit(card=CARD_A, pr=53, head=repo.first, checks=(check_ok("A ok"),)),
            Unit(card=CARD_B, pr=57, head=repo.first, checks=(check_fail(1, "B broken"),)),
        )
        groups = groups_for(repo, units)
        assert len(groups) == 1

        def one_round() -> tuple[tuple[str, ...], str]:
            runs = vb.run_groups(repo.path, groups, log_dir=batch_dir)
            paths = vb.write_batch_json(batch_dir, runs, base=repo.base)
            assert len(paths) == 1
            return paths, Path(paths[0]).read_text(encoding="utf-8")

        paths_first, first = one_round()
        paths_second, second = one_round()
        assert paths_first == paths_second
        assert Path(paths_first[0]).name == f"batch-{groups[0].batch_id}.json"

        # 序列化口径逐字对齐：sort_keys=True / ensure_ascii=False / 缩进 2 / 末尾换行
        payload = json.loads(first)
        assert first.endswith("\n")
        assert not first.endswith("\n\n")
        assert first == json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"

        # 结构：§5 的字段一个不少，逐 unit 带 log
        assert payload["batch_id"] == groups[0].batch_id
        assert payload["kind"] == "patch"
        assert payload["key"] == groups[0].key
        assert payload["base"] == repo.base
        assert payload["head_sha"] == repo.first
        assert isinstance(payload["created_at"], int)
        assert payload["group_rc"] == 0
        assert [item["card"] for item in payload["units"]] == [CARD_A, CARD_B]
        assert [item["verdict"] for item in payload["units"]] == ["PASS", "FAIL"]
        assert all(item["head_sha"] == repo.first for item in payload["units"])
        assert all(item["log"].endswith(".log") for item in payload["units"])

        # 除 created_at 外逐字相同 —— 归一后真跑 diff，rc 必须是 0
        def strip_created_at(text: str) -> str:
            return re.sub(r'"created_at": \d+', '"created_at": 0', text)

        left = tmp_path / "first.json"
        right = tmp_path / "second.json"
        left.write_text(strip_created_at(first), encoding="utf-8")
        right.write_text(strip_created_at(second), encoding="utf-8")
        proc = subprocess.run(
            ["diff", "-u", str(left), str(right)], capture_output=True, text=True, check=False
        )
        print(f"diff rc={proc.returncode}")
        assert proc.returncode == 0, proc.stdout
        print(f"batch_json={paths_first[0]}")
        print(f"created_at_first={payload['created_at']}")


class TestWorktreeCleanup:
    """验收第 5 条：跑完不留 worktree；``keep_worktrees`` 时保留并打进摘要。"""

    def test_worktree_removed_after_run(self, repo: Repo, tmp_path: Path) -> None:
        units = (Unit(card=CARD_A, pr=53, head=repo.first, checks=(check_ok("A ok"),)),)
        runs = vb.run_groups(repo.path, groups_for(repo, units), log_dir=tmp_path / "evidence")

        listing = must_git(repo.path, "worktree", "list", "--porcelain")
        assert "verify-batch-" not in listing
        assert runs[0].worktree is None
        # 临时根目录也一起删了
        assert not Path(runs[0].log_path).parent.joinpath("verify-batch-").exists()
        worktree_lines = [
            line for line in listing.splitlines() if line.startswith("worktree ")
        ]
        print(f"worktrees_after_run={len(worktree_lines)}")

    def test_keep_worktrees(self, repo: Repo, tmp_path: Path) -> None:
        units = (Unit(card=CARD_A, pr=53, head=repo.first, checks=(check_ok("A ok"),)),)
        runs = vb.run_groups(
            repo.path,
            groups_for(repo, units),
            log_dir=tmp_path / "evidence",
            keep_worktrees=True,
        )
        kept = runs[0].worktree
        assert kept is not None
        assert Path(kept).is_dir()
        assert Path(kept).name == f"vb-{runs[0].group.batch_id}"

        listing = must_git(repo.path, "worktree", "list")
        assert kept in listing

        summary = vb.render_summary(runs, batch_dir=tmp_path / "evidence")
        assert f"worktree: {kept}" in summary
        print(f"kept={kept}")

        # 用例自己收尾，别把临时目录留在盘上
        must_git(repo.path, "worktree", "remove", "--force", kept)
        shutil.rmtree(Path(kept).parent, ignore_errors=True)
        assert kept not in must_git(repo.path, "worktree", "list")


class TestSoloDegraded:
    """§0 D4：单 unit → ``kind="solo"`` + 一行 ``degraded:``（pm 2026-10-31 口径）。"""

    def test_single_unit_prints_degraded(self, repo: Repo, tmp_path: Path) -> None:
        units = (Unit(card=CARD_A, pr=53, head=repo.first, checks=(check_ok("A ok"),)),)
        runs = vb.run_groups(repo.path, groups_for(repo, units), log_dir=tmp_path / "evidence")
        group = runs[0].group
        assert group.kind == "solo"

        summary = vb.render_summary(runs, batch_dir=tmp_path / "evidence")
        assert f"degraded: {group.batch_id} 单 unit 走逐卡流程" in summary
        # 两 unit 的组不许出现 degraded 行
        multi = two_units(repo.first, check_ok("A ok"), check_ok("B ok"))
        multi_runs = vb.run_groups(
            repo.path, groups_for(repo, multi), log_dir=tmp_path / "evidence"
        )
        multi_summary = vb.render_summary(multi_runs, batch_dir=tmp_path / "evidence")
        assert "degraded:" not in multi_summary
        print(summary)
