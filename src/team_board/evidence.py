"""evidence 契约 v1 的判定层：kind 表、角色矩阵、payload 校验与 ``judge()``（**零 IO**）。

本模块是「evidence 事件」的唯一契约来源，只做纯函数：

- 不连库、不 import sqlite3、不读环境、不 print —— 写入口（``evidence_store``）与取数层
  （``evidence.collect``）各自负责 IO，判定一律调这里的 :func:`judge`，不许在 CLI 里
  重写一遍；
- :data:`KINDS` 是七种事件的固定顺序；:data:`ROLE_RULES` 是 kind → 允许 profile 的唯一
  矩阵；:data:`EMITTABLE_KINDS` 去掉 ``merged``（由 ``merge_audit`` trailer 承载，
  emit 一律拒绝）；
- :func:`validate_payload` 是 payload 的唯一校验入口（缺键 / 多键 / 类型 / 取值域一律
  ``ValueError``，文案带键名与实际值）；:func:`normalize` 是落库与比较的唯一序列化口径；
- ``head_sha`` **逐字比较**：40 或 64 位小写十六进制，不做前缀、不做大小写归一。

判定优先级（多个问题同时存在时只取一个 verdict，其余进 :attr:`Judgment.detail`）::

    角色不符 > 缺证据 > 证据对新 head 无效 > 双问阻塞 > 缺老板点头 > 证据串卡

三个刻意写下来的口径（契约未逐字规定，实现取保守一侧）：

1. :class:`Event.payload` 的类型是 ``Mapping[str, object]`` —— 调用方传 ``dict`` 即可
   （SQLite 读出来的 dict、``json.loads`` 的 dict 都符合），本层不复制、不改写它；
2. :func:`judge` 的 ``pr`` 缺省 ``None`` = 调用方不知道被合并的 PR 号（``evidence audit``
   的 ``--pr`` 是可选参数）：此时**只跳过依赖 PR 相等的那半** —— 「证据串卡」不判、
   ``boss_approve`` 不跟 ``pr`` 比；但 ``tier == "C"`` 一律要求存在 ``boss_approve``
   （没有就是「缺老板点头」，与 ``pr`` 无关）。其余判定照常；
3. ``tier_required`` 缺省 ``None`` = 不额外要求档位；给了值就要求 tier 槽位证据的
   ``tier`` **恰好等于**它（不等时该槽位记 ``missing``，verdict 落「缺证据」，detail 写清
   要求值与实际值）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 七种 evidence 事件的固定顺序（``merged`` 在末尾：保留字，不可 emit）
KINDS: tuple[str, ...] = (
    "spec_sign",
    "impl_done",
    "tier",
    "qa_pass",
    "review",
    "boss_approve",
    "merged",
)

#: kind → 允许的 profile 集合（逐字取契约 v1 的角色矩阵；不得在本矩阵之外另立一份）
ROLE_RULES: dict[str, frozenset[str]] = {
    "spec_sign": frozenset({"qa"}),
    "impl_done": frozenset({"engineer"}),
    "tier": frozenset({"pm"}),
    "qa_pass": frozenset({"qa"}),
    "review": frozenset({"pm", "qa"}),
    "boss_approve": frozenset({"paimeng"}),
    "merged": frozenset({"repo"}),
}

#: 可 emit 的 kind（= :data:`KINDS` 去掉 ``merged``；emit 拒绝 ``merged``）
EMITTABLE_KINDS: tuple[str, ...] = tuple(kind for kind in KINDS if kind != "merged")

#: 合并必需四槽（顺序即报告行顺序）；每槽取该 kind 里 id 最大的一条事件
REQ_SLOTS: tuple[str, ...] = ("tier", "qa_pass", "review:defect", "review:regression")

#: 槽位名 → (事件 kind, review 类事件的 payload.kind 判别值 / None)
SLOT_SOURCES: dict[str, tuple[str, str | None]] = {
    "tier": ("tier", None),
    "qa_pass": ("qa_pass", None),
    "review:defect": ("review", "defect"),
    "review:regression": ("review", "regression"),
}

#: 每种 kind 的 payload 必填键（**恰好**这些，多一个少一个都 ValueError）
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "spec_sign": (),
    "impl_done": ("pr", "head_sha"),
    "tier": ("tier", "reason", "head_sha"),
    "qa_pass": ("head_sha", "run_id"),
    "review": ("kind", "head_sha", "verdict"),
    "boss_approve": ("pr", "approved_by"),
}

#: ``head_sha`` 的形态：40 或 64 位小写十六进制（整串 fullmatch；缩写/大写/空白都不算）
HEAD_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$", re.ASCII)

#: 卡分支的唯一形态：``wt/t_<8 位小写十六进制>``（整串 fullmatch）
BRANCH_RE = re.compile(r"^wt/(t_[0-9a-f]{8})$")

#: PR 号上限（与 ``prclaim.MAX_PR`` / ``reviewhead.PR_MAX`` 同口径：13 位毫秒与天文数字
#: 属于「填错字段」）
PR_MAX = 999_999_999

#: ``tier.reason`` 的字符数上限（按字符不按字节）
REASON_MAX = 200

#: ``tier.tier`` 的取值域
TIERS: frozenset[str] = frozenset({"A", "B", "C"})

#: ``review.kind`` / ``review.verdict`` 的取值域
REVIEW_KINDS: frozenset[str] = frozenset({"defect", "regression"})
REVIEW_VERDICTS: frozenset[str] = frozenset({"pass", "block"})

# ---- 七个 VERDICT 字面量（全仓只此七种，不出现第八个判定词）----

VERDICT_PASS = "PASS"  # nosec B105 - 判定字面量不是口令，bandit 误报
VERDICT_MISSING = "缺证据"
VERDICT_HEAD_MISMATCH = "证据对新 head 无效"
VERDICT_BLOCKED = "双问阻塞"
VERDICT_NO_BOSS = "缺老板点头"
VERDICT_CROSS_CARD = "证据串卡"
VERDICT_ROLE_MISMATCH = "角色不符"

#: 判定优先级（左高右低；多个问题同时存在时取最左一个当 verdict）
VERDICT_PRIORITY: tuple[str, ...] = (
    VERDICT_ROLE_MISMATCH,
    VERDICT_MISSING,
    VERDICT_HEAD_MISMATCH,
    VERDICT_BLOCKED,
    VERDICT_NO_BOSS,
    VERDICT_CROSS_CARD,
)

# ---- 槽位状态（:attr:`Slot.state` 的取值域）----

SLOT_OK = "ok"
SLOT_MISSING = "missing"
SLOT_HEAD_MISMATCH = "head-mismatch"
SLOT_BLOCKED = "blocked"
SLOT_ROLE_MISMATCH = "role-mismatch"


@dataclass(frozen=True)
class Event:
    """一条 evidence 事件（``task_events`` 的一行；payload 由调用方解好传进来）。

    - ``run_id`` / ``profile``：``task_events.run_id`` 与 ``task_runs.profile`` 的 join
      结果，本层**只信**这两个字段判角色（``tasks.assignee`` 不作凭据）；
    - ``created_at``：epoch 秒，只做留痕，不参与判定（同一 kind 取 id 最大的那条）。
    """

    id: int
    kind: str
    payload: Mapping[str, object]
    run_id: int | None = None
    profile: str | None = None
    created_at: int = 0


@dataclass(frozen=True)
class Slot:
    """一个必需槽位的判定结果。

    ``state`` ∈ ``ok`` / ``missing`` / ``head-mismatch`` / ``blocked`` / ``role-mismatch``；
    ``event`` 是该槽位 id 最大的那条证据（``missing`` 时为 ``None``）。
    """

    name: str
    event: Event | None
    state: str

    @property
    def ok(self) -> bool:
        """该槽位自身通过（= ``ok``）。"""
        return self.state == SLOT_OK


@dataclass(frozen=True)
class Judgment:
    """一次 :func:`judge` 的全部结果：一个 verdict + 四槽明细 + 缺哪些槽 + 人话说明。"""

    verdict: str
    slots: tuple[Slot, ...]
    missing: tuple[str, ...]
    detail: str

    @property
    def ok(self) -> bool:
        """判成 ``PASS``（CLI 的 rc0）。"""
        return self.verdict == VERDICT_PASS


def _check_pr(key: str, value: object) -> int:
    """``pr``：1-:data:`PR_MAX` 的 int（bool / 字符串 / 浮点一律拒）。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"payload['{key}'] 非法：{value!r}（应为 1-{PR_MAX} 的 int）")
    if value < 1 or value > PR_MAX:
        raise ValueError(f"payload['{key}'] 非法：{value!r}（应为 1-{PR_MAX} 的 int）")
    return value


