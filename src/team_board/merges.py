"""合并认账三字段：解析、校验、生成、渲染（纯逻辑层）+ 事后稽核取数（``audit``）。

本模块的 ``parse_trailers`` / ``record_from_commit`` / ``legacy_record`` /
``check_message`` / ``build_message`` / ``render_report`` 是**纯函数**：输入是
message 字符串与提交元数据，输出是 ``Problem`` / ``MergeRecord`` /
``MergeReport`` 与待提交的 message 文本，不调 git、不做文件 IO。

唯一的两处取数入口是 :func:`audit` 与 :func:`card_assignee`：前者调 ``git log``
扫合并提交、读基线文件，再把结果交给上面那些纯函数判定；后者**只读**打开看板库
取 ``tasks.assignee``（``mode=ro``，不建表、不写），供判据① 比对。git 调用层
（``_git``）在本模块自写一份，**不 import ``team_board.branches``** —— 两个子命令
各自封装，谁也不牵连谁。

三字段写在合并提交 message 的**末尾 trailer 块**（每行一条，键名大小写敏感、
**恒小写**）：

    merged-by: <profile>/run<NNN>
    merge-card: t_[0-9a-f]{8}
    merge-pr: #<正整数>

``Merge-By`` / ``Merge-Card`` / ``Merge-PR`` 这套旧的大写键**一律不再接受**，也
**不做大小写兼容别名**（两套口径并存会让「哪套算数」变成口头约定）：大写键在
trailer 块里不属于 ``TRAILER_KEYS``，于是被当成无关键忽略，三个小写键各报一条
``缺字段``。

判定口径
--------
1. ``parse_trailers`` 从 message 末尾往上扫：先跳过空行，再取连续的
   ``^[A-Za-z][A-Za-z0-9-]*: `` 形态行构成 trailer 块，遇到第一个不匹配的行即停。
   只挑 ``TRAILER_KEYS`` 里的键，其它键（``Signed-off-by`` 等）忽略且不算问题。
2. 三键之一出现在块外（正文里写了 ``merge-card: …``）→ ``字段未落在 trailer 区``；
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
8. ``audit`` 只对 ``<baseline>..<rev>`` 区间内的合并提交走 ``record_from_commit``
   （判定段）；``<baseline>`` 及其更早的合并另走 ``legacy_record``（历史段），
   **只回显不判定** —— 历史提交无法回填 trailer，拿 trailer 路径去判它们会让
   整段假红。历史段里 ``legacy_record`` 认不出的（``Merge branch ...``）也保留，
   记成 ``card=None`` 的记录，不许静默丢条目。
9. **判定对象 = first-parent 链上的合并提交**（两段取数都带 ``--first-parent``）。
   真正的认账事件只有一种：**把卡分支合进 main** 的那个合并提交 —— 它在 main 的
   first-parent 链上。分支侧 ``git merge origin/main`` 产生的同步合并（把 main
   并回卡分支）落在 second-parent 链上，它**既不合任何 PR，也本来就不该有**
   ``merge-card`` / ``merge-pr``：**既不判、也不计入** records / problems / 历史段。
   不加 ``--all-merges`` 之类的反向开关 —— 判定对象是口径，不是可选项。
10. 基线来源：``baseline`` 参数 > ``baseline_file`` 参数 > ``<repo>/.team-board/
    merge-audit-baseline``。文件不存在 / 不是 UTF-8 / 空 / 多行 / 不是 7–40 位
    小写十六进制 → ``MergeAuditError``（**不静默当成「没有基线」**）。
11. **判据①**：``merged-by`` 的 profile 部分（``/`` 之前）必须**严格等于**该卡
    ``tasks.assignee``。只在 ``audit(only_card=…)``（即 CLI 给了 ``--card``）时生效，
    且只判 ``source="trailer"`` 的记录 —— 历史段（``source="legacy"``）没有
    ``merged-by`` 值，照样只回显「by=无法判定」，不参与判据①。``assignee`` 由
    :func:`card_assignee` 只读打开看板库取；库不存在 / 不是合法 SQLite / 没有
    ``tasks`` 表 / 卡不存在 / 卡没有 assignee 一律 ``MergeAuditError``（rc2，
    **不静默跳过判据**）。``merged-by`` 值本身缺失或格式非法时，``缺字段`` /
    ``格式非法`` 已经报过，判据① 不再重复报第二条，输出行写「不适用」。
"""

