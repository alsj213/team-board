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
import re
import subprocess  # nosec B404
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

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
