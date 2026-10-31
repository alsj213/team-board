"""``merge_guard`` 的**证据状态机闸门**（四槽绑当前 head 才许合并）单测。

判据来源是 ``t_1429d01c`` 卡正文的验收标准（契约 v1 的 E4 段），七条逐条对应：

1. 四槽齐 + head 一致 → ``证据状态机 | PASS`` 行落在 ``认领唯一性 | PASS`` 之后、
   ``双问绑定 | PASS`` 之前；
2. 缺 ``qa_pass`` → rc2、stdout 0 字节、stderr 恰一行含「缺证据：qa_pass」；
3. ``qa_pass`` 绑旧 head → rc2、stderr 含「证据对新 head 无效」；
4. ``tier=C`` 缺 ``boss_approve`` → rc2、stderr 含「缺老板点头」；
5. ``--branch`` 不是 ``wt/t_xxxxxxxx`` → rc2，且 ``gh pr merge`` 调用 0 次；
6. ``--db`` 指向不存在的库 → rc2、stderr 含 ``--db`` 与该路径；
7. 全量 pytest / ruff 由交卡命令覆盖（本文件本身不跑 pytest）。

**假 runner 的口径**：只打桩 :func:`team_board.merge_guard._run`（gh / git 的唯一出口）与
``reviewhead.fetch_bodies``（gh api 的唯一出口），其余全是真代码 —— 真 sqlite 库、
:func:`team_board.evidence.collect` / ``judge``、真 ``claim_gate`` / ``review_head_gate``。
所以「哪一行在哪儿」不是拼出来的，是整条 ``_run_merge`` 跑出来的。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from team_board import merge_guard as mg
from team_board import reviewhead

#: 预检读到的 head（= 合并时 ``--match-head-commit`` 用的那条 sha）
HEAD_OID = "4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a2"
#: 另一条提交（模拟「证据绑的是上一个 head」）
OLD_OID = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
#: base 提交（rev-parse 私有 base ref 用）
BASE_OID = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"

#: 实现卡（从 ``--branch`` 取）；分支必须整串等于 ``wt/`` + 卡号
IMPL_CARD = "t_bbeab29f"
BRANCH = f"wt/{IMPL_CARD}"
#: 收口（合并）卡：``--card`` 与 ``PR-CLAIM`` 都写它
MERGE_CARD = "t_0ddc0ffe"
MERGE_BY = "pm/run200"
PR = 9
REMOTE_URL = "git@github.com:alsj213/team-board.git"

#: 判「认领唯一」的看板行：合并卡正文顶格写 PR-CLAIM
CLAIM_BODY = f"PR-CLAIM: #{PR}\n合并卡（收口）\n"


def e_row(kind: str, payload: dict[str, object], run_id: int | None) -> tuple[str, str, int | None]:
    """一条 evidence 事件（``task_events`` 的一行：kind / payload JSON / run_id）。"""

    return kind, json.dumps(payload, ensure_ascii=False), run_id


def four_slots(
    *,
    head: str = HEAD_OID,
    tier: str = "B",
    qa_head: str | None = None,
    with_qa: bool = True,
) -> list[tuple[str, str, int | None]]:
    """四槽齐的默认证据集（``head`` 全绑当前 head；qa_head 可单独指旧 head）。"""

    rows = [
        e_row("tier", {"tier": tier, "reason": "文档 + 单测", "head_sha": head}, 1),
        e_row("review", {"kind": "defect", "head_sha": head, "verdict": "pass"}, 2),
        e_row("review", {"kind": "regression", "head_sha": head, "verdict": "pass"}, 1),
    ]
    if with_qa:
        rows.append(e_row("qa_pass", {"head_sha": qa_head or head, "run_id": 2}, 2))
    return rows


def board_db(
    tmp_path: Path,
    *,
    events: list[tuple[str, str, int | None]],
    impl_card: str = IMPL_CARD,
    cards: list[tuple[str, str, str, str]] | None = None,
    with_event_tables: bool = True,
) -> Path:
    """造一个只含 ``merge_guard`` 所需最小列的看板库（真 sqlite，``mode=ro`` 打开）。

    列名照真库的子集：``iter_claim_rows`` 只读 ``tasks(id,status,assignee,body)``；
    ``evidence.collect`` 读 ``task_events(id,task_id,kind,payload,run_id,created_at)``
    并 ``LEFT JOIN task_runs`` 取 profile。
    """

    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, assignee TEXT, body TEXT)")
            if with_event_tables:
                conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, profile TEXT)")
                conn.execute(
                    "CREATE TABLE task_events ("
                    "id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, "
                    "run_id INTEGER, created_at INTEGER)"
                )
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?)",
                cards
                if cards is not None
                else [
                    (MERGE_CARD, "in_progress", "pm", CLAIM_BODY),
                    (impl_card, "running", "engineer", "实现卡正文"),
                ],
            )
            if with_event_tables:
                conn.executemany(
                    "INSERT INTO task_runs VALUES (?,?)",
                    [(1, "pm"), (2, "qa"), (3, "paimeng")],
                )
                conn.executemany(
                    "INSERT INTO task_events (task_id,kind,payload,run_id,created_at) "
                    "VALUES (?,?,?,?,0)",
                    [(impl_card, kind, payload, run_id) for kind, payload, run_id in events],
                )
    finally:
        conn.close()
    return path


class FakeRunner:
    """假的 gh / git 执行器：记录每次 argv，并按 argv 形态回放预设结果。

    ``ls_remote`` 是 ``ls-remote`` 的退出码序列（合并前存在性探针 ``0`` → 合并后的轮询
    ``2`` = 已删）；序列耗尽后一直用最后一个值。``merge_rc`` 是 ``gh pr merge`` 的退出码。
    """

    def __init__(
        self,
        *,
        head_oid: str = HEAD_OID,
        head_ref: str = BRANCH,
        ls_remote: tuple[int, ...] = (0, 2),
        merge_rc: int = 0,
        view_rc: int = 0,
        config_rc: int = 0,
    ) -> None:
        self.head_oid = head_oid
        self.head_ref = head_ref
        self.ls_remote = list(ls_remote)
        self.merge_rc = merge_rc
        self.view_rc = view_rc
        self.config_rc = config_rc
        self.calls: list[tuple[str, ...]] = []

    # -- 判定辅助 ---------------------------------------------------------

    def merge_calls(self) -> list[tuple[str, ...]]:
        """所有 ``gh pr merge`` 调用（验收 5 断言它为 0 次）。"""

        return [argv for argv in self.calls if argv[:3] == ("gh", "pr", "merge")]

    def write_calls(self) -> list[tuple[str, ...]]:
        """所有会写远端的调用（``gh pr merge``；``gh pr view`` / ``gh api`` 都是读）。"""

        return self.merge_calls()

    def commands(self) -> list[str]:
        return [" ".join(argv) for argv in self.calls]

    # -- 接口（与 merge_guard._run 同签名）--------------------------------

    def __call__(self, argv: tuple[str, ...], *, cwd: str, timeout: float) -> mg.CommandResult:
        argv = tuple(argv)
        self.calls.append(argv)
        rc, out, err = 0, "", ""
        if argv[0] == "git":
            rc, out = self._git(argv)
        else:
            rc, out = self._gh(argv)
        return mg.CommandResult(argv=argv, returncode=rc, stdout=out, stderr=err)

    def _git(self, argv: tuple[str, ...]) -> tuple[int, str]:
        if "ls-remote" in argv:
            rc = self.ls_remote.pop(0) if len(self.ls_remote) > 1 else self.ls_remote[0]
            return rc, "" if rc != 0 else f"{self.head_oid}\trefs/heads/{self.head_ref}\n"
        if "config" in argv:
            return self.config_rc, f"{REMOTE_URL}\n"
        if "fetch" in argv:
            return 0, ""
        if "rev-parse" in argv:
            return 0, (self.head_oid if argv[-1].endswith("/head") else BASE_OID) + "\n"
        if "merge-base" in argv:
            return 0, ""
        return 1, ""  # pragma: no cover - 本模块没有别的 git 调用

    def _gh(self, argv: tuple[str, ...]) -> tuple[int, str]:
        if argv[:2] == ("gh", "pr") and argv[2] == "view":
            payload = {
                "number": PR,
                "state": "OPEN",
                "headRefName": self.head_ref,
                "baseRefName": "main",
                "headRefOid": self.head_oid,
            }
            return self.view_rc, json.dumps(payload)
        if argv[:3] == ("gh", "pr", "merge"):
            return self.merge_rc, ""
        return 1, ""  # pragma: no cover


def review_bodies(head: str = HEAD_OID) -> list[str]:
    """双问绑定闸门要的两条结构化标记（defect + regression 各一条，都指当前 head）。"""

    return [
        f"REVIEW-HEAD: {head}\nREVIEW-KIND: defect\n\n缺陷视角：非阻塞",
        f"REVIEW-HEAD: {head}\nREVIEW-KIND: regression\n\n回归视角：非阻塞",
    ]


def cli_argv(
    tmp_path: Path,
    *,
    db: Path | None = None,
    branch: str = BRANCH,
    verify_only: bool = False,
) -> list[str]:
    """一条完整的合并命令（``--repo`` 指向 tmp_path，body 文件由本函数写）。"""

    body = tmp_path / "body.txt"
    body.write_text(
        f"merged-by: {MERGE_BY}\nmerge-card: {MERGE_CARD}\nmerge-pr: #{PR}\n",
        encoding="utf-8",
    )
    argv = [
        "--pr",
        str(PR),
        "--branch",
        branch,
        "--delete-branch",
        "--repo",
        str(tmp_path),
        "--db",
        str(db if db is not None else tmp_path / "kanban.db"),
        "--timeout",
        "5",
        "--interval",
        "1",
        "--command-timeout",
        "5",
    ]
    if verify_only:
        return [*argv, "--verify-only"]
    return [
        *argv,
        "--card",
        MERGE_CARD,
        "--by",
        MERGE_BY,
        "--subject",
        f"Merge PR #{PR}（{IMPL_CARD}）",
        "--body-file",
        str(body),
    ]


def run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    events: list[tuple[str, str, int | None]] | None = None,
    runner: FakeRunner | None = None,
    db: Path | None = None,
    branch: str = BRANCH,
    verify_only: bool = False,
    with_event_tables: bool = True,
    bodies: list[str] | None = None,
) -> tuple[int, str, str]:
    """跑一次 ``merge_guard.main``：返回（rc, stdout, stderr）。"""

    path = db
    if path is None:
        path = board_db(
            tmp_path,
            events=four_slots() if events is None else events,
            with_event_tables=with_event_tables,
        )
    fake = FakeRunner(head_ref=branch) if runner is None else runner
    monkeypatch.setattr(mg, "_run", fake)
    monkeypatch.setattr(
        reviewhead, "fetch_bodies", lambda pr: review_bodies() if bodies is None else bodies
    )
    rc = mg.main(cli_argv(tmp_path, db=path, branch=branch, verify_only=verify_only))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------- 验收 1：PASS + 位置

def test_pass_line_sits_between_claim_and_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """四槽齐 + head 一致：``证据状态机 | PASS`` 落在认领之后、双问之前，且整条合并走通。

    位置是要害：闸门排在 preflight 之后才有 ``headRefOid``，排在 ``gh pr merge`` 之前才能
    拦住合并；行序与调用序一致，下游 grep「认领唯一性 | PASS」与「双问绑定 | PASS」之间
    就该看到证据行。
    """

    rc, out, err = run_main(tmp_path, monkeypatch, capsys)

    assert (rc, err) == (0, "")
    lines = out.splitlines()
    claim = next(i for i, line in enumerate(lines) if line.startswith("认领唯一性 | PASS |"))
    evidence = next(i for i, line in enumerate(lines) if line.startswith("证据状态机 | PASS |"))
    review = next(i for i, line in enumerate(lines) if line.startswith("双问绑定 | PASS |"))
    assert claim < evidence < review
    assert (
        f"PYTHONPATH=src python3 -m team_board.cli evidence audit "
        f"--card {IMPL_CARD} --head {HEAD_OID} --pr {PR} --db {tmp_path / 'kanban.db'}"
    ) in lines[evidence]
    assert "tier=B" in lines[evidence]
    assert "qa_pass=***" in lines[evidence]
    assert "review=defect+regression" in lines[evidence]


def test_pass_line_after_head_sha_and_before_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """证据行也在 ``head sha`` 之后、「合并 | PASS」之前（整条证据链的顺序钉子）。"""

    rc, out, _ = run_main(tmp_path, monkeypatch, capsys)

    assert rc == 0
    lines = out.splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith("head sha | PASS |"))
    evidence = next(i for i, line in enumerate(lines) if line.startswith("证据状态机 | PASS |"))
    merge = next(i for i, line in enumerate(lines) if line.startswith("合并 | PASS |"))
    assert head < evidence < merge


def test_tier_c_with_boss_approve_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``tier=C`` 有 ``boss_approve``（pr 相等）→ 照常放行（不是一律拒 C 档）。"""

    events = [*four_slots(tier="C"), e_row("boss_approve", {"pr": PR, "approved_by": "boss"}, 3)]
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, events=events)

    assert (rc, err) == (0, "")
    assert "tier=C" in out


