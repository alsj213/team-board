"""证据闸门拒绝后「按缺失槽位建对应角色卡」：纯计划函数 + 子进程适配（E5）。

本模块只做两件事，判定一律留在 :mod:`team_board.evidence`：

1. :func:`plan` —— 纯函数：把一个非 ``PASS`` 的 :class:`team_board.evidence.Judgment`
   翻成若干 :class:`CardSpec`（每个缺失槽位 / 每条异常一张卡），不做任何 IO；
2. :class:`CardCreator` —— 适配层：按契约把每条 :class:`CardSpec` 落成
   ``hermes kanban --board devteam create …`` 子进程（body 写临时文件、用幂等 key 去重），
   **不直写 sqlite**，也不建「合并卡 / 收口卡」（那两张由 repo 侧建）。

槽位 → 角色是写死的唯一一份 :data:`SLOT_OWNER`，不做成参数：谁补哪种证据是契约的
一部分，允许调用方传就会长出第二套口径。

三处实现口径（契约未逐字规定，取保守一侧，改动前先问 pm）：

- **``impl_done`` 怎么判缺**：``evidence.judge`` 的 :attr:`Judgment.slots` 只有四个必需
  槽位（``tier`` / ``qa_pass`` / ``review:defect`` / ``review:regression``），
  ``impl_done`` / ``boss_approve`` 不在其中。所以 ``plan`` 的可选关键字
  ``events`` 是「补 ``impl_done``」的唯一信息来源：给了 events 才判它缺不缺（缺 → engineer
  卡），不给（``None``）就不判 —— 现状下 merge_guard 的闸门手里就有 events，会传；
  单测里想验这一条也是显式传空 tuple。``boss_approve`` 不靠 events：``tier=C`` 缺它时
  judge 直接给「缺老板点头」，按 verdict 出卡即可。
- **``head-mismatch`` 与 ``missing`` 同归属**：证据存在但绑在旧 head 上，要补的是**同槽位
  在新 head 上的事件**，所以出卡的角色与标题跟「缺证据」一样，只在「要做什么」里点明
  「需重发」。不新造第三个角色。
- **幂等 key 里的槽位逐字用证据槽位名**（含 ``review:defect`` 的冒号）：契约写死
  ``evidence-<实现卡号>-<槽位>-<head 前 12 位>``，冒号在 argv 里无需转义，不做二次改名 ——
  一个槽位只许有一个 key 形态。
"""

from __future__ import annotations

import subprocess  # nosec B404 - 只为起 hermes kanban create 子进程
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from team_board import evidence

#: 建卡落哪个板（契约写死，不从环境读：环境变量能让「建到哪个板」悄悄改口）
BOARD = "devteam"

#: 卡正文 UTF-8 字节上限（与内核 ``tasks.body`` 的硬上限同一数字，R16）
BODY_LIMIT_BYTES = 2048

#: 「要做什么」一节超限时截断到多少字符（截断只动这一节，结构不动）
DO_LINE_MAX_CHARS = 200

#: engineer 卡的验收标准段：段头与段内首行都是内核建卡闸门的硬要求（缺了子进程 rc2）
QA_SECTION = "## 验收标准"
QA_SPEC_LINE = "QA-SPEC: qa"

#: 标题里不许出现的四种触发词（会让人以为这是合并卡 / 收口卡）
TITLE_BANNED: tuple[str, ...] = ("合并卡", "收口卡", "合并 PR", "收口：")

#: 守卫兜底的计划文件落哪个目录（相对 CWD；与 gate 日志同目录）
PLAN_DIR = "evidence"

#: 槽位 → 角色（**唯一**一份；kind 级角色矩阵在 :data:`team_board.evidence.ROLE_RULES`）
SLOT_OWNER: dict[str, str] = {
    "qa_pass": "qa",  # nosec B105 - 槽位名 / profile 名不是口令
    "review:defect": "qa",
    "review:regression": "qa",
    "tier": "pm",
    "boss_approve": "pm",
    "impl_done": "engineer",
}

#: 槽位 → （标题前缀，事件 kind）
SLOT_TITLE: dict[str, str] = {
    "qa_pass": "补 qa_pass 证据",  # nosec B105 - 标题文案不是口令
    "review:defect": "补缺陷视角双问",
    "review:regression": "补回归视角双问",
    "tier": "补档位判定",
    "boss_approve": "C 档待老板点头",
    "impl_done": "补 impl_done 事件",
}

