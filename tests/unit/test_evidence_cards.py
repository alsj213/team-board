"""``evidence_cards``（E5）单测：槽位→角色、body 字节上限、argv 形态、幂等、守卫兜底。

判据一律用 ``evidence.judge`` 真造出来的 ``Judgment``（不手搓 dataclass）：这样
「缺哪个槽」与线上闸门用的是同一份判定，改 judge 时这些用例会跟着红。

本文件覆盖卡的验收 1/2/3/4 的库内部分；``--no-create-card`` 的 CLI 级调用见
``tests/integration/test_merge_guard_evidence_cards.py``。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from team_board import evidence, evidence_cards

HEAD = "a" * 40
OLD_HEAD = "b" * 40
PR = 70
CARD = "t_deadbeef"


def slot_events(
    *,
    with_tier: bool = True,
    with_qa: bool = True,
    with_defect: bool = True,
    with_regression: bool = True,
    with_impl: bool = True,
    with_boss: bool = False,
    tier: str = "B",
    defect_verdict: str = "pass",
    tier_profile: str = "pm",
    head: str = HEAD,
) -> list[evidence.Event]:
    """按开关拼一批 evidence 事件（四槽全绑 ``head``，角色默认都对）。"""

    events: list[evidence.Event] = []
    next_id = 1

    def add(kind: str, payload: dict[str, object], profile: str) -> None:
        nonlocal next_id
        events.append(
            evidence.Event(
                id=next_id,
                kind=kind,
                payload=payload,
                run_id=None if profile == "-" else 100 + next_id,
                profile=None if profile == "-" else profile,
            )
        )
        next_id += 1

    if with_tier:
        add("tier", {"tier": tier, "reason": "常规改动", "head_sha": head}, tier_profile)
    if with_qa:
        add("qa_pass", {"head_sha": head, "run_id": 202}, "qa")
    if with_defect:
        add(
            "review",
            {"kind": "defect", "head_sha": head, "verdict": defect_verdict},
            "qa",
        )
    if with_regression:
        add("review", {"kind": "regression", "head_sha": head, "verdict": "pass"}, "qa")
    if with_impl:
        add("impl_done", {"pr": PR, "head_sha": head}, "engineer")
    if with_boss:
        add("boss_approve", {"pr": PR, "approved_by": "boss"}, "paimeng")
    return events


def judged(events: list[evidence.Event], *, head: str = HEAD) -> evidence.Judgment:
    """真调 ``evidence.judge``（不手搓 Judgment）。"""

    return evidence.judge(events, head, PR)


# ------------------------------------------------------------------ 验收 1：槽位 → 角色


def test_missing_qa_pass_goes_to_qa() -> None:
    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    assert [(s.idempotency_key.split("-")[2], s.assignee) for s in specs] == [("qa_pass", "qa")]
    assert specs[0].title == f"补 qa_pass 证据：{CARD} @ {HEAD[:12]}"


def test_missing_tier_goes_to_pm() -> None:
    specs = evidence_cards.plan(judged(slot_events(with_tier=False)), CARD, PR, HEAD)
    assert [s.assignee for s in specs] == ["pm"]
    assert specs[0].idempotency_key == f"evidence-{CARD}-tier-{HEAD[:12]}"
    assert specs[0].title == f"补档位判定：{CARD} @ {HEAD[:12]}"


def test_missing_review_defect_goes_to_qa() -> None:
    specs = evidence_cards.plan(judged(slot_events(with_defect=False)), CARD, PR, HEAD)
    assert [s.assignee for s in specs] == ["qa"]
    assert specs[0].title == f"补缺陷视角双问：{CARD} @ {HEAD[:12]}"


def test_missing_review_regression_goes_to_qa() -> None:
    specs = evidence_cards.plan(judged(slot_events(with_regression=False)), CARD, PR, HEAD)
    assert [s.assignee for s in specs] == ["qa"]
    assert specs[0].title == f"补回归视角双问：{CARD} @ {HEAD[:12]}"


def test_missing_boss_approve_goes_to_pm() -> None:
    judgment = judged(slot_events(tier="C"))
    assert judgment.verdict == evidence.VERDICT_NO_BOSS
    specs = evidence_cards.plan(judgment, CARD, PR, HEAD)
    assert [(s.assignee, s.title) for s in specs] == [
        ("pm", f"C 档待老板点头：{CARD} @ {HEAD[:12]}")
    ]
    assert specs[0].idempotency_key == f"evidence-{CARD}-boss_approve-{HEAD[:12]}"


def test_missing_impl_done_goes_to_engineer_with_qa_spec_section() -> None:
    events = slot_events(with_qa=False, with_impl=False)
    specs = evidence_cards.plan(judged(events), CARD, PR, HEAD, events=events)
    by_owner = {s.assignee: s for s in specs}
    assert set(by_owner) == {"qa", "engineer"}
    engineer = by_owner["engineer"]
    assert engineer.idempotency_key == f"evidence-{CARD}-impl_done-{HEAD[:12]}"
    lines = engineer.body.splitlines()
    # 行首 `## 验收标准` 段，段内第一条非空行整行 `QA-SPEC: qa`
    at = lines.index(evidence_cards.QA_SECTION)
    assert lines[at] == "## 验收标准"
    assert lines[at + 1] == "QA-SPEC: qa"
    assert lines[0].startswith("依据：缺证据（merge_guard 证据闸门，卡 t_deadbeef，head ")


def test_blocked_review_goes_to_engineer_as_rework() -> None:
    judgment = judged(slot_events(defect_verdict="block"))
    assert judgment.verdict == evidence.VERDICT_BLOCKED
    specs = evidence_cards.plan(judgment, CARD, PR, HEAD)
    assert [(s.assignee, s.title) for s in specs] == [
        ("engineer", f"返工：{CARD} 双问阻塞 @ {HEAD[:12]}")
    ]
    assert "## 验收标准" in specs[0].body


def test_role_mismatch_goes_to_pm_as_anomaly() -> None:
    judgment = judged(slot_events(tier_profile="engineer"))
    assert judgment.verdict == evidence.VERDICT_ROLE_MISMATCH
    specs = evidence_cards.plan(judgment, CARD, PR, HEAD)
    assert [(s.assignee, s.title) for s in specs] == [
        ("pm", f"证据异常核查：{CARD} @ {HEAD[:12]}")
    ]


def test_cross_card_goes_to_pm_as_anomaly() -> None:
    events = slot_events()
    events[-1] = evidence.Event(
        id=events[-1].id,
        kind="impl_done",
        payload={"pr": PR + 1, "head_sha": HEAD},
        run_id=999,
        profile="engineer",
    )
    judgment = judged(events)
    assert judgment.verdict == evidence.VERDICT_CROSS_CARD
    specs = evidence_cards.plan(judgment, CARD, PR, HEAD, events=events)
    assert [(s.assignee, s.title) for s in specs] == [
        ("pm", f"证据异常核查：{CARD} @ {HEAD[:12]}")
    ]


def test_head_mismatch_keeps_slot_owner_and_says_resend() -> None:
    judgment = judged(slot_events(head=OLD_HEAD))
    assert judgment.verdict == evidence.VERDICT_HEAD_MISMATCH
    specs = evidence_cards.plan(judgment, CARD, PR, HEAD)
    assert {s.assignee for s in specs} == {"qa", "pm"}
    assert all("需重发" in s.body for s in specs)
    assert len(specs) == 4  # tier→pm + qa_pass + 两条 review


def test_pass_judgment_plans_nothing() -> None:
    assert judged(slot_events()) .verdict == evidence.VERDICT_PASS
    assert evidence_cards.plan(judged(slot_events()), CARD, PR, HEAD) == ()
    assert evidence_cards.plan(judged(slot_events()), CARD, PR, HEAD, events=()) == ()


def test_events_none_does_not_judge_impl_done() -> None:
    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    assert [s.assignee for s in specs] == ["qa"]


# ------------------------------------------------- 验收 2：body ≤ 2048 字节、标题无禁词


@pytest.mark.parametrize(
    ("events", "kwargs"),
    [
        (slot_events(with_qa=False), {"events": slot_events(with_qa=False)}),
        (slot_events(with_tier=False), {}),
        (slot_events(with_defect=False), {}),
        (slot_events(with_regression=False), {}),
        (slot_events(tier="C"), {}),
        (slot_events(with_qa=False, with_impl=False), {"events": slot_events(with_qa=False, with_impl=False)}),
        (slot_events(defect_verdict="block"), {}),
        (slot_events(tier_profile="engineer"), {}),
    ],
)
def test_body_bytes_and_title_words(events: list[evidence.Event], kwargs: dict[str, Any]) -> None:
    specs = evidence_cards.plan(judged(events), CARD, PR, HEAD, **kwargs)
    assert specs, "这组应该有卡"
    sizes = {s.title: s.body_bytes for s in specs}
    print(f"body 字节数：{sizes}")
    for spec in specs:
        assert spec.body_bytes <= evidence_cards.BODY_LIMIT_BYTES, (spec.title, spec.body_bytes)
        for banned in evidence_cards.TITLE_BANNED:
            assert banned not in spec.title, (banned, spec.title)


def test_long_action_is_truncated_not_structural() -> None:
    """「要做什么」超 200 字只截这一节（首行依据 / 命令 / 验收标准段必须还在）。"""

    parts = [
        "依据：缺证据（x）",
        "要做什么：" + evidence_cards.DO_SLOT,
        "写事件：cmd",
        "",
        evidence_cards.QA_SECTION,
        evidence_cards.QA_SPEC_LINE,
    ]
    body = evidence_cards._fit_body(parts, "很长的说明" * 200)
    assert len(body.encode("utf-8")) <= evidence_cards.BODY_LIMIT_BYTES
    assert body.startswith("依据：缺证据（x）")
    assert "写事件：cmd" in body
    assert body.endswith(evidence_cards.QA_SPEC_LINE)


# --------------------------------------------------- 验收 3：argv 形态 + 幂等（调用两次）


class FakeProc:
    def __init__(self, rc: int = 0, stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = ""
        self.stderr = stderr


class FakeRunner:
    """记录每次 argv 的假 runner（像 subprocess.run 一样吃 argv、返回 FakeProc）。"""

    def __init__(self, results: list[FakeProc] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._results = list(results or [])

    def __call__(self, argv: Sequence[str]) -> FakeProc:
        self.calls.append(tuple(argv))
        if self._results:
            return self._results.pop(0)
        return FakeProc()


def test_create_argv_shape(tmp_path: Path) -> None:
    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    runner = FakeRunner()
    creator = evidence_cards.CardCreator(runner=runner, echo=lambda _line: None,
                                         scratch_dir=tmp_path)
    creator.ensure(specs, impl_card=CARD, head=HEAD)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    print(f"argv：{argv}")
    assert argv[0] == "hermes"
    assert argv[1:3] == ("kanban", "--board")
    assert argv[3] == "devteam"
    assert argv[4] == "create"
    assert argv[argv.index("--assignee") + 1] == "qa"
    assert argv[argv.index("--idempotency-key") + 1] == specs[0].idempotency_key
    body_path = argv[argv.index("--body-file") + 1]
    assert body_path.endswith(".md")
    assert not Path(body_path).exists(), "body 临时文件用后必须删掉"


def test_same_key_twice_only_one_subprocess(tmp_path: Path) -> None:
    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    runner = FakeRunner()
    lines: list[str] = []
    creator = evidence_cards.CardCreator(runner=runner, echo=lines.append, scratch_dir=tmp_path)
    first = creator.ensure(specs, impl_card=CARD, head=HEAD)
    second = creator.ensure(specs, impl_card=CARD, head=HEAD)
    log = [f"call[{i}] {call[4]} {call[5]}" for i, call in enumerate(runner.calls)]
    print("子进程调用日志：", log)
    print("回显：", lines)
    assert len(runner.calls) == 1, log
    assert first[0].status == "created"
    assert second[0].status == "skipped"
    assert second[0].ok


def test_failure_line_and_no_retry(tmp_path: Path) -> None:
    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    runner = FakeRunner([FakeProc(rc=2, stderr="第一行\nbody 超过硬上限：2100 字节\n")])
    lines: list[str] = []
    creator = evidence_cards.CardCreator(runner=runner, echo=lines.append, scratch_dir=tmp_path)
    outcomes = creator.ensure(specs, impl_card=CARD, head=HEAD)
    assert [o.status for o in outcomes] == ["failed"]
    assert outcomes[0].line == "自动建卡失败 rc=2：body 超过硬上限：2100 字节"
    # 失败不重试，也不记成已建（下一轮还会再试）
    creator.ensure(specs, impl_card=CARD, head=HEAD)
    assert len(runner.calls) == 2


def test_missing_hermes_binary_is_a_failure_not_a_traceback(tmp_path: Path) -> None:
    """``hermes`` 不在 PATH（``OSError``）→ 一行 ``rc=127``，不冒异常（rc2 的语义不许被改）。"""

    def boom(_argv: Sequence[str]) -> FakeProc:
        raise FileNotFoundError("No such file or directory: 'hermes'")

    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    lines: list[str] = []
    creator = evidence_cards.CardCreator(
        runner=cast(evidence_cards.Runner, boom), echo=lines.append, scratch_dir=tmp_path
    )
    outcomes = creator.ensure(specs, impl_card=CARD, head=HEAD)

    assert [o.status for o in outcomes] == ["failed"]
    assert outcomes[0].rc == 127
    assert outcomes[0].line.startswith("自动建卡失败 rc=127：")
    assert lines == [outcomes[0].line]


def test_guard_block_falls_back_to_plan_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    runner = FakeRunner([FakeProc(rc=1, stderr="kanban: delegate_task child contexts cannot mutate")])
    lines: list[str] = []
    creator = evidence_cards.CardCreator(runner=runner, echo=lines.append, scratch_dir=tmp_path)
    outcomes = creator.ensure(specs, impl_card=CARD, head=HEAD)
    assert [o.status for o in outcomes] == ["guard-blocked"]
    plan_file = tmp_path / "evidence" / f"{CARD}-{HEAD[:12]}.cards.txt"
    assert plan_file.exists()
    expected = f"建卡被作用域守卫拦下，计划已落 evidence/{CARD}-{HEAD[:12]}.cards.txt，请转 paimeng"
    assert lines == [expected], lines
    text = plan_file.read_text(encoding="utf-8")
    assert "被作用域守卫拦下" in text
    assert specs[0].idempotency_key in text
    # 不重试、不换 env 绕过：仍然只发过一次子进程
    assert len(runner.calls) == 1


def test_guard_block_with_two_cards_keeps_both_in_plan_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两张卡同时缺、守卫拦住第一张 → 计划文件里两张都还在（不许被后一张覆盖掉）。

    这是双问缺陷视角收口的那条：逐张各写一次同一文件会让最后一张覆盖前面的，人工接手
    时漏卡。同时「守卫拦下后不再发子进程」= 一轮到底，不重试（R22）。
    """

    monkeypatch.chdir(tmp_path)
    events = slot_events(with_qa=False, with_tier=False)
    specs = evidence_cards.plan(judged(events), CARD, PR, HEAD, events=events)
    assert sorted(spec.assignee for spec in specs) == ["pm", "qa"]
    runner = FakeRunner([FakeProc(rc=1, stderr="kanban: scoped to task t_deadbeef")])
    lines: list[str] = []
    creator = evidence_cards.CardCreator(runner=runner, echo=lines.append, scratch_dir=tmp_path)
    outcomes = creator.ensure(specs, impl_card=CARD, head=HEAD)

    print("子进程调用次数：", len(runner.calls))
    print("回显：", lines)
    assert [o.status for o in outcomes] == ["guard-blocked", "guard-blocked"]
    assert len(runner.calls) == 1, "守卫拦下后不许再为剩下的卡发子进程"
    assert len(lines) == 1, "只回一行提示转 paimeng"
    text = (tmp_path / "evidence" / f"{CARD}-{HEAD[:12]}.cards.txt").read_text(encoding="utf-8")
    for spec in specs:
        assert spec.idempotency_key in text, spec.title
        assert spec.title in text, spec.title


