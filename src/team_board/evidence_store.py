"""``evidence emit`` 的写入层：身份解析、角色硬校验、追加**一条** ``task_events``。

契约 v1 · E2 段的唯一实现（``src/team_board/cli.py`` 只做参数解析与文案渲染）。
四条边界，代码里逐条可查：

1. **身份只从看板库取**：``tasks.current_run_id`` → ``task_runs(id).profile``，且该
   run 的 ``status`` 必须是 ``running``。``HERMES_PROFILE`` 之类「调用方自报」的
   profile 一律不读 —— 环境变量只用来定位**卡号**（``HERMES_KANBAN_TASK``，其次
   ``HERMES_KANBAN_WORKSPACE`` 的 basename）；
2. **只追加一行**：库里唯一的写语句是 :data:`INSERT_SQL`（``INSERT INTO
   task_events(...)``）—— 不 CREATE TABLE、不 UPDATE、不 DELETE、不碰 ``tasks``；
3. **校验一律调契约层**：payload 过 :func:`team_board.evidence.validate_payload`，
   落库存 :func:`team_board.evidence.normalize` 的结果；kind 的允许角色取
   :data:`team_board.evidence.ROLE_RULES`，本模块**不另立第二份** kind / 角色表；
4. **两种失败分两码**：:class:`EvidenceStoreError` = 参数形态错或库不可用 → rc2，
   CLI 渲染 ``错误: …``；:class:`EvidenceReject`（它的子类）= 判定不通过 → rc1，
   文案逐字打印、不加前缀（契约 C 的「拒绝写入：…」与 B/E 的引句都是这一种）。

``EvidenceReject`` 是 ``EvidenceStoreError`` 的子类，是刻意的：契约 H 只说
「evidence_store 抛 EvidenceStoreError(message)，cli 层不许冒 traceback」，没规定
一个类还是两个。子类让「一个 except 兜住全部、按类型分码」两件事同时成立，
调用方仍可只写 ``except evidence_store.EvidenceStoreError``。

契约没逐字规定、这里取保守一侧的三处（后续卡要改按契约流程改，别在这里悄悄放宽）：

- ``qa_pass`` 的 ``payload.run_id`` **不**与调用者 run 做等式校验：契约 E2 的 D 段
  只要求「payload 过 validate_payload、入库存 normalize 结果」。真实路径下两者
  本来就同值（emit 的人就是那个 run），多一条等式校验属于本卡范围外的新规则；
- ``task_runs`` 里查不到 ``current_run_id`` 指向的行 → 判 rc1（数据不一致让调用方
  拒绝写入，不当作「参数错」）；``task_runs`` / ``tasks`` / ``task_events`` 三张表
  缺任何一张 → rc2（库不可用）；
- ``spec_sign`` 的「验收标准段」判据只看**顶格行首** ``## 验收标准``：段界（下一个
  ``## `` 或文末）在本卡没有可读内容，等 E5 需要段内文本时再落地。

两条编码边界是 qa run 549 打回来的硬要求（都在本模块收口，CLI 不重写判据）：

- **``--payload`` 必须是 UTF-8 文本**：非 UTF-8 字节经 argv 进来时是孤立代理字符
  （``\\udc80-\\udcff``，如 ``LC_ALL=C`` / GBK 文件喂参），落库时才炸会变成
  ``UnicodeEncodeError`` → 未捕获 → **rc1 + traceback**，被自动化读成「证据不通过」。
  所以在 :func:`parse_payload`（参数文本）与 :func:`emit`（``normalize`` 后的落库文本，
  覆盖 ``"\\ud800"`` 这类 JSON 转义）两处各判一次 UTF-8 可编码性 → rc2、零写入；
  :func:`insert_event` 另兜一层 ``UnicodeError`` 当保险，绝不冒 traceback；
- **摘要先写得出、再提交**：stdout 编码表示不了摘要（``PYTHONIOENCODING=ascii``）时
  旧版在 ``INSERT`` 之后才发现 → 返回 rc2 却已落库，调用方按「rc2 = 没写」重试即重复。
  现在 :func:`emit` 在事务里先交给 ``before_commit`` 回调写摘要、再 ``commit``：
  回调抛异常就 ``rollback`` → rc2 时**一行都不多**。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import evidence
from .model import DEFAULT_DB

#: 卡号形态：``t_`` + 8 位小写十六进制（与 ``evidence.BRANCH_RE`` 的捕获组同口径）
CARD_RE = re.compile(r"^t_[0-9a-f]{8}$", re.ASCII)

#: ``spec_sign`` 要求的验收标准段锚点（必须顶格在行首）
SPEC_SECTION = "## 验收标准"

#: 库里唯一的写语句：只追加 ``task_events`` 一行，别的一概不写
INSERT_SQL = (  # nosec B608 - 常量字面量，无外部输入，不是注入面
    "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES(?, ?, ?, ?, ?)"
)

#: 写入口用到的三张表（缺任何一张 = 库不是看板库 → rc2）
REQUIRED_TABLES: tuple[str, ...] = ("tasks", "task_runs", "task_events")


class EvidenceStoreError(Exception):
    """参数形态错或库不可用 → CLI 渲染 ``错误: <message>`` + rc2。"""


class EvidenceReject(EvidenceStoreError):
    """判定不通过（角色 / 没有进行中的 run / payload 违约 / 缺验收标准段 / kind 不可 emit）。

    CLI 把 ``str(exc)`` **逐字**打到 stderr 一行、退出码 1 —— 不加 ``错误: `` 前缀，
    因为契约 C / F 的文案本身已是完整一句。
    """


@dataclass(frozen=True)
class Identity:
    """一次 emit 的调用者身份（全部取自看板库，不取 argv / env 自报）。"""

    card: str
    run_id: int
    profile: str
    status: str


@dataclass(frozen=True)
class EmitResult:
    """一次成功 emit 的结果（CLI 只按 :meth:`render` 打一行摘要）。"""

    identity: Identity
    kind: str
    event_id: int
    payload: str

    def render(self) -> str:
        return render_summary(self.identity, self.kind, self.event_id, self.payload)


def render_summary(identity: Identity, kind: str, event_id: int, payload_text: str) -> str:
    """摘要行的唯一拼法（``emit`` 的 ``before_commit`` 与 :meth:`EmitResult.render` 同一份）。

    ``payload`` 打的是**落库文本**（``evidence.normalize`` 的结果），不是 ``--payload``
    原文 —— 两者在键序 / 空白上可能不同，摘要只认前者。
    """

    return (
        f"evidence emit: kind={kind} card={identity.card} "
        f"run={identity.run_id} profile={identity.profile} "
        f"event={event_id} payload={payload_text}"
    )


# ---------------------------------------------------------------- 身份解析


def card_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """环境兜底卡号：``HERMES_KANBAN_TASK``，其次 ``HERMES_KANBAN_WORKSPACE`` 的 basename。

    两者都没有、或 workspace 的 basename 不是 ``t_[0-9a-f]{8}`` → ``None``（调用方据此
    判 rc2）。注意 ``HERMES_KANBAN_TASK`` 只做「有值就用」—— 形态非法由
    :func:`resolve_identity` 统一判 rc2，不在这里猜。
    """

    env = os.environ if env is None else env
    task = (env.get("HERMES_KANBAN_TASK") or "").strip()
    if task:
        return task
    workspace = (env.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if not workspace:
        return None
    base = Path(workspace).name
    return base if CARD_RE.fullmatch(base) else None


def resolve_identity(
    conn: sqlite3.Connection,
    card: str | None,
    env: Mapping[str, str] | None = None,
) -> Identity:
    """按契约 B 的五步解析调用者身份（不写库、不读 ``HERMES_PROFILE``）。

    1) 卡号定位：显式 ``card`` 优先，否则 env 兜底；都没有 → rc2；
    2) 显式卡号与 env 兜底值不相等 → rc1（防止拿着 A 卡的身份给 B 卡写证据）；
    3) ``tasks.current_run_id``：无该卡 → rc2；为 NULL → rc1；
    4) ``task_runs`` 里该 run 的 ``status`` 必须 ``running``，否则 rc1；
    5) profile 为空 → rc1。
    """

    env_card = card_from_env(env)
    target = card if card is not None else env_card
    if target is None:
        raise EvidenceStoreError(
            "无法确定调用者卡号：--card 未给，HERMES_KANBAN_TASK 与 "
            "HERMES_KANBAN_WORKSPACE 也没有给出 t_[0-9a-f]{8} 形态的卡号"
        )
    if not CARD_RE.fullmatch(target):
        raise EvidenceStoreError(
            f"卡号形态非法：{target!r}（应为 t_ 加 8 位小写十六进制，例 t_0123abcd）"
        )
    if card is not None and env_card is not None and card != env_card:
        raise EvidenceReject(f"--card 与调用者卡号不一致：--card={card}，调用者={env_card}")

    row = _fetchone(
        conn, "SELECT current_run_id FROM tasks WHERE id = ?", (target,), what="tasks"
    )
    if row is None:
        raise EvidenceStoreError(f"看板库里没有这张卡：{target}")
    run_id = row[0]
    if run_id is None:
        raise EvidenceReject(f"该卡没有进行中的 run：{target}")
    if isinstance(run_id, bool) or not isinstance(run_id, int):
        raise EvidenceReject(f"该卡的 current_run_id 不是整数：{target} → {run_id!r}")

    run = _fetchone(
        conn, "SELECT profile, status FROM task_runs WHERE id = ?", (run_id,), what="task_runs"
    )
    if run is None:
        raise EvidenceReject(f"当前 run 不在 task_runs 里：{target} → run {run_id}")
    profile, status = run
    if status != "running":
        raise EvidenceReject(f"当前 run 不是 running：run {run_id} 的 status 是 {status!r}")
    if not profile:
        raise EvidenceReject(f"当前 run 没有 profile：run {run_id}")
    return Identity(card=target, run_id=run_id, profile=profile, status=status)


# ---------------------------------------------------------------- 校验


def check_kind(kind: str) -> None:
    """kind 必须可 emit：``merged`` 与任何未知 kind 一律 rc1（不落库）。"""

    if kind in evidence.EMITTABLE_KINDS:
        return
    if kind == "merged":
        raise EvidenceReject(
            "拒绝写入：kind=merged 由 merge_audit trailer 承载，不许 emit 成 evidence 事件"
        )
    raise EvidenceReject(
        f"拒绝写入：kind={kind!r} 不在可 emit 的 kind 里（"
        f"{'/'.join(evidence.EMITTABLE_KINDS)}）"
    )


def ensure_utf8(text: str, *, where: str) -> None:
    """``text`` 必须能用 UTF-8 编码，否则 rc2（**在任何 INSERT 之前**）。

    ``--payload`` 绕过 shell 时可能是 GBK 字节：``LC_ALL=C`` 下 argv 解码用
    ``surrogateescape``，非 UTF-8 字节变成孤立代理字符（``\\udc80-\\udcff``）；
    JSON 转义 ``"\\ud800"`` 则是另一条路径。两条都到不了 UTF-8 列里，
    早期判掉比让 sqlite3 抛 ``UnicodeEncodeError``（未捕获 = rc1 + traceback）稳。
    """

    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceStoreError(
            f"--payload 不是合法 UTF-8：{where}第 {exc.start} 个字符起是孤立代理字符"
            "（\\ud800-\\udfff），编码不成 UTF-8；请以 UTF-8 文本传 --payload"
        ) from exc


def parse_payload(text: str) -> object:
    """``--payload`` 的唯一解析入口：不是合法 JSON → rc2（一条都不落库）。

    UTF-8 可编码性先判（见 :func:`ensure_utf8`）：非 UTF-8 字节经 argv 进来时
    ``json.loads`` 可能照样成功，落库时才炸 —— 那就从「参数形态错」掉成
    「rc1 + traceback」了。
    """

    ensure_utf8(text, where="参数文本")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise EvidenceStoreError(f"--payload 不是合法 JSON：{exc}") from exc


def check_payload(kind: str, payload: object) -> dict[str, object]:
    """过契约层的 :func:`validate_payload`；违约 → rc1（文案带键名与实际值）。"""

    try:
        return evidence.validate_payload(kind, payload)  # type: ignore[arg-type]
    except ValueError as exc:
        raise EvidenceReject(f"payload 违约：{exc}") from exc


def check_role(kind: str, identity: Identity) -> None:
    """角色硬校验：``profile not in ROLE_RULES[kind]`` → rc1，**返回前没有任何 INSERT**。"""

    allowed = evidence.ROLE_RULES[kind]
    if identity.profile in allowed:
        return
    raise EvidenceReject(
        f"拒绝写入：kind={kind} 只允许 {'/'.join(sorted(allowed))}，"
        f"调用者 run {identity.run_id} 的 profile 是 {identity.profile}"
    )


def check_run_id(claimed: int | None, identity: Identity) -> None:
    """``--run-id`` 与库里的身份 run 对账：不等 → rc1（「run 不属于本卡」那一档）。

    只核对、不采信：身份仍然只来自 ``tasks.current_run_id`` → ``task_runs.profile``。
    ``None`` = 调用方没声称，直接放过。
    """

    if claimed is None:
        return
    if claimed == identity.run_id:
        return
    raise EvidenceReject(
        f"run 不属于本卡：--run-id={claimed}，卡 {identity.card} 当前 running run 是 "
        f"{identity.run_id}（身份只取自看板库）"
    )


def check_spec_section(body: object) -> None:
    """``spec_sign`` 额外校验：卡正文必须有**顶格行首**的 ``## 验收标准`` 段。"""

    text = body if isinstance(body, str) else ""
    for line in text.splitlines():
        if line.startswith(SPEC_SECTION):
            return
    raise EvidenceReject(
        f"卡正文缺验收标准段：正文里必须有顶格一行 {SPEC_SECTION}（段界＝下一个 ## 或文末）"
    )


