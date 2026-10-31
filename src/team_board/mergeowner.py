"""``merge-owner-audit`` 的纯函数层：抽 PR 号、读豁免、五类判定、渲染与 rc。

口径唯一源是需求卡 ``t_6fca5f99`` 冻结的契约 v1（见本卡评论 #1），返工轮按契约 v2
增量改了两处：``UNOWNED`` 结论行代入真实最小无归属号、``others`` 同时吃三元组与
``db.iter_claim_rows`` 的四元组；契约 v3 增量再补四处在**输入侧**归一（超长数字串、
gh 状态大小写/空白、detail 换行与长度、CRLF 正文的豁免行），判定方向不变。
本模块**不读库、
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
—— 防「自认自关」。gh 只有 ``MERGED`` / ``CLOSED`` / ``OPEN`` 三态有效：状态先去空白
并转大写再比，空串 / 全空白 / 归一后不是三态的一律按取数失败（``WARN`` + ``gh 无结果``），
不是 ``MERGED`` / ``CLOSED`` 的合法三态走 ``OPEN`` 分支。

``MAX_PR`` 从 :mod:`team_board.prclaim` 导入，本模块不定义第二份 PR 号口径。

本轮（P2/P3 返工，2026-10-31）仍是**输入侧**增量，判定方向不变：``UNOWNED`` 行注明
PR 号来源（正文 / 评论；评论来源附「需在本卡正文写 ``PR-MERGE-EXEMPT: #n``」的提示）、
正文围栏不配对时结论行点名（:data:`FENCE_NOTE`）、降级日志没写成时结论行不再说
「（日志已写）」（:data:`VERDICT_DEGRADED_UNLOGGED`）。
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
    "FENCE_NOTE",
    "MAX_PR",
    "MERGED",
    "PR_RE",
    "REPO_RE",
    "SOURCE_BODY",
    "SOURCE_COMMENT",
    "TIMEOUT_DEFAULT",
    "TIMEOUT_MAX",
    "TIMEOUT_MIN",
    "UNOWNED",
    "UNOWNED_COMMENT_HINT",
    "URL_RE",
    "VERDICT_DEGRADED_UNLOGGED",
    "WARN",
    "ClaimRow",
    "GhResult",
    "MergeOwnerError",
    "Report",
    "Verdict",
    "exempt_prs",
    "exit_code",
    "extract_prs",
    "extract_prs_sources",
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

#: 豁免行：顶格、行尾无尾随字符（与 :data:`team_board.prclaim.CLAIM_RE` 同口径）。
#: ``\r?`` 认 CRLF —— 本板正文经 Windows 侧写入，``\r`` 属现实路径，不算尾随字符。
EXEMPT_RE = re.compile(r"^PR-MERGE-EXEMPT: #([1-9][0-9]{0,8})\r?$", re.MULTILINE)

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

#: gh 的合法三态之一（不是 :class:`Verdict` 的 kind，故不导出）
_GH_OPEN = "OPEN"

#: ``detail`` 的展示上限（字符）：超过就截断加 ``…``，保证整块输出远低于 output-audit 的 8192 字节
_DETAIL_MAX = 80

#: 四选一的结论行（``{n}`` / ``{k}`` / ``{m}`` 是整数；其余字面照抄，不许改写）
VERDICT_NO_PR = "结论: 抽取到 0 个 PR 号 → 无合并归属需要（直接通过）"
VERDICT_ALL_OK = "结论: {n} 个 PR 全部有归属 → 可关"
VERDICT_UNOWNED = (
    "结论: {n} 个 PR / 通过 {k} / 无归属 {m} → "
    "先建收口卡（首行 PR-CLAIM: #{pr}）再关本卡"
)
VERDICT_DEGRADED = "结论: gh 不可用（{detail}）→ 降级放行（日志已写），晨报须列"

#: 降级但**日志没写成**时的同一句：「（日志已写）」只在真写成时打印（P2，2026-10-31）
VERDICT_DEGRADED_UNLOGGED = (
    "结论: gh 不可用（{detail}）→ 降级放行（日志写入失败，见 stderr），晨报须列"
)

#: 正文围栏不配对时挂在结论行尾的说明：豁免行可能被围栏吞掉，必须点名，不许无声 rc1
FENCE_NOTE = "；注意：正文围栏不配对，豁免未生效"

#: ``UNOWNED`` 行的来源标签（正文 / 评论；两者都有时渲染成 ``正文+评论``）
SOURCE_BODY = "正文"
SOURCE_COMMENT = "评论"

#: 评论来源的 ``UNOWNED`` 提示尾巴：豁免只认正文，卡主要自己补一行才能自解（``{pr}`` 是号）
UNOWNED_COMMENT_HINT = "；需在本卡正文写 PR-MERGE-EXEMPT: #{pr}"

#: 只认 ASCII 十进制数字、不带前导零：``int()`` 会静默接受 ``" 20"`` / ``"+20"``
_TIMEOUT_DIGITS = re.compile(r"[1-9][0-9]*", re.ASCII)

#: CPython 对十进制串的 ``int()`` 有位数额度（默认 4300 位，3.11+ 起），超了抛裸
#: ``ValueError``。那是**环境异常不是参数异常**，CLI 只捕 :class:`MergeOwnerError`，
#: 漏出去就是 traceback + rc1（应当是 ``错误: …`` + rc2），所以这里先量位数再转换。
_MAX_INT_DIGITS = 4300

#: 报错里回显输入的上限：4301 位原文塞进 ``错误: …`` 会撞 output-audit 的 8192 字节
_ECHO_MAX = 16


class MergeOwnerError(Exception):
    """参数错误（卡号 / 仓库 / 超时取值非法）→ CLI 渲染成 ``错误: …`` + rc2。"""


def _one_line(text: str) -> str:
    """把 ``detail`` 压成单行短句：空白折叠成一个空格、去首尾、超长截断加 ``…``。

    目的有二：``render`` 的每一行恒为「一条判定」（detail 里带换行会把结论行顶碎），
    以及整块输出不被超长 gh 报错撑破（``output-audit`` 上限 8192 字节）。
    结果长度恒 ``<= _DETAIL_MAX``。
    """
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) > _DETAIL_MAX:
        return collapsed[: _DETAIL_MAX - 1] + "…"
    return collapsed


@dataclass(frozen=True)
class Verdict:
    """一个 PR 的判定结论；``detail`` 在 ``CLAIMED`` / ``WARN`` / ``UNOWNED`` 时非空。

    ``detail`` 在构造时就归一成单行（见 :func:`_one_line`），所以 ``Report`` 里存的
    已经是可展示形态，CLI 层拿它拼别的文案也不会带出换行。``UNOWNED`` 的 ``detail``
    是「来源: 正文 / 评论」，评论来源另带一句补豁免行的提示（P2，2026-10-31）。
    """

    pr: int
    kind: str
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _one_line(self.detail))


@dataclass(frozen=True)
class Report:
    """一次 ``merge-owner-audit`` 的全部判定数据（``verdicts`` 按 PR 号升序）。

    ``fence_unbalanced`` / ``log_written`` 是**渲染输入**，不参与判定：

    - ``fence_unbalanced``：本卡正文的行首 ``` 是奇数 —— 豁免行可能被围栏吞掉，
      结论行必须点名「围栏不配对，豁免未生效」（不许无声 rc1）；
    - ``log_written``：降级日志是否真写成 —— 只有写成了才许说「（日志已写）」。
    """

    card: str
    status: str
    verdicts: tuple[Verdict, ...]
    fence_unbalanced: bool = False
    log_written: bool = True

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
    """CLI 层一次 ``gh`` 取数的结果：成功填 ``status``，失败填一句话的 ``error``。

    ``status`` 不要求已经归一 —— 大小写、首尾空白都容忍（``"merged\\n"`` / ``"MERGED "``
    都算 ``MERGED``）；归一后不是三态则按取数失败处理（见 :func:`_judge_one`）。
    """

    status: str = ""
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error)

    @property
    def state(self) -> str:
        """归一后的状态：去首尾空白 + 转大写（空值给空串）。"""
        return (self.status or "").strip().upper()


def extract_prs(*texts: str | None) -> tuple[int, ...]:
    """按入参顺序扫正文/评论，去重后**升序**返回 PR 号。

    ``None`` 与空串当作没内容（不报错）；``PR #0`` / ``/pull/0`` / 10 位以上都不命中。
    """
    found: set[int] = set()
    for text in texts:
        found |= _scan(text)
    return tuple(sorted(found))


def extract_prs_sources(
    body: str | None, comments: Iterable[str | None] = ()
) -> dict[int, tuple[str, ...]]:
    """``{PR 号: 来源标签}``，键**升序**；来源是 ``正文`` / ``评论`` / 两者都有的 ``正文+评论``。

    ``UNOWNED`` 行要注明「这个号是从哪儿来的」：本卡无法自解的典型场景是**别人**在
    评论里提了一个未闭合 PR 号 —— 只看得到卡号不够，得让卡主知道该往正文写豁免行。
    与 :func:`extract_prs` 共用同一份 :func:`_scan`，不复制 PR 号口径。
    """
    labels: dict[int, set[str]] = {}
    if body:
        for pr in _scan(body):
            labels.setdefault(pr, set()).add(SOURCE_BODY)
    for text in comments:
        for pr in _scan(text):
            labels.setdefault(pr, set()).add(SOURCE_COMMENT)
    return {
        pr: tuple(tag for tag in (SOURCE_BODY, SOURCE_COMMENT) if tag in labels[pr])
        for pr in sorted(labels)
    }


def _scan(text: str | None) -> set[int]:
    """一坨文本里的 PR 号（两种引用形态的并集）；空串 / ``None`` 给空集。"""
    if not text:
        return set()
    return {int(n) for n in PR_RE.findall(text)} | {
        int(n) for n in URL_RE.findall(text)
    }


def exempt_prs(body: str | None) -> frozenset[int]:
    """卡正文里顶格豁免行的 PR 号集合。"""
    if not body:
        return frozenset()
    return frozenset(int(n) for n in EXEMPT_RE.findall(body))


#: 「另一张卡」的认领行：``(卡号, 状态, 正文)`` 是 E1 里的紧凑形状，
#: ``(卡号, 状态, 负责人, 正文)`` 是 :func:`team_board.db.iter_claim_rows` 直出的形状
#: （E2 契约要求复用该函数，故两种都吃；判定只取 ``row[0]`` / ``row[1]`` / ``row[-1]``）
ClaimRow = tuple[str, str, str | None] | tuple[str, str, str | None, str | None]


def judge(
    card: str,
    status: str,
    prs: Iterable[int],
    *,
    exempt: frozenset[int] | None = None,
    gh: Mapping[int, GhResult] | None = None,
    others: Iterable[ClaimRow] = (),
    sources: Mapping[int, tuple[str, ...]] | None = None,
    fence_unbalanced: bool = False,
) -> Report:
    """把「本卡 + PR 号集合 + gh 结果 + 其它卡的认领」判成 :class:`Report`。

    - ``prs``：:func:`extract_prs` 的产物（重复与乱序都容忍，内部按升序去重）；
    - ``exempt``：:func:`exempt_prs` 的产物（本卡正文的豁免行）；
    - ``gh``：PR 号 → :class:`GhResult`；**缺条目按取数失败**（``WARN``）算；
    - ``others``：认领行，两种形状都吃 —— ``(卡号, 状态, 正文)`` 与
      ``(卡号, 状态, 负责人, 正文)``（:func:`db.iter_claim_rows` 的直出）；判定只认
      ``row[0]`` / ``row[1]`` / ``row[-1]``，中间列不参与口径。可以传全表 ——
      「另一张未结卡」的过滤（自己 / 已结）在这里做，判定口径不散到 CLI 层。
    - ``sources``：:func:`extract_prs_sources` 的产物，只用来给 ``UNOWNED`` 行注来源；
      缺省（``None``）时不注来源，判定与 rc 不变；
    - ``fence_unbalanced``：正文围栏不配对 —— 只影响结论行的一句提示。
    """
    exempt_set = frozenset(exempt or ())
    gh_map: Mapping[int, GhResult] = gh or {}
    source_map: Mapping[int, tuple[str, ...]] = sources or {}
    pool = tuple(
        (row[0], row[-1])
        for row in others
        if row[0] != card and row[1] not in DONE_STATUSES
    )
    verdicts = tuple(
        _judge_one(pr, exempt_set, gh_map, pool, source_map.get(pr, ()))
        for pr in sorted(set(prs))
    )
    return Report(
        card=card,
        status=status,
        verdicts=verdicts,
        fence_unbalanced=fence_unbalanced,
    )


def _judge_one(
    pr: int,
    exempt_set: frozenset[int],
    gh_map: Mapping[int, GhResult],
    pool: tuple[tuple[str, str | None], ...],
    sources: tuple[str, ...] = (),
) -> Verdict:
    """单个 PR 的五条优先级（顺序即契约里的 1→5）。"""
    if pr in exempt_set:
        return Verdict(pr, EXEMPT)
    result = gh_map.get(pr)
    if result is None:
        return Verdict(pr, WARN, "gh 无结果")
    if result.failed:
        return Verdict(pr, WARN, result.error)
    state = result.state
    if state == MERGED:
        return Verdict(pr, MERGED)
    if state == CLOSED:
        return Verdict(pr, CLOSED)
    if state != _GH_OPEN:
        # 空串 / 全空白 / 归一后不是三态 → 没取到状态，按取数失败算（契约 v3 ②）
        return Verdict(pr, WARN, "gh 无结果")
    claimers = sorted(cid for cid, body in pool if claims(body, pr))
    if claimers:
        return Verdict(pr, CLAIMED, ",".join(claimers))
    return Verdict(pr, UNOWNED, _unowned_detail(pr, sources))


def _unowned_detail(pr: int, sources: tuple[str, ...]) -> str:
    """``UNOWNED`` 行尾巴：``来源: 正文`` / ``来源: 评论；需在本卡正文写 PR-MERGE-EXEMPT: #n``。

    没有来源信息（老调用方不传 ``sources``）时给空串 —— 在**输入侧**兼容，判定不变。
    评论来源必须带补行的提示：豁免只认正文，卡主光看着 rc1 无法自解（P2，2026-10-31）。
    """
    if not sources:
        return ""
    text = f"来源: {'+'.join(sources)}"
    if SOURCE_COMMENT in sources:
        text += UNOWNED_COMMENT_HINT.format(pr=pr)
    return text


def render(report: Report) -> str:
    """把 :class:`Report` 渲染成整块 stdout 文本（不带尾随换行）。

    结论行按「无 PR → 有 UNOWNED → 有 WARN（降级）→ 全通过」的顺序选一条；两条附加
    说明只挂在结论行尾，**不加行**（报告行数仍恒为 ``1 + 判定数 + 1``）：

    - 围栏不配对 → 追加 :data:`FENCE_NOTE`（豁免可能没生效，不许无声）；
    - 降级但日志没写成 → 用 :data:`VERDICT_DEGRADED_UNLOGGED`，不说「（日志已写）」。
    """
    lines = [f"卡 {report.card} [{report.status}]"]
    lines.extend(_render_verdict(v) for v in report.verdicts)
    total = len(report.verdicts)
    unowned = report.unowned
    warns = report.warns
    if total == 0:
        conclusion = VERDICT_NO_PR
    elif unowned:
        conclusion = VERDICT_UNOWNED.format(
            n=total,
            k=total - len(unowned),
            m=len(unowned),
            pr=unowned[0].pr,  # verdicts 升序 → 第一项即最小无归属号
        )
    elif warns:
        template = VERDICT_DEGRADED if report.log_written else VERDICT_DEGRADED_UNLOGGED
        conclusion = template.format(detail=warns[0].detail)
    else:
        conclusion = VERDICT_ALL_OK.format(n=total)
    if report.fence_unbalanced:
        conclusion += FENCE_NOTE
    lines.append(conclusion)
    return "\n".join(lines)


def _render_verdict(verdict: Verdict) -> str:
    """一行一条判定；``detail`` 再归一一次（防手工构造的 ``Report`` 带出换行）。"""
    line = f"PR #{verdict.pr} {verdict.kind}"
    detail = _one_line(verdict.detail)
    return f"{line} {detail}" if detail else line


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
    """``--timeout``：``1``–``600`` 的 ASCII 十进制整数秒，否则抛 :class:`MergeOwnerError`。

    位数超过 :data:`_MAX_INT_DIGITS` 的串也抛 :class:`MergeOwnerError` —— 不许把
    ``int()`` 的裸 ``ValueError`` 漏给 CLI（那会让 rc 从 2 变成 1）。
    """
    if not isinstance(value, str) or _TIMEOUT_DIGITS.fullmatch(value) is None:
        raise MergeOwnerError(
            f"超时非法：{value!r}（应为 {TIMEOUT_MIN}-{TIMEOUT_MAX} 的整数秒）"
        )
    if len(value) > _MAX_INT_DIGITS:
        raise MergeOwnerError(
            f"超时非法：{value[:_ECHO_MAX]}…（{len(value)} 位数字，"
            f"应为 {TIMEOUT_MIN}-{TIMEOUT_MAX} 的整数秒）"
        )
    try:
        seconds = int(value)
    except ValueError:  # 位数额度被 sys.set_int_max_str_digits 调低时的兜底
        raise MergeOwnerError(
            f"超时非法：{value[:_ECHO_MAX]}…（整数转换失败，"
            f"应为 {TIMEOUT_MIN}-{TIMEOUT_MAX} 的整数秒）"
        ) from None
    if not TIMEOUT_MIN <= seconds <= TIMEOUT_MAX:
        raise MergeOwnerError(
            f"超时超出范围：{value!r}（应为 {TIMEOUT_MIN}-{TIMEOUT_MAX} 的整数秒）"
        )
    return seconds