def _check_head(key: str, value: object) -> str:
    """``head_sha``：40 或 64 位小写十六进制，逐字比较（不做前缀/大小写归一）。"""
    if not isinstance(value, str) or HEAD_RE.fullmatch(value) is None:
        raise ValueError(
            f"payload['{key}'] 非法：{value!r}（应为 40 或 64 位小写十六进制的提交 sha）"
        )
    return value


def _check_run_id(key: str, value: object) -> int:
    """``run_id``：正整数 int（bool 不算）。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"payload['{key}'] 非法：{value!r}（应为正整数 int）")
    return value


def _check_choice(key: str, value: object, allowed: frozenset[str]) -> str:
    """取值域校验（``tier`` / ``review.kind`` / ``review.verdict``）。"""
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(
            f"payload['{key}'] 非法：{value!r}（应为 {'/'.join(sorted(allowed))} 之一）"
        )
    return value


def _check_reason(key: str, value: object) -> str:
    """``tier.reason``：非空字符串（纯空白算空）且 ≤ :data:`REASON_MAX` 字符。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"payload['{key}'] 非法：{value!r}（不得为空串）")
    if len(value) > REASON_MAX:
        raise ValueError(
            f"payload['{key}'] 非法：{len(value)} 字符（不得超过 {REASON_MAX} 字符）"
        )
    return value


