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
import os
import sqlite3
import subprocess  # nosec B404 - 编码边界用例必须起真子进程（in-process 改不了 stdout 编码）
import sys
from pathlib import Path

import pytest

from team_board import cli, evidence, evidence_store
from team_board.model import DEFAULT_DB

#: 仓库根（``tests/unit/xxx.py`` 往上两级）；子进程用例靠它拼 ``PYTHONPATH=src``
ROOT = Path(__file__).resolve().parents[2]

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


# ---------------------------------------------------------------- 编码边界（qa run 549 返工）


def gbk_payload_for_tier(*, ascii_escape: bool = False) -> str:
    """造一份「中文走错编码」的 ``tier`` payload 文本（两条路径各有用例）。

    - ``ascii_escape=False``：模拟 ``--payload "$(cat gbk.json)"`` —— 参数文本里是
      **孤立代理字符**（``LC_ALL=C`` 下 argv 解码用 ``surrogateescape``，GBK 字节
      ``\\xd6\\xd0`` 变 ``\\udcd6\\udcd0``）；
    - ``ascii_escape=True``：参数文本纯 ASCII，JSON 转义 ``"\\ud800"`` 解出来才是
      孤立代理字符 —— ``json.loads`` 成功、落库文本非法。
    """

    if ascii_escape:
        # 参数文本纯 ASCII：JSON 转义 ``\ud800`` 要等 ``json.loads`` 才变成孤立代理字符，
        # 于是「参数文本合法 UTF-8、落库文本非法」这条路径被单独覆盖
        return '{"tier": "B", "reason": "GBK\\ud800文本", "head_sha": "' + HEAD + '"}'
    reason = b"\xd6\xd0\xce\xc4".decode("utf-8", "surrogateescape")
    return json.dumps({"tier": "B", "reason": reason, "head_sha": HEAD}, ensure_ascii=False)


@pytest.mark.parametrize("ascii_escape", [False, True])
def test_non_utf8_payload_rc2_one_line_and_no_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ascii_escape: bool
) -> None:
    """``--payload`` 含非 UTF-8 文本 → rc2 一行 stderr、**零落库**、零 traceback。

    旧版：``json.loads`` 照过，死在 ``INSERT``（sqlite3 编不成 UTF-8）→
    ``UnicodeEncodeError`` 未捕获 → **rc1 + traceback**，自动化把「跑不起来」
    读成「证据不通过」（qa ISSUE-1）。
    """

    db = make_board(tmp_path, profile="pm")  # tier 只允许 pm
    before = event_count(db)
    text = gbk_payload_for_tier(ascii_escape=ascii_escape)
    if ascii_escape:
        assert r"\ud800" in text  # 参数文本纯 ASCII，代理字符要等 json.loads 才出来
    else:
        assert any(0xDC80 <= ord(ch) <= 0xDCFF for ch in text)  # surrogateescape 产物

    rc, out, err = run_cli(emit_argv(db, "tier", text), capsys)

    assert rc == 2, err
    assert out == ""
    assert err.count("\n") == 1
    assert err.startswith("错误: --payload 不是合法 UTF-8")
    assert "Traceback" not in err
    assert event_count(db) == before == 0
    assert event_count(db, kind="tier") == 0


def test_non_utf8_payload_leaves_db_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """同一条卡上先失败一次、再成功一次：失败那次不留行（count 只是 +1 不是 +2）。"""

    db = make_board(tmp_path, profile="pm")
    bad = gbk_payload_for_tier()

    rc_bad, _o, e_bad = run_cli(emit_argv(db, "tier", bad), capsys)
    rc_ok, _o2, e_ok = run_cli(
        emit_argv(db, "tier", {"tier": "B", "reason": "正常原因", "head_sha": HEAD}), capsys
    )

    assert (rc_bad, rc_ok) == (2, 0), (e_bad, e_ok)
    assert event_count(db, kind="tier") == 1


