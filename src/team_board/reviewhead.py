"""双问结论「是否绑定某份内容」的判定：纯函数 + 唯一取数入口。

R18 的机器判据本体（只读）。判的是**证据结构**，不判结论对错。结论绑的是
**内容指纹**（``patch-id -U0``，见 :data:`FINGERPRINT_ALGO`），不是 head sha：纯
rebase 不改内容 → 结论继续可用；内容任何改动 → 立即失效。

- 取数：``gh api repos/alsj213/team-board/pulls/<N>/reviews`` 与
  ``issues/<N>/comments``（``--paginate``），**只看 body**，其它字段一概不读；
- 标记：同一条正文里**顶格**出现 ``REVIEW-KIND`` 与「``REVIEW-PATCH`` 或
  ``REVIEW-HEAD``」才算一条标记 ——

      REVIEW-PATCH: <40 位小写十六进制>      内容指纹（新写法，两种都在时优先）
      REVIEW-HEAD: <40 或 64 位小写十六进制>  历史 head sha（只读兼容，不追溯）
      REVIEW-KIND: defect|regression

  三条正则 :data:`PATCH_RE` / :data:`HEAD_RE` / :data:`KIND_RE` 是唯一来源
  （``re.MULTILINE``，行首行尾都锚死：缩进、行尾多空格、``REVIEW-…:`` 前后带别的字
  都不算）。同一条正文只取每条正则的第一个匹配；只有 ``REVIEW-KIND`` 不算一条标记。

四态（判定见 :func:`judge`；``resolve(sha) -> 指纹 | None`` 可注入，单测不碰 git）
------------------------------------------------------------------------------
- ``REVIEW-PATCH`` == current → ``OK``（计入 kinds）；
- ``REVIEW-PATCH`` != current → ``STALE``（不计入）；
- 只有 ``REVIEW-HEAD``，对象 / 指纹取不到 → ``UNVERIFIABLE``（不计入）；
- 只有 ``REVIEW-HEAD``，其内容指纹 == current → ``LEGACY-OK``（计入）；
- 只有 ``REVIEW-HEAD``，其内容指纹 != current（或算不出 == kind！=patch）→ ``STALE``。

一条标记 = 对「某份内容」的结论：值就是内容指纹，旧标记用它的 sha 现算指纹；内容
等价即结论可用，内容任何变化即失效。历史评论只读 —— 不改写、不补标记。

判定口径
--------
1. 内容指纹上 ``defect`` 与 ``regression`` 各 ≥1 → rc0（绑定成立）；
2. 缺一类 / 两类都缺 / 标记都指向别的内容（= 内容变了但双问没重跑）→ rc1；
3. 参数形态坏（``--head`` 不是 40/64 位小写十六进制、``--pr`` 不是正整数）、``gh``
   调用失败、git 侧失败（非 0 / 超时 / PATH 里没有 / 输出不是 UTF-8）→ rc2。

rc2 一律在取数与判定阶段抛出（:class:`ReviewHeadError` / :class:`PatchIdError`），所以
stdout 一个字节都不会写（报告整块在取数与判定都成功之后才拼）。

**不判结论对错**：正文里写「阻塞 / 非阻塞」是二层的事，本模块只认上面几行标记；
也**没有** ``--skip`` 之类开关，缺证据就是 rc1。

兼容形态（临时）：``analyze`` / ``render`` 收到的是 head sha 字符串而非
:class:`Fingerprint` 时，走「按 sha 判」的旧口径与旧渲染 —— 只为让 ``cli.py`` /
``merge_guard.py`` 的既有调用点（E3 / E4 接线前的状态）不炸；接线完成后这两条分支
应删除，本模块从此只有指纹口径。
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

#: 唯一仓库（``gh api`` 的路径前缀）
REPO = "alsj213/team-board"

#: 两类双问（顺序即结论行里 ``缺`` 的枚举顺序）
KINDS = ("defect", "regression")

#: ``REVIEW-PATCH:`` 行：内容指纹（MULTILINE、顶格、行尾锚死；只认 40 位小写十六进制）
PATCH_RE = re.compile(r"^REVIEW-PATCH: ([0-9a-f]{40})$", re.M)

#: ``REVIEW-HEAD:`` 行（MULTILINE、顶格、行尾锚死）
HEAD_RE = re.compile(r"^REVIEW-HEAD: ([0-9a-f]{40}|[0-9a-f]{64})$", re.M)

#: ``REVIEW-KIND:`` 行（同上）
KIND_RE = re.compile(r"^REVIEW-KIND: (defect|regression)$", re.M)

#: 四态（:func:`judge` 的唯一取值域；排序即报告里的展示顺序无关，逐条按标记原序）
STATE_OK = "OK"
STATE_LEGACY_OK = "LEGACY-OK"
STATE_STALE = "STALE"
STATE_UNVERIFIABLE = "UNVERIFIABLE"

#: 计入 ``kinds_on_patch`` 的两种状态（内容等价：直接命中或历史标记按内容复用）
STATES_COUNTED = frozenset({STATE_OK, STATE_LEGACY_OK})

#: ``--head`` 的形态：40 或 64 位小写十六进制（41-63 位也在这一档被拒）
HEAD_SHAPE_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}", re.ASCII)

#: ``--pr`` 的形态：ASCII 十进制正整数（``int()`` 会收下 ``" 1"`` / ``"+1"`` /
#: ``"1_0"`` / 阿拉伯-印度数字，所以先用字符串判）
PR_SHAPE_RE = re.compile(r"[1-9][0-9]*", re.ASCII)

#: ``--pr`` 上限：与 ``cli.MAX_MERGE_PR`` 同值（同一口径：9 位远超真实仓库；
#: 13 位毫秒与天文数字属「填错了字段」）。单测锁住两边相等，防漂移。
PR_MAX = 999_999_999

#: 单次 ``gh api`` 的硬超时（秒）：一次卡住的 gh 不许把整张卡拖到 worker 超时
GH_TIMEOUT_SECONDS = 120

#: 结论行（五种，逐字；渲染与测试共用同一份文案）
CONCLUSION_OK = "结论: 内容指纹 {short} 的 defect + regression 各 >=1 条 → 绑定成立"
CONCLUSION_MISSING = "结论: 内容指纹 {short} 缺 {missing} → 内容变了要重跑双问"
CONCLUSION_NO_MARK = "结论: 无结构化标记 → 该 PR 的双问不可机器绑定（按缺证据处理）"
CONCLUSION_BINARY = (
    "结论: 分支含二进制文件 {paths} → 内容指纹不覆盖二进制（patch-id 盲），按不可绑定处理"
)
CONCLUSION_EMPTY = "结论: 分支相对 {base} 无差异 → 无可绑定内容（不许当 PASS）"

#: 兼容形态（收到 head sha 而不是指纹）专用的旧文案：只给 E3 / E4 接线前的
#: ``cli.py`` / ``merge_guard.py`` 调用点用，接线后随同兼容分支一起删除。
LEGACY_CONCLUSION_OK = "结论: head {short} 的 defect + regression 各 >=1 条 → 绑定成立"
LEGACY_CONCLUSION_MISSING = "结论: head {short} 缺 {missing} → head 变了要重跑双问"


class ReviewHeadError(Exception):
    """参数或环境错误（CLI 转 rc2：stderr 一行 ``错误: …``、stdout 空）。"""


@dataclass(frozen=True)
class Mark:
    """一条正文里的结构化标记（值已通过形态校验）。

    ``patch`` 是 ``REVIEW-PATCH`` 的 40 hex 内容指纹，``head`` 是 ``REVIEW-HEAD`` 的
    40/64 hex 历史 head sha（两者都在时**以 ``patch`` 为准**，``head`` 只留痕）。``state``
    ∈ :data:`STATE_OK` / :data:`STATE_LEGACY_OK` / :data:`STATE_STALE` /
    :data:`STATE_UNVERIFIABLE`，由 :func:`judge` 填；``resolved`` 是旧标记现算出的内容
    指纹（``LEGACY-OK`` / 旧标记 ``STALE`` 时有值，其余 ``None``）。

    :func:`parse_marks` 只出形态（``state=""``、``resolved=None``）—— 判定归
    :func:`judge`，两件事分开才好在单测里注入 ``resolve`` 替身。
    """

    kind: str
    patch: str | None
    head: str | None
    state: str = ""
    resolved: str | None = None

    @property
    def value(self) -> str | None:
        """这条标记指向的内容值：``REVIEW-PATCH`` 优先，否则退回历史 head sha。"""

        return self.patch if self.patch is not None else self.head

    @property
    def short(self) -> str:
        """前 12 位（输出行用它，避免把 40/64 位值铺满一行）。"""

        return (self.value or "")[:12]


@dataclass(frozen=True)
class Report:
    """一次判定的全部结果：当前内容指纹、扫描条数、标记、已齐的 kind、缺的 kind。"""

    #: 当前内容指纹（:class:`Fingerprint`）；兼容形态（按 sha 判）时为 ``None``
    current: Fingerprint | None
    bodies_scanned: int
    marks: tuple[Mark, ...]
    #: 内容指纹上已齐的 kind（只算 ``OK`` / ``LEGACY-OK`` 的标记）
    kinds_on_patch: frozenset[str]
    missing: tuple[str, ...]
    #: 兼容形态的 head sha（指纹形态为空串，只为旧调用点渲染用）
    head: str = ""
    #: 仅用于空 diff 结论行点名 base
    base: str = ""

    @property
    def kinds_on_head(self) -> frozenset[str]:
        """旧名别名（``cli.py`` / ``merge_guard.py`` 仍在用；E3 / E4 接线后删）。"""

        return self.kinds_on_patch

    @property
    def ok(self) -> bool:
        """两类都在当前内容指纹上 ≥1 条才算绑定成立（= CLI rc0）。"""

        return not self.missing


def check_head(value: object) -> str:
    """``--head`` 形态校验：40 或 64 位小写十六进制，否则 :class:`ReviewHeadError`。"""

    if not isinstance(value, str) or HEAD_SHAPE_RE.fullmatch(value) is None:
        raise ReviewHeadError(
            f"--head 非法：{value!r}（应为 40 或 64 位小写十六进制的提交 sha）"
        )
    return value


def check_pr(value: object) -> int:
    """``--pr`` 形态校验：1-``PR_MAX`` 的 ASCII 十进制正整数。"""

    if not isinstance(value, str) or PR_SHAPE_RE.fullmatch(value) is None:
        raise ReviewHeadError(
            f"PR 号非法：{value!r}（应为 1-{PR_MAX} 的 ASCII 十进制正整数）"
        )
    pr = int(value)
    if pr > PR_MAX:
        raise ReviewHeadError(
            f"PR 号超出上限 {PR_MAX}：{value!r}（13 位毫秒与天文数字都不合法）"
        )
    return pr


def parse_marks(bodies: Iterable[str]) -> tuple[Mark, ...]:
    """逐条正文抽标记：``REVIEW-KIND`` + (``REVIEW-PATCH`` 或 ``REVIEW-HEAD``) 才算一条。

    同一条正文只取每条正则的**首个**匹配；两种值行都在时以 ``REVIEW-PATCH`` 为准
    （``head`` 照样留痕，判定不看它）。只有 ``REVIEW-KIND``（或只有值行）不算标记。
    返回的 ``state`` 是空串 —— 判定归 :func:`judge`。
    """

    marks: list[Mark] = []
    for body in bodies:
        text = body or ""
        kind = KIND_RE.search(text)
        if kind is None:
            continue
        patch = PATCH_RE.search(text)
        head = HEAD_RE.search(text)
        if patch is None and head is None:
            continue
        marks.append(
            Mark(
                kind=kind.group(1),
                patch=patch.group(1) if patch is not None else None,
                head=head.group(1) if head is not None else None,
            )
        )
    return tuple(marks)


def judge(
    marks: Sequence[Mark],
    current: Fingerprint,
    *,
    resolve: Callable[[str], Fingerprint | None] | None,
) -> tuple[Mark, ...]:
    """四态判定（表见模块 docstring）。

    ``resolve(sha)`` 把一条历史 head sha 现算成内容指纹：注入替身即可脱离 git（单测
    不碰网络），:func:`analyze` 注入的是 :func:`fingerprint_of_commit`。同一个 sha 在
    一轮判定里只算一次（memo）—— 一条 PR 有十几条评论时不必重复跑 git。

    - ``REVIEW-PATCH``：不调用 ``resolve``，与 ``current.value`` 逐字比；
    - 只有 ``REVIEW-HEAD``：``resolve`` 为 ``None``（没有仓库上下文）或返回 ``None`` /
      非 ``patch`` 指纹 → ``UNVERIFIABLE``；否则按指纹相等与否给 ``LEGACY-OK`` / ``STALE``。
    """

    memo: dict[str, Fingerprint | None] = {}
    judged: list[Mark] = []
    for mark in marks:
        if mark.patch is not None:
            matched = current.value is not None and mark.patch == current.value
            judged.append(
                replace(mark, state=STATE_OK if matched else STATE_STALE, resolved=None)
            )
            continue
        sha = mark.head or ""
        if sha in memo:
            fp = memo[sha]
        else:
            fp = resolve(sha) if resolve is not None else None
            memo[sha] = fp
        if fp is None or fp.kind != "patch" or fp.value is None:
            judged.append(replace(mark, state=STATE_UNVERIFIABLE, resolved=None))
            continue
        state = STATE_LEGACY_OK if fp.value == current.value else STATE_STALE
        judged.append(replace(mark, state=state, resolved=fp.value))
    return tuple(judged)


def _report_from(
    current: Fingerprint | None,
    marks: tuple[Mark, ...],
    bodies_scanned: int,
    *,
    head: str = "",
    base: str = "",
) -> Report:
    """按判定后的标记算 ``kinds_on_patch`` / ``missing``（``missing`` 顺序固定）。"""

    kinds = frozenset(mark.kind for mark in marks if mark.state in STATES_COUNTED)
    missing = tuple(kind for kind in KINDS if kind not in kinds)
    return Report(
        current=current,
        bodies_scanned=bodies_scanned,
        marks=marks,
        kinds_on_patch=kinds,
        missing=missing,
        head=head,
        base=base,
    )


def _unverifiable_report(
    current: Fingerprint | None,
    bodies: Sequence[str],
    *,
    head: str = "",
    base: str = "",
) -> Report:
    """内容指纹不可用（二进制 / 空 diff / 当前对象取不到）：两类全缺，标记一律不可验证。"""

    marks = tuple(
        replace(mark, state=STATE_UNVERIFIABLE, resolved=None)
        for mark in parse_marks(bodies)
    )
    return _report_from(current, marks, len(bodies), head=head, base=base)


def _legacy_report(head: str, bodies: Sequence[str]) -> Report:
    """兼容形态（没有仓库上下文）：按 sha 比，等价于接线前的旧口径。"""

    marks = tuple(
        replace(
            mark,
            state=(
                STATE_LEGACY_OK
                if mark.patch is None and mark.head == head
                else STATE_STALE
            ),
            resolved=None,
        )
        for mark in parse_marks(bodies)
    )
    return _report_from(None, marks, len(bodies), head=head)


def analyze(
    current: Fingerprint | str,
    bodies: Sequence[str],
    *,
    repo: str | Path | None = None,
    base: str | None = None,
    remote: str = "origin",
) -> Report:
    """判定「某份内容」上双问是否齐。

    ``current`` 是 :class:`Fingerprint`（正常形态，由调用方用 :func:`fingerprint` /
    :func:`fingerprint_of_commit` 算好）；兼容形态传 head sha 字符串（``repo`` /
    ``base`` 都没给时按 sha 判、走旧渲染 —— 见模块 docstring 末段）。

    - ``current.kind`` 不是 ``"patch"``（``binary`` / ``empty``）→ 两类全缺：二进制
      patch-id 盲、空 diff 没有可比内容，都不许当 PASS；标记一律 ``UNVERIFIABLE``；
    - 其余 → :func:`parse_marks` + :func:`judge`（``resolve`` =
      :func:`fingerprint_of_commit`；没给 ``repo`` / ``base`` 时旧标记只能 ``UNVERIFIABLE``）。
    """

    if isinstance(current, str):
        if repo is None or base is None:
            return _legacy_report(current, bodies)
        fp = fingerprint_of_commit(repo, base, current, remote)
        if fp is None:
            # 取不到对象 = 算不出当前指纹：旧标记全 UNVERIFIABLE，两类全缺（rc1 语义）
            return _unverifiable_report(None, bodies, head=current, base=base)
        head_sha, base_ref = current, base
    else:
        fp, head_sha, base_ref = current, "", (base or "")

    if fp.kind != "patch" or fp.value is None:
        return _unverifiable_report(fp, bodies, head=head_sha, base=base_ref)

    resolver: Callable[[str], Fingerprint | None] | None = None
    if repo is not None and base is not None:
        resolve_repo, resolve_base = repo, base

        def _resolve(sha: str) -> Fingerprint | None:
            return fingerprint_of_commit(resolve_repo, resolve_base, sha, remote)

        resolver = _resolve

    return _report_from(
        fp, judge(parse_marks(bodies), fp, resolve=resolver), len(bodies),
        head=head_sha, base=base_ref,
    )


def render(report: Report) -> str:
    """整块报告（指纹形态）。

    首行 ``algo=<FINGERPRINT_ALGO> current=<前12> mb=<前12>``，第二行扫描条数，然后每条
    ``<state> <pid12|head12> <kind>``，最后一行结论。``report.current`` 为 ``None`` 时是
    兼容形态（按 sha 判），走旧渲染（旧调用点的输出保持逐字不变）。
    """

    if report.current is None:
        return _legacy_render(report)

    current = report.current
    lines = [
        f"algo={FINGERPRINT_ALGO} current={current.value[:12] if current.value else '-'}"
        f" mb={current.merge_base[:12]}",
        f"扫描正文 {report.bodies_scanned} 条；带标记行 {len(report.marks)} 条",
    ]
    for mark in report.marks:
        lines.append(f"  {mark.state} {mark.short} {mark.kind}")
    short = (current.value or report.head or "")[:12]
    if current.kind == "binary":
        lines.append(CONCLUSION_BINARY.format(paths=",".join(current.paths)))
    elif current.kind == "empty":
        lines.append(CONCLUSION_EMPTY.format(base=report.base or "base"))
    elif not report.marks:
        lines.append(CONCLUSION_NO_MARK)
    elif report.missing:
        lines.append(CONCLUSION_MISSING.format(short=short, missing=",".join(report.missing)))
    else:
        lines.append(CONCLUSION_OK.format(short=short))
    return "\n".join(lines)


def _legacy_render(report: Report) -> str:
    """兼容形态的旧渲染（逐字保持接线前的输出，旧调用点测试不破）。"""

    short = report.head[:12]
    lines = [f"扫描正文 {report.bodies_scanned} 条；带标记行 {len(report.marks)} 条"]
    if not report.marks:
        lines.append(CONCLUSION_NO_MARK)
        return "\n".join(lines)
    for mark in report.marks:
        flag = "OK" if mark.state in STATES_COUNTED else "STALE"
        lines.append(f"  {flag} {(mark.value or '')[:12]} {mark.kind}")
    if report.missing:
        lines.append(
            LEGACY_CONCLUSION_MISSING.format(
                short=short, missing=",".join(report.missing)
            )
        )
    else:
        lines.append(LEGACY_CONCLUSION_OK.format(short=short))
    return "\n".join(lines)


# --------------------------------------------------------------- 取数（gh api）

def parse_pages(stdout: str, *, endpoint: str = "") -> list[dict[str, object]]:
    """解析 ``gh api --paginate`` 的输出：多段 JSON 数组首尾相接，逐段拼成一个列表。

    空输出 = 没有条目（合法的「0 条 Review / 0 条评论」）；不是合法 JSON、或某段
    不是对象数组 → :class:`ReviewHeadError`（rc2），不静默当成 0 条。
    """

    items: list[dict[str, object]] = []
    decoder = json.JSONDecoder()
    index = 0
    size = len(stdout)
    while True:
        while index < size and stdout[index].isspace():
            index += 1
        if index >= size:
            return items
        try:
            value, end = decoder.raw_decode(stdout, index)
        except json.JSONDecodeError as exc:
            raise ReviewHeadError(
                f"gh api {endpoint} 输出不是合法 JSON：{exc.msg}（第 {exc.pos} 字符）"
            ) from exc
        if not isinstance(value, list):
            raise ReviewHeadError(
                f"gh api {endpoint} 输出不是对象数组（{type(value).__name__}）"
            )
        for item in value:
            if not isinstance(item, dict):
                raise ReviewHeadError(
                    f"gh api {endpoint} 输出的数组里有非对象元素（{type(item).__name__}）"
                )
            items.append(item)
        index = end


def _gh_items(endpoint: str) -> list[dict[str, object]]:
    """跑一次 ``gh api repos/<REPO>/<endpoint> --paginate``；失败一律 rc2。"""

    argv = ("gh", "api", f"repos/{REPO}/{endpoint}", "--paginate")
    try:
        # bandit B603/B607 假阳性：可执行文件名写死（"gh"，走 PATH），参数以 argv
        # 列表传入、shell=False；endpoint 由本模块内部拼（只含数字 PR 号与固定段名）
        proc = subprocess.run(  # nosec B603 B607
            list(argv),
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewHeadError(
            f"gh api {endpoint} 超时：{GH_TIMEOUT_SECONDS} 秒内没有返回"
        ) from exc
    except FileNotFoundError as exc:
        raise ReviewHeadError(
            "找不到 gh 可执行文件（PATH 里没有 gh），无法读取 PR 的 Review 与评论"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ReviewHeadError(
            f"gh api {endpoint} 输出不是 UTF-8（第 {exc.start} 字节起解码失败）"
        ) from exc
    except OSError as exc:
        raise ReviewHeadError(f"无法执行 gh: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or [""])[0][:200]
        raise ReviewHeadError(f"gh api {endpoint} 失败 rc={proc.returncode}: {detail}")
    return parse_pages(proc.stdout, endpoint=endpoint)


def fetch_bodies(pr: int) -> tuple[str, ...]:
    """拉该 PR 的 Review 与 issue 评论正文（先 reviews 后 comments，只取 ``body``）。"""

    bodies: list[str] = []
    for endpoint in (f"pulls/{pr}/reviews", f"issues/{pr}/comments"):
        for item in _gh_items(endpoint):
            body = item.get("body")
            bodies.append(body if isinstance(body, str) else "")
    return tuple(bodies)


# ------------------------------------------------- 分支内容指纹（patch-id -U0，唯一算法处）
#
# 这一层只回答一个问题：「两个 rev 之间的**内容改动**是不是同一份」。判据是
# ``git patch-id --stable`` 对 **-U0** diff 的哈希：
#
# - ``-U0`` 必须显式写。``git patch-id`` 会把 ±3 行上下文一起哈希，于是纯 rebase
#   （main 在同文件别处改动、行号位移）会换 id —— 那是误杀，不是内容变了。
# - 换文件 / 真改内容仍然换 id：这正是要被判出来的两件事。
# - 二进制**判不了**：patch-id 只哈希 ``Binary files a/x and b/x differ`` 这一行，
#   两个不同的 blob 得到同一个 id。所以见到二进制就返回 ``kind="binary"`` 让上层
#   拒绝复用证据，不许当成「内容没变」。
# - **空白**：默认档（``FINGERPRINT_ALGO``）的 patch-id 会把空白规范化掉，本 PR 已
#   改过的那一行只改缩进会得到同一个 id —— 所以返回值的 ``algo`` 必须一起看：
#   ``algo="patch-id-U0-v1"`` 时「同指纹 ≠ 内容相同（空白类改动看不见）」，报告行要
#   带出 ``FINGERPRINT_LEGACY_CAVEAT``。``git patch-id --verbatim`` 可用时（探针
#   :func:`patch_id_supports_verbatim`，判据是 rc 不是版本号）走空白敏感档
#   ``algo="patch-id-U0-verbatim-v1"``。
# - **git 配置**：两次 diff 都钉 :data:`DIFF_CONFIG_PIN` + :data:`DIFF_PREFIX_PIN` +
#   ``--no-renames``，``patch-id`` 钉 ``patchid.verbatim``，所以 ``diff.renames`` /
#   ``core.quotePath`` / ``diff.interHunkContext`` / ``color.ui`` / ``diff.noprefix`` /
#   ``diff.mnemonicPrefix`` 这些**仓库内**配置都不许改变指纹或 ``paths`` 的字面（非 ASCII
#   名给真名字）。环境里的配置载体（``GIT_CONFIG_PARAMETERS`` 组 / ``GIT_CONFIG_GLOBAL`` /
#   ``GIT_CONFIG_SYSTEM``）在算指纹这一路一律关掉（见 :func:`_git_env`），不再有「从哪条
#   载体注入」的区别；取对象那一路（``git fetch``）反过来一律保留。

#: 指纹算法标识（报告行必须引用它；换算法 = 换这个字符串）
FINGERPRINT_ALGO = "patch-id-U0-v1"

#: 空白敏感的那一档标识：``git patch-id --verbatim`` 可用时才启用（见 :func:`patch_id_supports_verbatim`）
FINGERPRINT_ALGO_VERBATIM = "patch-id-U0-verbatim-v1"

#: ``algo == FINGERPRINT_ALGO`` 时必须随指纹一起带出的盲区说明（报告行原样引用）
#:
#: 旧档 patch-id 会把空白规范化掉：**本 PR 已改过的那一行**只改缩进（8 空格 → 12 空格）
#: 会得到同一个 patch-id。所以「同指纹」只说明非空白内容相同，不许据此复用证据
#: （本机 git 2.34.1 实测：两个分支只差已改行的缩进，patch-id 同为
#: ``d8a66f86f17670d741a6c2ad5d0aa8577f31e382``）。
FINGERPRINT_LEGACY_CAVEAT = (
    f"algo={FINGERPRINT_ALGO}：同指纹 ≠ 内容相同（空白类改动看不见），"
    "消费方不得据此复用证据"
)

#: 两次 diff 都钉死的配置（前缀，必须放在 ``diff`` 子命令之前）：
#:
#: - ``diff.renames=false`` + ``--no-renames``：rename 一律拆成 delete + add，
#:   指纹不再取决于调用方仓库里的 ``diff.renames`` 开关（``-c`` 压过配置文件，
#:   ``--no-renames`` 压过命令行默认）；
#: - ``core.quotePath=false``：非 ASCII 路径在 numstat / patch 里给**真名字**
#:   （默认会写成 ``"\\344\\270\\255…"`` 的 C 转义）。真·非 UTF-8 名字仍是
#:   :class:`PatchIdError`（下游严格按 UTF-8 解码）。
#: - ``diff.interHunkContext=0``：相邻两个 hunk 之间隔 ≤N 行时 git 会把它们**并成一个**
#:   hunk，patch 字节跟着变、patch-id 随之换值。默认值本来就是 0，显式给只是不让「仓库
#:   或环境里配成非 0」把同一份内容算成两个指纹（本机 git 2.34.1 实测：两处改动隔 2 行，
#:   ``-U0`` 下默认两个 ``@@``，``diff.interHunkContext=2`` 变成一个 ``@@ -2,4 +2,4 @@``，
#:   patch-id ``cea9cf2c…`` → ``af7f5b85…``）。仓库内配置摘不掉，只能在这里钉死。
DIFF_CONFIG_PIN = (
    "-c",
    "diff.renames=false",
    "-c",
    "core.quotePath=false",
    "-c",
    "diff.interHunkContext=0",
)

#: 两次 diff 都钉死的前缀形态：``diff.noprefix`` / ``diff.mnemonicPrefix``（个人机器上很常见）
#: 会把 ``diff --git a/x b/x`` 改成 ``diff --git x x`` / ``i/x w/x``，patch-id 随之换值 →
#: 同一内容被判 STALE。``-c diff.noprefix=false`` 也能压住 ``diff.noprefix``，但压不住
#: ``diff.mnemonicPrefix``；显式给前缀是两个一起钉，且与「默认值」逐字相同。
#: ``--no-renames``（在调用处显式给）与 :data:`DIFF_CONFIG_PIN` 一起把改名这一维也钉死。
DIFF_PREFIX_PIN = ("--src-prefix=a/", "--dst-prefix=b/")

#: 单次 git 调用的硬超时（秒）：一次卡住的 git 不许把整张卡拖到 worker 超时
GIT_TIMEOUT_SECONDS = 120

#: patch-id 的形态：40 或 64 位小写十六进制（宽度跟仓库哈希算法走：``git init
#: --object-format=sha256`` 的仓里 ``git patch-id`` 输出 64 位）。与 :data:`COMMIT_SHA_RE`
#: 同口径 —— 40/64 只在一处放宽就是新的不对称。
PATCH_ID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}", re.ASCII)

#: 二进制文件在 ``git diff --numstat`` 里的行首（``-\t-\t<path>``）
NUMSTAT_BINARY_PREFIX = "-\t-\t"

#: ``--numstat`` 的一行：``<added|->\t<deleted|->\t<path>``。
#: 注意 ``git diff --numstat -U0``（git 2.34.1 实测）会把 numstat 行**接着**打一份完整
#: patch 出来（``-U<n>`` 本身隐含 -p），所以只能从混排流里先切出 numstat 段，
#: 不能整段当路径用，也不能只按行筛（见 :func:`_numstat_rows`）。
NUMSTAT_ROW_RE = re.compile(r"(?:[0-9]+|-)\t(?:[0-9]+|-)\t.*", re.ASCII)

#: 混排输出里 patch 段的起始行前缀（``--numstat`` 之后紧跟的整份 patch）。
PATCH_SECTION_PREFIXES = ("diff --git ", "diff --cc ", "diff --combined ")

#: 无论哪一路都摘：把 git 指向**别处**的开关（留着就会让「在 B 仓算指纹」被静默搬到
#: A 仓上去算 —— 结果看着正常，算的是另一个仓）。
GIT_ENV_STRIP_ALWAYS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_EXTERNAL_DIFF",
    }
)

#: 只在**算指纹的那一路**（``merge-base`` / ``diff`` / ``patch-id`` / ``cat-file``）摘：
#: git 配置载体。「``git -c k=v``」及其子进程实际走的环境变量（``GIT_CONFIG_COUNT`` 那组
#: 的另一种写法）不摘就等于「调用方（含 pre-push hook 里的 ``git -c``）能注进被测子进程」，
#: 同一份内容换指纹。
GIT_ENV_STRIP_CONFIG = frozenset({"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"})

#: 同上，按前缀摘：``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>`` 成对注入 git 配置
GIT_ENV_STRIP_CONFIG_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

#: 算指纹那一路把「全局 / 系统配置」指向空设备（**不是**删环境变量：删掉 ``GIT_CONFIG_GLOBAL``
#: 只是让 git 回落到 ``~/.gitconfig``，配置照样进来）。见 :func:`_git_env`。
GIT_CONFIG_DISABLED = os.devnull

#: 「按提交 sha 算指纹」时 sha 的形态：**40 或 64 位小写十六进制**。与 :data:`HEAD_RE` /
#: :data:`HEAD_SHAPE_RE`（``REVIEW-HEAD`` 标记）同口径 —— sha256 仓的 ``headRefOid`` 就是
#: 64 位，只收 40 位会让整层永远 rc2。
COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}", re.ASCII)

#: 「看着像 sha」的 rev：40 / 64 位 ASCII 十六进制（**大小写都算像**）。只用来决定要不要对
#: :func:`fingerprint` 的 head 施加 sha 形态校验 —— 分支名（``feat`` / ``origin/main``）
#: 不匹配它，正常调用面不受影响；而「40 或 64 位十六进制」这种一定是 sha 的写法，必须与
#: :func:`fingerprint_of_commit` 得到同一个结论。
SHA_LIKE_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", re.ASCII)


class PatchIdError(Exception):
    """git 侧失败（缺 git / 超时 / 非 UTF-8 / 非 0 退出）：CLI 层映射 rc2。"""


class GitMissingError(PatchIdError):
    """PATH 里没有 git（环境故障：不许被当成「对象取不到」吞掉）。"""


@dataclass(frozen=True)
class Fingerprint:
    """一段分支内容的指纹。

    ``kind`` ∈ ``{"patch", "binary", "empty"}``；``value`` 只在 ``kind=="patch"``
    时有值（40 hex），其余为 ``None``；``merge_base`` 是 ``merge-base`` 的 40 hex
    （报告行要打印它做复核）；``paths`` 是 numstat 里出现过的路径（二进制时点名用；
    rename 已拆成 delete + add，非 ASCII 名给真名字）；``algo`` 是算出 ``value`` 的
    算法标识（:data:`FINGERPRINT_ALGO` 或 :data:`FINGERPRINT_ALGO_VERBATIM`）。

    **盲区（消费方必须看）**：``algo == FINGERPRINT_ALGO`` 时「同指纹 ≠ 内容相同
    （空白类改动看不见）」—— 只有本 PR 已改过的那一行改缩进才会同指纹，但也足以让
    两份内容不同的分支拿到同一个 ``value``。报告行必须原样带出
    :attr:`caveat`（= :data:`FINGERPRINT_LEGACY_CAVEAT`），不得据此复用证据。
    ``algo == FINGERPRINT_ALGO_VERBATIM`` 时（``git patch-id --verbatim`` 可用）
    空白也进哈希，没有这一档盲区。
    """

    kind: str
    value: str | None
    merge_base: str
    paths: tuple[str, ...]
    algo: str

    @property
    def caveat(self) -> str:
        """报告行要带出的盲区说明：旧档给 :data:`FINGERPRINT_LEGACY_CAVEAT`，verbatim 档给空串。"""

        return "" if self.algo == FINGERPRINT_ALGO_VERBATIM else FINGERPRINT_LEGACY_CAVEAT


def _git_env(*, keep_config_carriers: bool = False) -> dict[str, str]:
    """git 子进程的环境：复制 ``os.environ``，按「这一路要不要配置」分两档。

    **两条路共有的底**（``keep_config_carriers`` 取哪一档都一样）：摘掉
    :data:`GIT_ENV_STRIP_ALWAYS` 里那些把 git 指向别处的开关。

    **算指纹那一路**（``keep_config_carriers=False``，默认）：所有 git 配置载体一律关掉 ——
    :data:`GIT_ENV_STRIP_CONFIG` / :data:`GIT_ENV_STRIP_CONFIG_PREFIXES` 这类「``git -c``
    等价注入」直接删，全局 / 系统配置经 :data:`GIT_CONFIG_DISABLED` 指向空设备。理由：
    「同一份内容 ⇒ 同一指纹」是这一层的契约，配置注进来的每一维（``diff.algorithm`` /
    ``diff.interHunkContext`` / ``diff.renames`` …）都能换 patch 字节。删环境变量不够 ——
    删掉 ``GIT_CONFIG_GLOBAL`` 只是让 git 回落到 ``~/.gitconfig``，配置照样进来。

    **取对象那一路**（``keep_config_carriers=True``，只给 ``git fetch`` 用）：配置载体
    **一律保留**（``GIT_CONFIG_PARAMETERS`` 组与 ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM``
    同口径，不再一条摘一条留）。理由有两条：① fetch 只取对象，对象按 sha 内容寻址，配置
    注入改不了算出来的指纹；② ``url.*.insteadOf`` / ``credential.helper`` / ``safe.directory``
    恰恰只能靠配置载体注进来（pre-push hook 与 CI wrapper 走 ``git -c``，个人机器走全局
    配置），摘掉任一条都会让 fetch 静默变成「取不到」。旧版只摘 ``GIT_CONFIG_PARAMETERS``
    却保留 ``GIT_CONFIG_GLOBAL``：同一份 ``insteadOf`` 换个载体，一个 rc=0 算出指纹、另一个
    rc=128 静默返回 ``None``（t_9f4673a2 / D1 实测）。

    其余原样保留 —— SSH（``GIT_SSH*`` / ``SSH_AUTH_SOCK`` / ``GIT_ASKPASS``）与代理
    （``HTTPS_PROXY`` / ``https_proxy`` 等）凭据变量必须留着，否则 ``fetch`` 会被误判成
    环境故障。
    """

    env = dict(os.environ)
    for key in list(env):
        if key in GIT_ENV_STRIP_ALWAYS:
            del env[key]
    if keep_config_carriers:
        return env
    for key in list(env):
        if key in GIT_ENV_STRIP_CONFIG or key.startswith(GIT_ENV_STRIP_CONFIG_PREFIXES):
            del env[key]
    env["GIT_CONFIG_GLOBAL"] = GIT_CONFIG_DISABLED
    env["GIT_CONFIG_SYSTEM"] = GIT_CONFIG_DISABLED
    return env


def _check_rev(value: object, what: str) -> str:
    """rev 形态：非空字符串、且不以 ``-`` 开头。

    ``-`` 开头的 rev 会被 git 当成选项（``--help`` / ``-n1`` / ``--output=…``），
    等于把调用面交给调用方 —— 这里一律 :class:`PatchIdError`，不往下传。
    """

    if not isinstance(value, str):
        raise PatchIdError(f"{what} 非法：{value!r}（应为字符串形态的 rev）")
    if not value:
        raise PatchIdError(f"{what} 非法：空字符串（应为非空 rev）")
    if value.startswith("-"):
        raise PatchIdError(f"{what} 非法：{value!r}（以 '-' 开头，会被 git 当成选项）")
    return value


def _check_commit_sha(value: object) -> str:
    """提交 sha 形态：**40 或 64 位小写十六进制**，否则 :class:`PatchIdError`。

    宽度跟仓库的哈希算法走（sha256 仓的 ``headRefOid`` 是 64 位），与 :data:`HEAD_RE` /
    :data:`HEAD_SHAPE_RE` 对齐；大写一律拒（同一个 sha 的两种写法不该在指纹层得到两种身份）。
    """

    if not isinstance(value, str) or COMMIT_SHA_RE.fullmatch(value) is None:
        raise PatchIdError(
            f"提交 sha 非法：{value!r}（应为 40 或 64 位小写十六进制）"
        )
    return value


def _check_rev_shape(value: object, what: str) -> str:
    """rev 形态校验（``base_ref`` 与 ``head_rev`` 共用一份口径）。

    先过 :func:`_check_rev`（非空、不以 ``-`` 开头）；**看着像 sha 的**（:data:`SHA_LIKE_RE`，
    40 / 64 位 ASCII 十六进制、大小写都算）再走 :func:`_check_commit_sha` 的同一套小写校验。

    两个位置（base 与 head）用同一个函数是**有意的行为变更**：本 PR 之前
    ``fingerprint(repo, "A"*40, "topic")`` 会把大写 sha 原样交给 git（git 自己收大写对象名，
    实测 ``rev-parse`` / ``merge-base`` / ``diff`` / ``cat-file`` 全 rc=0），于是 base 位置
    「能算」、head 位置却 :class:`PatchIdError` —— 同一个 sha 的两种写法在指纹层得到两种
    身份。现在两处一起拒（rc2），接线方传大写 sha 会从「能算」变成「参数非法」。

    只对**看着像 sha 的**施加这条：分支名（``feat`` / ``origin/main``）不匹配
    :data:`SHA_LIKE_RE`，正常调用面不受影响。41–63 位十六进制不匹配它，仍走 rev 路径
    （P3，本卡不动）。
    """

    rev = _check_rev(value, what)
    if SHA_LIKE_RE.fullmatch(rev) is not None:
        return _check_commit_sha(rev)
    return rev


def _run_git(
    repo: str | Path,
    argv: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    what: str = "",
    keep_config_carriers: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """跑一次 ``git -C repo <argv>``（argv 列表、shell=False、硬超时）。

    只在**执行不成立**时抛 :class:`PatchIdError`（缺 git / 超时 / 其它 OSError）；
    rc 由调用方判 —— 有的调用（``merge-base`` / ``cat-file``）本来就允许非 0。
    环境走 :func:`_git_env`（摘掉 ``GIT_DIR`` 一类「指向别处」的开关；配置载体按
    ``keep_config_carriers`` 分档：只有 ``git fetch`` 那一路传 ``True``）。
    """

    label = what or " ".join(argv)
    cmd = ["git", "-C", str(repo), *argv]
    try:
        # bandit B603/B607 假阳性：可执行文件名写死（"git"，走 PATH），参数以 argv
        # 列表传入、shell=False；repo 与 rev 都是调用方给的实参，不经 shell。
        return subprocess.run(  # nosec B603 B607
            cmd,
            input=input_bytes,
            capture_output=True,
            check=False,
            shell=False,
            env=_git_env(keep_config_carriers=keep_config_carriers),
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PatchIdError(f"{label} 超时：{GIT_TIMEOUT_SECONDS} 秒内没有返回") from exc
    except FileNotFoundError as exc:
        raise GitMissingError("找不到 git 可执行文件（PATH 里没有 git）") from exc
    except OSError as exc:
        raise PatchIdError(f"无法执行 git（{label}）: {exc}") from exc


def _stdout_text(proc: subprocess.CompletedProcess[bytes], label: str) -> str:
    """按 UTF-8 严格解码 stdout；不是 UTF-8 → :class:`PatchIdError`。"""

    try:
        return proc.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PatchIdError(
            f"{label} 输出不是 UTF-8（第 {exc.start} 字节起解码失败）"
        ) from exc


def _stderr_first(proc: subprocess.CompletedProcess[bytes]) -> str:
    """stderr 首行（截 200 字符，只用于报错正文）。"""

    text = proc.stderr.decode("utf-8", errors="replace")
    return (text.strip().splitlines() or [""])[0][:200]


def _expect_ok(proc: subprocess.CompletedProcess[bytes], label: str) -> None:
    """rc≠0 → :class:`PatchIdError`（附 stderr 首行）。"""

    if proc.returncode != 0:
        raise PatchIdError(f"{label} 失败 rc={proc.returncode}: {_stderr_first(proc)}")


def _expect_and_get(
    repo: str | Path, argv: Sequence[str], label: str
) -> subprocess.CompletedProcess[bytes]:
    """跑一次 git 并要求 rc=0，返回完整结果（stdout 是 bytes，给 patch-id 吃）。"""

    proc = _run_git(repo, argv, what=label)
    _expect_ok(proc, label)
    return proc


def _numstat_rows(raw: str, label: str) -> list[str]:
    """从 ``diff --numstat -U0`` 的混排输出里只取 numstat 段。

    git 2.34.1 实测：``git diff --numstat -U0`` 的 stdout = numstat 行 + 空行 + 一整份
    patch（``-U<n>`` 本身隐含 ``-p``；``--numstat -U0 --no-patch`` 会把 numstat 一起关掉，
    不可用）。patch 正文的删除行本身就长成 ``-\\t5\\tghost`` / ``-\\t-\\tpayload``，逐行按
    :data:`NUMSTAT_ROW_RE` 筛会把它当成 numstat 行 —— 后果是纯文本 diff 被报成
    ``kind="binary"``、``paths`` 混进正文碎片。所以先按第一个 ``diff --git`` 行切段，
    再在 numstat 段内逐行校验：段里出现解析不了的行 = 格式不认识，fail-closed 抛
    :class:`PatchIdError`，不猜。
    """

    lines = raw.splitlines()
    boundary = len(lines)
    for index, line in enumerate(lines):
        if line.startswith(PATCH_SECTION_PREFIXES):
            boundary = index
            break
    rows = [line for line in lines[:boundary] if line.strip()]
    if not rows:
        raise PatchIdError(
            f"{label} 有输出但解析不出 numstat 行（按取不到明细处理，不猜）"
        )
    for line in rows:
        if NUMSTAT_ROW_RE.fullmatch(line) is None:
            raise PatchIdError(
                f"{label} 的 numstat 段有解析不了的行（不猜）: {line[:120]!r}"
            )
    return rows


#: 探针结果的进程内缓存（键 = repo 路径）：同一 repo 只探一次
_VERBATIM_SUPPORT: dict[str, bool] = {}


def patch_id_supports_verbatim(repo: str | Path) -> bool:
    """``git patch-id --verbatim`` 在本机是否可用：唯一判据是探针命令的 rc。

    探针逐字是 ``git patch-id --verbatim </dev/null``（``input_bytes=b""`` 即空 stdin）：
    rc=0 → 可用（``--verbatim`` = 空白也进哈希，见 git-patch-id(1)「do not strip any
    whitespace」）；rc≠0 → 不可用（本机 git 2.34.1 实测 rc=129，
    ``usage: git patch-id [--stable | --unstable]``）。

    **不按 git 版本号猜**：判据只有 rc。结果按 repo 路径在进程内缓存一次（同一 repo
    不重复起子进程）。缺 git（:class:`GitMissingError`）照旧上抛 —— 环境故障，
    不是「探针返回 False」。
    """

    key = os.fspath(repo)
    cached = _VERBATIM_SUPPORT.get(key)
    if cached is None:
        proc = _run_git(
            repo,
            ("patch-id", "--verbatim"),
            input_bytes=b"",
            what="git patch-id --verbatim",
        )
        cached = proc.returncode == 0
        _VERBATIM_SUPPORT[key] = cached
    return cached


def fingerprint(
    repo: str | Path,
    base_ref: str,
    head_rev: str,
    *,
    verbatim: bool | None = None,
) -> Fingerprint:
    """``base_ref`` 与 ``head_rev`` 之间内容改动的指纹（算法档见返回值的 ``algo``）。

    1. ``merge-base --all`` 取分叉点：候选**恰好 1 个**才算数 —— rc≠0（不是共同祖先 /
       未知 rev）或候选 ≠1（criss-cross，没有唯一分叉点）→ :class:`PatchIdError`；
    2. ``diff --numstat --no-color --no-renames -U0 mb head``（前面带
       :data:`DIFF_CONFIG_PIN`）：空输出 → ``kind="empty"``；任一行以
       ``-\\t-\\t`` 开头 → ``kind="binary"``（paths 收全）；
    3. 否则 ``diff -U0 --no-color --no-renames mb head | patch-id <档位旗标>`` 的第一列
       40/64 hex → ``kind="patch"``。档位旗标只有两个形态：旧档 ``--stable``、verbatim 档
       ``--verbatim``（``--verbatim`` 隐含 ``--stable``，两者**不能同时显式给** ——
       新 git 实测 rc=129 ``options '--verbatim' and '--stable' cannot be used together``）。

    算法档由 :func:`patch_id_supports_verbatim` 的探针结果决定（不按版本号猜）：

    - 支持 → ``algo="patch-id-U0-verbatim-v1"``，patch-id 带 ``--verbatim``
      （空白敏感，``caveat`` 为空串）；
    - 不支持 → ``algo="patch-id-U0-v1"``，即 :data:`FINGERPRINT_ALGO`：
      **同指纹 ≠ 内容相同（空白类改动看不见）**，报告行必须带出
      :data:`FINGERPRINT_LEGACY_CAVEAT`，消费方不得据此复用证据。

    两次 diff 都带 :data:`DIFF_CONFIG_PIN` + :data:`DIFF_PREFIX_PIN` + ``--no-renames``：
    指纹不随调用方仓库的 ``diff.renames`` / ``core.quotePath`` / ``diff.interHunkContext`` /
    ``diff.noprefix`` / ``diff.mnemonicPrefix`` 变（rename 拆成 delete + add），``paths``
    给真名字（非 ASCII 不转义）。环境里的 git 配置载体（``git -c`` 等价组 / 全局 / 系统配置）
    在算指纹这一路**一律关掉**（见 :func:`_git_env`），所以「同一份内容 ⇒ 同一指纹」不再
    取决于机器配置从哪条通道注入。

    ``base_ref`` 与 ``head_rev`` 同一份形态校验（:func:`_check_rev_shape`）：以 ``-`` 开头
    一律 :class:`PatchIdError`；看着像 sha（40 / 64 位十六进制，**大小写都算像**）的必须
    是小写，否则 :class:`PatchIdError`。**这是有意的行为变更**：本 PR 之前大写 sha 在 head
    位置被拒、在 ``base_ref`` 位置却原样交给 git（能算），同一个 sha 两种写法两种身份；
    现在两处一起拒（接线方传大写 sha 会从「能算」变 rc2）。
    ``verbatim`` 只给测试 / 诊断用（``None`` = 走探针）；显式 ``True`` 在无
    ``--verbatim`` 的 git 上是 :class:`PatchIdError`，不静默降级。
    """

    base = _check_rev_shape(base_ref, "base_ref")
    head = _check_rev_shape(head_rev, "head_rev")
    if verbatim is None:
        verbatim = patch_id_supports_verbatim(repo)
    algo = FINGERPRINT_ALGO_VERBATIM if verbatim else FINGERPRINT_ALGO
    mb_label = f"git merge-base --all {base} {head}"
    mb_proc = _run_git(repo, ("merge-base", "--all", base, head), what=mb_label)
    _expect_ok(mb_proc, mb_label)
    candidates = [
        line.strip()
        for line in _stdout_text(mb_proc, mb_label).splitlines()
        if line.strip()
    ]
    if len(candidates) != 1:
        raise PatchIdError(
            f"{mb_label} 得到 {len(candidates)} 个候选 merge base（应为 1 个；"
            "多基 = criss-cross 历史没有唯一分叉点，按取不到处理，不猜）"
        )
    merge_base = candidates[0]

    numstat_label = "git diff --numstat --no-color --no-renames -U0"
    numstat_raw = _stdout_text(
        _expect_and_get(
            repo,
            (
                *DIFF_CONFIG_PIN,
                "diff",
                "--numstat",
                "--no-color",
                "--no-renames",
                *DIFF_PREFIX_PIN,
                "-U0",
                merge_base,
                head,
            ),
            numstat_label,
        ),
        numstat_label,
    )
    if not numstat_raw.strip():
        return Fingerprint(
            kind="empty", value=None, merge_base=merge_base, paths=(), algo=algo
        )
    # 同一条命令的 stdout 里 numstat 段后面还跟着一整份 patch，先切段再逐行校验
    lines = _numstat_rows(numstat_raw, numstat_label)
    paths = tuple(line.split("\t")[-1] for line in lines)
    if any(line.startswith(NUMSTAT_BINARY_PREFIX) for line in lines):
        return Fingerprint(
            kind="binary", value=None, merge_base=merge_base, paths=paths, algo=algo
        )

    diff_label = "git diff -U0 --no-color --no-renames"
    diff_proc = _expect_and_get(
        repo,
        (
            *DIFF_CONFIG_PIN,
            "diff",
            "-U0",
            "--no-color",
            "--no-renames",
            *DIFF_PREFIX_PIN,
            merge_base,
            head,
        ),
        diff_label,
    )
    # ``patchid.verbatim`` 也钉死：老 git 上没有这个键（``-c`` 设置未知键是合法的空操作），
    # 新 git 上它能把默认档悄悄翻成 verbatim，必须压住，否则 algo 与实际算法不一致。
    #
    # ``--stable`` 与 ``--verbatim`` **不能同时显式给**（新 git 实测 rc=129：
    # ``error: options '--verbatim' and '--stable' cannot be used together``；
    # ``--verbatim`` 本身已隐含 ``--stable``），所以两档各给一个旗标。
    if verbatim:
        pid_label = "git patch-id --verbatim"
        pid_argv = (
            "-c",
            "patchid.verbatim=true",
            "patch-id",
            "--verbatim",
        )
    else:
        pid_label = "git patch-id --stable"
        pid_argv = (
            "-c",
            "patchid.verbatim=false",
            "patch-id",
            "--stable",
        )
    pid_proc = _run_git(
        repo, pid_argv, input_bytes=diff_proc.stdout, what=pid_label
    )
    _expect_ok(pid_proc, pid_label)
    for line in _stdout_text(pid_proc, pid_label).splitlines():
        columns = line.split()
        if columns and PATCH_ID_RE.fullmatch(columns[0]) is not None:
            return Fingerprint(
                kind="patch",
                value=columns[0],
                merge_base=merge_base,
                paths=paths,
                algo=algo,
            )
    raise PatchIdError(f"{pid_label} 没有输出 40 或 64 位 hex 的 patch-id（diff 非空却无哈希）")


def _has_commit(repo: str | Path, rev: str) -> bool:
    """本地是否已有该 commit 对象（``cat-file -e <rev>^{commit}``，rc≠0 即没有）。"""

    proc = _run_git(
        repo, ("cat-file", "-e", f"{rev}^{{commit}}"), what=f"git cat-file -e {rev}"
    )
    return proc.returncode == 0


def fingerprint_of_commit(
    repo: str | Path, base_ref: str, sha: str, remote: str = "origin"
) -> Fingerprint | None:
    """按「某条历史 head sha」算指纹：本地没有对象就先按 sha fetch 一次。

    取不到对象不是异常（调用方按「不可验证」处理）：fetch 失败（含超时 / rc≠0 /
    没有这个 remote）→ 返回 ``None``。真·环境故障（缺 git、diff 失败、非 UTF-8）
    仍然是 :class:`PatchIdError` —— 所以 ``except GitMissingError: raise`` 必须排在
    ``except PatchIdError: return None`` 之前，否则「PATH 里没有 git」会被静默吞成
    「这个 sha 取不到」。

    ``sha`` 必须是 40 或 64 位小写十六进制（与 ``REVIEW-HEAD`` 同口径），否则
    :class:`PatchIdError`；``base_ref`` 与 ``fingerprint`` 的 ``base_ref`` **同一份形态
    校验**（:func:`_check_rev_shape`：看着像 sha 就必须是小写）—— 两个形态校验都在**入口**，
    早于 ``cat-file`` / ``fetch``，所以「这个 sha 取不到」的结论不会把「base_ref 非法」漂成
    ``None``。

    取对象那一步（``git fetch``）用 :func:`_git_env` 的 ``keep_config_carriers=True`` 那一档：
    ``GIT_CONFIG_PARAMETERS`` 组与 ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` **同口径保留**，
    于是 ``url.*.insteadOf`` / ``credential.helper`` / ``safe.directory`` 不管从哪条载体注入都
    一样生效（旧版摘掉 ``GIT_CONFIG_PARAMETERS`` 却保留 ``GIT_CONFIG_GLOBAL``：同一份 insteadOf
    换个载体，一个 rc=0 算出指纹、另一个 rc=128 静默 ``None``）。fetch 只取对象、对象按 sha
    内容寻址，配置注入改不了算出来的指纹；算指纹那一路仍然把所有配置载体关掉（见
    :func:`_git_env` 与 :func:`fingerprint`）。
    """

    sha = _check_commit_sha(sha)
    _check_rev_shape(base_ref, "base_ref")
    if not _has_commit(repo, sha):
        fetch_label = f"git fetch --no-tags --quiet {remote} {sha}"
        try:
            _run_git(
                repo,
                ("fetch", "--no-tags", "--quiet", remote, sha),
                what=fetch_label,
                # 取对象那一路：配置载体一律保留（见 _git_env 的两档）
                keep_config_carriers=True,
            )
        except GitMissingError:
            # 缺 git 是环境故障，不许被下面那条当成「对象取不到」
            raise
        except PatchIdError:
            # 缺 git 之外的失败（网络 / 超时 / 没这个 remote / rc≠0）都归「对象取不到」
            return None
        if not _has_commit(repo, sha):
            return None
    return fingerprint(repo, base_ref, sha)