def validate_payload(kind: str, payload: Mapping[str, object]) -> dict[str, object]:
    """校验一条 evidence 事件的 payload，成功返回**归一后的新 dict**（不改原对象）。

    缺键 / 多键 / 类型错 / 取值域错一律 ``ValueError``（文案带键名与实际值）；
    ``kind`` 不在 :data:`EMITTABLE_KINDS` 时也 ``ValueError`` —— ``merged`` 由
    ``merge_audit`` trailer 承载，不许被 emit 成 evidence 事件。
    """
    if kind not in KINDS:
        raise ValueError(f"kind 非法：{kind!r}（应为 {'/'.join(KINDS)} 之一）")
    if kind not in EMITTABLE_KINDS:
        raise ValueError(
            f"kind 不可 emit：{kind!r}（merged 由 merge_audit trailer 承载，"
            "不许写成 evidence 事件）"
        )
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"payload 非法：{payload!r}（应为 dict，kind={kind}）"
        )

    required = REQUIRED_KEYS[kind]
    actual = set(payload.keys())
    absent = [key for key in required if key not in actual]
    if absent:
        raise ValueError(
            f"payload 缺键：{'/'.join(absent)}（kind={kind}；实际键：{sorted(actual)}）"
        )
    extra = sorted(actual - set(required))
    if extra:
        raise ValueError(
            f"payload 多键：{'/'.join(extra)}（kind={kind}；允许键：{list(required)}）"
        )

    if kind == "spec_sign":
        return {}
    if kind == "impl_done":
        return {
            "pr": _check_pr("pr", payload["pr"]),
            "head_sha": _check_head("head_sha", payload["head_sha"]),
        }
    if kind == "tier":
        return {
            "tier": _check_choice("tier", payload["tier"], TIERS),
            "reason": _check_reason("reason", payload["reason"]),
            "head_sha": _check_head("head_sha", payload["head_sha"]),
        }
    if kind == "qa_pass":
        return {
            "head_sha": _check_head("head_sha", payload["head_sha"]),
            "run_id": _check_run_id("run_id", payload["run_id"]),
        }
    if kind == "review":
        return {
            "kind": _check_choice("kind", payload["kind"], REVIEW_KINDS),
            "head_sha": _check_head("head_sha", payload["head_sha"]),
            "verdict": _check_choice("verdict", payload["verdict"], REVIEW_VERDICTS),
        }
    # boss_approve
    approved_by = payload["approved_by"]
    if approved_by != "boss":
        raise ValueError(
            f"payload['approved_by'] 非法：{approved_by!r}（必须恰为 'boss'）"
        )
    return {
        "pr": _check_pr("pr", payload["pr"]),
        "approved_by": "boss",
    }


