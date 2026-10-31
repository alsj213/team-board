"""PR 认领唯一性（R17）的唯一判据，以及 ``pr-claim-audit`` 的纯函数与渲染。

三种消费者共用这一份实现，不许各写一份：

- ``parse_pr``：``--pr`` 的取值域（ASCII 十进制正整数、不超 :data:`MAX_PR`），
  ``pr-claim-audit`` 与 ``merge-record`` 都调它；
- ``audit``：把 ``db.iter_claim_rows`` 的行判成 :class:`Report`
  （未结卡里恰好一张卡认领该 PR = 唯一认领）；
- ``render``：把 :class:`Report` 渲染成 ``pr-claim-audit`` 的整块文本；
- ``needs_claim`` / ``first_claim_pr`` / ``audit_gaps`` / ``render_gaps``：
  ``pr-claim-audit --gaps`` 的缺口判据（标题命中合并语义、正文首行却不是
  ``PR-CLAIM: #<N>``）。触发谓词与内核建卡闸门逐字相同，**首行判据刻意更严**
  （不 strip、按 ``\\n`` 切），差异集 4 类（前导空白 / 行尾空白 / CRLF / 其它换行符，
  字符级变体含 NBSP、VT、FF、``\\x1c``、U+2028 等）见 ``docs/pr-claim-gate.md`` 第 2 节。

判定口径（照 R17 原文，一个字都不放宽）：

- 认领行 = 卡正文里**顶格**一行 ``PR-CLAIM: #<N>``，行尾不许有尾随字符；
- 引用行 = 顶格一行 ``PR-REF: #<N>``，**只回显、不参与唯一性判定**；
  同一张卡既认领又引用时只算认领（不重复列）；
- 只看 ``status NOT IN ('done', 'archived')`` 的卡（由调用方的 SQL 定）；
- 一张卡认领多次（正文里出现两行一样的 ``PR-CLAIM``）仍算**一张**卡 ——
  重复认领按「卡」数，不按「行」数。

本模块不读文件、不读环境、不连库、不 print —— 全是纯函数，便于单测与被
``merge_guard`` 直接 import（禁止子进程 + 禁止第二份判定逻辑）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

#: 认领行的唯一形态：顶格、行尾无尾随字符（MULTILINE）
CLAIM_RE = re.compile(r"^PR-CLAIM: #([1-9][0-9]*)$", re.MULTILINE)

#: 引用行的唯一形态（同样顶格；只回显，不参与判定）
REF_RE = re.compile(r"^PR-REF: #([1-9][0-9]*)$", re.MULTILINE)

#: 合并语义触发（只看标题前缀；关键词版显著更宽 —— 现板实测见
#: ``docs/pr-claim-gate.md`` 第 3 节，含返工/文档卡误中）
#: 与内核建卡闸门（``hermes_cli/kanban_db.py`` 的 ``_PR_CLAIM_TRIGGER_RE``）
#: **正则文本必须逐字相同**：改动时两边一起改，详见 ``docs/pr-claim-gate.md``。
TRIGGER_RE = re.compile(r"^(?:合并卡|收口卡|合并\s*PR\s*#\s*\d+|收口[：:（(])")

#: 正文首行认领行（与 :data:`CLAIM_RE` 同形态，但**只判首行**且不做 MULTILINE。
#: 刻意**不** ``strip()``、不用 ``str.splitlines()``：比内核建卡闸门更严，
#: 差异集 4 类 / 字符级变体钉在
#: ``tests/unit/test_prclaim_gaps.py`` 的 ``TestKernelDifferencePin``）
FIRST_CLAIM_RE = re.compile(r"^PR-CLAIM: #([1-9][0-9]*)$")

#: ``audit_gaps`` 的末行两种文案（``{count}`` 由 :func:`render_gaps` 填）
VERDICT_GAPS_NONE = "结论: 无缺口（合并类卡都带 PR-CLAIM）"
VERDICT_GAPS_SOME = "结论: 缺口 {count} 张（逐张把 PR-CLAIM: #<n> 放到正文首行）"

#: ``GapRow.first`` 的截断长度（整行太长会把一行报告撑爆）
FIRST_LINE_LIMIT = 40

#: ``--pr`` 的上限（PR 号池只有一个口径，``merge-record`` 也用它）。GitHub 的
#: PR 号是仓库内递增计数，9 位（< 2**31）已经远超任何真实仓库；13 位毫秒
#: （``1793160000000``）与天文数字（``99999999999999999999``）都属「填错了
#: 字段」，一律 rc2 而不是收下。
MAX_PR = 999_999_999

#: 只认 ASCII 十进制数字：``int()`` 会静默接受 ``" 15"`` / ``"+15"`` /
#: ``"1_0"`` / 阿拉伯-印度数字
_PR_DIGITS = re.compile(r"[1-9][0-9]*", re.ASCII)

#: 三种结论的末行文案（``{pr}`` / ``{card}`` 由 :func:`render` 填）
VERDICT_UNIQUE = "结论: 认领唯一 → {card}"
VERDICT_NONE = "结论: 无未结卡认领该 PR（合并前必须先认领）"
VERDICT_DUPE = "结论: 重复认领 → 只许一张卡对同一 PR 发 gh 写操作，其余改 PR-REF"


class ClaimAuditError(Exception):
    """参数或环境错误（``--pr`` 取值非法等）→ stderr 一行 ``错误: …`` + rc2。"""


def parse_pr(value: str) -> int:
    """``--pr``：ASCII 十进制正整数且不超 :data:`MAX_PR`，否则抛 ClaimAuditError。

    只用字符串接收、在这里转 int（argparse 不挂 ``type=int``）：``type=int`` 抛的
    是 ``SystemExit(2)`` + argparse 自己的文案，与其它 rc2 的 ``错误: …`` 不一致，
    且它拦不住 ``0``、``1_0``（=10）、``+1``、``1793160000000``。
    """

    if not isinstance(value, str) or _PR_DIGITS.fullmatch(value) is None:
        raise ClaimAuditError(
            f"PR 号非法：{value!r}（应为 1-{MAX_PR} 的 ASCII 十进制正整数）"
        )
    pr = int(value)
    if pr > MAX_PR:
        raise ClaimAuditError(
            f"PR 号超出上限 {MAX_PR}：{value!r}（13 位毫秒与天文数字都不合法）"
        )
    return pr


@dataclass(frozen=True)
class ClaimRow:
    """一张未结卡（``id`` / ``status`` / ``assignee``；NULL 已归成空串）。"""

    id: str
    status: str
    assignee: str


@dataclass(frozen=True)
class Report:
    """一次 ``pr-claim-audit`` 的全部判据数据。

    - ``pending``：扫描了多少张未结卡；
    - ``claims``：认领该 PR 的卡（按卡号升序，由调用方的 SQL 定序）；
    - ``refs``：只引用该 PR 的卡（认领过的卡不会重复出现在这里）。
    """

    pr: int
    pending: int
    claims: tuple[ClaimRow, ...]
    refs: tuple[ClaimRow, ...]

    @property
    def ok(self) -> bool:
        """恰好一张卡认领 = 唯一认领。"""
        return len(self.claims) == 1

    @property
    def sole_claim_id(self) -> str | None:
        """唯一认领的卡号；0 张或 ≥2 张时是 ``None``（别拿第一张顶包）。"""
        return self.claims[0].id if self.ok else None


def claimed_prs(body: str | None) -> tuple[int, ...]:
    """卡正文里**顶格认领行**的 PR 号（按出现顺序，可能重复）。"""
    if not body:
        return ()
    return tuple(int(n) for n in CLAIM_RE.findall(body))


def referenced_prs(body: str | None) -> tuple[int, ...]:
    """卡正文里**顶格引用行**的 PR 号（按出现顺序，可能重复）。"""
    if not body:
        return ()
    return tuple(int(n) for n in REF_RE.findall(body))


def claims(body: str | None, pr: int) -> bool:
    """这张卡的正文是否认领了 ``pr``。"""
    return pr in claimed_prs(body)


def references(body: str | None, pr: int) -> bool:
    """这张卡的正文是否引用了 ``pr``。"""
    return pr in referenced_prs(body)


def first_line(body: str | None) -> str:
    """正文第一个非空行（原样返回，不改写空白）；没有正文/全空行时是空串。

    只判空用 ``strip()``，返回值不 strip：``"  PR-CLAIM: #34"`` 与
    ``"PR-CLAIM: #34\\r"`` 都要保持原样，交给 :func:`first_claim_pr` 判死。
    按 ``"\\n"`` 切而不是 ``str.splitlines()`` —— 后者会吞掉 ``\\r``，让
    ``\\r\\n`` 行尾的卡在缺口稽核里「通过」而 :data:`CLAIM_RE` 却不认它。

    这是**刻意**比内核建卡闸门更严的地方（内核 ``_validate_pr_claim`` 用
    ``ln.strip()`` + ``str.splitlines()``）：稽核与 :data:`CLAIM_RE` 对齐，
    因为 ``merge_guard`` 认认领行时只看 ``CLAIM_RE``。差异集 4 类 —— 前导空白 /
    行尾空白 / CRLF / 其它换行符（字符级变体含 NBSP、VT、FF、``\\x1c``、U+2028 等），
    钉在 ``tests/unit/test_prclaim_gaps.py::TestKernelDifferencePin``。
    """
    if not body:
        return ""
    for line in body.split("\n"):
        if line.strip():
            return line
    return ""


def needs_claim(title: str | None) -> bool:
    """标题是否命中合并语义（只看**标题前缀**，理由见 ``docs/pr-claim-gate.md``）。"""
    if not title:
        return False
    return TRIGGER_RE.match(title) is not None


def trigger_hit(title: str | None) -> str:
    """命中合并语义的标题片段（如 ``合并卡``）；不命中时是空串。"""
    if not title:
        return ""
    match = TRIGGER_RE.match(title)
    return "" if match is None else match.group(0)


def first_claim_pr(body: str | None) -> int | None:
    """正文**首行**认领的 PR 号；首行不是 ``PR-CLAIM: #<N>`` 时是 ``None``。

    首行 = 第一个非空行（见 :func:`first_line`）；认领行必须在首行，且与
    :data:`CLAIM_RE` 同一形态（顶格、行尾无尾随字符）—— 比它更严，不会更宽。
    比内核建卡闸门**也更严**（内核会 ``strip()`` 首行，见 :func:`first_line`
    的说明与 ``TestKernelDifferencePin``）。
    """
    match = FIRST_CLAIM_RE.fullmatch(first_line(body))
    return None if match is None else int(match.group(1))


def audit(rows: Iterable[tuple[str, str, str, str]], pr: int) -> Report:
    """把 ``(id, status, assignee, body)`` 行判成 :class:`Report`。

    ``pr`` 已经过 :func:`parse_pr`（正整数）；这里不再重复校验参数。
    """
    claims_out: list[ClaimRow] = []
    refs_out: list[ClaimRow] = []
    pending = 0
    for id_, status, assignee, body in rows:
        pending += 1
        row = ClaimRow(id=id_, status=status, assignee=assignee)
        if claims(body, pr):
            claims_out.append(row)
        elif references(body, pr):
            refs_out.append(row)
    return Report(
        pr=pr,
        pending=pending,
        claims=tuple(claims_out),
        refs=tuple(refs_out),
    )


def exit_code(report: Report) -> int:
    """0 = 恰好一张卡认领；1 = 0 张（无人认领）或 ≥2 张（重复认领）。"""
    return 0 if report.ok else 1


def render(report: Report) -> str:
    """渲染整块报告（不带尾随换行）。

    四段、顺序固定：``未结卡 N 张`` → 逐张 ``CLAIM <卡号> [status] assignee`` →
    逐张 ``REF <卡号> [status] assignee`` → 末行结论。

    一条卡一行的**每一列**（卡号 / 状态 / assignee，末行的卡号同样）都经
    :func:`visible` 转义：``assignee`` / ``status`` 里带 ``\\n`` 会把一条 CLAIM 撑成
    两条物理行（``splitlines()`` 数出来比卡数多），``\\r`` 会把光标拉回行首 —— 与
    :func:`render_gaps` 同口径，转义只动渲染，判定（:func:`audit`）仍用原文。

    :func:`visible` 连 ``str.splitlines()`` 认的三个 Unicode 行边界（U+0085 NEL /
    U+2028 / U+2029）一起转义，所以 ``render(...).splitlines()`` 的行数恒为
    ``2 + CLAIM 张数 + REF 张数``（表头 + 末行结论，加每条卡一行）。
    """
    lines = [f"未结卡 {report.pending} 张"]
    lines.extend(
        f"CLAIM {visible(row.id)} [{visible(row.status)}] {visible(row.assignee)}"
        for row in report.claims
    )
    lines.extend(
        f"REF {visible(row.id)} [{visible(row.status)}] {visible(row.assignee)}"
        for row in report.refs
    )
    if report.ok:
        lines.append(VERDICT_UNIQUE.format(card=visible(report.claims[0].id)))
    elif not report.claims:
        lines.append(VERDICT_NONE)
    else:
        lines.append(VERDICT_DUPE)
    return "\n".join(lines)


@dataclass(frozen=True)
class GapRow:
    """一张「标题命中合并语义、正文首行却无 PR-CLAIM」的未结卡。

    - ``hit``：命中的标题片段（如 ``合并卡``）；
    - ``first``：正文第一个非空行，截 :data:`FIRST_LINE_LIMIT` 字符（控行宽）。
    """

    id: str
    status: str
    assignee: str
    hit: str
    first: str


@dataclass(frozen=True)
class GapReport:
    """一次 ``pr-claim-audit --gaps`` 的全部判据数据。

    - ``pending``：扫描了多少张未结卡；
    - ``gaps``：缺口卡（按调用方 SQL 的 id 升序）。
    """

    pending: int
    gaps: tuple[GapRow, ...]

    @property
    def ok(self) -> bool:
        """没有缺口 = 所有合并类卡的首行都带了 ``PR-CLAIM``。"""
        return not self.gaps


def audit_gaps(rows: Iterable[tuple[str, str, str, str, str]]) -> GapReport:
    """把 ``(id, status, assignee, title, body)`` 行判成 :class:`GapReport`。

    缺口判据两条同时成立：:func:`needs_claim` 为真（标题命中合并语义）且
    :func:`first_claim_pr` 为 ``None``（首行没有认领行）。两条都用本模块的
    纯函数，不重复写第三份判定逻辑。
    """
    gaps: list[GapRow] = []
    pending = 0
    for id_, status, assignee, title, body in rows:
        pending += 1
        if not needs_claim(title):
            continue
        if first_claim_pr(body) is not None:
            continue
        gaps.append(
            GapRow(
                id=id_,
                status=status,
                assignee=assignee,
                hit=trigger_hit(title),
                first=first_line(body)[:FIRST_LINE_LIMIT],
            )
        )
    return GapReport(pending=pending, gaps=tuple(gaps))


def exit_code_gaps(report: GapReport) -> int:
    """0 = 无缺口；1 = 有缺口（逐张把认领行放到正文首行）。"""
    return 0 if report.ok else 1


def visible(text: str) -> str:
    """把控制字符与 Unicode 行分隔符换成可见转义，供报告里显示。

    正文首行可能带 ``\\r``（CRLF 卡）或 ``\\t``：原样打进终端时 ``\\r`` 会把光标
    拉回行首、污染整行报告。只动**渲染**，不动判定（:func:`audit_gaps` 的
    ``GapRow.first`` 仍是原文的前 :data:`FIRST_LINE_LIMIT` 个字符）。

    除 ``\\t`` / ``\\r`` / ``<0x20`` / ``0x7f`` 外，还转义 ``str.splitlines()`` 认定的
    三个 Unicode 行边界：NEL（U+0085）、LINE SEPARATOR（U+2028）、PARAGRAPH
    SEPARATOR（U+2029）。少转这三个，一条 GAP 照样会被撑成两条物理行；转义后的
    文本里因此不存在任何 ``splitlines()`` 边界。
    """
    out: list[str] = []
    for ch in text:
        if ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        elif ch in "\u0085\u2028\u2029":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def render_gaps(report: GapReport) -> str:
    """渲染 ``--gaps`` 的整块报告（不带尾随换行）。

    三段、顺序固定：``未结卡 N 张`` → 逐张
    ``GAP <卡号> [status] assignee 触发=<hit> 首行=<first>`` → 末行结论。
    **五列全部**经 :func:`visible` 转义控制字符后显示（判定不受影响）：
    ``hit`` 来自标题，标题里的换行（``合并\\nPR #5`` 会被 ``\\s*`` 吃掉）与
    ``status`` / ``assignee`` 里的控制字符同样不能让一条 GAP 变成两条物理行。
    ``visible`` 连 NEL / U+2028 / U+2029 这三个 Unicode 行边界一起转义，转义后
    **每一列的值里**都不再有 ``str.splitlines()`` 边界（列间分隔用的 ``"\\n"`` 除外），
    故 ``render_gaps(...).splitlines()`` 的行数恒为 ``2 + len(report.gaps)``。
    """
    lines = [f"未结卡 {report.pending} 张"]
    lines.extend(
        f"GAP {visible(row.id)} [{visible(row.status)}] {visible(row.assignee)} "
        f"触发={visible(row.hit)} 首行={visible(row.first)}"
        for row in report.gaps
    )
    if report.ok:
        lines.append(VERDICT_GAPS_NONE)
    else:
        lines.append(VERDICT_GAPS_SOME.format(count=len(report.gaps)))
    return "\n".join(lines)