SLOT_EVENT_KIND: dict[str, str] = {
    "qa_pass": "qa_pass",  # nosec B105 - 事件 kind 不是口令
    "review:defect": "review",
    "review:regression": "review",
    "tier": "tier",
    "boss_approve": "boss_approve",
    "impl_done": "impl_done",
}

#: 「证据异常核查」卡的统一标题前缀（证据串卡 / 角色不符）
ANOMALY_TITLE = "证据异常核查"

#: 双问阻塞 → 返工卡的标题前缀（契约给的是「返工：<卡> 双问阻塞 @ <head12>」，故单独拼）
REWORK_TITLE = "返工"

#: 守卫拦下的判据（stderr 命中任一即走兜底；契约写「首行含」，实现按整段判：
#: 守卫文案的行号与顺序会随 hermes 版本变，漏判会把「请转 paimeng」漏成「自动建卡失败」）
GUARD_MARKERS: tuple[str, ...] = ("cannot mutate", "scoped")


@dataclass(frozen=True)
class CardSpec:
    """一张待建的卡（契约形状：title / assignee / body / idempotency_key）。"""

    title: str
    assignee: str
    body: str
    idempotency_key: str

    @property
    def body_bytes(self) -> int:
        """正文的 UTF-8 字节数（R16 的 2048 判据按字节不按字符）。"""
        return len(self.body.encode("utf-8"))


@dataclass(frozen=True)
class CreateOutcome:
    """一次建卡调用的结果。``status`` ∈ ``created`` / ``skipped`` / ``failed`` /
    ``guard-blocked``；``line`` 是要回给调用方（stderr）的那一行。"""

    spec: CardSpec
    status: str
    rc: int
    line: str

    @property
    def ok(self) -> bool:
        """卡已存在（新建成功或被幂等跳过）。"""
        return self.status in {"created", "skipped"}


class Runner(Protocol):
    """子进程适配点：吃 argv、吐一个带 ``returncode`` / ``stderr`` 的对象。

    默认实现是 :func:`subprocess.run`；单测用假 runner 数调用次数、核 argv。
    """

    def __call__(self, argv: Sequence[str]) -> Any: ...


def _short(head: str) -> str:
    """head 的前 12 位（标题 / 幂等 key 里用的形态）。"""
    return head[:12]


#: 「要做什么」一节在模板里的占位符（**不用** ``str.format``：payload 样例里就有 ``{}``，
#: 用 format 会把 JSON 花括号当成字段名炸掉）
DO_SLOT = "\x00action\x00"


def _fit_body(parts: list[str], action: str) -> str:
    """拼正文；超过 :data:`BODY_LIMIT_BYTES` 就截断「要做什么」这一节（结构不动）。

    ``action`` 先按 :data:`DO_LINE_MAX_CHARS` 截一次（契约的截断点），仍然超限就逐字符
    往回收 —— 只会动这一节，首行依据、命令、验收标准段一律保留。
    """

    trimmed = action if len(action) <= DO_LINE_MAX_CHARS else action[:DO_LINE_MAX_CHARS]
    while True:
        text = "\n".join(part.replace(DO_SLOT, trimmed) for part in parts)
        if len(text.encode("utf-8")) <= BODY_LIMIT_BYTES or not trimmed:
            return text
        trimmed = trimmed[: max(0, len(trimmed) - 10)].rstrip()


def _payload_sample(slot: str, head: str, pr: int) -> dict[str, object]:
    """该槽位一条可直接跑通的 ``--payload`` 样例（键与校验口径同 evidence.REQUIRED_KEYS）。"""

    if slot == "qa_pass":
        return {"head_sha": head, "run_id": 1}
    if slot == "review:defect":
        return {"head_sha": head, "kind": "defect", "verdict": "pass"}
    if slot == "review:regression":
        return {"head_sha": head, "kind": "regression", "verdict": "pass"}
    if slot == "tier":
        return {"head_sha": head, "tier": "B", "reason": "档位理由"}
    if slot == "boss_approve":
        return {"pr": pr, "approved_by": "boss"}
    return {"pr": pr, "head_sha": head}


