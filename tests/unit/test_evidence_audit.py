"""``evidence audit`` 的取数（``collect``）、报告（``render``）与 CLI 三档 rc。

分三层，全部用临时 SQLite 库（不碰真板）：

- ``TestCollect``：一条 SQL、``LEFT JOIN task_runs`` 拿 profile、非 evidence 事件跳过、
  坏 JSON 丢弃并计数、缺表 → ``EvidenceReadError``、连接只读；
- ``TestRender``：报告行序与行格式（表头 / 四槽 / 非必需槽位 / ``missing:`` 尾行）；
- ``TestCliAcceptance``：验收标准 1-5 的逐条实跑（rc0 / rc1 / rc1 / rc2 / rc1），
  并用 monkeypatch 钉死「判定只走 ``evidence.judge``，不在 CLI 里重写一遍」。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from team_board import db, evidence

HEAD = "a" * 40
HEAD64 = "0123456789abcdef" * 4
OLD_HEAD = "b" * 40
CARD = "t_2ad370ff"
PR = 42

#: (run_id, profile)：101=pm / 202=qa / 303=engineer / 404=paimeng
RUNS: tuple[tuple[int, str], ...] = (
    (101, "pm"),
    (202, "qa"),
    (303, "engineer"),
    (404, "paimeng"),
)


def make_board(path: Path, rows: Sequence[tuple[str, str, int | None]]) -> Path:
    """造一个最小看板库：``tasks`` / ``task_events`` / ``task_runs`` 三张表 + 四条 run。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, status TEXT, body TEXT);
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, kind TEXT, payload TEXT, run_id INTEGER, created_at INTEGER
        );
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, profile TEXT, status TEXT);
        """
    )
    conn.execute("INSERT INTO tasks VALUES (?, 'engineer', 'running', '')", (CARD,))
    conn.executemany(
        "INSERT INTO task_runs VALUES (?, ?, 'done')", [(rid, profile) for rid, profile in RUNS]
    )
    for kind, payload, run_id in rows:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, run_id, created_at)"
            " VALUES (?, ?, ?, ?, 100)",
            (CARD, kind, payload, run_id),
        )
    conn.commit()
    conn.close()
    return path


def four_slots(*, head: str = HEAD, qa_run: int = 202) -> list[tuple[str, str, int | None]]:
    """四槽齐、全绑 ``head``、角色全对的基线行（顺序即 id 顺序）。"""
    return [
        ("tier", json.dumps({"tier": "B", "reason": "常规改动", "head_sha": head}), 101),
        ("qa_pass", json.dumps({"head_sha": head}), qa_run),
        ("review", json.dumps({"kind": "defect", "head_sha": head, "verdict": "pass"}), 101),
        ("review", json.dumps({"kind": "regression", "head_sha": head, "verdict": "pass"}), 202),
    ]


def slot_lines(stdout: str) -> list[str]:
    """报告里四个必需槽位的行（按 ``<槽位> | `` 前缀取）。"""
    names = ("tier | ", "qa_pass | ", "review:defect | ", "review:regression | ")
    return [line for line in stdout.splitlines() if line.startswith(names)]


class TestCollect:
    def test_returns_tuple_of_events_with_profile_from_runs(self, tmp_path: Path) -> None:
        """``collect`` 是 ``tuple[Event, ...]``，profile 来自 ``task_runs`` 的 join。"""
        board = make_board(tmp_path / "board.db", four_slots())
        conn = db.connect(board)
        try:
            events = evidence.collect(conn, CARD)
        finally:
            conn.close()

        assert isinstance(events, tuple)
        assert len(events) == 4
        assert [event.kind for event in events] == ["tier", "qa_pass", "review", "review"]
        assert [event.profile for event in events] == ["pm", "qa", "pm", "qa"]
        assert events[0].created_at == 100
        assert isinstance(events[0].payload, dict)

    def test_orders_by_id_and_isolates_card(self, tmp_path: Path) -> None:
        """只取本卡的事件，且按 id 升序（同一 kind 取最大 id 依赖这个顺序）。"""
        board = make_board(tmp_path / "board.db", four_slots())
        conn = sqlite3.connect(str(board))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, run_id, created_at)"
            " VALUES ('t_deadbeef', 'tier', ?, 101, 100)",
            (json.dumps({"tier": "A", "reason": "别的卡", "head_sha": HEAD}),),
        )
        conn.commit()
        conn.close()

        conn = db.connect(board)
        try:
            events = evidence.collect(conn, CARD)
        finally:
            conn.close()
        assert [event.id for event in events] == [1, 2, 3, 4]

    def test_skips_non_evidence_kinds(self, tmp_path: Path) -> None:
        """非 evidence 事件（评论 / claimed 等）直接跳过，不算解析失败。"""
        rows = [
            ("commented", json.dumps({"body": "无关"}), 101),
            ("claimed", json.dumps({}), 101),
            *four_slots(),
        ]
        board = make_board(tmp_path / "board.db", rows)
        conn = db.connect(board)
        try:
            events = evidence.collect(conn, CARD)
        finally:
            conn.close()
        assert len(events) == 4
        assert events.parse_failures == 0

    @pytest.mark.parametrize("bad", ["{not json", "", "[]", "1", '"str"', "null"])
    def test_bad_payload_is_dropped_and_counted(self, tmp_path: Path, bad: str) -> None:
        """非法 JSON / 非 JSON 对象：该事件丢弃 + 进 ``解析失败`` 计数，绝不抛异常。"""
        rows = [*four_slots(), ("qa_pass", bad, 202)]
        board = make_board(tmp_path / "board.db", rows)
        conn = db.connect(board)
        try:
            events = evidence.collect(conn, CARD)
        finally:
            conn.close()
        assert len(events) == 4  # 坏的那条不见了
        assert events.parse_failures == 1

    def test_missing_tables_raise_evidence_read_error(self, tmp_path: Path) -> None:
        """没有 ``task_events`` 表 → ``EvidenceReadError``（cli 映射 rc2），不冒 sqlite 异常。"""
        path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE tasks (id TEXT)")
        conn.commit()
        conn.close()

        conn = db.connect(path)
        try:
            with pytest.raises(evidence.EvidenceReadError) as excinfo:
                evidence.collect(conn, CARD)
        finally:
            conn.close()
        assert "task_events" in str(excinfo.value)

    def test_missing_task_runs_table_raises(self, tmp_path: Path) -> None:
        """缺 ``task_runs`` 表同样按取数失败处理（profile 拿不到就不许瞎判角色）。"""
        path = tmp_path / "noruns.db"
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, payload TEXT,"
            " run_id INTEGER, created_at INTEGER)"
        )
        conn.commit()
        conn.close()

        conn = db.connect(path)
        try:
            with pytest.raises(evidence.EvidenceReadError):
                evidence.collect(conn, CARD)
        finally:
            conn.close()

    def test_read_only_connection_refuses_writes(self, tmp_path: Path) -> None:
        """取数一律 ``mode=ro``：连上来的连接写不了（「绝不写库」的机器判据）。"""
        board = make_board(tmp_path / "board.db", four_slots())
        conn = db.connect(board)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO task_events (task_id, kind) VALUES ('x', 'tier')")
        finally:
            conn.close()


class TestRender:
    def test_pass_report_layout(self) -> None:
        """PASS：表头一行 + 四槽四行 + ``missing: -`` 尾行。"""
        judgment = evidence.judge(
            [
                evidence.Event(id=i + 1, kind=kind, payload=payload, run_id=run_id, profile=profile)
                for i, (kind, payload, run_id, profile) in enumerate(
                    [
                        ("tier", {"tier": "B", "reason": "常规", "head_sha": HEAD}, 101, "pm"),
                        ("qa_pass", {"head_sha": HEAD}, 202, "qa"),
                        (
                            "review",
                            {"kind": "defect", "head_sha": HEAD, "verdict": "pass"},
                            101,
                            "pm",
                        ),
                        (
                            "review",
                            {"kind": "regression", "head_sha": HEAD, "verdict": "pass"},
                            202,
                            "qa",
                        ),
                    ]
                )
            ],
            HEAD,
            PR,
        )
        lines = evidence.render(judgment, CARD, HEAD)
        assert lines[0] == f"evidence audit card={CARD} head={HEAD} verdict=PASS"
        assert lines[1] == f"tier | PASS | 1 | {HEAD[:12]} | pm"
        assert lines[2] == f"qa_pass | PASS | 2 | {HEAD[:12]} | qa"
        assert lines[-1] == "missing: -"
        assert len(lines) == 6

    def test_render_is_pure(self) -> None:
        """render 不 print、不读环境：只吃三个参数就能出完整报告。"""
        judgment = evidence.judge([], HEAD)
        lines = evidence.render(judgment, CARD, HEAD)
        assert lines[-1] == "missing: tier,qa_pass,review:defect,review:regression"
        assert lines[0] == "缺证据：tier,qa_pass,review:defect,review:regression"
        assert f"evidence audit card={CARD} head={HEAD} verdict=缺证据" in lines

    def test_extra_slots_render_but_do_not_change_verdict(self) -> None:
        """``spec_sign`` / ``impl_done`` / ``boss_approve`` 有就各渲一行，不参与 rc。"""
        events = [
            evidence.Event(id=1, kind="spec_sign", payload={}, run_id=202, profile="qa"),
            evidence.Event(
                id=2, kind="impl_done", payload={"pr": 7, "head_sha": HEAD}, run_id=303,
                profile="engineer",
            ),
            evidence.Event(id=3, kind="boss_approve", payload={"pr": 7, "approved_by": "boss"},
                           run_id=404, profile="paimeng"),
        ]
        judgment = evidence.judge(events, HEAD, PR)
        lines = evidence.render(judgment, CARD, HEAD, events=events)
        assert "spec_sign | PASS | 1 | - | qa" in lines
        assert f"impl_done | PASS | 2 | {HEAD[:12]} | engineer" in lines
        assert "boss_approve | PASS | 3 | - | paimeng" in lines
        assert judgment.verdict == evidence.VERDICT_MISSING  # 四槽仍缺 → 判定不受影响

    def test_extra_slot_with_wrong_role_is_flagged(self) -> None:
        """非必需槽位写错角色时行里标「角色不符」，但 rc 由四槽决定。"""
        events = [evidence.Event(id=1, kind="impl_done",
                                 payload={"pr": 7, "head_sha": HEAD}, run_id=101, profile="pm")]
        lines = evidence.render(evidence.judge(events, HEAD), CARD, HEAD, events=events)
        assert "impl_done | 角色不符 | 1 |" in "\n".join(lines)

    def test_parse_failures_line_only_when_positive(self) -> None:
        """``解析失败`` 只在 >0 时出结论行，且逐条点名坏事件 id（返工 t_39c86400）。"""
        judgment = evidence.judge([], HEAD)
        assert not any(line.startswith("解析失败") for line in evidence.render(judgment, CARD, HEAD))
        lines = evidence.render(judgment, CARD, HEAD, parse_failures=2)
        assert lines[0] == "解析失败 2 条（坏事件 id: -）；无法确认槽位的最新证据"
        named = evidence.render(
            judgment, CARD, HEAD, parse_failures=2, parse_failure_ids=(7, 9)
        )
        assert named[0] == "解析失败 2 条（坏事件 id: 7,9）；无法确认槽位的最新证据"

    def test_review_kind_out_of_domain_is_named(self) -> None:
        """``review.kind`` 域外被 ``_latest`` 静默滤掉 → 报告点名该事件 id（返工 t_39c86400 P3）。"""
        events = [
            evidence.Event(
                id=5,
                kind="review",
                payload={"kind": "defectt", "head_sha": HEAD, "verdict": "pass"},
                run_id=101,
                profile="pm",
            )
        ]
        lines = evidence.render(evidence.judge(events, HEAD), CARD, HEAD, events=events)
        assert (
            "review kind 域外: 事件 id=5 kind='defectt'（不在 defect/regression 之一，已丢弃）"
            in lines
        )
        # 良性 review 事件不出这一行（正常库里不留噪音）
        good = [
            evidence.Event(
                id=6,
                kind="review",
                payload={"kind": "defect", "head_sha": HEAD, "verdict": "pass"},
                run_id=101,
                profile="pm",
            )
        ]
        assert not any(
            line.startswith("review kind 域外")
            for line in evidence.render(evidence.judge(good, HEAD), CARD, HEAD, events=good)
        )


class TestCliAcceptance:
    """验收标准 1-5：逐条实跑 ``team_board.cli.main``（stderr/stdout 都真读）。"""

    def run_cli(self, capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
        from team_board import cli

        rc = cli.main(argv)
        captured = capsys.readouterr()
        return rc, captured.out, captured.err

    def test_four_slots_pass_rc0(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """标准 1：四槽齐 + head 一致 → rc0，每槽一行 ``<槽位> | PASS | <来源>``。"""
        board = make_board(tmp_path / "board.db", four_slots())
        rc, out, _err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(board)]
        )
        assert rc == 0
        lines = slot_lines(out)
        assert [line.split(" | ")[0] for line in lines] == [
            "tier",
            "qa_pass",
            "review:defect",
            "review:regression",
        ]
        assert all(line.split(" | ")[1] == "PASS" for line in lines)
        assert f"tier | PASS | 1 | {HEAD[:12]} | pm" == lines[0]
        assert f"qa_pass | PASS | 2 | {HEAD[:12]} | qa" == lines[1]
        assert out.splitlines()[-1] == "missing: -"
        assert "verdict=PASS" in out.splitlines()[0]

    def test_missing_qa_pass_rc1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """标准 2：缺 qa_pass → rc1，**首行逐字**「缺证据：qa_pass」。"""
        rows = [row for row in four_slots() if row[0] != "qa_pass"]
        board = make_board(tmp_path / "board.db", rows)
        rc, out, _err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(board)]
        )
        assert rc == 1
        assert out.splitlines()[0] == "缺证据：qa_pass"
        assert "qa_pass | 缺证据 | - | - | -" in out
        assert out.splitlines()[-1] == "missing: qa_pass"

    def test_old_head_rc1_without_stale_word(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """标准 3：qa_pass 绑旧 sha → rc1，含「证据对新 head 无效」，且全仓不出现 STALE。"""
        rows = four_slots()
        rows[1] = ("qa_pass", json.dumps({"head_sha": OLD_HEAD}), 202)
        board = make_board(tmp_path / "board.db", rows)
        rc, out, err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(board)]
        )
        assert rc == 1
        assert "证据对新 head 无效" in out
        assert "STALE" not in out
        assert "STALE" not in err
        assert f"qa_pass | 证据对新 head 无效 | 2 | {OLD_HEAD[:12]} | qa" in out

    @pytest.mark.parametrize(
        "argv",
        [
            ["--card", CARD, "--head", "a" * 39],  # head 39 位
            ["--card", CARD, "--head", "A" * 40],  # head 大写
            ["--card", "t_x", "--head", HEAD],  # 卡号形态坏
            ["--card", "wt/t_2ad370ff", "--head", HEAD],
            ["--card", CARD, "--head", HEAD, "--pr", "0"],
            ["--card", CARD, "--head", HEAD, "--pr", "-1"],
            ["--card", CARD, "--head", HEAD, "--pr", "abc"],
            ["--card", CARD, "--head", HEAD, "--pr", "1793160000000"],
        ],
    )
    def test_bad_args_rc2_stdout_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        """标准 4（参数半）：head / card / pr 形态坏各一条 rc2，stdout 一个字节都不写。"""
        board = make_board(tmp_path / "board.db", four_slots())
        rc, out, err = self.run_cli(capsys, ["evidence", "audit", *argv, "--db", str(board)])
        assert rc == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: ")

    @pytest.mark.parametrize("db_path", ["/nonexistent/board.db", ""])
    def test_db_missing_rc2_one_stderr_line(
        self, capsys: pytest.CaptureFixture[str], db_path: str
    ) -> None:
        """标准 4（库半）：库文件不存在 → rc2、stderr 恰一行、stdout 空、不冒 traceback。"""
        rc, out, err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", db_path]
        )
        assert rc == 2
        assert out == ""
        assert len(err.splitlines()) == 1
        assert err.startswith("错误: ")

    def test_forged_qa_pass_with_pm_run_is_role_mismatch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """标准 5：伪造行（kind=qa_pass 但 run_id 指向 pm 的 run）→ rc1「角色不符」。

        证的是判定层不看 kind 就信：同一份 payload、同一个 head，只是写入者角色不对。
        """
        rows = four_slots()
        rows[1] = ("qa_pass", json.dumps({"head_sha": HEAD}), 101)  # 101 = pm 的 run
        board = make_board(tmp_path / "board.db", rows)
        rc, out, _err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(board)]
        )
        assert rc == 1
        assert "角色不符" in out
        assert "verdict=角色不符" in out
        assert "qa_pass | 角色不符 | 2 |" in out

    def test_missing_run_id_is_role_mismatch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``run_id`` 为 NULL（LEFT JOIN 拿不到 profile）同样判角色不符，不许默认放行。"""
        rows = four_slots()
        rows[1] = ("qa_pass", json.dumps({"head_sha": HEAD}), None)
        board = make_board(tmp_path / "board.db", rows)
        rc, out, _err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(board)]
        )
        assert rc == 1
        assert "qa_pass | 角色不符 | 2 |" in out

    def test_tier_c_requires_boss_approve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """tier=C 缺 boss_approve → rc1「缺老板点头」；补上后 rc0（--pr 也对齐）。"""
        rows = four_slots()
        rows[0] = (
            "tier",
            json.dumps({"tier": "C", "reason": "碰 CI", "head_sha": HEAD}),
            101,
        )
        board = make_board(tmp_path / "board.db", rows)
        argv = ["evidence", "audit", "--card", CARD, "--head", HEAD, "--pr", str(PR), "--db", str(board)]
        rc, out, _err = self.run_cli(capsys, argv)
        assert rc == 1
        assert "verdict=缺老板点头" in out

        rows.append(("boss_approve", json.dumps({"pr": PR, "approved_by": "boss"}), 404))
        board = make_board(tmp_path / "boss.db", rows)
        argv = ["evidence", "audit", "--card", CARD, "--head", HEAD, "--pr", str(PR), "--db", str(board)]
        rc, out, _err = self.run_cli(capsys, argv)
        assert rc == 0
        assert "boss_approve | PASS | 5 | - | paimeng" in out

    def test_impl_done_pr_mismatch_is_cross_card(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--pr`` 与 impl_done 的 pr 不等 → rc1「证据串卡」；不传 ``--pr`` 时跳过这半。"""
        rows = [*four_slots(), ("impl_done", json.dumps({"pr": 7, "head_sha": HEAD}), 303)]
        board = make_board(tmp_path / "board.db", rows)
        rc, out, _err = self.run_cli(
            capsys,
            ["evidence", "audit", "--card", CARD, "--head", HEAD, "--pr", str(PR), "--db", str(board)],
        )
        assert rc == 1
        assert "证据串卡" in out

        rc, out, _err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(board)]
        )
        assert rc == 0
        assert "verdict=PASS" in out

    def test_64_hex_head_is_accepted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """head 允许 64 位小写十六进制（sha256 仓库），逐字比较不做前缀归一。"""
        board = make_board(tmp_path / "board.db", four_slots(head=HEAD64))
        rc, out, _err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD64, "--db", str(board)]
        )
        assert rc == 0
        assert f"qa_pass | PASS | 2 | {HEAD64[:12]} | qa" in out

    def test_judge_is_the_only_verdict_layer(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """判定只走 ``evidence.judge``：CLI 里不许有第二份判定逻辑。"""
        calls: list[tuple[str, int | None]] = []
        real_judge = evidence.judge

        def spy(events, head, pr=None, tier_required=None):
            calls.append((head, pr))
            return real_judge(events, head, pr, tier_required)

        monkeypatch.setattr(evidence, "judge", spy)
        board = make_board(tmp_path / "board.db", four_slots())
        rc, _out, _err = self.run_cli(
            capsys,
            ["evidence", "audit", "--card", CARD, "--head", HEAD, "--pr", str(PR), "--db", str(board)],
        )
        assert rc == 0
        assert calls == [(HEAD, PR)]

    def test_no_argument_pr_skips_cross_card_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--pr`` 缺省 → judge 收到 ``None``（不判 impl_done 串卡），不是 0。"""
        board = make_board(tmp_path / "board.db", four_slots())
        rc, out, _err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(board)]
        )
        assert rc == 0
        assert "串卡" not in out

    def test_broken_payload_does_not_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """四槽齐 + 一条坏 JSON：整次稽核照跑，但 **rc1** 且结论行点名坏事件 id。

        返工 t_39c86400（P2）：旧实现四槽齐就 rc0 —— 同库的 ``merge_guard`` 证据闸门
        对同一份库判「拒绝」，两个入口给出相反结论。「解析不了」不等于「没有证据」，
        结论行必须点名是哪条事件（id=5 是追加的那条坏 payload）。
        """

        rows = [*four_slots(), ("qa_pass", "{坏", 202)]
        board = make_board(tmp_path / "board.db", rows)
        rc, out, _err = self.run_cli(
            capsys, ["evidence", "audit", "--card", CARD, "--head", HEAD, "--db", str(board)]
        )
        assert rc == 1
        assert out.splitlines()[0] == (
            "解析失败 1 条（坏事件 id: 5）；无法确认槽位的最新证据"
        )

    def test_bare_evidence_without_subcommand_rc2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``evidence`` 后面不跟动词 → argparse rc2（子命令必填）。"""
        from team_board import cli

        with pytest.raises(SystemExit) as excinfo:
            cli.main(["evidence"])
        assert excinfo.value.code == 2
