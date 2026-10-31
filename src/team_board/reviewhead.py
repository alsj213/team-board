"""双问结论「是否绑定某条 head sha」的判定：纯函数 + 唯一取数入口。

R18 的机器判据本体（只读）。判的是**证据结构**，不判结论对错：

- 取数：``gh api repos/alsj213/team-board/pulls/<N>/reviews`` 与
  ``issues/<N>/comments``（``--paginate``），**只看 body**，其它字段一概不读；
- 标记：同一条正文里**顶格**两行都出现才算一条标记 ——

      REVIEW-HEAD: <40 或 64 位小写十六进制>
      REVIEW-KIND: defect|regression

  两条正则 ``HEAD_RE`` / ``KIND_RE`` 是唯一来源（``re.MULTILINE``，行首行尾都锚死：
  缩进、行尾多空格、``REVIEW-HEAD:`` 前后带别的字都不算）。同一条正文只取每条
  正则的第一个匹配；已有两行才记一条 :class:`Mark`。

判定口径
--------
1. ``--head`` 上 ``defect`` 与 ``regression`` 各 ≥1 → rc0（绑定成立）；
2. 缺一类 / 两类都缺 / 只有别的 sha 的标记（= head 变了但双问没重跑）→ rc1；
3. ``--head`` 不是 40 或 64 位小写十六进制、``--pr`` 不是正整数、``gh`` 调用失败
   （非 0 / 超时 / PATH 里没有 / 输出不是 UTF-8 或不是 JSON）→ rc2。

rc2 一律在取数阶段抛出 :class:`ReviewHeadError`，所以 stdout 一个字节都不会写
（报告整块在取数与判定都成功之后才拼）。

**不判结论对错**：正文里写「阻塞 / 非阻塞」是二层的事，本模块只认上面两行标记；
也**没有** ``--skip`` 之类开关，缺证据就是 rc1。
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: 唯一仓库（``gh api`` 的路径前缀）
REPO = "alsj213/team-board"

#: 两类双问（顺序即结论行里 ``缺`` 的枚举顺序）
KINDS = ("defect", "regression")

#: ``REVIEW-HEAD:`` 行（MULTILINE、顶格、行尾锚死）
HEAD_RE = re.compile(r"^REVIEW-HEAD: ([0-9a-f]{40}|[0-9a-f]{64})$", re.M)

#: ``REVIEW-KIND:`` 行（同上）
KIND_RE = re.compile(r"^REVIEW-KIND: (defect|regression)$", re.M)

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

#: 结论行（三种，逐字；渲染与测试共用同一份文案）
CONCLUSION_OK = "结论: head {short} 的 defect + regression 各 >=1 条 → 绑定成立"
CONCLUSION_MISSING = "结论: head {short} 缺 {missing} → head 变了要重跑双问"
CONCLUSION_NO_MARK = "结论: 无结构化标记 → 该 PR 的双问不可机器绑定（按缺证据处理）"


class ReviewHeadError(Exception):
    """参数或环境错误（CLI 转 rc2：stderr 一行 ``错误: …``、stdout 空）。"""


@dataclass(frozen=True)
class Mark:
    """一条正文里的结构化标记（sha 已通过形态校验）。"""

    sha: str
    kind: str

    @property
    def short(self) -> str:
        """前 12 位（输出行与结论行用它，避免把 64 位 sha 铺满一行）。"""
        return self.sha[:12]


@dataclass(frozen=True)
class Report:
    """一次判定的全部结果：扫描条数、标记、该 head 上已齐的 kind、缺的 kind。"""

    head: str
    bodies_scanned: int
    marks: tuple[Mark, ...]
    kinds_on_head: frozenset[str]
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """两类都在该 head 上 ≥1 条才算绑定成立（= CLI rc0）。"""
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
    """逐条正文抽标记；两行都出现才算一条（同一条正文只取每条正则的首个匹配）。"""

    marks: list[Mark] = []
    for body in bodies:
        text = body or ""
        head = HEAD_RE.search(text)
        kind = KIND_RE.search(text)
        if head is not None and kind is not None:
            marks.append(Mark(sha=head.group(1), kind=kind.group(1)))
    return tuple(marks)


def analyze(head: str, bodies: Sequence[str]) -> Report:
    """判定该 head 上双问是否齐（纯函数；``head`` 已由 :func:`check_head` 规整）。"""

    marks = parse_marks(bodies)
    kinds = frozenset(mark.kind for mark in marks if mark.sha == head)
    missing = tuple(kind for kind in KINDS if kind not in kinds)
    return Report(
        head=head,
        bodies_scanned=len(bodies),
        marks=marks,
        kinds_on_head=kinds,
        missing=missing,
    )


def render(report: Report) -> str:
    """整块报告：扫描条数 / 带标记行数 / 每条 ``OK|STALE <sha12> <kind>`` / 结论。"""

    lines = [f"扫描正文 {report.bodies_scanned} 条；带标记行 {len(report.marks)} 条"]
    if not report.marks:
        lines.append(CONCLUSION_NO_MARK.format(short=report.head[:12]))
        return "\n".join(lines)
    for mark in report.marks:
        flag = "OK" if mark.sha == report.head else "STALE"
        lines.append(f"  {flag} {mark.short} {mark.kind}")
    if report.missing:
        lines.append(
            CONCLUSION_MISSING.format(
                short=report.head[:12], missing=",".join(report.missing)
            )
        )
    else:
        lines.append(CONCLUSION_OK.format(short=report.head[:12]))
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

#: 指纹算法标识（报告行必须引用它；换算法 = 换这个字符串）
FINGERPRINT_ALGO = "patch-id-U0-v1"

#: 单次 git 调用的硬超时（秒）：一次卡住的 git 不许把整张卡拖到 worker 超时
GIT_TIMEOUT_SECONDS = 120

#: patch-id 的形态：40 位小写十六进制
PATCH_ID_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)

#: 二进制文件在 ``git diff --numstat`` 里的行首（``-\t-\t<path>``）
NUMSTAT_BINARY_PREFIX = "-\t-\t"

#: ``--numstat`` 的一行：``<added|->\t<deleted|->\t<path>``。
#: 注意 ``git diff --numstat -U0``（git 2.34.1 实测）会把 numstat 行**接着**打一份完整
#: patch 出来（``-U<n>`` 本身隐含 -p），所以只能从混排流里先切出 numstat 段，
#: 不能整段当路径用，也不能只按行筛（见 :func:`_numstat_rows`）。
NUMSTAT_ROW_RE = re.compile(r"(?:[0-9]+|-)\t(?:[0-9]+|-)\t.*", re.ASCII)

#: 混排输出里 patch 段的起始行前缀（``--numstat`` 之后紧跟的整份 patch）。
PATCH_SECTION_PREFIXES = ("diff --git ", "diff --cc ", "diff --combined ")

#: git 子进程环境里**必须摘掉**的键（「指向别处」类）：
#: 留着就会让「在 B 仓算指纹」被静默搬到 A 仓上去算 —— 结果看着正常，算的是另一个仓。
GIT_ENV_STRIP_EXACT = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_EXTERNAL_DIFF",
        "GIT_CONFIG_COUNT",
    }
)

#: 同上，按前缀摘：``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>`` 成对注入 git 配置
GIT_ENV_STRIP_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

#: 「按提交 sha 算指纹」时 sha 的形态：40 位小写十六进制（与 ``REVIEW-HEAD`` 同口径）
COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)


class PatchIdError(Exception):
    """git 侧失败（缺 git / 超时 / 非 UTF-8 / 非 0 退出）：CLI 层映射 rc2。"""


class GitMissingError(PatchIdError):
    """PATH 里没有 git（环境故障：不许被当成「对象取不到」吞掉）。"""


@dataclass(frozen=True)
class Fingerprint:
    """一段分支内容的指纹。

    ``kind`` ∈ ``{"patch", "binary", "empty"}``；``value`` 只在 ``kind=="patch"``
    时有值（40 hex），其余为 ``None``；``merge_base`` 是 ``merge-base`` 的 40 hex
    （报告行要打印它做复核）；``paths`` 是 numstat 里出现过的路径（二进制时点名用）。
    """

    kind: str
    value: str | None
    merge_base: str
    paths: tuple[str, ...]


def _git_env() -> dict[str, str]:
    """git 子进程的环境：复制 ``os.environ``，只摘「指向别处」的那些开关。

    摘掉的键见 :data:`GIT_ENV_STRIP_EXACT` / :data:`GIT_ENV_STRIP_PREFIXES`；其余原样
    保留 —— SSH（``GIT_SSH*`` / ``SSH_AUTH_SOCK`` / ``GIT_ASKPASS``）与代理
    （``HTTPS_PROXY`` / ``https_proxy`` 等）凭据变量必须留着，否则 ``fetch`` 会被误判成
    环境故障。摘 ``GIT_CONFIG_COUNT`` 那组是因为它们能把 ``color.ui=always`` 之类配置
    注进子进程，破坏「同一份内容 ⇒ 同一指纹」。
    """

    env = dict(os.environ)
    for key in list(env):
        if key in GIT_ENV_STRIP_EXACT or key.startswith(GIT_ENV_STRIP_PREFIXES):
            del env[key]
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
    """提交 sha 形态：40 位小写十六进制，否则 :class:`PatchIdError`。"""

    if not isinstance(value, str) or COMMIT_SHA_RE.fullmatch(value) is None:
        raise PatchIdError(f"提交 sha 非法：{value!r}（应为 40 位小写十六进制）")
    return value


def _run_git(
    repo: str | Path,
    argv: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    what: str = "",
) -> subprocess.CompletedProcess[bytes]:
    """跑一次 ``git -C repo <argv>``（argv 列表、shell=False、硬超时）。

    只在**执行不成立**时抛 :class:`PatchIdError`（缺 git / 超时 / 其它 OSError）；
    rc 由调用方判 —— 有的调用（``merge-base`` / ``cat-file``）本来就允许非 0。
    环境走 :func:`_git_env`（摘掉 ``GIT_DIR`` 一类「指向别处」的开关）。
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
            env=_git_env(),
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


