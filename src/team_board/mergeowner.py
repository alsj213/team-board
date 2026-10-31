"""``merge-owner-audit`` 的纯函数层：抽 PR 号、读豁免、五类判定、渲染与 rc。

口径唯一源是需求卡 ``t_6fca5f99`` 冻结的契约 v1（见本卡评论 #1）。本模块**不读库、
不调 gh、不写文件、不 print**：取数（db 取正文与评论、gh 判状态、降级写日志）全在
CLI 层，本模块只吃字符串与 :class:`GhResult`，吐 :class:`Report`。

五个判定（每个 PR 只出一个 :class:`Verdict`，按此优先级）

1. PR 在本卡正文的豁免行里（``PR-MERGE-EXEMPT: #<n>``，顶格）→ ``EXEMPT``；
2. gh 说 ``MERGED`` → ``MERGED``；``CLOSED`` → ``CLOSED``；
3. gh 说 ``OPEN`` 且**另一张**未结卡（``status NOT IN ('done','archived')`` 且
   ``card_id != 本卡``）的正文认领了它（``prclaim.claims``）→ ``CLAIMED``，
   ``detail`` 是那些卡号升序逗号分隔（多张也算通过）；
4. gh 说 ``OPEN`` 且没人认领 → ``UNOWNED``；
5. gh 取数失败 → ``WARN``，``detail`` 一句话。

**本卡自己认领不算**（本卡未结、正文里写了自己的 ``PR-CLAIM: #n`` 仍判 ``UNOWNED``）
—— 防「自认自关」。gh 只产出 ``MERGED`` / ``CLOSED`` / ``OPEN`` 三态或一条错误；
非 ``MERGED`` / ``CLOSED`` 一律走 ``OPEN`` 分支。

``MAX_PR`` 从 :mod:`team_board.prclaim` 导入，本模块不定义第二份 PR 号口径。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .prclaim import MAX_PR, claims

__all__ = [
    "CARD_RE",
    "CLAIMED",
    "CLOSED",
    "DONE_STATUSES",
    "EXEMPT",
    "EXEMPT_RE",
    "MAX_PR",
    "MERGED",
    "PR_RE",
    "REPO_RE",
    "TIMEOUT_DEFAULT",
    "TIMEOUT_MAX",
    "TIMEOUT_MIN",
    "UNOWNED",
    "URL_RE",
    "WARN",
    "GhResult",
    "MergeOwnerError",
    "Report",
    "Verdict",
    "exempt_prs",
    "exit_code",
    "extract_prs",
    "judge",
    "parse_card",
    "parse_repo",
    "parse_timeout",
    "render",
]

#: 卡号的唯一形态：``t_`` + 8 位小写十六进制（``parse_card`` 用 ``fullmatch``）
CARD_RE = re.compile(r"t_[0-9a-f]{8}", re.ASCII)

#: ``--repo`` 的形态：``OWNER/REPO``（两段，字符集与 GitHub 一致）
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)

#: 正文里引用 PR 的第一种形态：``PR #52`` —— ``PR`` 与 ``#`` 之间**恰好一个空格**
#:
#: 尾部 ``(?![0-9])`` 是必须的：契约要求 10 位以上（含 13 位毫秒）**不命中**，
#: 而 ``[1-9][0-9]{0,8}`` 本身会从 13 位数里截出前 9 位来匹配。加这条断言后
#: ``PR #1793160000000`` 一位都不取（回溯也过不去），不是把长数字截短收下。
PR_RE = re.compile(r"PR #([1-9][0-9]{0,8})(?![0-9])")

#: 正文里引用 PR 的第二种形态：URL 的 ``/pull/<n>`` 段（同样不许截短长数字）
URL_RE = re.compile(r"/pull/([1-9][0-9]{0,8})(?![0-9])")

#: 豁免行：顶格、行尾无尾随字符（与 :data:`team_board.prclaim.CLAIM_RE` 同口径）
EXEMPT_RE = re.compile(r"^PR-MERGE-EXEMPT: #([1-9][0-9]{0,8})$", re.MULTILINE)

#: ``--timeout`` 的取值域（秒）
TIMEOUT_MIN = 1
TIMEOUT_MAX = 600
TIMEOUT_DEFAULT = 20

#: 五种判定（``Verdict.kind`` 的取值）
MERGED = "MERGED"
CLOSED = "CLOSED"
CLAIMED = "CLAIMED"
EXEMPT = "EXEMPT"
UNOWNED = "UNOWNED"
WARN = "WARN"

#: 「已结」卡：这些状态的卡认领 PR 不算归属（本卡自身的状态也照此判）
DONE_STATUSES = ("done", "archived")

#: 四选一的结论行（``{n}`` / ``{k}`` / ``{m}`` 是整数；其余字面照抄，不许改写）
VERDICT_NO_PR = "结论: 抽取到 0 个 PR 号 → 无合并归属需要（直接通过）"
VERDICT_ALL_OK = "结论: {n} 个 PR 全部有归属 → 可关"
VERDICT_UNOWNED = (
    "结论: {n} 个 PR / 通过 {k} / 无归属 {m} → "
    "先建收口卡（首行 PR-CLAIM: #<最小无归属号>）再关本卡"
)
VERDICT_DEGRADED = "结论: gh 不可用（{detail}）→ 降级放行（日志已写），晨报须列"

#: 只认 ASCII 十进制数字、不带前导零：``int()`` 会静默接受 ``" 20"`` / ``"+20"``
_TIMEOUT_DIGITS = re.compile(r"[1-9][0-9]*", re.ASCII)


class MergeOwnerError(Exception):
    """参数错误（卡号 / 仓库 / 超时取值非法）→ CLI 渲染成 ``错误: …`` + rc2。"""


@dataclass(frozen=True)
class Verdict:
    """一个 PR 的判定结论；``detail`` 只在 ``CLAIMED`` / ``WARN`` 时非空。"""

    pr: int
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class Report:
    """一次 ``merge-owner-audit`` 的全部判定数据（``verdicts`` 按 PR 号升序）。"""

    card: str
    status: str
    verdicts: tuple[Verdict, ...]

    @property
    def unowned(self) -> tuple[Verdict, ...]:
        """判成 ``UNOWNED`` 的那些（保持 PR 号升序，第一项即最小无归属号）。"""
        return tuple(v for v in self.verdicts if v.kind == UNOWNED)

    @property
    def warns(self) -> tuple[Verdict, ...]:
        """判成 ``WARN`` 的那些（原地顺序，第一项即「第一个 WARN」）。"""
        return tuple(v for v in self.verdicts if v.kind == WARN)


@dataclass(frozen=True)
class GhResult:
    """CLI 层一次 ``gh`` 取数的结果：成功填 ``status``，失败填一句话的 ``error``。"""

    status: str = ""
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error)


def extract_prs(*texts: str | None) -> tuple[int, ...]:
    """按入参顺序扫正文/评论，去重后**升序**返回 PR 号。

    ``None`` 与空串当作没内容（不报错）；``PR #0`` / ``/pull/0`` / 10 位以上都不命中。
    """
    found: set[int] = set()
    for text in texts:
        if not text:
            continue
        found.update(int(n) for n in PR_RE.findall(text))
        found.update(int(n) for n in URL_RE.findall(text))
    return tuple(sorted(found))


def exempt_prs(body: str | None) -> frozenset[int]:
    """卡正文里顶格豁免行的 PR 号集合。"""
    if not body:
        return frozenset()
    return frozenset(int(n) for n in EXEMPT_RE.findall(body))


def judge(
    card: str,
    status: str,
    prs: Iterable[int],
    *,
    exempt: frozenset[int] | None = None,
    gh: Mapping[int, GhResult] | None = None,
    others: Iterable[tuple[str, str, str | None]] = (),
) -> Report:
    """把「本卡 + PR 号集合 + gh 结果 + 其它卡的认领」判成 :class:`Report`。

    - ``prs``：:func:`extract_prs` 的产物（重复与乱序都容忍，内部按升序去重）；
    - ``exempt``：:func:`exempt_prs` 的产物（本卡正文的豁免行）；
    - ``gh``：PR 号 → :class:`GhResult`；**缺条目按取数失败**（``WARN``）算；
    - ``others``：``(卡号, 状态, 正文)`` 三元组，可以传全表 —— 「另一张未结卡」的
      过滤（自己 / 已结）在这里做，判定口径不散到 CLI 层。
    """
    exempt_set = frozenset(exempt or ())
    gh_map: Mapping[int, GhResult] = gh or {}
    pool = tuple(
        (other_id, body)
        for other_id, other_status, body in others
        if other_id != card and other_status not in DONE_STATUSES
    )
    verdicts = tuple(
        _judge_one(pr, exempt_set, gh_map, pool) for pr in sorted(set(prs))
    )
    return Report(card=card, status=status, verdicts=verdicts)


def _judge_one(
    pr: int,
    exempt_set: frozenset[int],
    gh_map: Mapping[int, GhResult],
    pool: tuple[tuple[str, str | None], ...],
) -> Verdict:
    """单个 PR 的五条优先级（顺序即契约里的 1→5）。"""
    if pr in exempt_set:
        return Verdict(pr, EXEMPT)
    result = gh_map.get(pr)
    if result is None:
        return Verdict(pr, WARN, "gh 无结果")
    if result.failed:
        return Verdict(pr, WARN, result.error)
    if result.status == MERGED:
        return Verdict(pr, MERGED)
    if result.status == CLOSED:
        return Verdict(pr, CLOSED)
    claimers = sorted(cid for cid, body in pool if claims(body, pr))
    if claimers:
        return Verdict(pr, CLAIMED, ",".join(claimers))
    return Verdict(pr, UNOWNED)


def render(report: Report) -> str:
    """把 :class:`Report` 渲染成整块 stdout 文本（不带尾随换行）。"""
    lines = [f"卡 {report.card} [{report.status}]"]
    lines.extend(_render_verdict(v) for v in report.verdicts)
    total = len(report.verdicts)
    unowned = report.unowned
    warns = report.warns
    if total == 0:
        lines.append(VERDICT_NO_PR)
    elif unowned:
        lines.append(
            VERDICT_UNOWNED.format(
                n=total, k=total - len(unowned), m=len(unowned)
            )
        )
    elif warns:
        lines.append(VERDICT_DEGRADED.format(detail=warns[0].detail))
    else:
        lines.append(VERDICT_ALL_OK.format(n=total))
    return "\n".join(lines)


def _render_verdict(verdict: Verdict) -> str:
    line = f"PR #{verdict.pr} {verdict.kind}"
    return f"{line} {verdict.detail}" if verdict.detail else line


def exit_code(report: Report) -> int:
    """有任一 ``UNOWNED`` → ``1``，否则 ``0``（含无 PR / 全通过 / 降级）。

    本模块**不返回 2** —— rc2 归 CLI 的参数错误与库异常。
    """
    return 1 if report.unowned else 0


def parse_card(value: str) -> str:
    """``--card`` / 位置参数：``t_`` + 8 位小写十六进制，否则抛 :class:`MergeOwnerError`。"""
    if not isinstance(value, str) or CARD_RE.fullmatch(value) is None:
        raise MergeOwnerError(
            f"卡号非法：{value!r}（应为 t_ 加 8 位小写十六进制，例如 t_0123abcd）"
        )
    return value


def parse_repo(value: str) -> str:
    """``--repo``：``OWNER/REPO``，否则抛 :class:`MergeOwnerError`。"""
    if not isinstance(value, str) or REPO_RE.fullmatch(value) is None:
        raise MergeOwnerError(f"仓库名非法：{value!r}（应为 OWNER/REPO）")
    return value


def parse_timeout(value: str) -> int:
    """``--timeout``：``1``–``600`` 的 ASCII 十进制整数秒，否则抛 :class:`MergeOwnerError`。"""
    if not isinstance(value, str) or _TIMEOUT_DIGITS.fullmatch(value) is None:
        raise MergeOwnerError(
            f"超时非法：{value!r}（应为 {TIMEOUT_MIN}-{TIMEOUT_MAX} 的整数秒）"
        )
    seconds = int(value)
    if not TIMEOUT_MIN <= seconds <= TIMEOUT_MAX:
        raise MergeOwnerError(
            f"超时超出范围：{value!r}（应为 {TIMEOUT_MIN}-{TIMEOUT_MAX} 的整数秒）"
        )
    return seconds