# ------------------------------------------------------- 验收 2：缺 qa_pass

def test_missing_qa_pass_rc2_zero_stdout_one_stderr_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺 ``qa_pass`` → rc2、stdout 0 字节、stderr 恰一行含「缺证据：qa_pass」、零写调用。"""

    fake = FakeRunner()
    rc, out, err = run_main(
        tmp_path, monkeypatch, capsys, events=four_slots(with_qa=False), runner=fake
    )

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert err.startswith("错误: ")
    assert "缺证据：qa_pass" in err
    assert fake.write_calls() == []


# ------------------------------------------- 验收 3：qa_pass 绑旧 head

def test_qa_pass_bound_to_old_head_rc2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``qa_pass`` 绑旧 head → rc2、stdout 0 字节、stderr 含「证据对新 head 无效」。"""

    fake = FakeRunner()
    rc, out, err = run_main(
        tmp_path, monkeypatch, capsys, events=four_slots(qa_head=OLD_OID), runner=fake
    )

    assert rc == 2
    assert out == ""
    assert "证据对新 head 无效" in err
    assert "STALE" not in err  # 全仓不出现 STALE 这个判定词
    assert fake.write_calls() == []


# --------------------------------------------- 验收 4：tier=C 缺老板点头

def test_tier_c_without_boss_approve_rc2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``tier=C`` 缺 ``boss_approve`` → rc2、stderr 含「缺老板点头」。"""

    rc, out, err = run_main(tmp_path, monkeypatch, capsys, events=four_slots(tier="C"))

    assert rc == 2
    assert out == ""
    assert "缺老板点头" in err


def test_tier_c_boss_approve_for_other_pr_rc2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``boss_approve`` 批的是别的 PR → 同样「缺老板点头」（pr 必须相等）。"""

    events = [*four_slots(tier="C"), e_row("boss_approve", {"pr": PR + 1, "approved_by": "boss"}, 3)]
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, events=events)

    assert rc == 2
    assert out == ""
    assert "缺老板点头" in err


