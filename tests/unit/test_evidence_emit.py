"""``evidence emit`` 的单测：身份只从库取、角色硬校验、成功时只追加一条事件。

每个用例都在 ``tmp_path``（= scratch 目录）里自建临时 SQLite，**不碰真板**；调用
一律走 ``cli.main([...])`` 拿真实退出码与真实 stderr，所以测的是「命令行行为」而不是
「函数返回值」。三条硬约束各有用例盯着：

- 身份不读 env 自报：``HERMES_PROFILE=qa`` 但库里 run 的 profile 是 ``pm`` → 仍 rc1；
- 角色不符时不落库：用例同时断言 ``count(task_events)`` 前后相等 + stderr 恰一行；
- 只追加一行：一次成功只多一行 ``kind=<kind>``，``tasks`` 表一个字节都不变。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from team_board import cli, evidence, evidence_store
from team_board.model import DEFAULT_DB

CARD = "t_deadbeef"
OTHER_CARD = "t_00000000"
HEAD = "a" * 40
HEAD64 = "0123456789abcdef" * 4
PR = 42
RUN_ID = 7

#: 与真板 ``kanban.db`` 同形的三张表（只留本卡用到的列，类型/主键照抄）
SCHEMA = """
CREATE TABLE tasks (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT '',
    body           TEXT,
    assignee       TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    created_at     INTEGER NOT NULL DEFAULT 0,
    current_run_id INTEGER
);
CREATE TABLE task_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    profile    TEXT,
    status     TEXT NOT NULL,
    started_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