def card_from_branch(branch: object) -> str | None:
    """从分支名取卡号：整串 ``wt/t_<8 位小写十六进制>`` 才返回 ``t_xxxxxxxx``。

    其它形态（``wt/t_x`` / ``feature/x`` / 空串 / 非字符串）一律 ``None`` ——
    调用方据此 rc2，不许猜。
    """
    if not isinstance(branch, str):
        return None
    match = BRANCH_RE.fullmatch(branch)
    return None if match is None else match.group(1)


def normalize(payload: Mapping[str, object]) -> str:
    """落库与比较的唯一序列化口径：``sort_keys`` + 紧凑分隔符 + 不转义中文。"""
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _latest(events: Sequence[Event], kind: str, disc: str | None) -> Event | None:
    """该 kind（review 类再按 ``payload.kind == disc`` 过滤）里 id 最大的一条。"""
    found: Event | None = None
    for event in events:
        if event.kind != kind:
            continue
        if disc is not None and event.payload.get("kind") != disc:
            continue
        if found is None or event.id > found.id:
            found = event
    return found


def _short(value: object) -> str:
    """sha 的前 12 位（报告里用，避免把 64 位 sha 铺满一行）；非字符串原样 ``repr``。"""
    if isinstance(value, str):
        return value[:12]
    return repr(value)


def _absent_keys(kind: str, payload: Mapping[str, object]) -> list[str]:
    """该 kind 的 payload 少了哪些必填键（顺序即 :data:`REQUIRED_KEYS` 的顺序）。

    与 :func:`validate_payload` 共用同一张 :data:`REQUIRED_KEYS` 表：槽位判定里要判
    「证据够不够格」的地方都调这里，不许另抄一份键名。
    """

    return [key for key in REQUIRED_KEYS[kind] if key not in payload]


def _review_kind_is_legal(value: object) -> bool:
    """review 事件的 ``payload['kind']``：必须**是** ``defect`` / ``regression`` 之一。

    报告里用它点名「kind 域外、被 ``_latest`` 静默丢掉」的事件（返工 t_39c86400 P3）。
    与 :func:`validate_payload` 共用同一个 :data:`REVIEW_KINDS`。
    """

    return isinstance(value, str) and value in REVIEW_KINDS


def _verdict_is_legal(value: object) -> bool:
    """review 槽的 ``payload['verdict']``：必须**是** ``pass`` / ``block`` 之一。

    缺键（``None``）、非字符串、大小写走样（``BLOCK`` / ``Block``）一律 ``False`` ——
    槽位记 ``missing``、verdict 落「缺证据」。旧实现只比 ``== "block"``，于是
    「缺 verdict 键」「写大写 BLOCK」全被读成通过（P1 fail-open，返工 t_39c86400）。
    与 :func:`validate_payload` 共用同一个 :data:`REVIEW_VERDICTS`。
    """

    return isinstance(value, str) and value in REVIEW_VERDICTS


def _tier_is_legal(value: object) -> bool:
    """tier 槽的档位取值域：``payload['tier']`` 必须**是** ``A`` / ``B`` / ``C`` 之一。

    缺键（``None``）、非字符串、取值域外一律 ``False`` —— 槽位记 ``missing``、verdict 落
    「缺证据」。与 :func:`validate_payload` 共用同一个 :data:`TIERS`，但**不**在这里跑整份
    ``validate_payload``：那会把 head / 角色问题也折成「缺证据」，打乱
    :data:`VERDICT_PRIORITY` 已定的先后（角色不符 > 缺证据 > 证据对新 head 无效）。
    """

    return isinstance(value, str) and value in TIERS