# --------------------------------- 验收 5：--branch 不是 wt/t_xxxxxxxx

@pytest.mark.parametrize("branch", ["wt/foo", "feature/x", "wt/t_bbeab29", "wt/t_BBEAB29F", "wt/t_bbeab29fg"])
def test_branch_not_wt_card_rc2_and_zero_merge_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    branch: str,
) -> None:
    """``--branch`` 不是 ``wt/t_<8 位小写十六进制>`` → rc2，且一次 ``gh pr merge`` 都不发。

    直接喂 ``main``：预检（假 ``gh pr view``）会回同名的 ``headRefName``，于是拒绝只能来自
    证据闸门的「无法定位实现卡」这一步 —— 位置对了才拦得住。
    """

    fake = FakeRunner(head_ref=branch)
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, runner=fake, branch=branch)

    assert rc == 2
    assert out == ""
    assert "无法定位实现卡" in err
    assert fake.merge_calls() == []
    # 连 gh 读调用都只到预检为止：证据闸门在双问闸门之前，一条 gh api 都不该发
    assert not any(argv[:2] == ("gh", "api") for argv in fake.calls)


# ------------------------------------ 验收 6：--db 不存在

def test_missing_db_rc2_names_flag_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--db`` 指向不存在的库 → rc2、stdout 0 字节、stderr 含 ``--db`` 与该路径。"""

    missing = tmp_path / "nowhere.db"
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, db=missing)

    assert rc == 2
    assert out == ""
    assert "--db" in err
    assert str(missing) in err


def test_evidence_gate_missing_db_message_is_evidence_specific(tmp_path: Path) -> None:
    """闸门自身的库错误文案（绕过认领闸门直接调）也点名 ``--db`` 与该路径。

    ``main`` 路径上认领闸门排在前面，它的文案同样含 ``--db``；这里单独钉证据闸门那一份，
    免得「验收 6 过了」其实只是认领闸门的文案过了。
    """

    opts = mg.validate(mg.build_parser().parse_args(cli_argv(tmp_path, db=tmp_path / "nope.db")))
    info = mg.Preflight(
        number=PR, state="OPEN", head_ref=BRANCH, base_ref="main", head_oid=HEAD_OID
    )

    with pytest.raises(mg.MergeGuardError) as excinfo:
        mg.evidence_gate(opts, info)

    message = str(excinfo.value)
    assert "--db" in message
    assert str(tmp_path / "nope.db") in message
    assert "证据" in message


def test_db_without_event_tables_rc2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """库在、但没有 ``task_events`` → 取数失败也是 rc2（「读不到」不许读成「没有证据」）。"""

    rc, out, err = run_main(tmp_path, monkeypatch, capsys, with_event_tables=False)

    assert rc == 2
    assert out == ""
    assert "--db" in err
    assert "evidence" in err or "task_events" in err


# ------------------------------- 其它：verify-only 不跑新闸门 / 摘要口径

def test_verify_only_never_runs_evidence_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--verify-only`` 不新增这一步：库不存在也照样只报远端 ref，不因证据缺席 rc2。"""

    missing = tmp_path / "nowhere.db"
    fake = FakeRunner(ls_remote=(2,))
    monkeypatch.setattr(mg, "_run", fake)

    rc = mg.main(cli_argv(tmp_path, db=missing, verify_only=True))

    captured = capsys.readouterr()
    assert rc == 0
    assert "证据状态机" not in captured.out
    assert captured.err == ""


