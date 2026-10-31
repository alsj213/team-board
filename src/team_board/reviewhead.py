"""双问结论「是否绑定某份内容」的判定：纯函数 + 唯一取数入口。

R18 的机器判据本体（只读）。判的是**证据结构**，不判结论对错。结论绑的是
**内容指纹**（``git diff -U0`` 的**内容行** sha256，见 :data:`FINGERPRINT_ALGO`），不是
head sha：纯 rebase / 行号位移不改内容 → 结论继续可用；内容任何改动 —— **含只改空白、
缩进、行尾空格** —— → 立即失效。

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
2. 缺一类 / 两类都缺 / 标记都指向别的内容（= 内容变了但双问没重跑）→ rc1；当前 head
   的指纹算不出来（对象取不到、或与 base 无共同祖先）同样是 rc1，结论行点名「取不到
   <sha>」—— 那是「证据不可验证」，不是环境故障；
3. 参数形态坏（``--head`` 不是 40/64 位小写十六进制、``--pr`` 不是正整数、``--base`` /
   ``--remote`` 是空串或空白）、``--base`` 解析不到、``--repo`` 不是 git 工作树、``gh``
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

import hashlib
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

#: ``--base`` 的默认值与回退：默认 ``origin/main``，解析不到就试本地 ``main``；
#: 两者都解析不到 → ``PatchIdError``（rc2），不静默换 base。
DEFAULT_BASE = "origin/main"

#: 上一条的本地回退（CI 里没有 ``origin`` 远端时的常见形态）
FALLBACK_BASE = "main"

#: ``--remote`` 的默认值（只用于 ``git fetch --no-tags <remote> <sha>``）
DEFAULT_REMOTE = "origin"

#: ``--remote`` 的形态：远端名（首字符不许是 ``-``，免得被 git 当成选项；不许空白）
REMOTE_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*", re.ASCII)

#: 单次 ``gh api`` 的硬超时（秒）：一次卡住的 gh 不许把整张卡拖到 worker 超时
GH_TIMEOUT_SECONDS = 120

#: 结论行（五种，逐字；渲染与测试共用同一份文案）
CONCLUSION_OK = "结论: 内容指纹 {short} 的 defect + regression 各 >=1 条 → 绑定成立"
CONCLUSION_MISSING = "结论: 内容指纹 {short} 缺 {missing} → 内容变了要重跑双问"
CONCLUSION_NO_MARK = "结论: 无结构化标记 → 该 PR 的双问不可机器绑定（按缺证据处理）"
CONCLUSION_BINARY = (
    "结论: 分支含二进制文件 {paths} → 内容指纹不覆盖二进制（diff 正文只有 differ 一行），"
    "按不可绑定处理"
)
CONCLUSION_EMPTY = "结论: 分支相对 {base} 无差异 → 无可绑定内容（不许当 PASS）"

#: head 对象取不到（本地没有、按 sha fetch 也失败）：说的不是「重跑双问」而是「先取到对象」
CONCLUSION_UNFETCHABLE = (
    "结论: 取不到 {sha}，无法按内容绑定；先 git fetch 或在新 head 上重贴 REVIEW-PATCH"
)

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


def check_repo(repo: object) -> str:
    """``--repo`` 校验：必须是 git 能识别的目录（``git -C <repo> rev-parse --git-dir``）。

    不是目录 / 不是工作树 → :class:`PatchIdError`（CLI 转 rc2）；PATH 里没有 git →
    :class:`GitMissingError`（同样的 rc2），不许被当成「对象取不到」吞成 rc1。
    """

    path = str(repo) if isinstance(repo, (str, Path)) else ""
    if not path.strip():
        raise PatchIdError(f"--repo 非法：{repo!r}（应为 git 工作树的路径，默认 .）")
    if not Path(path).is_dir():
        raise PatchIdError(f"--repo 不是目录：{path}")
    label = f"git -C {path} rev-parse --git-dir"
    proc = _run_git(path, ("rev-parse", "--git-dir"), what=label)
    if proc.returncode != 0:
        raise PatchIdError(
            f"--repo 不是 git 工作树：{path}（{label} 失败 rc={proc.returncode}: "
            f"{_stderr_first(proc)}）"
        )
    return path


def resolve_base(repo: str | Path, base: str | None = None) -> str:
    """解析 base ref：默认 ``origin/main`` → 解析不到回退 ``main``；都解析不到 → rc2。

    显式给了别的 ``--base`` 就**只认这一个**（不回退，免得把「写错了 base」静默成
    「按 main 判」）；**显式给了空串 / 空白同样 rc2**（静默当默认值是替调用方猜）。
    失败抛 :class:`PatchIdError`，消息以 ``找不到 base`` / ``--base 非法`` 开头。
    """

    text = "" if base is None else str(base)
    if base is not None and not text.strip():
        raise PatchIdError(
            f"--base 非法：{base!r}（空串 / 空白不是 ref；要用默认行为就别传 --base）"
        )
    candidates: tuple[str, ...] = (
        (DEFAULT_BASE, FALLBACK_BASE) if text.strip() in ("", DEFAULT_BASE) else (text,)
    )
    for candidate in candidates:
        proc = _run_git(
            repo,
            ("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"),
            what=f"git rev-parse {candidate}",
        )
        if proc.returncode == 0:
            return candidate
    raise PatchIdError(
        "找不到 base："
        + " / ".join(candidates)
        + " 在该仓库里都解析不到（先 git fetch，或用 --base 指定已有 ref）"
    )


def check_remote(value: object) -> str:
    """``--remote`` 校验：必须是远端名（不许空串 / 空白 / 以 ``-`` 开头）。

    空串静默当 ``origin`` 是错的 —— 调用方写错了就得说，不许替他猜（也不许把空字符串
    塞进 ``git fetch`` 的 argv）。失败抛 :class:`PatchIdError`（CLI 转 rc2）。
    """

    if not isinstance(value, str) or REMOTE_RE.fullmatch(value) is None:
        raise PatchIdError(
            f"--remote 非法：{value!r}（应为远端名，例 origin；"
            "不许空串 / 空白 / 以 - 开头）"
        )
    return value


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
      diff 正文只有 differ 一行、空 diff 没有可比内容，都不许当 PASS；标记一律
      ``UNVERIFIABLE``；
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


def unfetchable(head_sha: str, bodies: Sequence[str], *, base: str = "") -> Report:
    """head 对象取不到（本地没有、按 sha fetch 也失败）时的报告：rc1 但不算「重跑双问」。

    指纹无从计算 → 两类全缺、标记一律 ``UNVERIFIABLE``（与二进制 / 空 diff 同一档），
    但结论行点名「取不到 <sha>」—— 这条的修法是先 ``git fetch`` 或在新 head 上重贴
    ``REVIEW-PATCH``，不是去补双问。
    """

    fp = Fingerprint(kind="unfetchable", value=None, merge_base="", paths=())
    return _unverifiable_report(fp, bodies, head=head_sha, base=base)


def render(report: Report) -> str:
    """整块报告（指纹形态）。

    首行 ``algo=<FINGERPRINT_ALGO> current=<前12> base=<base ref> mb=<前12>``，第二行
    扫描条数，然后每条 ``<state> <pid12|head12> <kind>``，最后一行结论。``report.current``
    为 ``None`` 时是兼容形态（按 sha 判），走旧渲染（旧调用点的输出保持逐字不变）。
    """

    if report.current is None:
        return _legacy_render(report)

    current = report.current
    lines = [
        f"algo={FINGERPRINT_ALGO} current={current.value[:12] if current.value else '-'}"
        f" base={report.base or '-'}"
        f" mb={current.merge_base[:12] if current.merge_base else '-'}",
        f"扫描正文 {report.bodies_scanned} 条；带标记行 {len(report.marks)} 条",
    ]
    for mark in report.marks:
        lines.append(f"  {mark.state} {mark.short} {mark.kind}")
    if current.kind == "patch":
        # 旧 REVIEW-HEAD 的内容比对不了（对象取不到 / 无共同祖先 / 本身就是二进制 / 空
        # diff）：逐条点名，别让人对着 `UNVERIFIABLE` 猜该 git fetch 还是该重跑双问。
        lines.extend(
            f"  取不到 {mark.head} 的内容：旧 REVIEW-HEAD 无法按内容复用"
            "（先 git fetch，或在新 head 上重贴 REVIEW-PATCH）"
            for mark in report.marks
            if mark.state == STATE_UNVERIFIABLE and mark.patch is None and mark.head
        )
    short = (current.value or report.head or "")[:12]
    if current.kind == "unfetchable":
        lines.append(CONCLUSION_UNFETCHABLE.format(sha=report.head or "-"))
    elif current.kind == "binary":
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


# ------------------------------------------------- 分支内容指纹（内容行 sha256，唯一算法处）
#
# 这一层只回答一个问题：「两个 rev 之间的**内容改动**是不是同一份」。判据是
# ``sha256(git diff -U0 的内容行)`` 取前 40 位十六进制：
#
# - ``-U0`` 必须显式写。带上下文（``-U3``）时，上游在同文件别处插入几行就会让上下文
#   一起变 —— 那是误杀，不是内容变了。丢掉 ``@@`` 头与 ``index`` 行之后，行号位移
#   （纯 rebase / 上游在别处改动）不再影响指纹。
# - 但**每一行内容逐字节保留**：``git patch-id``（E2 提交 aa75f27 那一版）会把行首/行尾
#   与连续空白归一化，于是「只改缩进」不换指纹 —— 而 Python 里缩进能改语义。空白、缩进、
#   行尾空格、空行都属于内容，必须换指纹，所以不能用 ``patch-id``。本机 git 2.34.1 没有
#   ``patch-id --verbatim``，因此自己算。
# - 换文件 / 真改内容仍然换指纹：这正是要被判出来的两件事。
# - 二进制**判不了**：diff 正文只有 ``Binary files a/x and b/x differ``，两个不同的 blob
#   长得一样。所以见到二进制就返回 ``kind="binary"`` 让上层拒绝复用证据，不许当成
#   「内容没变」。

#: 指纹算法标识（报告行必须引用它；换算法 = 换这个字符串）
FINGERPRINT_ALGO = "diff-sha256-U0-v1"

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

#: 指纹的线格式：40 位小写十六进制（sha256 取前 40 位；与 ``REVIEW-PATCH:`` 的
#: 40 hex 契约一致，160 bit 足够防碰撞）
FINGERPRINT_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)

#: ``diff -U0`` 输出里**不参与**指纹的行前缀：hunk 头（含行号 / 行数，纯位移也会变）
#: 与 ``index <blob>..<blob>`` 行（rebase 后 base 侧 blob 变了，内容却没变）。
#: 注意内容行在 diff 里恒以 ``+`` / ``-`` / 空格开头，删掉的 ``@@`` 行是 ``-@@ …``，
#: 不会被这两条前缀误伤。
DIGEST_SKIP_PREFIXES = (b"@@", b"index ")

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


class UnrelatedHistoryError(PatchIdError):
    """两个 rev 没有共同祖先（``merge-base`` 非 0）：内容取不到，按「不可验证」处理。

    :class:`PatchIdError` 的子类：既有调用点的 ``except PatchIdError`` 照旧成立（不会
    漏成 traceback），但 :func:`fingerprint_of_commit` 会把它折成 ``None`` → 上层 rc1
    「取不到 <sha>」，而不是 rc2。理由：PR 正文删不掉，历史里一条指向孤儿 sha 的旧标记
    不该让整条命令永久变成环境错误。
    """


@dataclass(frozen=True)
class Fingerprint:
    """一段分支内容的指纹。

    ``kind`` ∈ ``{"patch", "binary", "empty", "unfetchable"}``；``value`` 只在
    ``kind=="patch"`` 时有值（40 hex），其余为 ``None``；``merge_base`` 是 ``merge-base``
    的 40 hex（报告行要打印它做复核；``unfetchable`` 时为空串）；``paths`` 是 numstat
    里出现过的路径（二进制时点名用）。

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


