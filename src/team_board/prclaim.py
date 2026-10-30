"""PR 认领唯一性（R17）的唯一判据，以及 ``pr-claim-audit`` 的纯函数与渲染。

三种消费者共用这一份实现，不许各写一份：

- ``parse_pr``：``--pr`` 的取值域（ASCII 十进制正整数、不超 :data:`MAX_PR`），
  ``pr-claim-audit`` 与 ``merge-record`` 都调它；
- ``audit``：把 ``db.iter_claim_rows`` 的行判成 :class:`Report`
  （未结卡里恰好一张卡认领该 PR = 唯一认领）；
- ``render``：把 :class:`Report` 渲染成 ``pr-claim-audit`` 的整块文本。

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
    """
    lines = [f"未结卡 {report.pending} 张"]
    lines.extend(
        f"CLAIM {row.id} [{row.status}] {row.assignee}" for row in report.claims
    )
    lines.extend(f"REF {row.id} [{row.status}] {row.assignee}" for row in report.refs)
    if report.ok:
        lines.append(VERDICT_UNIQUE.format(card=report.claims[0].id))
    elif not report.claims:
        lines.append(VERDICT_NONE)
    else:
        lines.append(VERDICT_DUPE)
    return "\n".join(lines)
