"""合并认账三字段：解析、校验、生成、渲染（纯逻辑层）。

本模块**只做纯函数**：输入是 message 字符串与提交元数据，输出是 ``Problem`` /
``MergeRecord`` / ``MergeReport`` 与待提交的 message 文本。不调 git、不做文件 IO、
不读看板库 —— 取数（``git log``）在 ``merge-audit`` 子命令里另做，本模块被它调用。

三字段写在合并提交 message 的**末尾 trailer 块**（每行一条，键名大小写敏感）：

    Merge-By: <profile>/run<NNN>
    Merge-Card: t_[0-9a-f]{8}
    Merge-PR: #<正整数>

判定口径
--------
1. ``parse_trailers`` 从 message 末尾往上扫：先跳过空行，再取连续的
   ``^[A-Za-z][A-Za-z0-9-]*: `` 形态行构成 trailer 块，遇到第一个不匹配的行即停。
   只挑 ``TRAILER_KEYS`` 里的键，其它键（``Signed-off-by`` 等）忽略且不算问题。
2. 三键之一出现在块外（正文里写了 ``Merge-Card: …``）→ ``字段未落在 trailer 区``；
   此时若块内也没有该键，**不再**报 ``缺字段``（否则同一件事报两次）。
3. 同一键在块内出现多次 → ``重复字段``，值取最后一条。
4. ``record_from_commit`` 在 ``parents`` 少于 2 时追加 ``不是合并提交``（不抛异常）；
   其余按 1–3 判定，三键齐且格式合法即零问题、``source="trailer"``。它返回的
   每一条 ``Problem.sha`` 都是入参 ``sha``（结构类问题也改写，不是 ``""``）；
   ``parse_trailers`` 无提交上下文，仍按 ``""`` 报。
5. ``check_message`` 判 message 本身（不判 ``parents``），传参且与 message 里的值
   不一致时追加 ``字段与参数不一致``；传参自身的格式由调用方先判，这里不重复判。
6. ``build_message`` 参数先校验，非法抛 ``MergeAuditError``（CLI 转 rc2）。
7. ``legacy_record`` 认 GitHub 默认合并标题，``by`` 一律 ``None`` —— 单账号只记
   ``alsj213``，作者/账号都推不出「谁按了合并」，绝不猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

#: ``Merge-By`` 取值：``<profile>/run<正整数>``，profile 为小写字母开头的短名
MERGE_BY_RE = re.compile(r"^[a-z][a-z0-9-]*/run[0-9]+$")

#: ``Merge-Card`` 取值：卡号 ``t_`` + 8 位小写十六进制
MERGE_CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")

#: ``Merge-PR`` 取值：``#`` + 正整数（大数字一样合法，不设上限）
MERGE_PR_RE = re.compile(r"^#[1-9][0-9]*$")

#: 认账三字段的键名（顺序即渲染与校验顺序，大小写敏感）
TRAILER_KEYS = ("Merge-By", "Merge-Card", "Merge-PR")

#: GitHub 默认合并提交标题：``Merge pull request #<pr> from <owner>/<branch>``，
#: 分支名允许带 ``wt/`` 前缀
LEGACY_SUBJECT_RE = re.compile(
    r"^Merge pull request #(?P<pr>[0-9]+) from [^/]+/(?:wt/)?(?P<card>t_[0-9a-f]{8})$"
)

#: trailer 行形态：``Key: value``（冒号后必须有一个空格）
_TRAILER_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*): (.*)$")

KIND_MISSING = "缺字段"
KIND_BAD_FORMAT = "格式非法"
KIND_DUPLICATE = "重复字段"
KIND_NOT_TRAILER = "字段未落在 trailer 区"
KIND_MISMATCH = "字段与参数不一致"
KIND_NOT_MERGE = "不是合并提交"

_FORMAT_BY_KEY = {
    "Merge-By": MERGE_BY_RE,
    "Merge-Card": MERGE_CARD_RE,
    "Merge-PR": MERGE_PR_RE,
}


class MergeAuditError(Exception):
    """参数/环境级错误（CLI 把它转成退出码 2）。"""


@dataclass(frozen=True)
class MergeRecord:
    """一条合并提交的认账记录（``source`` 为 ``trailer`` 或 ``legacy``）。"""

    sha: str
    subject: str
    parents: tuple[str, ...]
    by: str | None
    card: str | None
    pr: str | None
    source: str


@dataclass(frozen=True)
class Problem:
    """一处认账问题；``sha`` 为空串表示「候选 message，还没有提交」。"""

    kind: str
    sha: str
    detail: str


@dataclass(frozen=True)
class MergeReport:
    """一次合并认账稽核的结果。"""

    repo: str
    baseline: str
    rev: str
    records: tuple[MergeRecord, ...]
    problems: tuple[Problem, ...]
    history: tuple[MergeRecord, ...]

    @property
    def ok(self) -> bool:
        """无问题即通过。"""

        return not self.problems


@dataclass(frozen=True)
class _Scan:
    """``message`` 的裸扫描结果：块内取值、块外出现的键、结构类问题。"""

    values: dict[str, str]
    misplaced: frozenset[str]
    problems: tuple[Problem, ...]


def _lines(message: str) -> list[str]:
    """按通用换行切行（CRLF / CR 都归一成 LF，避免值尾带 ``\\r``）。"""

    return message.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _scan_message(message: str) -> _Scan:
    """切出末尾 trailer 块，收集块内值、块外出现的键与结构类问题。"""

    lines = _lines(message)
    index = len(lines) - 1
    while index >= 0 and lines[index].strip() == "":
        index -= 1

    block: list[tuple[int, str, str]] = []
    while index >= 0:
        match = _TRAILER_LINE_RE.match(lines[index])
        if match is None:
            break
        block.append((index, match.group(1), match.group(2)))
        index -= 1
    block.reverse()

    in_block = {line_no for line_no, _, _ in block}

    values: dict[str, str] = {}
    counts: dict[str, int] = {}
    for _, key, value in block:
        if key not in TRAILER_KEYS:
            continue
        values[key] = value  # 重复时后者覆盖前者 → 取最后一条
        counts[key] = counts.get(key, 0) + 1

    misplaced: set[str] = set()
    for line_no, line in enumerate(lines):
        if line_no in in_block:
            continue
        match = _TRAILER_LINE_RE.match(line)
        if match is not None and match.group(1) in TRAILER_KEYS:
            misplaced.add(match.group(1))

    problems: list[Problem] = []
    for key in TRAILER_KEYS:
        if counts.get(key, 0) > 1:
            problems.append(Problem(KIND_DUPLICATE, "", f"{key} 出现 {counts[key]} 次"))
    for key in TRAILER_KEYS:
        if key in misplaced:
            problems.append(
                Problem(KIND_NOT_TRAILER, "", f"{key} 出现在正文（不在末尾 trailer 块）")
            )

    return _Scan(values=values, misplaced=frozenset(misplaced), problems=tuple(problems))


def _fields(scan: _Scan, sha: str) -> tuple[dict[str, str | None], list[Problem]]:
    """按 1–3 的判定把三键落成「合法值或 None」，缺键/格式非法各报一条。"""

    values: dict[str, str | None] = {}
    problems: list[Problem] = []
    for key in TRAILER_KEYS:
        raw = scan.values.get(key)
        if raw is None:
            if key in scan.misplaced:
                # 「字段未落在 trailer 区」已经报过，不再报「缺字段」
                values[key] = None
                continue
            values[key] = None
            problems.append(Problem(KIND_MISSING, sha, f"{key} 缺失"))
            continue
        if _FORMAT_BY_KEY[key].match(raw) is None:
            values[key] = None
            problems.append(Problem(KIND_BAD_FORMAT, sha, f'{key} 的值 "{raw}" 不合法'))
            continue
        values[key] = raw
    return values, problems


def parse_trailers(message: str) -> tuple[dict[str, str], tuple[Problem, ...]]:
    """切出 message 末尾 trailer 块里的三键原值（不做格式校验）。

    返回 ``(values, problems)``：``values`` 只含块内出现过的 ``TRAILER_KEYS`` 键
    （重复时取最后一条），``problems`` 只含结构类问题（``重复字段`` /
    ``字段未落在 trailer 区``）。格式与缺键由 :func:`record_from_commit` /
    :func:`check_message` 判。
    """

    scan = _scan_message(message)
    return dict(scan.values), scan.problems


def record_from_commit(
    sha: str, subject: str, parents: tuple[str, ...], message: str
) -> tuple[MergeRecord, tuple[Problem, ...]]:
    """把一条提交元数据判成 ``MergeRecord`` + 问题元组（永不抛异常）。

    ``parents`` 少于 2 时追加一条 ``不是合并提交``；``source`` 恒为 ``"trailer"``
    —— 本函数走的是「提交 message 里有/该有认账块」这条路，legacy 标题另由
    :func:`legacy_record` 判。

    ``_scan_message`` 出来的结构类问题（``重复字段`` / ``字段未落在 trailer 区``）
    本身不带提交上下文，``sha`` 是其构造时的 ``""``；这里统一改写成入参 ``sha``，
    否则渲染出来这一行会是以 ``-`` 开头的「候选 message」口径，
    与同一次调用里「缺字段 / 格式非法」带真 sha 的两种口径并存。
    """

    scan = _scan_message(message)
    problems: list[Problem] = []
    if len(parents) < 2:
        problems.append(
            Problem(KIND_NOT_MERGE, sha, f"parents={len(parents)}，合并提交应至少 2 个父提交")
        )
    problems.extend(replace(problem, sha=sha) for problem in scan.problems)
    values, field_problems = _fields(scan, sha)
    problems.extend(field_problems)

    record = MergeRecord(
        sha=sha,
        subject=subject,
        parents=tuple(parents),
        by=values["Merge-By"],
        card=values["Merge-Card"],
        pr=values["Merge-PR"],
        source="trailer",
    )
    return record, tuple(problems)


def legacy_record(sha: str, subject: str) -> MergeRecord | None:
    """GitHub 默认合并标题 → legacy 记录（``by=None``，绝不猜执行者）。

    PR 号 < 1（``#0``）返回 ``None``：trailer 路径的 ``MERGE_PR_RE`` 要求
    ``#[1-9][0-9]*``，两条口径必须一致。
    """

    match = LEGACY_SUBJECT_RE.match(subject)
    if match is None:
        return None
    raw_pr = match.group("pr")
    if int(raw_pr) < 1:
        return None
    return MergeRecord(
        sha=sha,
        subject=subject,
        parents=(),
        by=None,
        card=match.group("card"),
        pr="#" + raw_pr,
        source="legacy",
    )


def check_message(
    message: str,
    *,
    card: str | None = None,
    by: str | None = None,
    pr: int | None = None,
) -> tuple[Problem, ...]:
    """判一条**候选** message（判 1–3 与格式，不判 ``parents``）。

    传了 ``card`` / ``by`` / ``pr`` 且与 message 里的值不一致时追加
    ``字段与参数不一致``；传参自身的格式非法属参数错误，由调用方先判，这里不重复判。
    空元组 = 通过。
    """

    scan = _scan_message(message)
    problems: list[Problem] = list(scan.problems)
    _, field_problems = _fields(scan, "")
    problems.extend(field_problems)

    expected: dict[str, str] = {}
    if card is not None:
        expected["Merge-Card"] = card
    if by is not None:
        expected["Merge-By"] = by
    if pr is not None:
        expected["Merge-PR"] = f"#{pr}"
    for key, want in expected.items():
        got = scan.values.get(key)
        if got is not None and got != want:
            problems.append(
                Problem(KIND_MISMATCH, "", f'{key} 提交里是 "{got}"，参数要求 "{want}"')
            )
    return tuple(problems)


def build_message(*, card: str, by: str, pr: int, subject: str | None = None) -> str:
    """生成带认账块的合并提交 message（恰好三段：标题 / 空行 / 三行 trailer）。

    参数先校验，非法一律抛 :class:`MergeAuditError`（CLI 转 rc2），不把原生
    ``TypeError`` / ``AttributeError`` 漏给调用方。类型与形态分开判：非 ``str``
    先挡（``None`` / ``int`` 直接进正则会抛 ``TypeError``），再用 ``fullmatch``
    判形态 —— ``re.match`` + ``$`` 会放过结尾多一个 ``\\n`` 的值，生成出来的
    message 再喂 :func:`check_message` 就报「字段未落在 trailer 区」，闭环断掉。

    ``subject`` 去首尾空白后为空同样抛错：那样第一行是空行，git 的 ``%s`` 会把
    ``Merge-By:`` 当提交标题。``subject=None`` 走契约默认标题。
    """

    if not isinstance(card, str):
        raise MergeAuditError(f"卡号非法：{card!r}（应为 str，形如 t_1234abcd）")
    if MERGE_CARD_RE.fullmatch(card) is None:
        raise MergeAuditError(f"卡号非法：{card!r}（应形如 t_1234abcd）")
    if not isinstance(by, str):
        raise MergeAuditError(f"执行者标识非法：{by!r}（应为 str，形如 pm/run137）")
    if MERGE_BY_RE.fullmatch(by) is None:
        raise MergeAuditError(f"执行者标识非法：{by!r}（应形如 pm/run137）")
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        raise MergeAuditError(f"PR 号非法：{pr!r}（应为正整数）")

    if subject is None:
        title = f"Merge PR #{pr}（{card}）"
    elif not isinstance(subject, str):
        raise MergeAuditError(f"提交标题非法：{subject!r}（应为 str 或 None）")
    elif not subject.strip():
        raise MergeAuditError(
            f"提交标题非法：{subject!r}（去首尾空白后为空，git 会把 Merge-By 行当标题）"
        )
    else:
        title = subject
    return (
        f"{title}\n"
        "\n"
        f"Merge-By: {by}\n"
        f"Merge-Card: {card}\n"
        f"Merge-PR: #{pr}\n"
    )


def _short(sha: str) -> str:
    """渲染用短 sha；空 sha（候选 message）打 ``-``。"""

    return sha[:7] if sha else "-"


def render_report(report: MergeReport) -> str:
    """把 ``MergeReport`` 渲染成人类可读文本（末行是结论，不打印退出码）。"""

    lines: list[str] = [
        "合并认账稽核（合并提交三字段）",
        f"仓库: {report.repo}",
        f"基线: {report.baseline}",
        f"区间: {report.rev}",
        f"判定（{len(report.records)} 条）:",
    ]
    if not report.records:
        lines.append("  （无）")
    for index, record in enumerate(report.records, start=1):
        lines.append(
            f"  {index}. {_short(record.sha)} {record.card or '-'} "
            f"{record.pr or '-'} by={record.by or '-'}"
        )

    lines.append(f"问题（{len(report.problems)} 处）:")
    if not report.problems:
        lines.append("  （无）")
    for index, problem in enumerate(report.problems, start=1):
        lines.append(f"  {index}. {_short(problem.sha)} {problem.kind}：{problem.detail}")

    lines.append(f"历史（未判定，{len(report.history)} 条）:")
    if not report.history:
        lines.append("  （无）")
    for index, record in enumerate(report.history, start=1):
        lines.append(
            f"  {index}. {_short(record.sha)} {record.card or '-'} "
            f"{record.pr or '-'} by=无法判定"
        )

    total = len(report.problems)
    lines.append("结论：无问题" if total == 0 else f"结论：发现 {total} 个问题")
    return "\n".join(lines)