# ------------------------------------- qa P3：本地写失败只许降级成一行，不许冒异常出去
#
# 判据（qa 2026-11-01 request-changes）：建卡善后遇上「body 临时文件写不了」/「计划落不了
# 盘」时，merge_guard 只剩 `内部错误：PermissionError`，把「缺证据：qa_pass」这个真因吃掉。
# 修法是把这两处 OSError 归进 failed / None，异常一律不出口。下面的用例钉住这两条。


def test_body_tmpfile_in_missing_dir_is_a_failed_line(tmp_path: Path) -> None:
    """body 临时文件目录不存在（磁盘满 / TMPDIR 被删的同类）→ 一行 ``failed``，不抛。"""

    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    runner = FakeRunner()
    lines: list[str] = []
    creator = evidence_cards.CardCreator(
        runner=runner,
        echo=lines.append,
        scratch_dir=tmp_path / "gone",  # 不存在 → NamedTemporaryFile 抛 FileNotFoundError
    )

    outcomes = creator.ensure(specs, impl_card=CARD, head=HEAD)  # 不许抛出去

    print("回显：", lines)
    assert [o.status for o in outcomes] == ["failed"]
    assert outcomes[0].rc == 127
    assert "body 临时文件写不了" in outcomes[0].line
    assert "FileNotFoundError" in outcomes[0].line
    assert runner.calls == [], "body 都写不下去，不该起子进程"
    assert lines == [outcomes[0].line]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root 无视目录权限位，chmod 500 挡不住写入",
)
def test_body_tmpfile_permission_denied_is_a_failed_line(tmp_path: Path) -> None:
    """TMPDIR 不可写（qa probe_f 的库内等价物）→ 一行点名 ``PermissionError``，不抛。"""

    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
        lines: list[str] = []
        creator = evidence_cards.CardCreator(
            runner=FakeRunner(), echo=lines.append, scratch_dir=ro
        )
        outcomes = creator.ensure(specs, impl_card=CARD, head=HEAD)
    finally:
        ro.chmod(0o700)

    assert [o.status for o in outcomes] == ["failed"]
    assert "PermissionError" in outcomes[0].line
    assert len(lines) == 1