from __future__ import annotations

import os
import re
import sqlite3

# bandit B404 假阳性：只用于调用固定的 git 可执行文件（见 _git），
# 调用形式为 argv 列表、shell=False，不拼 shell 字符串
import subprocess  # nosec B404
from dataclasses import dataclass, replace
from pathlib import Path

from .db import BoardError
from .db import connect as _connect_board
from .model import DEFAULT_DB

#: ``merged-by`` 取值：``<profile>/run<正整数>``，profile 为小写字母开头的短名
MERGE_BY_RE = re.compile(r"^[a-z][a-z0-9-]*/run[0-9]+$")

#: ``merge-card`` 取值：卡号 ``t_`` + 8 位小写十六进制
MERGE_CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")

#: ``merge-pr`` 取值：``#`` + 正整数（大数字一样合法，不设上限）
MERGE_PR_RE = re.compile(r"^#[1-9][0-9]*$")

#: 认账三字段的键名（顺序即渲染与校验顺序，大小写敏感、恒小写；
#: 旧的大写键 ``Merge-By`` / ``Merge-Card`` / ``Merge-PR`` 不再接受）
TRAILER_KEYS = ("merged-by", "merge-card", "merge-pr")

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

#: 判据①：``merged-by`` 的 profile 部分与该卡 ``tasks.assignee`` 不符
KIND_MERGED_BY_MISMATCH = "merged-by 与卡 assignee 不符"

#: ``audit(require=True)`` 追加的两类「该卡没有可认账的合并」问题
KIND_MISSING_HISTORY = "缺认账字段（历史，基线前）"
KIND_NO_MERGE = "缺认账字段"

#: 历史段命中的解释：基线之前的合并无法回填认账字段
DETAIL_MISSING_HISTORY = (
    "基线之前的合并无法回填认账字段；卡面只能写 PR/卡号/sha，merged-by 无法判定"
)

#: 两段都没命中（区间内没有该卡的合并，历史里也没有）
DETAIL_NO_MERGE = "历史与区间内都没有该卡的合并提交"

#: 基线文件默认路径（相对仓库根）：``<repo>/.team-board/merge-audit-baseline``
DEFAULT_BASELINE_FILE = Path(".team-board") / "merge-audit-baseline"

#: 基线文件内容：7–40 位小写十六进制 sha（与 ``branches.SHA_RE`` 同口径）
BASELINE_SHA_RE = re.compile(r"[0-9a-f]{7,40}")

#: ``git log`` 条目 / 字段分隔符（``--format`` 里用 ``%x1e`` ``%x1f`` 打出来）
_RS = "\x1e"
_US = "\x1f"

#: 判定段：sha / parents / subject / message 全文（``maxsplit`` 兜住 message 里的 ``\x1f``）
_JUDGE_FORMAT = "%x1e%H%x1f%P%x1f%s%x1f%B"

#: 历史段：只要 sha 与 subject（历史段不调 ``record_from_commit``，不取 message）
_HISTORY_FORMAT = "%x1e%H%x1f%s"