def judge(
    events: Iterable[Event],
    head: str,
    pr: int | None = None,
    tier_required: str | None = None,
) -> Judgment:
    """判「四槽证据是否齐、是否绑 ``head``」（纯函数；不查库、不解 JSON）。

    四槽（:data:`REQ_SLOTS`）各取 id 最大的一条，逐槽按
    ``角色 → 缺证据 → head → 双问`` 的顺序定状态；四个之外再补两条：
    ``impl_done`` 存在但 ``pr`` 不等 → 证据串卡；``tier == "C"`` 必须有 ``pr`` 相等的
    ``boss_approve``。多个问题同时存在时按 :data:`VERDICT_PRIORITY` 取最左一个当
    verdict，其余问题写进 :attr:`Judgment.detail`。

    ``pr`` 为 ``None``（``--pr`` 没给）时跳过依赖 PR 相等的两条判定；
    ``tier_required`` 给了值就要求 tier 槽位证据的档位**恰好等于**它。

    tier 槽在 head 之外还要求 ``payload['tier']`` 落在 :data:`TIERS`（``A``/``B``/``C``）
    里 —— 缺键 / 非字符串 / 取值域外一律记 ``missing``（verdict 落「缺证据」），
    不许「head 对得上就算齐」。
    """
    ordered = tuple(events)
    slots: list[Slot] = []
    missing: list[str] = []
    problems: dict[str, list[str]] = {verdict: [] for verdict in VERDICT_PRIORITY}

    for name in REQ_SLOTS:
        src_kind, disc = SLOT_SOURCES[name]
        event = _latest(ordered, src_kind, disc)
        state = SLOT_OK
        if event is None:
            state = SLOT_MISSING
            missing.append(name)
            problems[VERDICT_MISSING].append(f"{name}：无 {src_kind} 事件")
        else:
            allowed = ROLE_RULES[src_kind]
            if event.run_id is None or event.profile is None or event.profile not in allowed:
                state = SLOT_ROLE_MISMATCH
                problems[VERDICT_ROLE_MISMATCH].append(
                    f"{name}：run_id={event.run_id} profile={event.profile!r} "
                    f"不在 {src_kind} 的允许集合 {sorted(allowed)}"
                )
            elif name == "tier" and (absent := _absent_keys("tier", event.payload)):
                # 返工 t_39c86400（P2）：tier 槽原先只查 ``payload['tier']`` 的值，
                # ``{"tier":"B","head_sha":…}`` 缺 reason 的事件照样 PASS。档位证据按
                # :data:`REQUIRED_KEYS` 的三键（tier/reason/head_sha）齐才算齐 ——
                # 缺键 = 缺证据，不因为「head 对得上」放过。判在 head 之前：缺键比
                # 「绑错 head」更接近「没有这条证据」（与 VERDICT_PRIORITY 顺序一致）。
                state = SLOT_MISSING
                missing.append(name)
                problems[VERDICT_MISSING].append(
                    f"tier：档位证据缺键 {'/'.join(absent)}"
                    f"（三键必须齐：{'/'.join(REQUIRED_KEYS['tier'])}）"
                )
            elif event.payload.get("head_sha") != head:
                state = SLOT_HEAD_MISMATCH
                problems[VERDICT_HEAD_MISMATCH].append(
                    f"{name}：{_short(event.payload.get('head_sha'))} != {_short(head)}"
                )
            elif name == "tier" and not _tier_is_legal(event.payload.get("tier")):
                # tier 槽的档位是「本 PR 走哪一档」的唯一依据：缺键 / 非字符串 /
                # 取值域外的值一律按「缺证据」处理，绝不因为「head 对得上」就 PASS
                # （P2-1 fail-open：追加一条只带 head_sha 的 tier 事件曾能翻成 PASS）。
                state = SLOT_MISSING
                missing.append(name)
                problems[VERDICT_MISSING].append(
                    f"tier：档位证据的 tier 缺键或不在 {'/'.join(sorted(TIERS))} 之一"
                    f"（实际 {event.payload.get('tier')!r}）"
                )
            elif name == "tier" and tier_required is not None and (
                event.payload.get("tier") != tier_required
            ):
                state = SLOT_MISSING
                missing.append(name)
                problems[VERDICT_MISSING].append(
                    f"tier：要求 {tier_required}，证据是 {event.payload.get('tier')!r}"
                )
            elif src_kind == "review" and not _verdict_is_legal(
                event.payload.get("verdict")
            ):
                # 返工 t_39c86400（P1）：review 槽原先只比 ``== "block"``，于是缺
                # ``verdict`` 键、写 ``BLOCK``、写 ``Block`` 全被读成通过。取值域外
                # 一律按「缺证据」处理，只有 ``pass`` 才算通过、``block`` 才算阻塞。
                state = SLOT_MISSING
                missing.append(name)
                problems[VERDICT_MISSING].append(
                    f"{name}：verdict 缺键或不在 "
                    f"{'/'.join(sorted(REVIEW_VERDICTS))} 之一"
                    f"（实际 {event.payload.get('verdict')!r}）"
                )
            elif src_kind == "review" and event.payload.get("verdict") == "block":
                state = SLOT_BLOCKED
                problems[VERDICT_BLOCKED].append(f"{name}：最新一条 verdict=block")
        slots.append(Slot(name=name, event=event, state=state))

    tier_event = slots[0].event
    if tier_event is not None and tier_event.payload.get("tier") == "C":
        # 「必须有 boss_approve」这半与 PR 无关：没有 boss 一律缺老板点头；
        # 只有「boss 存在，要比 pr 是否相等」那半才依赖 pr（pr 缺省时比不了，跳过）。
        boss = _latest(ordered, "boss_approve", None)
        if boss is None:
            where = "" if pr is None else f"（pr #{pr}）"
            problems[VERDICT_NO_BOSS].append(f"tier=C 但无 boss_approve 事件{where}")
        elif pr is not None and boss.payload.get("pr") != pr:
            problems[VERDICT_NO_BOSS].append(
                f"tier=C 但 boss_approve pr={boss.payload.get('pr')!r} != {pr}"
            )

    impl = _latest(ordered, "impl_done", None)
    if pr is not None and impl is not None and impl.payload.get("pr") != pr:
        problems[VERDICT_CROSS_CARD].append(
            f"impl_done pr={impl.payload.get('pr')!r} != 被合并 PR {pr}"
        )

    verdict = VERDICT_PASS
    for candidate in VERDICT_PRIORITY:
        if problems[candidate]:
            verdict = candidate
            break

    if verdict == VERDICT_PASS:
        detail = f"四槽齐且绑定 head {_short(head)}" + ("" if pr is None else f"（pr #{pr}）")
    else:
        lines = [
            f"{candidate}：{line}"
            for candidate in VERDICT_PRIORITY
            for line in problems[candidate]
        ]
        detail = "；".join(lines)

    return Judgment(
        verdict=verdict,
        slots=tuple(slots),
        missing=tuple(missing),
        detail=detail,
    )