"""

SPEC_BODY = "## 验收标准\nQA-SPEC: qa\n1. 一条\n"


def make_board(
    tmp_path: Path,
    *,
    profile: str | None = "qa",
    run_status: str = "running",
    body: str | None = SPEC_BODY,
    current_run_id: int | None = RUN_ID,
    run_row: bool = True,
    card: str = CARD,
) -> Path:
    """造一个最小看板库：一张卡 + 一条 run（按参数可调坏）。"""

    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO tasks(id, title, body, assignee, status, created_at, current_run_id)"
        " VALUES(?, '演示卡', ?, 'engineer', 'running', 0, ?)",
        (card, body, current_run_id),
    )
    if run_row:
        conn.execute(
            "INSERT INTO task_runs(id, task_id, profile, status, started_at)"
            " VALUES(?, ?, ?, ?, 0)",
            (RUN_ID, card, profile, run_status),
        )
    conn.commit()
    conn.close()
    return path


def event_count(path: Path, card: str = CARD, kind: str | None = None) -> int:
    conn = sqlite3.connect(path)
    try:
        if kind is None:
            row = conn.execute(
                "SELECT count(*) FROM task_events WHERE task_id = ?", (card,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT count(*) FROM task_events WHERE task_id = ? AND kind = ?",
                (card, kind),
            ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def latest_event(path: Path, card: str = CARD) -> tuple:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT task_id, run_id, kind, payload, created_at FROM task_events"
            " WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (card,),
        ).fetchone()
    finally:
        conn.close()
    return row


@pytest.fixture(autouse=True)
def worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 worker 进程的环境：``HERMES_KANBAN_TASK`` 指向本用例的临时卡。

    用例自己按需增删（「哪都拿不到卡号」「拿 workspace 兜底」「--card 与 env 不符」
    这三类会显式改环境）。
    """

    for name in ("HERMES_KANBAN_WORKSPACE", "HERMES_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """跑一次 ``cli.main``，返回 ``(rc, stdout, stderr)``。"""

    rc = cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def emit_argv(db: Path, kind: str = "qa_pass", payload: object | str | None = None) -> list[str]:
    """组一条 ``evidence emit`` 命令行（payload 默认 qa_pass 的合法值）。"""

    if payload is None:
        payload = {"head_sha": HEAD, "run_id": RUN_ID}
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ["evidence", "emit", "--kind", kind, "--payload", text, "--db", str(db)]


# ---------------------------------------------------------------- 成功路径


def test_qa_emit_qa_pass_rc0_and_writes_one_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_board(tmp_path, profile="qa")
    before = event_count(db)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 0
    assert err == ""
    assert out.count("\n") == 1
    assert out.startswith("evidence emit: kind=qa_pass card=" + CARD)
    assert "run=7" in out and "profile=qa" in out
    assert event_count(db) == before + 1
    assert event_count(db, kind="qa_pass") == 1

    task_id, run_id, kind, payload, created_at = latest_event(db)
    assert (task_id, run_id, kind) == (CARD, RUN_ID, "qa_pass")
    assert created_at > 0
    stored = json.loads(payload)
    assert stored["run_id"] == RUN_ID  # 事件行 run_id 与 payload.run_id 同值
    assert stored["head_sha"] == HEAD
    assert payload == evidence.normalize(stored)  # 落库文本 = normalize 结果


def test_engineer_emit_impl_done_rc0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_board(tmp_path, profile="engineer")

    rc, out, err = run_cli(
        emit_argv(db, "impl_done", {"pr": PR, "head_sha": HEAD64}), capsys
    )

    assert rc == 0 and err == ""
    assert "kind=impl_done" in out and "profile=engineer" in out
    assert event_count(db, kind="impl_done") == 1


def test_emit_created_at_uses_now_parameter(tmp_path: Path) -> None:
    db = make_board(tmp_path, profile="qa")

    result = evidence_store.emit(
        "qa_pass",
        json.dumps({"head_sha": HEAD, "run_id": RUN_ID}),
        db_path=db,
        now=1_700_000_000,
    )

    assert result.event_id > 0
    assert latest_event(db)[4] == 1_700_000_000


@pytest.mark.parametrize("profile", ["pm", "qa"])
def test_review_allowed_for_both_roles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], profile: str
) -> None:
    db = make_board(tmp_path, profile=profile)

    rc, _out, err = run_cli(
        emit_argv(db, "review", {"kind": "defect", "head_sha": HEAD, "verdict": "block"}),
        capsys,
    )

    assert rc == 0, err
    assert event_count(db, kind="review") == 1


# ---------------------------------------------------------------- 角色硬校验


def test_pm_emit_qa_pass_rc1_one_line_and_no_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """验收 2：pm 身份 emit qa_pass → rc1、stderr 恰一行、事件 count 前后相等。"""

    db = make_board(tmp_path, profile="pm")
    before = event_count(db)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 1
    assert out == ""
    assert err == (
        "拒绝写入：kind=qa_pass 只允许 qa，调用者 run 7 的 profile 是 pm\n"
    )
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert event_count(db) == before  # count 前后相等
    assert event_count(db) == 0


def test_role_check_happens_before_any_insert(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """连跑三次被拒的 emit，库里仍然 0 行（不是「先写后校验」）。"""

    db = make_board(tmp_path, profile="engineer")

    for _ in range(3):
        rc, _out, _err = run_cli(emit_argv(db), capsys)
        assert rc == 1

    assert event_count(db) == 0


def test_hermes_profile_env_is_not_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``HERMES_PROFILE=qa`` 不能把库里的 pm run 洗成 qa。"""

    db = make_board(tmp_path, profile="pm")
    monkeypatch.setenv("HERMES_PROFILE", "qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 1
    assert out == ""
    assert "profile 是 pm" in err
    assert event_count(db) == 0


# ---------------------------------------------------------------- 身份解析


def test_card_flag_equal_to_env_rc0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = make_board(tmp_path, profile="qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)

    rc, _out, err = run_cli([*emit_argv(db), "--card", CARD], capsys)

    assert rc == 0, err
    assert event_count(db) == 1


def test_card_flag_differs_from_env_rc1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = make_board(tmp_path, profile="qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", OTHER_CARD)

    rc, out, err = run_cli([*emit_argv(db), "--card", CARD], capsys)

    assert rc == 1
    assert out == ""
    assert err == (
        f"--card 与调用者卡号不一致：--card={CARD}，调用者={OTHER_CARD}\n"
    )
    assert event_count(db) == 0


def test_env_task_used_when_card_flag_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = make_board(tmp_path, profile="qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", CARD)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 0, err
    assert f"card={CARD}" in out


def test_workspace_basename_fallback_rc0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = make_board(tmp_path, profile="qa", card=CARD)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", f"/tmp/boards/{CARD}")

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 0, err
    assert f"card={CARD}" in out


def test_no_card_anywhere_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = make_board(tmp_path, profile="qa")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: 无法确定调用者卡号")
    assert err.count("\n") == 1
    assert event_count(db) == 0


def test_workspace_basename_not_card_shape_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = make_board(tmp_path, profile="qa")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/tmp/boards/not-a-card")

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 2 and out == ""
    assert err.startswith("错误: 无法确定调用者卡号")


@pytest.mark.parametrize("bad", ["t_XYZ", "t_deadbee", "t_deadbeef0", "../t_deadbeef"])
def test_card_shape_invalid_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    db = make_board(tmp_path, profile="qa")

    rc, out, err = run_cli([*emit_argv(db), "--card", bad], capsys)

    assert rc == 2 and out == ""
    assert err.startswith("错误: 卡号形态非法")
    assert event_count(db) == 0


def test_card_not_in_tasks_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = make_board(tmp_path, profile="qa")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    rc, out, err = run_cli([*emit_argv(db), "--card", OTHER_CARD], capsys)

    assert rc == 2 and out == ""
    assert err.startswith("错误: 看板库里没有这张卡")
    assert event_count(db) == 0


def test_no_current_run_rc1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = make_board(tmp_path, profile="qa", current_run_id=None)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 1 and out == ""
    assert err == f"该卡没有进行中的 run：{CARD}\n"
    assert event_count(db) == 0


@pytest.mark.parametrize("status", ["done", "blocked", "crashed", "timed_out", "released"])
def test_run_not_running_rc1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], status: str
) -> None:
    db = make_board(tmp_path, profile="qa", run_status=status)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 1 and out == ""
    assert err == f"当前 run 不是 running：run {RUN_ID} 的 status 是 {status!r}\n"
    assert event_count(db) == 0


def test_run_row_missing_rc1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = make_board(tmp_path, profile="qa", run_row=False)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 1 and out == ""
    assert err.startswith(f"当前 run 不在 task_runs 里：{CARD}")
    assert event_count(db) == 0


def test_run_profile_null_rc1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = make_board(tmp_path, profile=None)

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 1 and out == ""
    assert err.startswith(f"当前 run 没有 profile：run {RUN_ID}")
    assert event_count(db) == 0


def test_resolve_identity_dataclass(tmp_path: Path) -> None:
    db = make_board(tmp_path, profile="qa")
    conn = evidence_store.open_rw(db)
    try:
        identity = evidence_store.resolve_identity(conn, CARD, env={})
    finally:
        conn.close()

    assert identity == evidence_store.Identity(
        card=CARD, run_id=RUN_ID, profile="qa", status="running"
    )


def test_card_from_env_fallbacks() -> None:
    assert evidence_store.card_from_env({"HERMES_KANBAN_TASK": CARD}) == CARD
    assert (
        evidence_store.card_from_env({"HERMES_KANBAN_WORKSPACE": f"/x/{CARD}/"}) == CARD
    )
    assert evidence_store.card_from_env({"HERMES_KANBAN_WORKSPACE": "/x/plain"}) is None
    assert evidence_store.card_from_env({}) is None


# ---------------------------------------------------------------- payload


def test_payload_not_json_rc2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = make_board(tmp_path, profile="qa")

    rc, out, err = run_cli(emit_argv(db, payload="{not json"), capsys)

    assert rc == 2 and out == ""
    assert err.startswith("错误: --payload 不是合法 JSON")
    assert err.count("\n") == 1
    assert event_count(db) == 0


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("qa_pass", {}),  # 缺键
        ("qa_pass", {"head_sha": HEAD, "run_id": RUN_ID, "extra": 1}),  # 多键
        ("qa_pass", {"head_sha": "A" * 40, "run_id": RUN_ID}),  # 大写 sha
        ("qa_pass", {"head_sha": HEAD[:39], "run_id": RUN_ID}),  # 39 位
        ("qa_pass", {"head_sha": HEAD, "run_id": 0}),  # run_id 0
        ("impl_done", {"pr": 0, "head_sha": HEAD}),  # pr 0
        ("impl_done", {"pr": 1_700_000_000_000, "head_sha": HEAD}),  # 13 位毫秒
        ("tier", {"tier": "D", "reason": "常规", "head_sha": HEAD}),  # tier 越界
        ("tier", {"tier": "B", "reason": "", "head_sha": HEAD}),  # reason 空串
        ("review", {"kind": "defect", "head_sha": HEAD, "verdict": "PASS"}),  # verdict 越界
    ],
)
def test_payload_violates_contract_rc1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    payload: dict[str, object],
) -> None:
    db = make_board(tmp_path, profile="qa")

    rc, out, err = run_cli(emit_argv(db, kind, payload), capsys)

    assert rc == 1 and out == ""
    assert err.startswith("payload 违约：")
    assert err.count("\n") == 1
    assert event_count(db) == 0