def _action(slot: str, *, state: str, head: str) -> str:
    """「要做什么」一行（``head-mismatch`` 额外点明要重发）。"""

    h = _short(head)
    text = {
        "qa_pass": f"在 head {h} 上跑完验收，写 qa_pass 事件（run_id 填本 run）",
        "review:defect": f"补 head {h} 的缺陷视角双问，写 review(kind=defect) 事件",
        "review:regression": f"补 head {h} 的回归视角双问，写 review(kind=regression) 事件",
        "tier": f"给 head {h} 定档（A/B/C）并写 tier 事件",
        "boss_approve": "C 档需老板点头，写 boss_approve 事件（approved_by=boss）",
        "impl_done": f"补 head {h} 的 impl_done 事件（pr 填被合并 PR 号）",
    }[slot]
    if state == evidence.SLOT_HEAD_MISMATCH:
        return f"{text}；现有事件绑在旧 head 上，需重发"
    return text


def _build(
    *,
    slot: str,
    title: str,
    assignee: str,
    action: str,
    verdict: str,
    impl_card: str,
    pr: int,
    head: str,
) -> CardSpec:
    """按契约 B 段模板拼一张卡（首行依据 + 三行 + engineer 卡的验收标准段）。"""

    kind = SLOT_EVENT_KIND[slot]
    sample = evidence.normalize(_payload_sample(slot, head, pr))
    parts = [
        f"依据：{verdict}（merge_guard 证据闸门，卡 {impl_card}，head {head}，PR #{pr}）",
        "要做什么：" + DO_SLOT,
        f"写事件：PYTHONPATH=src python -m team_board.cli evidence emit "
        f"--kind {kind} --payload '{sample}'",
        '交卡口径：kanban_request_review(reviewer="qa")',
    ]
    if assignee == "engineer":
        parts += [
            "",
            QA_SECTION,
            QA_SPEC_LINE,
            f"1. 上面那条 emit rc0，且绑定 head {_short(head)}",
        ]
    body = _fit_body(parts, action)
    return CardSpec(
        title=title,
        assignee=assignee,
        body=body,
        idempotency_key=f"evidence-{impl_card}-{slot}-{_short(head)}",
    )


def _titled(prefix: str, impl_card: str, head: str) -> str:
    """``<前缀>：<实现卡> @ <head12>``（契约 A 段的标题形态）。"""

    return f"{prefix}：{impl_card} @ {_short(head)}"