# ---- E3：取数（唯一 SQL）与报告渲染（本段只追加，不改上面 E1 的 judge / 常量）----

#: 卡号的唯一形态：``t_<8 位小写十六进制>``（整串 fullmatch；``t_x`` / 大写 / ``wt/t_…`` 都不算）。
#: 与 :data:`BRANCH_RE` 同口径，E1 段没有这条常量（``card_from_branch`` 只从分支名取），
#: 故在 E3 段追加，**不改** BRANCH_RE 本身的文本。
CARD_RE = re.compile(r"^t_[0-9a-f]{8}$", re.ASCII)

#: 非必需槽位（有就额外各渲一行，**不参与** rc / verdict）；顺序即报告行顺序
EXTRA_SLOTS: tuple[str, ...] = ("spec_sign", "impl_done", "boss_approve")

#: 槽位状态 → 报告里的判定词（报告只用 VERDICT_* 七个字面量，不另造词）
SLOT_WORDS: dict[str, str] = {
    SLOT_OK: VERDICT_PASS,
    SLOT_MISSING: VERDICT_MISSING,
    SLOT_HEAD_MISMATCH: VERDICT_HEAD_MISMATCH,
    SLOT_BLOCKED: VERDICT_BLOCKED,
    SLOT_ROLE_MISMATCH: VERDICT_ROLE_MISMATCH,
}

#: **唯一**取数 SQL（``evidence audit`` 与后续 E4 的证据闸门都调 :func:`collect`，
#: 不许另写第二份）。``LEFT JOIN task_runs`` 取 profile：判角色只信 run 身份，
#: 不看 ``tasks.assignee``（契约 v1 / R8）。
EVENT_SQL = """
SELECT e.id, e.kind, e.payload, e.run_id, e.created_at, r.profile
  FROM task_events e
  LEFT JOIN task_runs r ON r.id = e.run_id
 WHERE e.task_id = ?
 ORDER BY e.id
"""


