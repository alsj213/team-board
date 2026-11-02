"""路径认领：并行派工前判「两张卡的改动面是否相交」。

卡正文用**顶格**一行声明改动面（可重复，逗号分隔）::

    PATH-CLAIM: src/team_board/foo.py, tests/unit/test_foo.py

口径（与 ``PR-CLAIM`` 一样只认顶格行，避免正文引用被误判）：

- 路径按 POSIX 形态比较；拒绝绝对路径、``..`` 段、反斜杠与控制字符；
- 以 ``/`` 结尾表示目录认领，目录与其下任意路径相交；
- 含 ``*`` / ``?`` / ``[`` 的认领按 :func:`fnmatch.fnmatchcase` 做保守匹配；
- 两张**未结卡**的认领路径有交集 → 冲突，不应并行派工；已结卡不参与。

本模块只做纯解析与判定；CLI 取数在 ``path-claim-audit`` / ``path-claim-check``。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

__all__ = [
    "CLAIM_RE",
    "Claim",
    "Conflict",
    "Issue",
    "Report",
    "audit",
    "exit_code",
    "overlap",
    "parse_body",
    "parse_rows",
    "render",
    "render_json",
    "validate_path",
]

#: 顶格 ``PATH-CLAIM: <逗号分隔路径>``；行尾允许空白，不允许前导缩进
CLAIM_RE = re.compile(r"^PATH-CLAIM:[ \t]*(.*?)[ \t]*\r?$", re.MULTILINE)

#: 路径里不允许出现的字符（控制字符、反斜杠、NUL）
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")

#: 目录认领 / glob 认领的形态
_GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class Claim:
    """一张卡的路径认领。``paths`` 已去重并保持正文出现顺序。"""

    card_id: str
    status: str
    assignee: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Issue:
    """正文里一条无法解析的认领行（卡号 + 原行 + 原因）。"""

    card_id: str
    line: str
    reason: str


@dataclass(frozen=True)
class Conflict:
    """两张卡的路径交集；``paths`` 是命中的第一组路径对（按字典序取最小）。"""

    card_a: str
    card_b: str
    path_a: str
    path_b: str

    @property
    def cards(self) -> tuple[str, str]:
        return (self.card_a, self.card_b)


@dataclass(frozen=True)
class Report:
    """一次稽核的完整结果。``ok`` 仅在无冲突且无解析错误时为真。"""

    claims: tuple[Claim, ...]
    conflicts: tuple[Conflict, ...]
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.issues


def validate_path(raw: str) -> str:
    """校验并归一化一条认领路径；非法时抛 :class:`ValueError`（一句中文原因）。"""

    path = raw.strip()
    if not path:
        raise ValueError("路径为空")
    if path.startswith("/"):
        raise ValueError(f"不允许绝对路径: {path}")
    if path.startswith("~"):
        raise ValueError(f"不允许 ~ 路径: {path}")
    if _BAD_CHARS.search(path):
        raise ValueError(f"路径含控制字符或反斜杠: {path!r}")
    parts = path.split("/")
    if ".." in parts:
        raise ValueError(f"路径含 .. 段: {path}")
    if path in {".", "./"}:
        raise ValueError(f"路径不能是当前目录: {path}")
    while path.startswith("./"):
        path = path[2:]
    if path.endswith("//"):
        path = path.rstrip("/") + "/"
    return path


def parse_body(body: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """从正文解析 ``(paths, issues)``；同一条路径重复只保留第一次。"""

    paths: list[str] = []
    issues: list[str] = []
    for match in CLAIM_RE.finditer(body):
        line = match.group(0).strip()
        raw_items = match.group(1).split(",")
        line_paths: list[str] = []
        line_error: str | None = None
        for item in raw_items:
            try:
                path = validate_path(item)
            except ValueError as exc:
                line_error = str(exc)
                break
            if path not in line_paths:
                line_paths.append(path)
        if line_error is not None:
            issues.append(f"{line} → {line_error}")
            continue
        for path in line_paths:
            if path not in paths:
                paths.append(path)
    return tuple(paths), tuple(issues)


def parse_rows(
    rows: tuple[tuple[str, str, str, str], ...] | list[tuple[str, str, str, str]],
) -> tuple[tuple[Claim, ...], tuple[Issue, ...]]:
    """把 ``(id, status, assignee, body)`` 行解析成认领与问题。

    空 ``paths`` 的卡也保留一条 :class:`Claim`（便于渲染“已声明/未声明”），
    但只有 ``paths`` 非空的卡参与冲突判定。
    """

    claims: list[Claim] = []
    issues: list[Issue] = []
    for card_id, status, assignee, body in rows:
        paths, card_issues = parse_body(body or "")
        claims.append(
            Claim(
                card_id=str(card_id),
                status=str(status),
                assignee=str(assignee),
                paths=paths,
            )
        )
        for text in card_issues:
            for line, reason in _split_issue(text):
                issues.append(
                    Issue(card_id=str(card_id), line=line, reason=reason)
                )
    return tuple(claims), tuple(issues)


def _split_issue(text: str) -> tuple[tuple[str, str], ...]:
    """``"<line> → <reason>"`` → ``((line, reason),)``；没有箭头时整行当 reason。"""

    if " → " in text:
        line, reason = text.split(" → ", 1)
        return ((line, reason),)
    return ((text, "无法解析"),)


def _is_dir(path: str) -> bool:
    return path.endswith("/")


def _is_glob(path: str) -> bool:
    return any(char in _GLOB_CHARS for char in path)


def _pattern_prefix(path: str) -> str:
    """glob 模式里第一个通配符前的字面前缀（用于两个模式之间的保守比较）。"""

    positions = [path.find(char) for char in _GLOB_CHARS if char in path]
    return path[: min(positions)] if positions else path


def overlap(path_a: str, path_b: str) -> bool:
    """两条认领路径是否相交（目录前缀 / glob / 精确三种口径）。"""

    a = path_a.rstrip("/")
    b = path_b.rstrip("/")
    if a == b:
        return True

    if _is_dir(path_a) and (b == a or b.startswith(a + "/")):
        return True
    if _is_dir(path_b) and (a == b or a.startswith(b + "/")):
        return True

    if _is_glob(path_a) and fnmatch.fnmatchcase(b, path_a):
        return True
    if _is_glob(path_b) and fnmatch.fnmatchcase(a, path_b):
        return True

    if _is_glob(path_a) and _is_glob(path_b):
        prefix_a = _pattern_prefix(path_a)
        prefix_b = _pattern_prefix(path_b)
        if prefix_a == prefix_b:
            return True
        if prefix_a.startswith(prefix_b) or prefix_b.startswith(prefix_a):
            return True
    return False


def audit(claims: tuple[Claim, ...], issues: tuple[Issue, ...] = ()) -> Report:
    """两两比较有路径的认领，返回按卡号排序的冲突列表。"""

    active = [claim for claim in claims if claim.paths]
    conflicts: list[Conflict] = []
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            hit = _first_overlap(left.paths, right.paths)
            if hit is None:
                continue
            path_a, path_b = hit
            if left.card_id > right.card_id:
                left_id, right_id = right.card_id, left.card_id
                path_a, path_b = path_b, path_a
            else:
                left_id, right_id = left.card_id, right.card_id
            conflicts.append(
                Conflict(
                    card_a=left_id,
                    card_b=right_id,
                    path_a=path_a,
                    path_b=path_b,
                )
            )
    conflicts.sort(key=lambda item: (item.card_a, item.card_b, item.path_a, item.path_b))
    return Report(claims=claims, conflicts=tuple(conflicts), issues=tuple(issues))


def _first_overlap(
    left: tuple[str, ...], right: tuple[str, ...]
) -> tuple[str, str] | None:
    """按路径字典序找第一组相交路径（输出稳定，测试不依赖声明顺序）。"""

    for path_a in sorted(left):
        for path_b in sorted(right):
            if overlap(path_a, path_b):
                return (path_a, path_b)
    return None


def exit_code(report: Report) -> int:
    """0 无冲突且无解析问题 / 1 有冲突或解析问题。"""

    return 0 if report.ok else 1


def check_card(claims: tuple[Claim, ...], card_id: str, issues: tuple[Issue, ...] = ()) -> Report:
    """只判指定卡与其它卡的冲突；本卡没有认领也算不通过（由调用方渲染）。"""

    target = next((claim for claim in claims if claim.card_id == card_id), None)
    if target is None or not target.paths:
        return Report(claims=claims, conflicts=(), issues=issues)
    subset = tuple(claim for claim in claims if claim.paths)
    conflicts: list[Conflict] = []
    for other in subset:
        if other.card_id == card_id:
            continue
        hit = _first_overlap(target.paths, other.paths)
        if hit is None:
            continue
        conflicts.append(
            Conflict(
                card_a=min(card_id, other.card_id),
                card_b=max(card_id, other.card_id),
                path_a=hit[0],
                path_b=hit[1],
            )
        )
    conflicts.sort(key=lambda item: (item.card_a, item.card_b, item.path_a, item.path_b))
    return Report(claims=claims, conflicts=tuple(conflicts), issues=tuple(issues))


def render(report: Report) -> str:
    """人读报告（结论行固定为最后一行）。"""

    lines = ["路径认领稽核"]
    with_paths = [claim for claim in report.claims if claim.paths]
    if not with_paths:
        lines.append("无路径认领（正文顶格写 PATH-CLAIM: <path>[, <path>]）")
    for claim in with_paths:
        lines.append(f"{claim.card_id} | {claim.status} | {claim.assignee} | {', '.join(claim.paths)}")

    if report.issues:
        lines.append(f"解析问题 {len(report.issues)} 条：")
        for issue in report.issues:
            lines.append(f"  {issue.card_id}: {issue.line} → {issue.reason}")

    if report.conflicts:
        lines.append(f"冲突 {len(report.conflicts)} 对：")
        for conflict in report.conflicts:
            lines.append(
                f"  {conflict.card_a} ↔ {conflict.card_b}: "
                f"{conflict.path_a} ↔ {conflict.path_b}"
            )
    else:
        lines.append("冲突 0 对")

    if report.ok:
        lines.append("结论: 无路径冲突")
    elif report.conflicts and report.issues:
        lines.append(f"结论: {len(report.conflicts)} 对冲突，{len(report.issues)} 条解析问题")
    elif report.conflicts:
        lines.append(f"结论: {len(report.conflicts)} 对冲突（有交集的卡不能并行）")
    else:
        lines.append(f"结论: {len(report.issues)} 条解析问题")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    """机器可读报告（字段稳定，给 dispatcher / 看板插件用）。"""

    import json

    return json.dumps(
        {
            "ok": report.ok,
            "claims": [
                {
                    "card": claim.card_id,
                    "status": claim.status,
                    "assignee": claim.assignee,
                    "paths": list(claim.paths),
                }
                for claim in report.claims
            ],
            "conflicts": [
                {
                    "cards": [conflict.card_a, conflict.card_b],
                    "paths": [conflict.path_a, conflict.path_b],
                }
                for conflict in report.conflicts
            ],
            "issues": [
                {"card": issue.card_id, "line": issue.line, "reason": issue.reason}
                for issue in report.issues
            ],
        },
        ensure_ascii=False,
    )