def plan(
    judgment: evidence.Judgment,
    impl_card: str,
    pr: int,
    head: str,
    *,
    events: Sequence[evidence.Event] | None = None,
) -> tuple[CardSpec, ...]:
    """``Judgment`` → 待建卡清单（纯函数：不查库、不 print、不碰子进程）。

    - ``PASS`` → ``()``（没有缺失就不该有人被叫起来）；
    - 每个非 ``ok`` 的槽位出一条：``missing`` / ``head-mismatch`` 按 :data:`SLOT_OWNER`
      出卡，``blocked``（双问 verdict=block）出 engineer 的返工卡，``role-mismatch``
      出 pm 的证据异常核查卡；
    - 判成「缺老板点头」时补一条 ``boss_approve`` → pm 的 C 档卡；
    - 判成「证据串卡」时补一条 :data:`ANOMALY_TITLE` → pm；
    - 给了 ``events`` 且其中没有 ``impl_done`` → 补一条 → engineer（见模块 docstring）。

    同一幂等 key 只出一张（槽位与 verdict 分支可能指向同一槽位，重复的按先来的留）。
    """

    if judgment.ok:
        return ()

    specs: list[CardSpec] = []
    seen: set[str] = set()

    def add(slot: str, title: str, assignee: str, action: str) -> None:
        if slot not in SLOT_OWNER:  # pragma: no cover - 槽位名只来自 evidence 常量
            raise ValueError(f"未知槽位：{slot!r}")
        spec = _build(
            slot=slot,
            title=title,
            assignee=assignee,
            action=action,
            verdict=judgment.verdict,
            impl_card=impl_card,
            pr=pr,
            head=head,
        )
        if spec.idempotency_key in seen:
            return
        seen.add(spec.idempotency_key)
        specs.append(spec)

    for slot in judgment.slots:
        if slot.ok:
            continue
        if slot.state == evidence.SLOT_BLOCKED:
            add(
                slot.name,
                f"{REWORK_TITLE}：{impl_card} 双问阻塞 @ {_short(head)}",
                "engineer",
                f"{slot.name} 最新一条 verdict=block：先改再重跑双问，然后写 review 事件"
                "（verdict=pass）",
            )
        elif slot.state == evidence.SLOT_ROLE_MISMATCH:
            add(
                slot.name,
                _titled(ANOMALY_TITLE, impl_card, head),
                "pm",
                f"{slot.name} 事件的写入 profile 不在允许集合（判角色只认 run profile，R8）",
            )
        else:  # missing / head-mismatch：补同槽位在新 head 上的事件
            add(
                slot.name,
                _titled(SLOT_TITLE[slot.name], impl_card, head),
                SLOT_OWNER[slot.name],
                _action(slot.name, state=slot.state, head=head),
            )

    if judgment.verdict == evidence.VERDICT_NO_BOSS:
        add(
            "boss_approve",
            _titled(SLOT_TITLE["boss_approve"], impl_card, head),
            SLOT_OWNER["boss_approve"],
            _action("boss_approve", state=evidence.SLOT_MISSING, head=head),
        )

    if judgment.verdict == evidence.VERDICT_CROSS_CARD:
        add(
            "impl_done",
            _titled(ANOMALY_TITLE, impl_card, head),
            "pm",
            "impl_done 的 pr 与被合并 PR 不等（证据串卡）：按 R8 核对归属后重发",
        )

    if events is not None and not any(event.kind == "impl_done" for event in events):
        add(
            "impl_done",
            _titled(SLOT_TITLE["impl_done"], impl_card, head),
            SLOT_OWNER["impl_done"],
            _action("impl_done", state=evidence.SLOT_MISSING, head=head),
        )

    return tuple(specs)


def render_plan(specs: Iterable[CardSpec], *, note: str = "--no-create-card 干跑，未建任何卡") -> list[str]:
    """计划行（``--no-create-card`` 只打印这个；一行一张卡，能直接照抄人工建卡）。"""

    items = tuple(specs)
    lines = [f"自动建卡计划：{len(items)} 张（{note}）"]
    lines.extend(
        f"{spec.assignee} | {spec.title} | key={spec.idempotency_key}" for spec in items
    )
    return lines


def plan_lines(specs: Iterable[CardSpec], *, reason: str) -> list[str]:
    """守卫兜底写进 ``evidence/<卡号>-<head12>.cards.txt`` 的内容（首行写明原因）。"""

    items = tuple(specs)
    lines = ["# 自动建卡计划", *render_plan(items, note=reason)]
    for spec in items:
        lines.extend(["", f"## {spec.title}", f"assignee: {spec.assignee}", spec.body])
    return lines


def create_argv(spec: CardSpec, body_path: str | Path, *, board: str = BOARD) -> tuple[str, ...]:
    """建卡子进程的 argv（**唯一**形态；不直写 sqlite、不加其它参数）。"""

    return (
        "hermes",
        "kanban",
        "--board",
        board,
        "create",
        spec.title,
        "--assignee",
        spec.assignee,
        "--body-file",
        str(body_path),
        "--idempotency-key",
        spec.idempotency_key,
    )


def _stderr_echo(line: str) -> None:
    """默认回显：一行到 stderr（stdout 在拒绝路径上必须 0 字节）。"""

    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):  # stderr 被关掉也不许让建卡崩
        return


def _default_runner(argv: Sequence[str]) -> Any:
    """真的起子进程：``hermes kanban create``（不 check，rc 交给调用方判）。"""

    return subprocess.run(  # nosec B603 - argv 是常量元组，不经 shell
        tuple(argv), capture_output=True, text=True, check=False
    )


def _guard_blocked(stderr: str) -> bool:
    """stderr 是否命中作用域守卫的判据（:data:`GUARD_MARKERS`，大小写不敏感）。"""

    lowered = stderr.lower()
    return any(marker in lowered for marker in GUARD_MARKERS)