class EvidenceReadError(Exception):
    """取数失败（库不可用 / 没有 ``task_events`` 或 ``task_runs`` 表 / 列缺失）。

    ``cli`` 一律映射成 rc2（参数或环境错误），不混进「判定不通过」的 rc1。
    """


class Collection(tuple):
    """``collect`` 的返回值：``tuple[Event, ...]`` **加上** ``解析失败`` 计数。

    契约把 ``collect`` 的返回类型钉成 ``tuple[Event, ...]``，而报告还要报
    ``解析失败`` 的条数（payload 不是合法 JSON 对象的事件被丢弃）。用 ``tuple``
    子类同时满足两边：``isinstance(collect(...), tuple)`` 为真，``len`` / 迭代 /
    与普通 tuple 的相等比较都不变，计数挂在 :attr:`parse_failures` 上 ——
    **不为拿计数再查一遍库**（那就成了第二份 SQL）。
    """

    parse_failures: int
    #: 解析失败的事件 id（与 :attr:`parse_failures` 同序；报告要逐条点名）
    parse_failure_ids: tuple[int, ...]

    def __new__(
        cls,
        events: Sequence[Event] = (),
        parse_failures: int = 0,
        parse_failure_ids: Sequence[int] = (),
    ) -> Collection:
        self = super().__new__(cls, tuple(events))
        self.parse_failures = parse_failures
        self.parse_failure_ids = tuple(parse_failure_ids)
        return self


def _event_from_row(row: Sequence[Any]) -> Event | None:
    """``task_events`` 的一行 → :class:`Event`；kind 不在 :data:`KINDS` 里返回 ``None``。

    payload 不是合法 JSON 对象时抛 ``ValueError`` / ``TypeError``，由 :func:`collect`
    记进 ``解析失败`` 计数 —— 契约要求「不许冒异常」。
    """
    event_id, kind, raw_payload, run_id, created_at, profile = row
    if kind not in KINDS:
        return None  # 非 evidence 事件（牌坊事件、评论事件等）直接跳过，不算解析失败
    payload = json.loads(raw_payload)
    if not isinstance(payload, Mapping):
        raise ValueError(f"payload 不是 JSON 对象：{type(payload).__name__}")
    return Event(
        id=int(event_id),
        kind=kind,
        payload=payload,
        run_id=None if run_id is None else int(run_id),
        profile=profile,
        created_at=0 if created_at is None else int(created_at),
    )


def collect(conn: Any, card: str) -> Collection:
    """一张卡的全部 evidence 事件（只读；连接由调用方 :func:`team_board.db.connect` 开）。

    ``conn`` 是 ``sqlite3.Connection``（本模块零 IO 契约不许 import sqlite3，故标
    ``Any``）。读失败一律 :class:`EvidenceReadError`：**这里不 import sqlite3**，
    所以按「任何异常都是取数失败」处理，异常原文进文案（排障要看得到 ``no such
    table``）。非法 JSON 的事件丢弃并计数，绝不因一行脏数据让整次稽核冒异常。
    """
    try:
        rows = conn.execute(EVENT_SQL, (card,)).fetchall()
    except Exception as exc:  # 零 IO 契约禁 import sqlite3：任何读失败都算取数失败
        raise EvidenceReadError(f"读取卡 {card} 的 evidence 事件失败：{exc}") from exc

    events: list[Event] = []
    failures = 0
    failure_ids: list[int] = []
    for row in rows:
        try:
            event = _event_from_row(row)
        except (TypeError, ValueError, IndexError):
            failures += 1
            try:
                failure_ids.append(int(row[0]))
            except (TypeError, ValueError, IndexError):
                failure_ids.append(-1)  # 连 id 都读不出来：报告里点 `-1`
            continue
        if event is not None:
            events.append(event)
    return Collection(events, failures, failure_ids)


def _slot_line(name: str, state: str, event: Event | None) -> str:
    """报告里一行槽位：``<槽位> | <判定词> | <事件 id> | <head 前 12 位> | <写入 profile>``。"""
    word = SLOT_WORDS.get(state, state)
    if event is None:
        return f"{name} | {word} | - | - | -"
    head_sha = event.payload.get("head_sha")
    shown = head_sha[:12] if isinstance(head_sha, str) else "-"
    profile = "-" if event.profile is None else event.profile
    return f"{name} | {word} | {event.id} | {shown} | {profile}"


