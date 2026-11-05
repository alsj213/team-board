"""脚本化的流程预检：卡面契约与交卡证据，不靠提示词自觉。

两件事：

1. :func:`audit_card` —— 派工前检查卡面是否有机器可读的
   ``PATH-CLAIM`` / ``TEST-PLAN`` / ``CONSUMER`` / ``QA-SPEC``；合并卡（标题命中
   合并语义 **且** 正文首行 ``PR-CLAIM``）豁免实现卡契约。
2. :func:`check_handoff` —— 交 QA 前检查 ``impl_done`` 是否绑当前 head、返工次数是否
   超预算。它只做**纯判定**；取数（看板事件、卡状态）在 CLI 层。

两个函数都返回带 ``findings`` 的报告；CLI 用 rc 0/1/2 表达通过/不通过/环境错误。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import pathclaim, prclaim

__all__ = [
    "CardAuditReport",
    "Finding",
    "HandoffReport",
    "audit_card",
    "check_handoff",
    "render_card",
    "render_card_json",
    "render_handoff",
    "render_handoff_json",
]

#: 卡面机器可读行（顶格；值大小写不敏感）
TEST_PLAN_RE = re.compile(r"^TEST-PLAN:[ \t]*(quick|full)[ \t]*\r?$", re.M | re.I)
CONSUMER_RE = re.compile(r"^CONSUMER:[ \t]*(\S.*?)[ \t]*\r?$", re.M)
QA_SPEC_RE = re.compile(r"^QA-SPEC:[ \t]*qa[ \t]*\r?$", re.M)

#: ``head_sha`` 形态：40 位（git sha1）或 64 位（sha256）小写十六进制，整串。
#: 与 :data:`team_board.evidence.HEAD_RE` 同形态 —— 否则 64 位 ``impl_done``
#: 会被这里判成“没有 impl_done”，两边契约打架。
HEAD_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$", re.ASCII)

#: 交卡阶段（review = 交 qa；pm = 交 pm；merge = 交 repo）
STAGES = ("review", "pm", "merge")

#: 同一张卡允许的 changes_requested 次数（第 2 次返工停手）
REWORK_LIMIT = 1


@dataclass(frozen=True)
class Finding:
    """一条不通过项（code 稳定，message 给人读）。"""

    code: str
    message: str


@dataclass(frozen=True)
class CardAuditReport:
    """卡面契约检查结果。``kind`` = ``impl`` / ``merge``。"""

    card_id: str
    kind: str
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass(frozen=True)
class HandoffReport:
    """交卡预检结果。"""

    card_id: str
    head: str
    stage: str
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


#: 自动收口脚本创建的动作卡统一后缀；它们没有实现契约，不能按实现卡闸门拦。
AUTO_ACTION_SUFFIX = "（自动收口卡）"


def audit_card(
    card_id: str, title: str, body: str, *, assignee: str | None = None
) -> CardAuditReport:
    """检查一张卡的机器可读契约。

    合并卡豁免**两条同时成立**（与 :func:`prclaim.audit_gaps` 同一口径，
    见 ``docs/pr-claim-gate.md`` §1）：标题命中合并语义
    :func:`prclaim.needs_claim`，且正文首行是 ``PR-CLAIM: #<n>``。

    只看首行 ``PR-CLAIM`` 会把开了 PR 的实现卡整体豁免 —— ``kanban_claim_pr``
    给**任何** PR 卡的首行补这一行，实现卡一旦提 PR 就不再受检。
    """

    if prclaim.needs_claim(title):
        if prclaim.first_claim_pr(body) is not None:
            return CardAuditReport(card_id=card_id, kind="merge", findings=())
        return CardAuditReport(
            card_id=card_id,
            kind="merge",
            findings=(
                Finding(
                    "pr-claim-missing",
                    "合并卡正文首行必须是 PR-CLAIM: #<n>（顶格、无尾随字符）",
                ),
            ),
        )
    if str(title).endswith(AUTO_ACTION_SUFFIX):
        # 复审 / rebase / 补证据动作卡由 pr_orphan_autoclose 生成，卡面只有
        # PR-CLAIM + 动作说明；它们不是实现卡，缺 PATH-CLAIM 等不代表卡坏。
        return CardAuditReport(card_id=card_id, kind="auto", findings=())
    if assignee is not None and str(assignee) != "engineer":
        # PATH-CLAIM / TEST-PLAN / CONSUMER / QA-SPEC 是实现卡契约；pm 的需求/拆卡卡、
        # qa 的验收卡、ops 的巡检卡不应被它拦。
        return CardAuditReport(card_id=card_id, kind="non-impl", findings=())

    findings: list[Finding] = []
    paths, issues = pathclaim.parse_body(body)
    if issues:
        findings.append(
            Finding("path-claim-invalid", "；".join(issues[:3]))
        )
    if not paths:
        findings.append(
            Finding(
                "path-claim-missing",
                "缺 PATH-CLAIM（顶格写 PATH-CLAIM: <path>[, <path>]）",
            )
        )
    if TEST_PLAN_RE.search(body) is None:
        findings.append(
            Finding(
                "test-plan-missing",
                "缺 TEST-PLAN: quick|full（决定 QA 测试深度）",
            )
        )
    if CONSUMER_RE.search(body) is None:
        findings.append(
            Finding(
                "consumer-missing",
                "缺 CONSUMER: <下游会执行的命令>（防止生成物不可用）",
            )
        )
    if QA_SPEC_RE.search(body) is None:
        findings.append(
            Finding("qa-spec-missing", "缺顶格 QA-SPEC: qa（内核建卡也会拒）")
        )
    return CardAuditReport(card_id=card_id, kind="impl", findings=tuple(findings))


def check_handoff(
    card_id: str,
    head: str,
    stage: str,
    *,
    impl_heads: tuple[str, ...],
    rework_count: int,
    status: str = "",
) -> HandoffReport:
    """检查交卡前置：``impl_done`` 绑当前 head + 返工未超预算。

    ``impl_heads`` 是按事件 id 升序的 ``impl_done`` head 列表（最后一条是最新）。
    ``stage=merge`` 额外要求实现卡 done；review/pm 不要求状态（调用方在对应 run 内）。
    """

    findings: list[Finding] = []
    if stage not in STAGES:
        raise ValueError(f"stage 必须是 {STAGES} 之一: {stage!r}")
    if rework_count > REWORK_LIMIT:
        findings.append(
            Finding(
                "rework-budget",
                f"changes_requested={rework_count} 超过预算 {REWORK_LIMIT}；"
                "停手交 ops/老板决定，不再自动返工",
            )
        )
    latest = impl_heads[-1] if impl_heads else ""
    if not latest:
        findings.append(
            Finding("impl-done-missing", "没有 impl_done 事件；先在 engineer run 内 emit")
        )
    elif latest != head:
        findings.append(
            Finding(
                "impl-done-stale",
                f"impl_done 绑 {latest} != 当前 head {head}；"
                "在新 head 上重发 impl_done 后再交",
            )
        )
    if stage == "merge" and status != "done":
        findings.append(
            Finding("impl-not-done", f"实现卡状态是 {status or '空'}，合并要求 done")
        )
    return HandoffReport(
        card_id=card_id, head=head, stage=stage, findings=tuple(findings)
    )


def render_card(report: CardAuditReport) -> str:
    """人读卡面检查报告（结论行最后）。"""

    lines = [f"卡面契约检查 {report.card_id}（{report.kind}）"]
    if report.ok:
        lines.append("缺项 0 条")
        lines.append("结论: 通过")
        return "\n".join(lines)
    lines.append(f"缺项 {len(report.findings)} 条：")
    lines.extend(f"  [{item.code}] {item.message}" for item in report.findings)
    lines.append(f"结论: 不通过（{len(report.findings)} 条）")
    return "\n".join(lines)


def render_card_json(report: CardAuditReport) -> str:
    return json.dumps(
        {
            "card": report.card_id,
            "kind": report.kind,
            "ok": report.ok,
            "findings": [
                {"code": item.code, "message": item.message}
                for item in report.findings
            ],
        },
        ensure_ascii=False,
    )


def render_handoff(report: HandoffReport) -> str:
    """人读交卡预检报告（结论行最后）。"""

    lines = [f"交卡预检 {report.card_id} stage={report.stage} head={report.head}"]
    if report.ok:
        lines.append("不通过项 0 条")
        lines.append("结论: 可以交卡")
        return "\n".join(lines)
    lines.append(f"不通过项 {len(report.findings)} 条：")
    lines.extend(f"  [{item.code}] {item.message}" for item in report.findings)
    lines.append(f"结论: 先修复（{len(report.findings)} 条）")
    return "\n".join(lines)


def render_handoff_json(report: HandoffReport) -> str:
    return json.dumps(
        {
            "card": report.card_id,
            "head": report.head,
            "stage": report.stage,
            "ok": report.ok,
            "findings": [
                {"code": item.code, "message": item.message}
                for item in report.findings
            ],
        },
        ensure_ascii=False,
    )