# --------------------- 返工 t_e7dc8082：三条 fail-open 钉子（零事件 / 缺 tier 键 / 坏 payload）

def test_zero_evidence_events_rc2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """库能读、``task_events`` 一张卡一条 evidence 事件都没有 → rc2「缺证据」，绝不 PASS。

    「零事件」是最容易 fail-open 的形态：取数成功（``parse_failures == 0``）+ 没有任何槽位
    可判，任何「查不到就当齐」的写法都会在这里放行一条没有任何验收证据的 PR。
    """

    fake = FakeRunner()
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, events=[], runner=fake)

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert err.startswith("错误: ")
    assert "缺证据" in err
    for slot in ("tier", "qa_pass", "review:defect", "review:regression"):
        assert slot in err
    assert fake.write_calls() == []


def test_tier_event_without_tier_key_never_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """id 最大的 tier 事件缺 ``tier`` 键 → 该槽「缺证据」、整条闸门 rc2，**不得 PASS**。

    P2-1 的原样复现（pm 在 PR #70 评论里跑出来的）：四槽本来齐全，再追加一条只带
    ``pr`` / ``head_sha`` 的 tier 事件（id 更大 → ``_latest`` 取到它），旧实现在 tier 槽
    只比 head，于是把「缺档位」读成「齐」，C 档缺老板点头也能翻成 PASS。
    """

    fake = FakeRunner()
    events = [*four_slots(), e_row("tier", {"pr": PR, "head_sha": HEAD_OID}, 1)]
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, events=events, runner=fake)

    assert rc == 2
    assert out == ""  # 一行 PASS 都不许打
    assert "缺证据" in err
    assert "tier" in err
    assert fake.write_calls() == []