def _extra_state(event: Event, head: str) -> str:
    """非必需槽位的状态：只看角色与 head（不参与 verdict，故不判「缺」「阻塞」）。"""
    allowed = ROLE_RULES[event.kind]
    if event.run_id is None or event.profile is None or event.profile not in allowed:
        return SLOT_ROLE_MISMATCH
    head_sha = event.payload.get("head_sha")
    if head_sha is not None and head_sha != head:
        return SLOT_HEAD_MISMATCH
    return SLOT_OK


def _problem_lines(judgment: Judgment) -> list[str]:
    """判成非 PASS 时的结论行（放在报告最前，**首行**就是 verdict 本身）。

    首行刻意写成 ``<verdict>：<缺失槽位逗号串>``：验收标准 2 要求缺 qa_pass 时
    首行**逐字**是 ``缺证据：qa_pass``，而 judge 的 ``detail`` 会把原因也拼进去
    （``缺证据：qa_pass：无 qa_pass 事件``），所以原因另起一行。其余 verdict 的
    首行仍是 ``detail`` 第一段（含 verdict 字面量）。
    """
    if judgment.verdict == VERDICT_MISSING and judgment.missing:
        lines = [f"{VERDICT_MISSING}：{','.join(judgment.missing)}"]
    else:
        lines = []
    for part in _detail_lines(judgment.detail):
        if part not in lines:
            lines.append(part)
    return lines


def _detail_lines(detail: str) -> list[str]:
    """把 ``Judgment.detail``（judge 用「；」拼的）切成逐条问题行（去空白、丢空段）。"""
    return [part.strip() for part in detail.split("；") if part.strip()]


def render(
    judgment: Judgment,
    card: str,
    head: str,
    *,
    parse_failures: int = 0,
    events: Sequence[Event] = (),
    parse_failure_ids: Sequence[int] = (),
) -> list[str]:
    """把一次判定渲染成 ``evidence audit`` 的整块报告（纯函数，不 print）。

    行序：``解析失败`` 结论行（仅当 >0，逐条点名坏事件 id）→ 非 PASS 时出结论行 →
    表头 ``evidence audit card=<卡> head=<sha>
    verdict=<判定>`` → 四个必需槽位各一行 → 非必需槽位（有才出）各一行 →
    ``review kind 域外`` 点名行（有才出）→ 末行 ``missing: <逗号分隔槽位名>`` / ``missing: -``。

    ``parse_failures`` / ``events`` / ``parse_failure_ids`` 是**可选**扩展位（默认值即
    契约里的三参数形态）：前者只为把 :func:`collect` 的计数与坏事件 id 带进报告，
    后者只为渲出非必需槽位的行 —— ``judge`` 不动（E1 段一行未改）。
    """
    lines: list[str] = []
    if parse_failures:
        ids = ",".join(str(event_id) for event_id in parse_failure_ids) or "-"
        lines.append(
            f"解析失败 {parse_failures} 条（坏事件 id: {ids}）；无法确认槽位的最新证据"
        )
    if not judgment.ok:
        lines.extend(_problem_lines(judgment))
    lines.append(f"evidence audit card={card} head={head} verdict={judgment.verdict}")
    for slot in judgment.slots:
        lines.append(_slot_line(slot.name, slot.state, slot.event))
    ordered = tuple(events)
    for name in EXTRA_SLOTS:
        event = _latest(ordered, name, None)
        if event is not None:
            lines.append(_slot_line(name, _extra_state(event, head), event))
    for event in ordered:
        # 返工 t_39c86400（P3）：``kind`` 不在 REVIEW_KINDS 的 review 事件被 ``_latest``
        # 静默滤掉（槽位成了「无 review 事件」）。报告必须点名这条事件的 id，否则
        # 「kind 写错」与「根本没写」在报告上一模一样。
        if event.kind == "review" and not _review_kind_is_legal(event.payload.get("kind")):
            lines.append(
                f"review kind 域外: 事件 id={event.id} kind={event.payload.get('kind')!r}"
                f"（不在 {'/'.join(sorted(REVIEW_KINDS))} 之一，已丢弃）"
            )
    lines.append(
        "missing: " + (",".join(judgment.missing) if judgment.missing else "-")
    )
    return lines