def load_body(conn: sqlite3.Connection, card: str) -> object:
    """读该卡的 ``tasks.body``（只读；供 :func:`check_spec_section` 用）。"""

    row = _fetchone(conn, "SELECT body FROM tasks WHERE id = ?", (card,), what="tasks")
    return None if row is None else row[0]


# ---------------------------------------------------------------- 库与写入


def open_rw(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """读写打开看板库（``mode=rw``）：文件不存在 / 不是合法库 / 缺表 → rc2。

    绝不 ``CREATE TABLE``：缺 ``tasks`` / ``task_runs`` / ``task_events`` 一律报错退出，
    这样「拿一个空文件当库」不会被写成半张表。
    """

    path = Path(db_path)
    if not path.exists():
        raise EvidenceStoreError(f"数据源不存在：{path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise EvidenceStoreError(f"打不开数据源 {path}：{exc}") from exc
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        missing = [name for name in REQUIRED_TABLES if not _has_table(conn, name)]
    except sqlite3.Error as exc:
        conn.close()
        raise EvidenceStoreError(f"不是合法的 SQLite 库 {path}：{exc}") from exc
    if missing:
        conn.close()
        raise EvidenceStoreError(f"看板库里缺表 {'/'.join(missing)}（{path}）")
    return conn


def insert_event(
    conn: sqlite3.Connection,
    identity: Identity,
    kind: str,
    payload_text: str,
    *,
    now: int | None = None,
) -> int:
    """追加一行 ``task_events``（**不 commit**：事务归 :func:`emit` 收），返回 event id。

    入参是 :func:`team_board.evidence.normalize` 的结果 —— 序列化口径只有那一处，
    不许在 CLI 里另写一种。``UnicodeError`` 兜在这里当保险：真出现编码不了的文本
    （旧版就在这一行冒 traceback、被读成 rc1）→ rc2，且事务回滚后库里一行都不多。
    """

    stamp = int(time.time()) if now is None else int(now)
    try:
        cursor = conn.execute(
            INSERT_SQL, (identity.card, identity.run_id, kind, payload_text, stamp)
        )
    except (sqlite3.Error, UnicodeError) as exc:
        conn.rollback()
        raise EvidenceStoreError(f"写 task_events 失败：{exc}") from exc
    return int(cursor.lastrowid or 0)


def emit(
    kind: str,
    payload_json: str,
    *,
    card: str | None = None,
    db_path: str | Path = DEFAULT_DB,
    env: Mapping[str, str] | None = None,
    now: int | None = None,
    run_id: int | None = None,
    before_commit: Callable[[str], None] | None = None,
) -> EmitResult:
    """``evidence emit`` 的全部流程：参数 → 身份 → 校验 → 只追加一行。

    顺序是刻意的：先判 kind（``merged`` 之类的「根本不许 emit」不碰库），再判
    payload 形态与取值（rc2 / rc1），然后才解析身份与角色 —— 角色不符时库里一行
    都不会多。任何一步失败都不提交事务。

    ``run_id`` 是调用方**声称**的自己的 run 号（看板卡的 ``--run-id``）：本函数不拿它
    当身份（身份永远取自库里），只做一致性核对 —— 与 ``tasks.current_run_id`` 解析
    出来的 run 不等 → :class:`EvidenceReject`（rc1，卡面「run 不属于本卡」那一档）。
    ``None`` = 调用方没声称，不核对。

    ``before_commit`` 是「rc2 与落库不并存」的机制：``INSERT`` 之后、``commit``
    之前把它叫起来，传入**已经渲染好**的摘要行（见 :func:`render_summary`）；回调
    抛任何异常就 ``rollback`` 再上抛 —— CLI 的 stdout 写不出去（``PYTHONIOENCODING=ascii``
    表示不了中文、``ENOSPC`` 一类）因此不会留下「说没写、其实写了」的行。
    唯一的残差是回调成功而 ``commit`` 自己失败（摘要已打给人看、库里没有行）。
    """

    check_kind(kind)
    raw = parse_payload(payload_json)
    payload = check_payload(kind, raw)
    text = evidence.normalize(payload)
    ensure_utf8(text, where="落库文本")

    conn = open_rw(db_path)
    try:
        identity = resolve_identity(conn, card, env)
        check_run_id(run_id, identity)
        check_role(kind, identity)
        if kind == "spec_sign":
            check_spec_section(load_body(conn, identity.card))
        event_id = insert_event(conn, identity, kind, text, now=now)
        result = EmitResult(identity=identity, kind=kind, event_id=event_id, payload=text)
        if before_commit is not None:
            try:
                before_commit(result.render() + "\n")
            except BaseException:
                conn.rollback()
                raise
        try:
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise EvidenceStoreError(f"提交 task_events 失败：{exc}") from exc
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------- 内部


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return bool(row and row[0])


def _fetchone(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...], *, what: str
) -> tuple | None:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.Error as exc:
        raise EvidenceStoreError(f"读取 {what} 失败：{exc}") from exc