def _content_digest(diff_bytes: bytes, label: str) -> str:
    """``git diff -U0`` 输出的「内容行」指纹：sha256 取前 40 位十六进制。

    只丢两类与内容无关的行（见 :data:`DIGEST_SKIP_PREFIXES`）：``@@`` hunk 头（含行号 /
    行数）与 ``index`` 行（blob sha）。剩下的**逐字节**进哈希 —— 多一个空格、少一层缩进、
    删一个空行，指纹都换。这样既能扛纯 rebase / 行号位移，又不会像 ``git patch-id`` 那样
    把空白归一化掉（那会让「只改缩进」复用旧证据 = fail-open）。

    输出里一条内容行都没有（不该发生：numstat 已说 diff 非空）→ :class:`PatchIdError`，
    不猜。
    """

    kept = [
        line
        for line in diff_bytes.split(b"\n")
        if not line.startswith(DIGEST_SKIP_PREFIXES)
    ]
    if not any(line[:1] in (b"+", b"-") for line in kept):
        raise PatchIdError(f"{label} 没有内容行（diff 非空却取不到 +/- 行，不猜）")
    return hashlib.sha256(b"\n".join(kept)).hexdigest()[:40]


def fingerprint(repo: str | Path, base_ref: str, head_rev: str) -> Fingerprint:
    """``base_ref`` 与 ``head_rev`` 之间内容改动的指纹（``FINGERPRINT_ALGO``）。

    1. ``merge-base`` 取分叉点：rc≠0（无共同祖先 / 未知 rev）→
       :class:`UnrelatedHistoryError`（内容取不到，不是环境故障）；
    2. ``diff --numstat -U0``：空输出 → ``kind="empty"``；任一行以
       ``-\\t-\\t`` 开头 → ``kind="binary"``（paths 收全）；
    3. 否则 ``diff -U0`` 的内容行 sha256 前 40 位 → ``kind="patch"``。
    """

    mb_label = f"git merge-base {base_ref} {head_rev}"
    mb_proc = _run_git(repo, ("merge-base", base_ref, head_rev), what=mb_label)
    if mb_proc.returncode != 0:
        raise UnrelatedHistoryError(
            f"{mb_label} 失败 rc={mb_proc.returncode}: {_stderr_first(mb_proc)}"
            f"（两条历史没有共同祖先，取不到可比的基线）"
        )
    merge_base = _stdout_text(mb_proc, "git merge-base").strip()
    if not merge_base:
        raise UnrelatedHistoryError(f"{mb_label} 无输出（空 merge base，取不到可比基线）")

    numstat_label = "git diff --numstat -U0 --no-color"
    numstat_raw = _stdout_text(
        _expect_and_get(
            repo, ("diff", "--numstat", "-U0", "--no-color", merge_base, head_rev), numstat_label
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

    diff_label = "git diff -U0 --no-color"
    diff_bytes = _expect_and_get(
        repo, ("diff", "-U0", "--no-color", merge_base, head_rev), diff_label
    ).stdout
    value = _content_digest(diff_bytes, diff_label)
    if FINGERPRINT_RE.fullmatch(value) is None:  # 不可达：hexdigest 恒为 hex
        raise PatchIdError(f"{diff_label} 算出的指纹形态不对：{value!r}")
    return Fingerprint(kind="patch", value=value, merge_base=merge_base, paths=paths)


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
            # 缺 git 是环境故障，不许吞成「对象取不到」（rc2 不是 rc1）
            raise
        except PatchIdError:
            # 其余失败（网络 / 超时 / 没这个 remote / rc≠0）都归「对象取不到」
            return None
        if not _has_commit(repo, sha):
            return None
    try:
        return fingerprint(repo, base_ref, sha)
    except UnrelatedHistoryError:
        # 与 base 无共同祖先 = 这份内容取不到（证据不可验证），不是环境故障：
        # 上层按 rc1 处理并点名「取不到 <sha>」。PR 正文删不掉，孤儿标记不许锁死 rc2。
        return None