def test_tier_event_without_reason_key_rc2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """id 最大的 tier 事件缺 ``reason`` 键 → 该槽「缺证据」、rc2（返工 t_39c86400 P2）。

    P2 的原样复现（pm 实测）：``{"tier":"B","head_sha":…}`` 只缺 reason，旧实现只查
    ``payload['tier']``，于是 tier 槽算「齐」、整条闸门 PASS。
    """

    fake = FakeRunner()
    events = [*four_slots(), e_row("tier", {"tier": "B", "head_sha": HEAD_OID}, 1)]
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, events=events, runner=fake)

    assert rc == 2
    assert out == ""
    assert "缺证据" in err
    assert "reason" in err
    assert fake.write_calls() == []


def test_review_verdict_out_of_domain_rc2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """id 最大的 review 事件 ``verdict`` 写成 ``BLOCK`` → 「缺证据」rc2（返工 t_39c86400 P1）。

    旧实现只比 ``== "block"``，大写 BLOCK / 缺 verdict 键都被读成通过 —— 这是 fail-open。
    """

    fake = FakeRunner()
    bad = e_row("review", {"kind": "defect", "head_sha": HEAD_OID, "verdict": "BLOCK"}, 2)
    rc, out, err = run_main(
        tmp_path, monkeypatch, capsys, events=[*four_slots(), bad], runner=fake
    )

    assert rc == 2
    assert out == ""
    assert "缺证据" in err
    assert "verdict" in err
    assert fake.write_calls() == []