def test_plan_file_unwritable_keeps_guard_line_and_returns_none(tmp_path: Path) -> None:
    """``evidence/`` 落不了盘（qa probe_d D2）→ ``write_plan_file`` 返 ``None``、只回一行。"""

    blocker = tmp_path / "blocker"
    blocker.write_text("我不是目录", encoding="utf-8")
    specs = evidence_cards.plan(judged(slot_events(with_qa=False)), CARD, PR, HEAD)
    lines: list[str] = []
    creator = evidence_cards.CardCreator(
        runner=FakeRunner([FakeProc(rc=1, stderr="kanban: scoped to task t_deadbeef")]),
        echo=lines.append,
        scratch_dir=tmp_path,
        plan_dir=blocker / "evidence",  # 父路径是普通文件 → mkdir 抛 NotADirectoryError
    )

    outcomes = creator.ensure(specs, impl_card=CARD, head=HEAD)

    print("回显：", lines)
    assert [o.status for o in outcomes] == ["guard-blocked"]
    assert outcomes[0].line == "建卡被作用域守卫拦下，计划落盘失败，请转 paimeng"
    assert lines[-1] == outcomes[0].line
    assert all(line.startswith("计划落盘失败（") for line in lines[:-1])
    assert "NotADirectoryError" in lines[0]
    assert creator.write_plan_file(specs, impl_card=CARD, head=HEAD) is None