def test_payload_json_not_object_rc1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = make_board(tmp_path, profile="qa")

    rc, out, err = run_cli(emit_argv(db, payload="[1, 2]"), capsys)

    assert rc == 1 and out == ""
    assert err.startswith("payload 违约：")
    assert event_count(db) == 0


def test_spec_sign_payload_is_empty_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_board(tmp_path, profile="qa")

    rc, out, err = run_cli(emit_argv(db, "spec_sign", {}), capsys)

    assert rc == 0, err
    assert "kind=spec_sign" in out
    assert latest_event(db)[3] == "{}"  # normalize({}) == "{}"


# ---------------------------------------------------------------- kind 校验


def test_kind_merged_rc1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = make_board(tmp_path, profile="qa")

    rc, out, err = run_cli(emit_argv(db, "merged", {}), capsys)

    assert rc == 1 and out == ""
    assert err == (
        "拒绝写入：kind=merged 由 merge_audit trailer 承载，不许 emit 成 evidence 事件\n"
    )
    assert event_count(db) == 0


def test_kind_unknown_rc1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = make_board(tmp_path, profile="qa")

    rc, out, err = run_cli(emit_argv(db, "nonsense", {}), capsys)

    assert rc == 1 and out == ""
    assert err.startswith("拒绝写入：kind='nonsense' 不在可 emit 的 kind 里")
    assert event_count(db) == 0


