"""E5 接线：证据闸门拒绝后按缺失槽位建对应角色卡（含 ``--no-create-card`` 干跑）。

这里是**整条 ``merge_guard.main``** 的端到端验证：真 sqlite 库、真
``evidence.collect`` / ``judge``、真 ``claim_gate`` / ``evidence_gate``，只有两处是替身
—— ``merge_guard._run``（gh / git 的唯一出口，复用 E4 单测里的 FakeRunner）与
``evidence_cards._default_runner``（``hermes kanban create`` 的唯一出口，见 :class:`HermesFake`）。
所以「拒绝之后建没建卡、建的是哪张」是跑出来的，不是拼出来的。

对应卡的验收 3（argv 形态 / 幂等）与验收 4（``--no-create-card`` → 子进程 0 次）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from team_board import evidence_cards, reviewhead
from team_board import merge_guard as mg
from tests.unit.test_merge_guard_evidence_gate import (
    BRANCH,
    HEAD_OID,
    IMPL_CARD,
    PR,
    FakeRunner,
    board_db,
    cli_argv,
    e_row,
    four_slots,
    review_bodies,
)


def with_impl_done(rows: list[tuple[str, str, int | None]]) -> list[tuple[str, str, int | None]]:
    """补一条 ``impl_done``（绑当前 head、pr 相等）：E5 的「缺 impl_done → engineer」不再触发。

    这四个必需槽位的用例只想验「缺哪个槽就建哪张卡」，所以基线里得有 ``impl_done`` ——
    否则每条用例都会多出一张 engineer 的补事件卡（那是另一条用例的事）。
    """

    return [*rows, e_row("impl_done", {"pr": PR, "head_sha": HEAD_OID}, 1)]


class FakeProc:
    """``subprocess.run`` 的替身（只带建卡路径要读的两个字段）。"""

    def __init__(self, rc: int = 0, stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = ""
        self.stderr = stderr


class HermesFake:
    """假的建卡子进程：记 argv、回放预设 rc / stderr（等价于把 ``hermes`` 从 PATH 上摘掉）。"""

    def __init__(self, rc: int = 0, stderr: str = "") -> None:
        self.rc = rc
        self.stderr = stderr
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> FakeProc:
        self.calls.append(tuple(argv))
        return FakeProc(self.rc, self.stderr)

    def argvs(self) -> list[str]:
        """``--opt value`` 形态拍平成一行（贴日志用）。"""

        return [" ".join(call) for call in self.calls]


def run_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    events: list[tuple[str, str, int | None]],
    hermes: HermesFake | None = None,
    extra: Sequence[str] = (),
    verify_only: bool = False,
) -> tuple[int, str, str, HermesFake]:
    """跑一次 ``merge_guard.main``：返回（rc, stdout, stderr, 建卡替身）。"""

    fake_hermes = HermesFake() if hermes is None else hermes
    path = board_db(tmp_path, events=events)
    monkeypatch.setattr(mg, "_run", FakeRunner(head_ref=BRANCH))
    monkeypatch.setattr(reviewhead, "fetch_bodies", lambda pr: review_bodies())
    # #73 起双问闸门按内容指纹判：本文件只测证据闸门 + E5 建卡接线，指纹层用固定值打桩
    # （与 tests/unit/test_merge_guard_evidence_gate.py 的 run_main 同一口径）——
    # 否则 PASS 用例会真的去 git merge-base 抓 refs，在临时库上必然 rc128。
    fixed = reviewhead.Fingerprint(
        kind="patch",
        value="c" * 40,
        merge_base="d" * 40,
        paths=(),
        algo=reviewhead.FINGERPRINT_ALGO,
    )
    monkeypatch.setattr(reviewhead, "fingerprint", lambda *a, **k: fixed)
    monkeypatch.setattr(reviewhead, "fingerprint_of_commit", lambda *a, **k: fixed)
    monkeypatch.setattr(evidence_cards, "_default_runner", fake_hermes)
    argv: list[str] = cli_argv(tmp_path, db=path, verify_only=verify_only)
    argv.extend(extra)
    rc = mg.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err, fake_hermes


def qa_card_key() -> str:
    return f"evidence-{IMPL_CARD}-qa_pass-{HEAD_OID[:12]}"


# ------------------------------------------------------ 验收 3：真建卡（argv + 幂等 key）


def test_reject_creates_qa_card_with_expected_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺 ``qa_pass`` → rc2 之外还建一张 qa 卡，argv 带 ``--board devteam`` 等三件套。"""

    rc, out, err, hermes = run_guard(
        tmp_path, monkeypatch, capsys, events=with_impl_done(four_slots(with_qa=False))
    )

    print("建卡调用：", hermes.argvs())
    print("stderr：", err.splitlines())
    assert rc == 2
    assert out == ""  # 拒绝路径 stdout 仍是 0 字节
    assert len(hermes.calls) == 1
    argv = hermes.calls[0]
    assert argv[:5] == ("hermes", "kanban", "--board", "devteam", "create")
    assert argv[argv.index("--assignee") + 1] == "qa"
    assert argv[argv.index("--idempotency-key") + 1] == qa_card_key()
    body_file = argv[argv.index("--body-file") + 1]
    assert body_file.endswith(".md")
    # 拒绝文案一个字都没被善后改掉
    assert f"错误: 证据闸门拦下 PR #{PR}（实现卡 {IMPL_CARD}）：缺证据；缺证据：qa_pass" in err
    assert "已建卡：补 qa_pass 证据" in err