def test_stdout_encoding_cannot_render_summary_rc2_and_no_write(tmp_path: Path) -> None:
    """stdout 编码表示不了摘要（``PYTHONIOENCODING=ascii``）→ rc2 且**零落库**。

    旧版先 ``INSERT`` 再写摘要：rc2 时库里已有 1 行，调用方按「rc2 = 没写」重试
    就会重复（qa ISSUE-2）。in-process 改不了 stdout 编码，所以这条起真子进程。
    """

    db = make_board(tmp_path, profile="pm")
    payload = json.dumps({"tier": "B", "reason": "中文原因", "head_sha": HEAD})

    proc = subprocess.run(  # nosec B603 - 参数是常量列表，没有 shell
        [
            sys.executable,
            "-m",
            "team_board.cli",
            "evidence",
            "emit",
            "--kind",
            "tier",
            "--payload",
            payload,
            "--db",
            str(db),
        ],
        capture_output=True,
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "ascii",
            "HERMES_KANBAN_TASK": CARD,
        },
        check=False,
        timeout=120,
    )

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout == b""
    err = proc.stderr.decode("utf-8", "replace")
    assert err.startswith("错误: 当前 stdout 编码 ")
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert event_count(db) == 0
    assert event_count(db, kind="tier") == 0


# ---------------------------------------------------------------- CLI 面


def test_evidence_help_lists_emit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["evidence", "--help"])
    out = capsys.readouterr().out

    assert excinfo.value.code == 0
    assert "emit" in out
    assert "audit" in out  # main 上的 audit 与 emit 并存（本卡把它们合到一个父解析器）