def test_kind_check_runs_before_db_access(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``merged`` 连库都不该碰：给一个不存在的库也回 rc1（不是 rc2）。"""

    missing = tmp_path / "nope.db"

    rc, out, err = run_cli(emit_argv(missing, "merged", {}), capsys)

    assert rc == 1 and out == ""
    assert "merge_audit trailer" in err


# ---------------------------------------------------------------- spec_sign 段


def test_spec_sign_with_section_rc0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_board(tmp_path, profile="qa", body=SPEC_BODY)

    rc, _out, err = run_cli(emit_argv(db, "spec_sign", {}), capsys)

    assert rc == 0, err
    assert event_count(db, kind="spec_sign") == 1


@pytest.mark.parametrize(
    "body",
    [
        None,  # 正文 NULL
        "",  # 空正文
        "## 目标\n没有验收段\n",
        "### 验收标准\n三级标题不算\n",  # 不是顶格 h2
        "  ## 验收标准\n缩进不算\n",  # 行首有空格
        "正文 ## 验收标准 出现在句中\n",  # 不是行首
    ],
)
def test_spec_sign_without_section_rc1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str | None
) -> None:
    db = make_board(tmp_path, profile="qa", body=body)

    rc, out, err = run_cli(emit_argv(db, "spec_sign", {}), capsys)

    assert rc == 1 and out == ""
    assert err.startswith("卡正文缺验收标准段")
    assert err.count("\n") == 1
    assert event_count(db) == 0


def test_section_check_only_applies_to_spec_sign(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """别的 kind 不要求验收段（``body=None`` 也能 emit qa_pass）。"""

    db = make_board(tmp_path, profile="qa", body=None)

    rc, _out, err = run_cli(emit_argv(db), capsys)

    assert rc == 0, err
    assert event_count(db, kind="qa_pass") == 1


# ---------------------------------------------------------------- 库处理


def test_db_file_missing_rc2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "kanban.db"

    rc, out, err = run_cli(emit_argv(missing), capsys)

    assert rc == 2 and out == ""
    assert err.startswith("错误: 数据源不存在：")
    assert err.count("\n") == 1
    assert "Traceback" not in err


def test_db_not_sqlite_rc2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "junk.db"
    path.write_bytes(b"not a sqlite file at all" * 10)

    rc, out, err = run_cli(emit_argv(path), capsys)

    assert rc == 2 and out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "Traceback" not in err


@pytest.mark.parametrize("table", ["tasks", "task_runs", "task_events"])
def test_db_missing_table_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], table: str
) -> None:
    db = make_board(tmp_path, profile="qa")
    conn = sqlite3.connect(db)
    conn.execute(f"DROP TABLE {table}")  # nosec B608 - 参数只来自用例常量
    conn.commit()
    conn.close()

    rc, out, err = run_cli(emit_argv(db), capsys)

    assert rc == 2 and out == ""
    assert err.startswith("错误: 看板库里缺表 ")
    assert table in err
    assert err.count("\n") == 1


def test_empty_db_file_is_not_created_into_a_board(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """空文件不是看板库：rc2 且**不建表**（文件大小保持 0）。"""

    path = tmp_path / "empty.db"
    path.write_bytes(b"")

    rc, out, err = run_cli(emit_argv(path), capsys)

    assert rc == 2 and out == ""
    assert err.startswith("错误: ")
    assert path.stat().st_size == 0


# ---------------------------------------------------------------- 只追加一行


def test_two_emits_append_two_rows_and_leave_tasks_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_board(tmp_path, profile="qa")
    conn = sqlite3.connect(db)
    tasks_before = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    runs_before = conn.execute("SELECT * FROM task_runs ORDER BY id").fetchall()
    conn.close()

    rc1, _o1, e1 = run_cli(emit_argv(db), capsys)
    rc2, _o2, e2 = run_cli(emit_argv(db, "review", {"kind": "regression", "head_sha": HEAD, "verdict": "pass"}), capsys)

    assert (rc1, rc2) == (0, 0), (e1, e2)
    assert event_count(db) == 2
    assert event_count(db, kind="qa_pass") == 1
    assert event_count(db, kind="review") == 1

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT * FROM tasks ORDER BY id").fetchall() == tasks_before
    assert conn.execute("SELECT * FROM task_runs ORDER BY id").fetchall() == runs_before
    assert {row[0] for row in conn.execute("SELECT DISTINCT kind FROM task_events")} == {
        "qa_pass",
        "review",
    }
    conn.close()


# ---------------------------------------------------------------- CLI 面


def test_evidence_help_lists_emit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["evidence", "--help"])
    out = capsys.readouterr().out

    assert excinfo.value.code == 0
    assert "emit" in out
    assert "audit" not in out  # 本卡只有 emit，audit 归 E3


@pytest.mark.parametrize(
    "argv",
    [
        ["evidence"],  # 缺动词
        ["evidence", "emit", "--payload", "{}"],  # 缺 --kind
        ["evidence", "emit", "--kind", "qa_pass"],  # 缺 --payload
        ["evidence", "audit"],  # 本卡未提供该动词
    ],
)
def test_argparse_level_errors_rc2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)

    assert excinfo.value.code == 2


def test_emit_defaults_to_model_default_db() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["evidence", "emit", "--kind", "qa_pass", "--payload", "{}"]
    )

    assert args.db == DEFAULT_DB
    assert args.card is None
    assert args.command == "evidence"
    assert args.evidence_action == "emit"


def test_evidence_emit_listed_in_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    out = capsys.readouterr().out

    assert excinfo.value.code == 0
    assert "evidence" in out