def test_two_missing_slots_create_two_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺 ``qa_pass`` + 缺 ``tier`` → 两张卡（qa + pm），各有自己的幂等 key。"""

    events = with_impl_done([row for row in four_slots(with_qa=False) if row[0] != "tier"])
    rc, _out, _err, hermes = run_guard(tmp_path, monkeypatch, capsys, events=events)

    keys = [call[call.index("--idempotency-key") + 1] for call in hermes.calls]
    owners = [call[call.index("--assignee") + 1] for call in hermes.calls]
    print("建卡调用：", hermes.argvs())
    assert rc == 2
    assert sorted(owners) == ["pm", "qa"]
    assert sorted(keys) == sorted(
        [
            f"evidence-{IMPL_CARD}-tier-{HEAD_OID[:12]}",
            qa_card_key(),
        ]
    )


# --------------------------------------------------------- 验收 4：--no-create-card 干跑


def test_no_create_card_prints_plan_and_calls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--no-create-card`` → 子进程 0 次，只把计划行打到 stderr。"""

    rc, out, err, hermes = run_guard(
        tmp_path,
        monkeypatch,
        capsys,
        events=with_impl_done(four_slots(with_qa=False)),
        extra=("--no-create-card",),
    )

    print("建卡调用数：", len(hermes.calls))
    print("stderr：", err.splitlines())
    assert rc == 2
    assert out == ""
    assert hermes.calls == []
    lines = err.splitlines()
    assert any(line.startswith("自动建卡计划：1 张") for line in lines)
    assert any(qa_card_key() in line and "qa |" in line for line in lines)
    assert any(line.startswith("错误: 证据闸门拦下") for line in lines)


def test_verify_only_ignores_no_create_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--verify-only`` 不跑证据闸门，``--no-create-card`` 在那里没有意义（也不建卡）。"""

    rc, out, err, hermes = run_guard(
        tmp_path,
        monkeypatch,
        capsys,
        events=with_impl_done(four_slots(with_qa=False)),
        extra=("--no-create-card",),
        verify_only=True,
    )

    assert rc == 0  # 远端分支已删 → verify-only 通过
    assert hermes.calls == []
    assert "自动建卡计划" not in err
    assert "结论：远端没有这个 ref" in out


# ------------------------------------------------------------ 守卫拦下 / 其它失败不发散


def test_scope_guard_block_writes_plan_file_and_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """作用域守卫拦下 → 计划落 ``evidence/<卡号>-<head12>.cards.txt``，只回一行提示转 paimeng。"""

    monkeypatch.chdir(tmp_path)
    hermes = HermesFake(rc=1, stderr="kanban: delegate_task child contexts cannot mutate")
    rc, _out, err, _hermes = run_guard(
        tmp_path, monkeypatch, capsys, events=with_impl_done(four_slots(with_qa=False)), hermes=hermes
    )

    plan_file = tmp_path / "evidence" / f"{IMPL_CARD}-{HEAD_OID[:12]}.cards.txt"
    print("plan 文件：", plan_file, plan_file.exists())
    assert rc == 2
    assert len(hermes.calls) == 1  # 不重试、不换 env 绕过（R22）
    assert plan_file.exists()
    assert f"建卡被作用域守卫拦下，计划已落 evidence/{IMPL_CARD}-{HEAD_OID[:12]}.cards.txt" in err
    assert "请转 paimeng" in err
    assert qa_card_key() in plan_file.read_text(encoding="utf-8")


def test_other_failure_does_not_change_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """建卡失败只回一行 ``自动建卡失败 rc=…``，拒绝的 rc2 与文案不变。"""

    hermes = HermesFake(rc=2, stderr="body 超过硬上限：2100 字节（UTF-8）")
    rc, out, err, _hermes = run_guard(
        tmp_path, monkeypatch, capsys, events=with_impl_done(four_slots(with_qa=False)), hermes=hermes
    )

    assert rc == 2
    assert out == ""
    assert "自动建卡失败 rc=2：body 超过硬上限：2100 字节（UTF-8）" in err
    assert "错误: 证据闸门拦下" in err


def test_pass_creates_no_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """四槽齐（PASS）→ 一张卡都不建（只在拒绝路径接线）。"""

    rc, _out, _err, hermes = run_guard(tmp_path, monkeypatch, capsys, events=four_slots())

    print("stderr：", _err.splitlines())
    assert rc == 0
    assert hermes.calls == []