def test_db_path_with_spaces_is_quoted_in_command_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """命令格的 ``--db`` 要能直接复跑：路径含空格时用 shell 引号包住（返工 t_39c86400 P3）。"""

    spaced = tmp_path / "with space"
    spaced.mkdir()
    db = board_db(spaced, events=four_slots())
    rc, out, err = run_main(tmp_path, monkeypatch, capsys, db=db)

    assert (rc, err) == (0, "")
    line = next(line for line in out.splitlines() if line.startswith("证据状态机 | PASS |"))
    assert f"--db '{db}'" in line


def test_truncated_event_payload_rc2_names_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """最新一条 review 事件的 payload 被截断 → ``parse_failures=1`` → rc2 且点名 1 条。

    P2-2 的原样复现：坏 payload 被 ``collect`` 丢掉后，剩下的旧 review 事件仍然「齐」，
    旧闸门于是 PASS；判据是「解析失败 > 0 = 无法确认最新证据」，不是「剩下的够不够」。
    """

    fake = FakeRunner()
    truncated = (
        "review",
        '{"kind":"defect","head_sha":"' + HEAD_OID + '","verdict":"blo',
        1,
    )
    rc, out, err = run_main(
        tmp_path, monkeypatch, capsys, events=[*four_slots(), truncated], runner=fake
    )

    assert rc == 2
    assert out == ""
    assert "解析失败" in err
    assert "1 条" in err
    assert fake.write_calls() == []


def test_summary_reads_tier_and_review_slots() -> None:
    """四槽摘要只取档位 + 已通过的双问类名（``qa_pass`` 打码，不抄 run 身份）。"""

    judgment = mg.evidence.judge(four_events(), HEAD_OID, PR)

    assert mg.evidence_slots_summary(judgment) == "tier=B, qa_pass=*** review=defect+regression"


def four_events() -> list[mg.evidence.Event]:
    """``judge`` 直接吃的事件对象（与 :func:`four_slots` 同内容，供摘要单测用）。"""

    return [
        mg.evidence.Event(id=index + 1, kind=kind, payload=json.loads(payload), run_id=run_id, profile=profile)
        for index, ((kind, payload, run_id), profile) in enumerate(
            zip(four_slots(), ("pm", "qa", "pm", "qa"), strict=True)
        )
    ]