def fingerprint(repo: str | Path, base_ref: str, head_rev: str) -> Fingerprint:
    """``base_ref`` 与 ``head_rev`` 之间内容改动的指纹（``FINGERPRINT_ALGO``）。

    1. ``merge-base --all`` 取分叉点：候选**恰好 1 个**才算数 —— rc≠0（不是共同祖先 /
       未知 rev）或候选 ≠1（criss-cross，没有唯一分叉点）→ :class:`PatchIdError`；
    2. ``diff --numstat --no-color -U0 mb head``：空输出 → ``kind="empty"``；任一行以
       ``-\\t-\\t`` 开头 → ``kind="binary"``（paths 收全）；
    3. 否则 ``diff -U0 --no-color mb head | patch-id --stable`` 的第一列 40 hex →
       ``kind="patch"``。

    两次 diff 都带 ``--no-color``（numstat 也带），``color.ui=always`` 不许改变指纹。
    ``base_ref`` / ``head_rev`` 以 ``-`` 开头一律 :class:`PatchIdError`（会被当选项吃）。
    """

    base = _check_rev(base_ref, "base_ref")
    head = _check_rev(head_rev, "head_rev")
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

    numstat_label = "git diff --numstat --no-color -U0"
    numstat_raw = _stdout_text(
        _expect_and_get(
            repo, ("diff", "--numstat", "--no-color", "-U0", merge_base, head), numstat_label
        ),
        numstat_label,
    )
    if not numstat_raw.strip():
        return Fingerprint(kind="empty", value=None, merge_base=merge_base, paths=())
    # 同一条命令的 stdout 里 numstat 段后面还跟着一整份 patch，先切段再逐行校验
    lines = _numstat_rows(numstat_raw, numstat_label)
    paths = tuple(line.split("\t")[-1] for line in lines)
    if any(line.startswith(NUMSTAT_BINARY_PREFIX) for line in lines):
        return Fingerprint(
            kind="binary", value=None, merge_base=merge_base, paths=paths
        )

    diff_label = "git diff -U0 --no-color"
    diff_proc = _expect_and_get(
        repo, ("diff", "-U0", "--no-color", merge_base, head), diff_label
    )
    pid_label = "git patch-id --stable"
    pid_proc = _run_git(
        repo, ("patch-id", "--stable"), input_bytes=diff_proc.stdout, what=pid_label
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
            )
    raise PatchIdError(f"{pid_label} 没有输出 40 hex 的 patch-id（diff 非空却无哈希）")


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

    ``sha`` 必须是 40 位小写十六进制（与 ``REVIEW-HEAD`` 同口径），否则
    :class:`PatchIdError`；``base_ref`` 同 :func:`fingerprint` 的形态校验。
    """

    sha = _check_commit_sha(sha)
    if not _has_commit(repo, sha):
        fetch_label = f"git fetch --no-tags --quiet {remote} {sha}"
        try:
            _run_git(
                repo,
                ("fetch", "--no-tags", "--quiet", remote, sha),
                what=fetch_label,
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