class CardCreator:
    """把 :class:`CardSpec` 落成子进程的适配层（幂等：同一 key 只真调一次）。

    - ``runner``：子进程适配点（默认 :func:`subprocess.run`），单测注入假 runner；
    - ``echo``：回显一行（默认 stderr），单测注入 list.append；
    - ``scratch_dir``：body 临时文件目录（默认系统临时目录；用后即删）；
    - 幂等靠实例内的 ``_done`` 集合：**同一实例**连调两次同一 key，第二次不调子进程
      （``hermes create --idempotency-key`` 是第二道保险，不是这里的替代）。
    """

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        echo: Callable[[str], None] | None = None,
        scratch_dir: str | Path | None = None,
        board: str = BOARD,
        plan_dir: str | Path | None = None,
    ) -> None:
        self._runner: Runner = runner or _default_runner
        self._echo: Callable[[str], None] = echo or _stderr_echo
        self._scratch = None if scratch_dir is None else str(scratch_dir)
        self._board = board
        self._plan_dir = None if plan_dir is None else Path(plan_dir)
        self._done: set[str] = set()

    def ensure(
        self, specs: Iterable[CardSpec], *, impl_card: str, head: str
    ) -> tuple[CreateOutcome, ...]:
        """逐条建卡，返回每条的结果（``skipped`` 表示同一 key 已建过、不发子进程）。"""

        outcomes: list[CreateOutcome] = []
        for spec in specs:
            if spec.idempotency_key in self._done:
                outcomes.append(
                    CreateOutcome(
                        spec,
                        "skipped",
                        0,
                        f"跳过（同一幂等 key 已建）：{spec.idempotency_key}",
                    )
                )
                continue
            outcome = self._create(spec, impl_card=impl_card, head=head)
            if outcome.status == "created":
                self._done.add(spec.idempotency_key)
            outcomes.append(outcome)
            self._echo(outcome.line)
        return tuple(outcomes)

    # ---- 内部 ----

    def _write_body(self, spec: CardSpec) -> Path:
        """body 写临时文件（scratch 下，用后删；``hermes create`` 只认 --body-file 路径）。"""

        with tempfile.NamedTemporaryFile(
            "w",
            suffix=".md",
            prefix="evidence-card-",
            dir=self._scratch,
            encoding="utf-8",
            delete=False,
        ) as handle:
            handle.write(spec.body)
        return Path(handle.name)

    def _create(self, spec: CardSpec, *, impl_card: str, head: str) -> CreateOutcome:
        path = self._write_body(spec)
        try:
            try:
                proc = self._runner(create_argv(spec, path, board=self._board))
            except OSError as exc:
                # hermes 不在 PATH / 起不来：等同「建卡失败」，绝不冒异常出去
                return CreateOutcome(
                    spec, "failed", 127, f"自动建卡失败 rc=127：{exc}"
                )
            raw_rc = getattr(proc, "returncode", 1)
            rc = 1 if raw_rc is None else int(raw_rc)  # 被信号杀掉（None）按失败算
            stderr = str(getattr(proc, "stderr", "") or "")
        finally:
            path.unlink(missing_ok=True)

        if rc == 0:
            return CreateOutcome(
                spec,
                "created",
                0,
                f"已建卡：{spec.title}（assignee={spec.assignee}，key={spec.idempotency_key}）",
            )
        if _guard_blocked(stderr):
            target = self.write_plan_file(
                (spec,), impl_card=impl_card, head=head, reason="被作用域守卫拦下"
            )
            return CreateOutcome(
                spec,
                "guard-blocked",
                rc,
                f"建卡被作用域守卫拦下，计划已落 {target}，请转 paimeng",
            )
        lines = [line for line in stderr.splitlines() if line.strip()]
        last = lines[-1].strip() if lines else "(无 stderr)"
        return CreateOutcome(spec, "failed", rc, f"自动建卡失败 rc={rc}：{last}")

    def write_plan_file(
        self,
        specs: Iterable[CardSpec],
        *,
        impl_card: str,
        head: str,
        reason: str = "自动建卡",
    ) -> Path:
        """计划落 ``evidence/<实现卡>-<head12>.cards.txt``（守卫兜底 / 人工接手用）。"""

        directory = Path(PLAN_DIR) if self._plan_dir is None else self._plan_dir
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{impl_card}-{_short(head)}.cards.txt"
        target.write_text("\n".join(plan_lines(specs, reason=reason)) + "\n", encoding="utf-8")
        return target