_FORMAT_BY_KEY = {
    "merged-by": MERGE_BY_RE,
    "merge-card": MERGE_CARD_RE,
    "merge-pr": MERGE_PR_RE,
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
    """一次合并认账稽核的结果。

    ``card`` / ``assignee`` 非 ``None`` 表示本次跑过判据①（CLI 给了 ``--card``）：
    ``card`` 是过滤用的卡号，``assignee`` 是从看板库读到的 ``tasks.assignee``。
    两个都默认 ``None``，其余调用方（不含判据① 的场景）构造 `MergeReport` 不用改。
    """

    repo: str
    baseline: str
    rev: str
    records: tuple[MergeRecord, ...]
    problems: tuple[Problem, ...]
    history: tuple[MergeRecord, ...]
    card: str | None = None
    assignee: str | None = None

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
        by=values["merged-by"],
        card=values["merge-card"],
        pr=values["merge-pr"],
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
        expected["merge-card"] = card
    if by is not None:
        expected["merged-by"] = by
    if pr is not None:
        expected["merge-pr"] = f"#{pr}"
    for key, want in expected.items():
        got = scan.values.get(key)
        if got is not None and got != want:
            problems.append(
                Problem(KIND_MISMATCH, "", f'{key} 提交里是 "{got}"，参数要求 "{want}"')
            )
    return tuple(problems)


def card_assignee(db_path: str | Path, card: str) -> str:
    """**只读**打开看板库取 ``tasks.assignee``（判据① 的对照值）。

    口径与 :mod:`team_board.db` 一致：``mode=ro`` 打开、不建表、不写任何东西
    （库文件调用前后字节不变）。SQL 固定为
    ``SELECT assignee FROM tasks WHERE id = ?``。

    以下情形一律抛 :class:`MergeAuditError`（CLI 转 rc2，**不静默跳过判据**）：
    卡号形态非法、库不存在 / 不是合法 SQLite / 没有 ``tasks`` 表、库里没有该卡、
    该卡的 ``assignee`` 是 ``NULL``（空 assignee 没法与任何 bot 严格相等，
    静默放过等于把判据变成永远通过）。
    """

    if not isinstance(card, str) or MERGE_CARD_RE.fullmatch(card) is None:
        raise MergeAuditError(f"卡号非法：{card!r}（应形如 t_1234abcd）")

    try:
        conn = _connect_board(db_path)
    except BoardError as exc:
        raise MergeAuditError(
            f"看板库不可读，无法做判据①（merged-by == 卡 assignee）：{exc}"
        ) from exc
    try:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (card,)).fetchone()
    except sqlite3.Error as exc:
        raise MergeAuditError(
            f"读看板库 {db_path} 的 tasks 表失败，无法做判据①（merged-by == 卡 assignee）："
            f"{exc}"
        ) from exc
    finally:
        conn.close()

    if row is None:
        raise MergeAuditError(
            f"看板库 {db_path} 里没有卡 {card}（判据① 需要该卡的 assignee，不静默跳过）"
        )
    assignee = row[0]
    if not isinstance(assignee, str) or not assignee:
        raise MergeAuditError(
            f"看板库 {db_path} 里卡 {card} 的 assignee 为空（{assignee!r}），"
            "无法与 merged-by 严格比对"
        )
    return assignee


def check_merged_by(record: MergeRecord, assignee: str) -> tuple[Problem, ...]:
    """判据①：``record.by`` 的 profile 部分（``/`` 之前）**严格等于** ``assignee``。

    空元组 = 通过。两条不判（返回空元组）：

    - ``record.source != "trailer"``：历史段没有 ``merged-by`` 值，不参与判据①；
    - ``record.by is None``：值缺失 / 格式非法已经由 ``缺字段`` / ``格式非法``
      报过，这里不重复报第二条（同一件事报两次会让问题数虚高）。
    """

    if record.source != "trailer" or record.by is None:
        return ()
    profile = record.by.partition("/")[0]
    if profile == assignee:
        return ()
    return (
        Problem(
            KIND_MERGED_BY_MISMATCH,
            record.sha,
            f"merged-by={record.by} assignee={assignee}",
        ),
    )