@pytest.mark.parametrize(
    "argv",
    [
        ["evidence"],  # 缺动词
        ["evidence", "emit", "--payload", "{}"],  # 缺 --kind
        ["evidence", "emit"],  # 缺 --kind 与 payload 来源
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
    assert args.evidence_command == "emit"


def test_evidence_emit_listed_in_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    out = capsys.readouterr().out

    assert excinfo.value.code == 0
    assert "evidence" in out


# ------------------------------------------------------------------ 便捷参数面
#
# 卡 t_66d75a4f 要求 emit 的 CLI 面是
#   --card / --kind / --disc / --head / --pr / --run-id / --db，
# 而契约层（evidence.validate_payload）对 tier 还要求 tier+reason、对 review 还要求
# verdict，所以这几个键只能再加 --tier / --reason / --verdict 才能拼出合法 payload。
# 下面这组用例钉住「便捷参数拼 payload」这条路，以及它与 --payload 的互斥。


def test_emit_tier_via_flags_rc0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--kind tier + --head/--tier/--reason/--run-id 拼出合法 payload → rc0。"""

    path = make_board(tmp_path, profile="pm")

    rc, out, err = run_cli(
        [
            "evidence", "emit", "--kind", "tier", "--head", HEAD,
            "--tier", "B", "--reason", "改 tests/", "--run-id", str(RUN_ID),
            "--db", str(path),
        ],
        capsys,
    )

    assert rc == 0, err
    assert err == ""
    assert str(event_count(path, kind="tier")) == "1"
    task_id, run_id, kind, payload, _ = latest_event(path)
    assert (task_id, run_id, kind) == (CARD, RUN_ID, "tier")
    assert json.loads(payload) == {"tier": "B", "reason": "改 tests/", "head_sha": HEAD}


def test_emit_review_via_flags_rc0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--disc 映射到 payload.kind（review 事件的判别值）→ rc0。"""

    path = make_board(tmp_path, profile="pm")

    rc, _out, err = run_cli(
        [
            "evidence", "emit", "--kind", "review", "--disc", "regression",
            "--head", HEAD, "--verdict", "pass", "--db", str(path),
        ],
        capsys,
    )

    assert rc == 0, err
    assert json.loads(latest_event(path)[3]) == {
        "kind": "regression", "head_sha": HEAD, "verdict": "pass"
    }


def test_emit_payload_conflicts_with_flags_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--payload 与便捷参数同时给 → rc2、stderr 恰一行、零落库。"""

    path = make_board(tmp_path)
    before = event_count(path)

    rc, out, err = run_cli(
        [
            "evidence", "emit", "--kind", "qa_pass", "--payload", json.dumps({"head_sha": HEAD, "run_id": RUN_ID}),
            "--head", HEAD, "--db", str(path),
        ],
        capsys,
    )

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert "--payload 与便捷参数不能同时给" in err
    assert event_count(path) == before


def test_emit_no_payload_source_rc2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """什么 payload 来源都没给 → rc2、stderr 恰一行。"""

    path = make_board(tmp_path)

    rc, out, err = run_cli(
        ["evidence", "emit", "--kind", "qa_pass", "--db", str(path)], capsys
    )

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert "缺 --payload" in err


def test_emit_run_id_not_of_this_card_rc1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--run-id 与卡当前 running run 不等 → rc1「run 不属于本卡」、零落库。"""

    path = make_board(tmp_path)
    before = event_count(path)

    rc, out, err = run_cli(
        [
            "evidence", "emit", "--kind", "qa_pass", "--head", HEAD,
            "--run-id", str(RUN_ID + 1), "--db", str(path),
        ],
        capsys,
    )

    assert rc == 1
    assert out == ""
    assert err.count("\n") == 1
    assert "run 不属于本卡" in err
    assert event_count(path) == before


@pytest.mark.parametrize("bad_head", ["", "ABC", "a" * 39, "a" * 41, "z" * 40])
def test_emit_bad_head_via_flag_rc1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_head: str
) -> None:
    """--head 形态非法 → rc1（payload 违约这一档）、stderr 恰一行、零落库。"""

    path = make_board(tmp_path)
    before = event_count(path)

    rc, out, err = run_cli(
        ["evidence", "emit", "--kind", "qa_pass", "--head", bad_head, "--db", str(path)],
        capsys,
    )

    assert rc == 1
    assert out == ""
    assert err.count("\n") == 1
    assert "head_sha" in err
    assert event_count(path) == before


def test_emit_tier_without_tier_value_rc1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """卡面验收第 1 条的字面命令（只给 --head/--run-id，不给 tier/reason）：

    契约层 ``REQUIRED_KEYS['tier']`` 要 tier+reason，所以这里必然是 rc1「payload 缺键」。
    本用例把这条口径钉死（不要改成 rc0 —— 那等于替 pm 编一个档位写进板）。
    """

    path = make_board(tmp_path, profile="pm")

    rc, out, err = run_cli(
        [
            "evidence", "emit", "--kind", "tier", "--head", HEAD,
            "--run-id", str(RUN_ID), "--db", str(path),
        ],
        capsys,
    )

    assert rc == 1
    assert out == ""
    assert err.count("\n") == 1
    assert "payload 缺键" in err
    assert event_count(path) == 0


def test_emit_bad_pr_flag_rc2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--pr 非正整数 → rc2、stderr 恰一行。"""

    path = make_board(tmp_path, profile="engineer")

    rc, out, err = run_cli(
        [
            "evidence", "emit", "--kind", "impl_done", "--pr", "0",
            "--head", HEAD, "--db", str(path),
        ],
        capsys,
    )

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert "--pr 非法" in err


# ------------------------------------------------------- 端到端：四槽写满 → audit rc0


def test_four_slots_emit_then_audit_rc0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """按角色分别 emit 四条 → ``evidence audit`` 判四槽齐且绑同一 head（rc0）。

    一个卡上 ``current_run_id`` 一次只有一个，所以四条事件分三轮改 run 写入：
    run 7 = pm（tier / review:defect）、run 8 = qa（qa_pass / review:regression）。
    """

    path = make_board(tmp_path, profile="pm")
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO task_runs(id, task_id, profile, status, started_at)"
        " VALUES(8, ?, 'qa', 'running', 0)",
        (CARD,),
    )
    conn.commit()
    conn.close()

    def switch_run(run_id: int) -> None:
        conn = sqlite3.connect(path)
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, CARD))
        conn.commit()
        conn.close()

    steps = [
        (7, ["--kind", "tier", "--head", HEAD, "--tier", "B", "--reason", "四槽端到端"]),
        (8, ["--kind", "qa_pass", "--head", HEAD, "--run-id", "8"]),
        (7, ["--kind", "review", "--disc", "defect", "--head", HEAD, "--verdict", "pass"]),
        (8, ["--kind", "review", "--disc", "regression", "--head", HEAD, "--verdict", "pass"]),
    ]
    for run_id, extra in steps:
        switch_run(run_id)
        rc, out, err = run_cli(
            ["evidence", "emit", *extra, "--card", CARD, "--db", str(path)], capsys
        )
        assert rc == 0, (extra, err)
        assert "event=" in out, out

    assert event_count(path) == 4

    switch_run(7)
    rc, out, err = run_cli(
        ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(path)], capsys
    )

    assert rc == 0, err
    assert err == ""
    assert "verdict=PASS" in out.splitlines()[0]
    assert "review:regression | PASS" in out
