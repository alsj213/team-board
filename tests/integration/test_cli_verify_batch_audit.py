"""``verify-batch`` 的 emit 行 + ``verify-batch-audit`` 的集成测试（E4）。

对应本卡（t_b641db6c）验收标准 1–4 条，口径唯一源是契约 v1
（``/home/liu/project/tb-contracts/t_f4e71bd3.md`` §4 emit 行 / §6 审计 / §8 边界）：

1. :class:`TestEmitCommands` —— 逐 unit 一条 ``evidence emit`` 命令原文：逐字含
   ``evidence emit --kind qa_pass``、``head_sha`` 是该 unit 的 head、``--card`` 是该 unit 的卡号、
   ``run_id`` 是占位符（不是猜出来的数字）；**FAIL 的 unit 不生成命令**（§4「缺证据即不许合并」）。
   另外锁 §0 D2 的角色边界：本模块只拼字符串 —— 一次 ``subprocess`` / ``evidence`` 都不碰。
2. :class:`TestAuditRc` —— 造只有 ``tasks`` + ``task_runs`` 两张表的临时库：
   3 条 qa run（有 batch_id / 无 / 形态坏）+ 1 条 engineer run → 只报 2 条违规、rc1；
   全合规 → rc0；``--since`` 不填 → argparse 级 rc2（不设默认值）。
3. :class:`TestAuditNoFile`` —— ``--batch-dir`` 下没有 ``batch-<id>.json`` → ``NOFILE``、属违规。
4. :class:`TestAuditReadOnly` —— ``mode=ro``：跑完库的 sha256 不变；库不存在时 rc2 且**不建文件**。

全部走真 ``cli.main``（真 argparse、真 rc），不 mock 任何东西；临时库一律 ``tmp_path``，
不碰本板真库。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from team_board import cli
from team_board import verifybatch as vb

CARD_A = "t_aaaaaaaa"
CARD_B = "t_c0ffee11"
CARD_C = "t_cccccccc"
CARD_D = "t_dddddddd"

HEAD_A = "af4547101111111111111111111111111111111111"
HEAD_B = "034ae4c0222222222222222222222222222222222"

BATCH_OK = "b-1a2b3c4d"
BATCH_SUMMARY = "b-2c3d4e5f"
BATCH_BAD = "b-NOPE"


# --------------------------------------------------------------------------- 夹具（纯内存）


def _pass_result(card: str, pr: int, head: str, batch_id: str = BATCH_OK) -> vb.UnitResult:
    return vb.UnitResult(
        card=card,
        pr=pr,
        head_sha=head,
        batch_id=batch_id,
        verdict=vb.VERDICT_PASS,
        rc=0,
        failed_check=None,
    )


def _fail_result(
    card: str, pr: int, head: str, failed_check: str, batch_id: str = BATCH_OK
) -> vb.UnitResult:
    return vb.UnitResult(
        card=card,
        pr=pr,
        head_sha=head,
        batch_id=batch_id,
        verdict=vb.VERDICT_FAIL,
        rc=1,
        failed_check=failed_check,
    )


def group_run(
    results: tuple[vb.UnitResult, ...],
    *,
    batch_id: str = BATCH_OK,
    kind: str = vb.KIND_PATCH,
) -> vb.GroupRun:
    """按 ``UnitResult`` 拼一个 ``GroupRun``（不建 worktree、不跑检查 —— emit 行是纯字符串）。"""

    units = tuple(vb.Unit(card=result.card, pr=result.pr, head=result.head_sha) for result in results)
    group = vb.Group(
        batch_id=batch_id,
        kind=kind,
        key=f"tree:{'0' * 40}",
        patch_id=None,
        head_sha=results[0].head_sha,
        units=units,
    )
    return vb.GroupRun(
        group=group,
        group_rc=0,
        log_path=f"/tmp/evidence/batch-{batch_id}.log",
        results=results,
    )


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    """直调 ``cli.main``，返回 ``(rc, stdout, stderr)``。"""

    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write_ledger(batch_dir: Path, batch_id: str) -> Path:
    batch_dir.mkdir(parents=True, exist_ok=True)
    path = batch_dir / f"batch-{batch_id}.json"
    path.write_text(json.dumps({"batch_id": batch_id}, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def make_db(path: Path, runs: list[dict[str, object]]) -> None:
    """造只含 ``tasks`` + ``task_runs`` 两张表的看板库（§0 D1 之外的最小只读面）。"""

    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
        conn.execute(
            """
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                profile TEXT,
                status TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                summary TEXT,
                metadata TEXT
            )
            """
        )
        for run in runs:
            started = run["started"]
            assert isinstance(started, int), started  # 夹具自带，不对外容错
            conn.execute(
                "INSERT INTO tasks (id, title) VALUES (?, ?)",
                (str(run["task"]), "fixture"),
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, summary, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(run["task"]),
                    str(run.get("profile", vb.QA_PROFILE)),
                    str(run.get("status", "done")),
                    started,
                    run.get("summary"),
                    run.get("metadata"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def violation_entries_text(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.split(" ")[0] in {"OK", "MISSING", "BAD", "NOFILE"}]


# --------------------------------------------------------------------------- 验收 1：emit 行


class TestEmitCommands:
    """§4 最后一段：逐 unit 一条 emit 命令原文，FAIL 的 unit 不给。"""

    def test_one_line_per_pass_unit_with_fixed_fragments(self) -> None:
        run = group_run(
            (
                _pass_result(CARD_A, 53, HEAD_A),
                _pass_result(CARD_B, 57, HEAD_B),
            )
        )

        commands = vb.emit_commands(run)

        assert len(commands) == 2
        assert commands[0] == (
            "PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass "
            f"--payload '{{\"head_sha\":\"{HEAD_A}\",\"run_id\":{vb.RUN_ID_PLACEHOLDER}}}' "
            f"--card {CARD_A}"
        )
        for command, (card, head) in zip(commands, ((CARD_A, HEAD_A), (CARD_B, HEAD_B)), strict=True):
            assert "evidence emit --kind qa_pass" in command
            assert f'"head_sha":"{head}"' in command
            assert command.endswith(f"--card {card}")
            assert vb.RUN_ID_PLACEHOLDER in command
            assert "\n" not in command
        print(commands[0])

    def test_run_id_is_a_placeholder_not_a_guessed_number(self) -> None:
        """占位符逐字是 ``<你的 run_id>``；不许拿 ``0`` / ``int(time.time())`` 之类糊上去。"""

        assert vb.RUN_ID_PLACEHOLDER == "<你的 run_id>"

        command = vb.emit_commands(group_run((_pass_result(CARD_A, 53, HEAD_A),)))[0]
        payload = command.split("--payload '", 1)[1].split("'", 1)[0]
        assert payload == f'{{"head_sha":"{HEAD_A}","run_id":{vb.RUN_ID_PLACEHOLDER}}}'
        assert json.loads(payload.replace(vb.RUN_ID_PLACEHOLDER, "1")) == {
            "head_sha": HEAD_A,
            "run_id": 1,
        }

    def test_failed_unit_gets_no_emit_line(self) -> None:
        """§4「缺证据即不许合并」：FAIL 的 unit 一条命令都不生成。"""

        run = group_run(
            (
                _pass_result(CARD_A, 53, HEAD_A),
                _fail_result(CARD_B, 57, HEAD_B, "unit:python -m pytest -q tests/x.py"),
            )
        )

        commands = vb.emit_commands(run)

        assert len(commands) == 1
        assert CARD_A in commands[0]
        assert CARD_B not in commands[0]
        assert HEAD_B not in commands[0]

    def test_group_level_failure_produces_no_emit_line_at_all(self) -> None:
        """组级失败把全组打成 FAIL（D3）→ 该组一条 emit 都没有。"""

        run = group_run(
            (
                _fail_result(CARD_A, 53, HEAD_A, "group:python -m pytest -q"),
                _fail_result(CARD_B, 57, HEAD_B, "group:python -m pytest -q"),
            )
        )

        assert vb.emit_commands(run) == ()

    def test_summary_appends_emit_lines_last(self) -> None:
        """摘要最后一段就是 emit 行（§4 示例），且与 ``emit_commands`` 同一份字符串。"""

        run = group_run(
            (
                _pass_result(CARD_A, 53, HEAD_A),
                _fail_result(CARD_B, 57, HEAD_B, "unit:python -m pytest -q tests/x.py"),
            )
        )

        summary = vb.render_summary((run,), batch_dir="/tmp/evidence")
        lines = summary.splitlines()
        expected = f"{vb.EMIT_PREFIX}{vb.emit_commands(run)[0]}"

        assert lines[-1] == expected
        assert summary.count(f"{vb.EMIT_PREFIX}PYTHONPATH=src") == 1
        assert CARD_B not in "\n".join(
            line for line in lines if line.startswith(vb.EMIT_PREFIX)
        )
        print(lines[-1])

    def test_emit_pure_formatting_never_touches_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """§0 D2：本模块只拼字符串 —— 连 ``subprocess`` 都不该被调到。"""

        def boom(*args: object, **kwargs: object) -> None:  # pragma: no cover - 只在违规时命中
            raise AssertionError("emit_commands 不许起子进程（它只打印命令原文）")

        monkeypatch.setattr(vb.subprocess, "run", boom)
        monkeypatch.setattr(vb.subprocess, "Popen", boom)

        command = vb.emit_commands(group_run((_pass_result(CARD_A, 53, HEAD_A),)))[0]
        assert "evidence emit --kind qa_pass" in command


# --------------------------------------------------------------------------- 验收 2：rc0 / rc1 / rc2


class TestAuditRc:
    """验收 2：3 条 qa run（有 / 无 / 形态坏）+ 1 条 engineer → 违规 2、rc1。"""

    def _three_plus_one(self, tmp_path: Path) -> tuple[Path, Path]:
        batch_dir = tmp_path / "evidence"
        write_ledger(batch_dir, BATCH_OK)
        db = tmp_path / "board.db"
        make_db(
            db,
            [
                {
                    "task": CARD_A,
                    "started": 100,
                    "metadata": json.dumps({"batch_id": BATCH_OK}),
                },
                {"task": CARD_B, "started": 200, "summary": "复核完了，没问题"},
                {
                    "task": CARD_C,
                    "started": 300,
                    "metadata": json.dumps({"batch_id": BATCH_BAD}),
                },
                # engineer 的 run 不在审计面内（§6 只扫 profile='qa'）
                {"task": CARD_D, "started": 400, "profile": "engineer", "summary": "写完了"},
            ],
        )
        return db, batch_dir

    def test_two_violations_rc1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db, batch_dir = self._three_plus_one(tmp_path)

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "0", "--batch-dir", str(batch_dir)
        )

        assert (code, err) == (1, "")
        assert violation_entries_text(out) == [
            f"OK run 1 card {CARD_A} started 100",
            f"MISSING run 2 card {CARD_B} started 200",
            f"BAD run 3 card {CARD_C} started 300",
        ]
        assert out.splitlines()[-1] == "结论: 3 条 qa run，违规 2 条"
        assert CARD_D not in out
        print(out.strip())

    def test_all_compliant_rc0(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        batch_dir = tmp_path / "evidence"
        write_ledger(batch_dir, BATCH_OK)
        write_ledger(batch_dir, BATCH_SUMMARY)
        db = tmp_path / "board.db"
        make_db(
            db,
            [
                {"task": CARD_A, "started": 100, "metadata": json.dumps({"batch_id": BATCH_OK})},
                # 第二种来源：summary 里顶格一行 batch_id:
                {"task": CARD_B, "started": 200, "summary": f"验收 PASS\nbatch_id: {BATCH_SUMMARY}\n"},
            ],
        )

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "0", "--batch-dir", str(batch_dir)
        )

        assert (code, err) == (0, "")
        assert violation_entries_text(out) == [
            f"OK run 1 card {CARD_A} started 100",
            f"OK run 2 card {CARD_B} started 200",
        ]
        assert out.splitlines()[-1] == "结论: 2 条 qa run，违规 0 条"

    def test_no_qa_runs_rc0(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db = tmp_path / "board.db"
        make_db(db, [{"task": CARD_D, "started": 100, "profile": "engineer"}])

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "0", "--batch-dir", str(tmp_path)
        )

        assert (code, err) == (0, "")
        assert out.splitlines() == ["结论: 0 条 qa run，违规 0 条"]

    def test_since_is_required(self, tmp_path: Path) -> None:
        """§6：``--since`` 必填、**不设默认值** —— 不填就是 argparse 级 rc2。"""

        db = tmp_path / "board.db"
        make_db(db, [])

        with pytest.raises(SystemExit) as excinfo:
            cli.main(["verify-batch-audit", "--db", str(db)])

        assert excinfo.value.code == 2

    @pytest.mark.parametrize("bad_since", ["", "-1", "+100", " 100", "1_000", "١٢٣", "abc"])
    def test_bad_since_is_rc2(self, tmp_path: Path, bad_since: str) -> None:
        """取值域边界一并卡住（空串 / 负数 / 正号 / 空白 / 下划线 / 非 ASCII 数字 / 非数字）。"""

        db = tmp_path / "board.db"
        make_db(db, [])

        with pytest.raises(SystemExit) as excinfo:
            cli.main(["verify-batch-audit", "--db", str(db), "--since", bad_since])

        assert excinfo.value.code == 2

    def test_since_filters_older_runs(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``started_at >= --since``：老 run 不进审计面（本规则不回溯）。"""

        batch_dir = tmp_path / "evidence"
        write_ledger(batch_dir, BATCH_OK)
        db = tmp_path / "board.db"
        make_db(
            db,
            [
                {"task": CARD_A, "started": 100},
                {"task": CARD_B, "started": 500, "metadata": json.dumps({"batch_id": BATCH_OK})},
            ],
        )

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "500", "--batch-dir", str(batch_dir)
        )

        assert (code, err) == (0, "")
        assert violation_entries_text(out) == [f"OK run 2 card {CARD_B} started 500"]

    def test_running_qa_run_is_skipped(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``status != 'running'``（§6）：在跑的 qa run 还没到验收，不算违规。"""

        db = tmp_path / "board.db"
        make_db(db, [{"task": CARD_A, "started": 100, "status": "running"}])

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "0", "--batch-dir", str(tmp_path)
        )

        assert (code, err) == (0, "")
        assert out.splitlines() == ["结论: 0 条 qa run，违规 0 条"]

    def test_unusable_db_is_rc2_one_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """库不可用 → rc2、stdout 0 字节、stderr 恰一行（不许长得像「没有违规」）。"""

        missing = tmp_path / "nope.db"

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(missing), "--since", "0", "--batch-dir", str(tmp_path)
        )

        assert (code, out) == (2, ""), out
        assert err.startswith("错误: ") and err.count("\n") == 1, err
        # mode=ro 不建文件
        assert not missing.exists()

    def test_db_without_task_runs_is_rc2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """只有 ``tasks`` 没有 ``task_runs`` → rc2（参数或库不可用），不是静默 rc0。"""

        db = tmp_path / "board.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "0", "--batch-dir", str(tmp_path)
        )

        assert (code, out) == (2, ""), out
        assert err.startswith("错误: ") and err.count("\n") == 1, err

    def test_json_report_matches_summary(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db, batch_dir = self._three_plus_one(tmp_path)

        code, out, err = run_cli(
            capsys,
            "verify-batch-audit",
            "--db",
            str(db),
            "--since",
            "0",
            "--batch-dir",
            str(batch_dir),
            "--json",
        )

        assert (code, err) == (1, "")
        payload = json.loads(out)
        assert payload["total"] == 3
        assert payload["violations"] == 2
        assert payload["since"] == 0
        assert payload["batch_dir"] == str(batch_dir.resolve())
        assert [(run["run"], run["status"]) for run in payload["runs"]] == [
            (1, "OK"),
            (2, "MISSING"),
            (3, "BAD"),
        ]

    def test_help_is_rc0(self, capsys: pytest.CaptureFixture[str]) -> None:
        # argparse 的 ``--help`` 走 ``SystemExit(0)``（与 ``verify-batch`` 的用例同一口径）
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["verify-batch-audit", "--help"])
        out, err = capsys.readouterr()

        assert excinfo.value.code == 0
        assert err == ""
        assert "usage: team-board verify-batch-audit" in out
        for option in ("--since", "--db", "--batch-dir", "--json"):
            assert option in out, (option, out)


# --------------------------------------------------------------------------- 验收 3：NOFILE


class TestAuditNoFile:
    """验收 3：``--batch-dir`` 下没有 ``batch-<batch_id>.json`` → ``NOFILE``、属违规。"""

    def test_missing_ledger_is_a_violation(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        empty_dir = tmp_path / "empty-evidence"
        empty_dir.mkdir()
        db = tmp_path / "board.db"
        make_db(db, [{"task": CARD_A, "started": 100, "metadata": json.dumps({"batch_id": BATCH_OK})}])

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "0", "--batch-dir", str(empty_dir)
        )

        assert (code, err) == (1, "")
        assert violation_entries_text(out) == [f"NOFILE run 1 card {CARD_A} started 100"]
        assert out.splitlines()[-1] == "结论: 1 条 qa run，违规 1 条"

    def test_missing_batch_dir_itself_is_nofile_not_rc2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """目录整个不存在也判 ``NOFILE``（账本没了 = 证据没了），不当环境错误放过。"""

        db = tmp_path / "board.db"
        make_db(db, [{"task": CARD_A, "started": 100, "summary": f"batch_id: {BATCH_SUMMARY}"}])

        code, out, err = run_cli(
            capsys,
            "verify-batch-audit",
            "--db",
            str(db),
            "--since",
            "0",
            "--batch-dir",
            str(tmp_path / "never-created"),
        )

        assert (code, err) == (1, "")
        assert violation_entries_text(out) == [f"NOFILE run 1 card {CARD_A} started 100"]

    def test_ledger_of_other_batch_does_not_count(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """名字必须逐字对上 ``batch-<batch_id>.json``：别的批次的账本不算。"""

        batch_dir = tmp_path / "evidence"
        write_ledger(batch_dir, BATCH_SUMMARY)
        db = tmp_path / "board.db"
        make_db(db, [{"task": CARD_A, "started": 100, "metadata": json.dumps({"batch_id": BATCH_OK})}])

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "0", "--batch-dir", str(batch_dir)
        )

        assert (code, err) == (1, "")
        assert violation_entries_text(out) == [f"NOFILE run 1 card {CARD_A} started 100"]


# --------------------------------------------------------------------------- 验收 4：只读


class TestAuditReadOnly:
    """验收 4：库只读（``mode=ro``），跑完 ``sha256`` 不变。"""

    def test_db_unchanged_after_audit(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        batch_dir = tmp_path / "evidence"
        write_ledger(batch_dir, BATCH_OK)
        db = tmp_path / "board.db"
        make_db(
            db,
            [
                {"task": CARD_A, "started": 100, "metadata": json.dumps({"batch_id": BATCH_OK})},
                {"task": CARD_B, "started": 200},
            ],
        )
        before = hashlib.sha256(db.read_bytes()).hexdigest()

        code, out, err = run_cli(
            capsys, "verify-batch-audit", "--db", str(db), "--since", "0", "--batch-dir", str(batch_dir)
        )

        assert (code, err) == (1, ""), err
        assert violation_entries_text(out) == [
            f"OK run 1 card {CARD_A} started 100",
            f"MISSING run 2 card {CARD_B} started 200",
        ]
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before
        print(f"sha256={before}")

    def test_module_opens_db_read_only(self, tmp_path: Path) -> None:
        """``verifybatch.audit_qa_runs`` 只读：不建表、不写（直接调函数也同一口径）。"""

        batch_dir = tmp_path / "evidence"
        write_ledger(batch_dir, BATCH_OK)
        db = tmp_path / "board.db"
        make_db(db, [{"task": CARD_A, "started": 100, "metadata": json.dumps({"batch_id": BATCH_OK})}])
        before = hashlib.sha256(db.read_bytes()).hexdigest()

        report = vb.audit_qa_runs(db, 0, batch_dir)

        assert report.total == 1
        assert report.violations == 0
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before


# --------------------------------------------------------------------------- 端到端：真跑一次 verify-batch


_GIT_ENV = {
    key: value
    for key, value in os.environ.items()
    if key
    not in {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_GLOBAL",
    }
}
_GIT_ENV.update(
    {
        "GIT_AUTHOR_NAME": "emit-e2e",
        "GIT_AUTHOR_EMAIL": "emit-e2e@example.com",
        "GIT_COMMITTER_NAME": "emit-e2e",
        "GIT_COMMITTER_EMAIL": "emit-e2e@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
)


def git_ok(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=dict(_GIT_ENV),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TestEmitEndToEnd:
    """真 ``verify-batch`` 一把：emit 行里的 sha 来自解析后的 head，FAIL 时一行都不该有。"""

    def _repo_and_plan(self, tmp_path: Path, *, rc: int) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        repo.mkdir()
        git_ok(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git_ok(repo, "add", "-A")
        git_ok(repo, "commit", "-q", "-m", "first")
        head = git_ok(repo, "rev-parse", "HEAD")

        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "base": head,
                    "group_checks": [],
                    "units": [
                        {
                            "card": CARD_A,
                            "pr": 53,
                            "head": head,
                            "checks": [[sys.executable, "-c", f"raise SystemExit({rc})"]],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return repo, plan

    def test_pass_unit_gets_emit_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo, plan = self._repo_and_plan(tmp_path, rc=0)
        head = git_ok(repo, "rev-parse", "HEAD")

        code, out, err = run_cli(
            capsys,
            "verify-batch",
            "--plan",
            str(plan),
            "--repo",
            str(repo),
            "--log-dir",
            str(tmp_path / "evidence"),
        )

        assert (code, err) == (0, ""), err
        emit_lines = [line for line in out.splitlines() if line.startswith(vb.EMIT_PREFIX)]
        assert emit_lines == [
            f"{vb.EMIT_PREFIX}PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass "
            f"--payload '{{\"head_sha\":\"{head}\",\"run_id\":{vb.RUN_ID_PLACEHOLDER}}}' --card {CARD_A}"
        ]
        assert out.splitlines()[-1] == emit_lines[0]
        print(emit_lines[0])

    def test_failed_unit_gets_no_emit_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo, plan = self._repo_and_plan(tmp_path, rc=1)

        code, out, err = run_cli(
            capsys,
            "verify-batch",
            "--plan",
            str(plan),
            "--repo",
            str(repo),
            "--log-dir",
            str(tmp_path / "evidence"),
        )

        assert (code, err) == (1, ""), err
        assert "verdict=FAIL" in out
        assert vb.EMIT_PREFIX not in out