def build_message(*, card: str, by: str, pr: int, subject: str | None = None) -> str:
    """生成带认账块的合并提交 message（恰好三段：标题 / 空行 / 三行 trailer）。

    参数先校验，非法一律抛 :class:`MergeAuditError`（CLI 转 rc2），不把原生
    ``TypeError`` / ``AttributeError`` 漏给调用方。类型与形态分开判：非 ``str``
    先挡（``None`` / ``int`` 直接进正则会抛 ``TypeError``），再用 ``fullmatch``
    判形态 —— ``re.match`` + ``$`` 会放过结尾多一个 ``\\n`` 的值，生成出来的
    message 再喂 :func:`check_message` 就报「字段未落在 trailer 区」，闭环断掉。

    ``subject`` 去首尾空白后为空同样抛错：那样第一行是空行，git 的 ``%s`` 会把
    ``merged-by:`` 当提交标题。``subject=None`` 走契约默认标题。
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
            f"提交标题非法：{subject!r}（去首尾空白后为空，git 会把 merged-by 行当标题）"
        )
    else:
        title = subject
    return (
        f"{title}\n"
        "\n"
        f"merged-by: {by}\n"
        f"merge-card: {card}\n"
        f"merge-pr: #{pr}\n"
    )


#: 会被子进程继承、把 git 指向「另一个仓库 / 另一个索引 / 另一套对象库」的定位类
#: 环境变量。调用 git 前必须剔除：``git -C <path>`` **只切目录，不隔离这些变量**
#: —— 在 git hook（hook 期间 git 会设 ``GIT_DIR``）、``git bisect run``、
#: ``git rebase --exec``、包装过的 IDE / CI 里调本命令时，父进程设的 ``GIT_DIR``
#: 会让 ``--repo`` 形同虚设（报头写 A 仓库，实际审 B 仓库，给出假绿）。逐项理由：
#: ``GIT_DIR`` / ``GIT_WORK_TREE`` / ``GIT_COMMON_DIR`` 换仓库与工作树；
#: ``GIT_INDEX_FILE`` / ``GIT_OBJECT_DIRECTORY`` / ``GIT_ALTERNATE_OBJECT_DIRECTORIES``
#: 换索引与对象库（读到的提交不是 ``<path>`` 的）；``GIT_NAMESPACE`` / ``GIT_PREFIX``
#: 换 ref 命名空间与相对路径前缀；``GIT_CEILING_DIRECTORIES`` /
#: ``GIT_DISCOVERY_ACROSS_FILESYSTEM`` 改仓库发现边界（能让「不是仓库」变成「是仓库」）。
#:
#: 只剔「定位」这一类，其余变量（``PATH`` / ``HOME`` / ``LANG`` / 代理 /
#: ``GIT_SSH*`` 等）**原样保留** —— 图省事写成 ``env={}`` 会把 ``PATH`` 一起抹掉，
#: git 直接找不到。
_GIT_LOCATION_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
)


def _git_env() -> dict[str, str]:
    """``os.environ`` 的一份副本，剔掉 :data:`_GIT_LOCATION_ENV` 里的定位变量。

    ``subprocess.run(env=...)`` 传 ``None`` 等于继承父进程全部环境（含 ``GIT_DIR``），
    传 ``{}`` 又等于清空（``PATH`` 没了）；所以这里显式构造「剔除后的副本」。
    """

    return {
        key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_ENV
    }


def _git(repo: str | os.PathLike[str], *args: str) -> subprocess.CompletedProcess[str]:
    """在 ``repo`` 里跑 git，不抛 ``CalledProcessError``，退出码交给调用方判。

    两类环境级错误一律 ``MergeAuditError``（CLI 转 rc2），不许冒 traceback：

    - ``PATH`` 里没有 ``git``（``FileNotFoundError``）或其它 ``OSError``；
    - git 输出**不是 UTF-8**：``i18n.logOutputEncoding=GBK`` 下 ``git log``
      会把提交标题转码输出，``core.quotePath=false`` 下非 UTF-8 提交信息原样
      出字节，``subprocess`` 的严格解码于是抛 ``UnicodeDecodeError``（``ValueError``
      系，不是 ``OSError``，会直接逃出去）。rc1 是「有问题」的专用码，崩溃必须
      与它分开 —— 解码失败说明整轮稽核没法进行，属环境错误。

    解码固定 UTF-8（不随 ``locale`` 漂移），同一仓库在任何机器上得到同样的 rc。
    """

    try:
        # bandit B603/B607 假阳性：可执行文件名写死为 "git"（走 PATH 以跨平台一致）；
        # 参数以 argv 列表传入、shell=False（默认），仓库路径/rev 只作为独立 argv
        # 元素，不经 shell 解释，无注入面
        #
        # env=_git_env()：显式传「剔除 git 定位类变量后的副本」，否则 hook /
        # bisect / rebase --exec 里继承来的 GIT_DIR 会让 -C <repo> 失效，
        # 静默去审另一个仓库并返回假绿（见 _GIT_LOCATION_ENV）
        return subprocess.run(  # nosec B603 B607
            ["git", "-C", os.fspath(repo), *args],
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise MergeAuditError(
            "找不到 git 可执行文件（PATH 里没有 git），无法做合并认账稽核"
        ) from exc
    except UnicodeDecodeError as exc:
        raise MergeAuditError(
            f"git 输出不是 UTF-8，无法解析（第 {exc.start} 字节起解码失败；"
            "常见原因：仓库配置 i18n.logOutputEncoding=GBK 或 "
            "core.quotePath=false 且提交信息含非 UTF-8 字节；"
            "可执行 git config --unset i18n.logOutputEncoding 后重试）"
        ) from exc
    except OSError as exc:
        raise MergeAuditError(f"无法执行 git: {exc}") from exc


def _git_ok(repo: str | os.PathLike[str], *args: str, what: str) -> str:
    """跑 git，非 0 退出即 ``MergeAuditError``（环境级错误）。"""

    proc = _git(repo, *args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {proc.returncode}"
        raise MergeAuditError(f"{what}: {tail}")
    return proc.stdout


def _resolve_repo(repo: str | Path) -> Path:
    """校验仓库路径并解析成工作树根（空串 / 不存在 / 不是目录 / 不是工作树 → 报错）。"""

    if not isinstance(repo, (str, Path)):
        raise MergeAuditError(f"仓库路径非法：{repo!r}（应为 str 或 Path）")
    raw = os.fspath(repo)
    if not raw.strip():
        raise MergeAuditError("仓库路径为空，无法稽核")
    path = Path(raw)
    if not path.exists():
        raise MergeAuditError(f"仓库路径不存在：{raw}")
    if not path.is_dir():
        raise MergeAuditError(f"仓库路径不是目录：{raw}")
    toplevel = _git_ok(path, "rev-parse", "--show-toplevel", what=f"不是 git 工作树：{raw}")
    return Path(toplevel.strip())


def _resolve_commit(repo: str | Path, value: str, what: str) -> str:
    """把 ``value`` 解析成 40 位提交 sha；解析不了（含空串）即 ``MergeAuditError``。"""

    if not isinstance(value, str):
        raise MergeAuditError(f"{what}非法：{value!r}（应为 str）")
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}")
    if proc.returncode != 0:
        raise MergeAuditError(f"{what}无法解析为提交：{value!r}")
    return proc.stdout.strip()


def _read_baseline_file(path: Path) -> str:
    """读基线文件并校验形态；不存在 / 非 UTF-8 / 空 / 多行 / 格式非法一律报错。

    **不静默**：文件不存在不许当成「没有基线」—— 那会让一条本该红的稽核变绿。
    """

    if not path.exists():
        raise MergeAuditError(
            f"基线文件不存在：{path}（用 --baseline 指定基线提交，"
            "或先按 CI 的方式写入该文件）"
        )
    if not path.is_file():
        raise MergeAuditError(f"基线文件不是普通文件：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MergeAuditError(
            f"基线文件 {path} 不是 UTF-8 文本（第 {exc.start} 字节起解码失败）"
        ) from exc
    except OSError as exc:  # 权限、路径失效等
        raise MergeAuditError(f"无法读取基线文件 {path}: {exc}") from exc

    value = text.strip()
    if not value:
        raise MergeAuditError(f"基线文件 {path} 是空文件（基线必填，不静默放过）")
    if "\n" in value or "\r" in value:
        raise MergeAuditError(f"基线文件 {path} 含多行内容，只允许一行 sha")
    if BASELINE_SHA_RE.fullmatch(value) is None:
        raise MergeAuditError(
            f"基线文件 {path} 的内容不是 7–40 位小写十六进制 sha：{value!r}"
        )
    return value


def _log_entries(text: str, fields: int, what: str) -> list[list[str]]:
    """把 ``git log --format`` 输出切成条目（``\\x1e`` 分条、``\\x1f`` 分字段）。"""

    entries: list[list[str]] = []
    for chunk in text.split(_RS):
        if not chunk.strip():
            continue
        parts = chunk.split(_US, fields - 1)
        if len(parts) != fields:
            raise MergeAuditError(
                f"{what}：git log 输出格式不符（期望 {fields} 段，实际 {len(parts)} 段）"
            )
        entries.append(parts)
    return entries


def audit(
    repo: str | Path = ".",
    *,
    baseline: str | None = None,
    baseline_file: str | Path | None = None,
    rev: str = "HEAD",
    only_card: str | None = None,
    require: bool = False,
    db_path: str | Path | None = None,
) -> MergeReport:
    """稽核 ``<baseline>..<rev>`` 区间内合并提交的认账三字段。

    判定段（区间内、**first-parent 链上**）走 :func:`record_from_commit`，逐条出 ``Problem``；
    历史段（``<baseline>`` 及更早，同样只取 first-parent 链）只走
    :func:`legacy_record` 回显，**不判定**。second-parent 侧的同步合并（把 main
    并回卡分支）两段都不进。

    参数/环境级错误一律抛 :class:`MergeAuditError`（CLI 转 rc2），检查顺序：
    仓库 → ``baseline``/``baseline_file`` 互斥 → ``only_card`` 形态 →
    ``require`` 依赖 → 判据① 的 ``assignee``（读看板库）→ 基线取值与解析 →
    ``rev`` 解析。

    ``only_card`` 只过滤两段的**记录清单**，不吞掉已判出的问题（有问题的合并
    即使不属于该卡也照样红）。``require=True`` 时：区间内有该卡零问题的
    ``source="trailer"`` 记录 → 通过；只有历史段命中 → 追加
    ``缺认账字段（历史，基线前）``；两段都没命中 → 追加 ``缺认账字段``。

    判据①（``merged-by`` 的 profile == 该卡 ``tasks.assignee``）只在给了
    ``only_card`` 时跑：``db_path`` 为 ``None`` 时用 :data:`team_board.model.DEFAULT_DB`，
    读失败一律 rc2（**不因为没传 db 就静默跳过判据**）。判据① 出的问题在
    ``require`` 之前并入 ``problems``，于是「``merged-by`` 不符」的合并也不算
    「零问题的认账合并」。
    """

    root = _resolve_repo(repo)

    if baseline is not None and baseline_file is not None:
        raise MergeAuditError("--baseline 与 --baseline-file 只能给一个（基线来源必须唯一）")
    if only_card is not None and (
        not isinstance(only_card, str) or MERGE_CARD_RE.fullmatch(only_card) is None
    ):
        raise MergeAuditError(f"卡号非法：{only_card!r}（应形如 t_1234abcd）")
    if require and only_card is None:
        raise MergeAuditError(
            "--require 必须与 --card 同用（要判定「某张卡有没有认账合并」）"
        )

    assignee: str | None = None
    if only_card is not None:
        assignee = card_assignee(DEFAULT_DB if db_path is None else db_path, only_card)

    if baseline is None:
        source = (
            Path(baseline_file)
            if baseline_file is not None
            else root / DEFAULT_BASELINE_FILE
        )
        baseline = _read_baseline_file(source)
    baseline_sha = _resolve_commit(root, baseline, "基线")
    rev_sha = _resolve_commit(root, rev, "区间终点")

    judged: list[MergeRecord] = []
    problems: list[Problem] = []
    # --first-parent：只判「把分支合进 main」的合并提交。分支侧 ``git merge
    # origin/main`` 产生的同步合并落在 second-parent 链上，它没有合任何 PR、
    # 本来就不该有 merge-card/merge-pr，判它必假红（t_d22c6399）。同一条参数
    # 也用在下面的历史段，否则历史段仍在数同步合并。
    judged_text = _git_ok(
        root,
        "log",
        "--merges",
        "--first-parent",
        f"--format={_JUDGE_FORMAT}",
        f"{baseline_sha}..{rev_sha}",
        what="无法读取区间内的合并提交",
    )
    for sha, parents_raw, subject, message in _log_entries(judged_text, 4, "判定段"):
        # parents 必须由 "%P" 的字符串 split 出来：传裸 40 字符字符串会被当成
        # 40 个单字符父提交，单父提交就会被静默当成合并提交收下
        record, record_problems = record_from_commit(
            sha, subject, tuple(parents_raw.split()), message
        )
        judged.append(record)
        problems.extend(record_problems)

    history: list[MergeRecord] = []
    history_text = _git_ok(
        root,
        "log",
        "--merges",
        "--first-parent",
        f"--format={_HISTORY_FORMAT}",
        baseline_sha,
        what="无法读取基线之前的合并提交",
    )
    for sha, subject in _log_entries(history_text, 2, "历史段"):
        legacy = legacy_record(sha, subject)
        if legacy is None:
            # 认不出的历史合并（如 "Merge branch ..."）也要保留，历史段必须完整
            legacy = MergeRecord(
                sha=sha,
                subject=subject,
                parents=(),
                by=None,
                card=None,
                pr=None,
                source="legacy",
            )
        history.append(legacy)

    if only_card is not None:
        judged = [record for record in judged if record.card == only_card]
        history = [record for record in history if record.card == only_card]

    # 判据①：只在给了 --card 时跑，且只判 trailer 记录（历史段没有该字段的值）。
    # 必须放在 require 之前 —— 否则「merged-by 不符」的合并会被 require 当成
    # 「零问题的认账合并」放过。
    if assignee is not None:
        for record in judged:
            problems.extend(check_merged_by(record, assignee))

    if require:
        bad_shas = {problem.sha for problem in problems}
        clean_trailer = [
            record
            for record in judged
            if record.source == "trailer" and record.sha not in bad_shas
        ]
        if not clean_trailer:
            if history:
                problems.append(
                    Problem(
                        KIND_MISSING_HISTORY,
                        history[0].sha,
                        DETAIL_MISSING_HISTORY,
                    )
                )
            else:
                problems.append(Problem(KIND_NO_MERGE, "", DETAIL_NO_MERGE))

    return MergeReport(
        repo=str(root),
        baseline=baseline_sha,
        rev=rev_sha,
        records=tuple(judged),
        problems=tuple(problems),
        history=tuple(history),
        card=only_card,
        assignee=assignee,
    )


def _short(sha: str) -> str:
    """渲染用短 sha；空 sha（候选 message）打 ``-``。"""

    return sha[:7] if sha else "-"


def _judgement_one_lines(report: MergeReport) -> list[str]:
    """判据① 的输出行（只在给了 ``--card`` 时打印）。

    格式固定（后续 CI / 文档会引用）：

        判据①（merged-by == 卡 assignee）：卡 t_xxxxxxxx assignee=<x>，merged-by=<原值> → PASS

    一条 trailer 记录一行；``merged-by`` 缺失或格式非法时该行写「不适用」——
    那种情况已经由「缺字段 / 格式非法」计进问题段，判据① 不重复报。
    """

    card = report.card
    assignee = report.assignee
    if card is None or assignee is None:
        return []
    prefix = f"判据①（merged-by == 卡 assignee）：卡 {card} assignee={assignee}，"
    trailer_records = [
        record for record in report.records if record.source == "trailer"
    ]
    if not trailer_records:
        return [f"{prefix}区间内无该卡的 trailer 记录 → 不适用"]
    failed = {
        problem.sha
        for problem in report.problems
        if problem.kind == KIND_MERGED_BY_MISMATCH
    }
    lines: list[str] = []
    for record in trailer_records:
        by = record.by if record.by is not None else "-"
        if record.sha in failed:
            verdict = "FAIL"
        elif record.by is None:
            verdict = "不适用（merged-by 缺失或非法，见问题段）"
        else:
            verdict = "PASS"
        lines.append(f"{prefix}merged-by={by} → {verdict}")
    return lines


def render_report(report: MergeReport) -> str:
    """把 ``MergeReport`` 渲染成人类可读文本（末行是结论，不打印退出码）。

    有 ``--card``（``report.card`` 非 ``None``）时在判定段之后插一段「判据①」；
    无 ``--card`` 时该段整段不打印。
    """

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

    lines.extend(_judgement_one_lines(report))

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
