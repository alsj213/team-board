"""tip 广播：合并后把新的 ``origin/main`` 通知给仍开着的卡分支，并可选更新分支。

设计口径：

- ``tip`` 是 ``<remote>/<main>`` 的 40 位提交号（默认 ``origin/main``）；
- 只检查 head 以 ``wt/`` 开头、base 为 main 的开放 PR（本仓卡分支的形态）；
- 对每个 PR 用 ``git merge-base --is-ancestor <tip> <head>`` 判是否落后：
  rc0 = 已包含 tip（最新），rc1 = 落后，其它 rc = 判不了（不猜，记失败）；
- 默认 **dry-run**：只报告谁落后；``--apply`` 才对该 PR 执行
  ``gh pr update-branch <n> --rebase``，逐条记录 rc，失败不影响其它 PR；
- 更新后内容可能变化，证据/双问是否仍有效由合并闸门（patch 指纹）重新判，
  本模块不代替 ``merge_guard``。

所有外部命令都走可注入的 ``run(argv, cwd) -> CommandResult``，单测不碰真 git/gh。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .train import CommandResult, default_run, repo_slug

__all__ = [
    "BroadcastReport",
    "PrTip",
    "TipError",
    "broadcast",
    "fetch_tip",
    "list_prs",
    "parse_pr_list",
    "render",
    "render_json",
]

#: 卡分支 / tip 的形态
_HEAD_RE = re.compile(r"^wt/t_[0-9a-f]{8}$", re.ASCII)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.ASCII)


@dataclass(frozen=True)
class PrTip:
    """一个开放 PR 与 tip 的关系。``behind`` 为 None 表示判不了。"""

    number: int
    head_ref: str
    head_oid: str
    base_ref: str
    draft: bool
    behind: bool | None = None
    update_rc: int | None = None
    update_detail: str = ""

    @property
    def applicable(self) -> bool:
        """是否参与 tip 广播（本仓卡分支 + base=main + 非 draft）。"""

        return (
            self.base_ref == "main"
            and _HEAD_RE.fullmatch(self.head_ref) is not None
            and not self.draft
        )


@dataclass(frozen=True)
class BroadcastReport:
    """一次 tip 广播的结果。"""

    tip: str
    prs: tuple[PrTip, ...]
    dry_run: bool
    errors: tuple[str, ...] = ()

    @property
    def behind(self) -> tuple[PrTip, ...]:
        return tuple(pr for pr in self.prs if pr.behind is True)

    @property
    def updated(self) -> tuple[PrTip, ...]:
        return tuple(pr for pr in self.prs if pr.update_rc == 0)

    @property
    def failed(self) -> tuple[PrTip, ...]:
        return tuple(pr for pr in self.prs if pr.update_rc not in (None, 0))


class TipError(Exception):
    """取 tip / gh 数据失败（CLI 渲染成 rc2）。"""


def fetch_tip(
    repo: str,
    remote: str,
    main: str,
    run: Callable[[Sequence[str], str], CommandResult] = default_run,
    *,
    fetch: bool = True,
) -> str:
    """取 ``<remote>/<main>`` 的 40 位 SHA；失败抛 :class:`TipError`。"""

    if fetch:
        proc = run(("git", "fetch", remote, main, "--quiet"), repo)
        if proc.returncode != 0:
            raise TipError(f"git fetch {remote} {main} 失败：{proc.stderr.strip() or proc.command}")
    proc = run(("git", "rev-parse", f"{remote}/{main}"), repo)
    sha = proc.stdout.strip()
    if proc.returncode != 0 or _SHA_RE.fullmatch(sha) is None:
        raise TipError(
            f"取 {remote}/{main} 失败（rc={proc.returncode}）：{proc.stderr.strip() or sha!r}"
        )
    return sha


def parse_pr_list(payload: object) -> tuple[PrTip, ...]:
    """解析 ``gh pr list --json ...`` 的数组；字段缺失的条目直接判错。"""

    if not isinstance(payload, list):
        raise TipError("gh pr list 返回的不是数组")
    prs: list[PrTip] = []
    for item in payload:
        if not isinstance(item, dict):
            raise TipError("gh pr list 数组里有非对象条目")
        try:
            number = int(item["number"])
            head_ref = str(item["headRefName"])
            head_oid = str(item["headRefOid"])
            base_ref = str(item["baseRefName"])
            draft = bool(item.get("isDraft", False))
        except (KeyError, TypeError, ValueError) as exc:
            raise TipError(f"gh pr list 条目字段缺失或类型错误：{item!r}") from exc
        prs.append(
            PrTip(
                number=number,
                head_ref=head_ref,
                head_oid=head_oid,
                base_ref=base_ref,
                draft=draft,
            )
        )
    return tuple(prs)


def list_prs(
    repo: str,
    slug: str,
    run: Callable[[Sequence[str], str], CommandResult] = default_run,
) -> tuple[PrTip, ...]:
    """列出开放 PR（只要本仓卡分支需要的字段）。"""

    proc = run(
        (
            "gh",
            "pr",
            "list",
            "-R",
            slug,
            "--state",
            "open",
            "--json",
            "number,headRefName,baseRefName,headRefOid,isDraft",
        ),
        repo,
    )
    if proc.returncode != 0:
        raise TipError(f"gh pr list 失败（rc={proc.returncode}）：{proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TipError(f"gh pr list 输出不是合法 JSON：{proc.stdout[:200]!r}") from exc
    return parse_pr_list(payload)


def _is_ancestor(
    repo: str,
    tip: str,
    head: str,
    run: Callable[[Sequence[str], str], CommandResult],
) -> bool | None:
    """rc0 → True、rc1 → False、其它 → None（判不了，不猜）。"""

    proc = run(("git", "merge-base", "--is-ancestor", tip, head), repo)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _with_behind(
    repo: str,
    tip: str,
    prs: Iterable[PrTip],
    run: Callable[[Sequence[str], str], CommandResult],
) -> tuple[PrTip, ...]:
    """给每个适用的 PR 填 ``behind``；不适用的保持 None（不参与广播）。"""

    entries: list[PrTip] = []
    for pr in prs:
        if not pr.applicable:
            entries.append(pr)
            continue
        ancestor = _is_ancestor(repo, tip, pr.head_oid, run)
        behind = None if ancestor is None else not ancestor
        entries.append(
            PrTip(
                number=pr.number,
                head_ref=pr.head_ref,
                head_oid=pr.head_oid,
                base_ref=pr.base_ref,
                draft=pr.draft,
                behind=behind,
            )
        )
    return tuple(entries)


def _update_branch(
    repo: str,
    slug: str,
    pr: PrTip,
    run: Callable[[Sequence[str], str], CommandResult],
) -> PrTip:
    """``gh pr update-branch --rebase``；失败只记 rc/detail，不抛。"""

    proc = run(
        ("gh", "pr", "update-branch", str(pr.number), "-R", slug, "--rebase"),
        repo,
    )
    return PrTip(
        number=pr.number,
        head_ref=pr.head_ref,
        head_oid=pr.head_oid,
        base_ref=pr.base_ref,
        draft=pr.draft,
        behind=pr.behind,
        update_rc=proc.returncode,
        update_detail=(proc.stderr.strip() or proc.stdout.strip())[:300],
    )


def broadcast(
    repo: str,
    *,
    remote: str = "origin",
    main: str = "main",
    apply: bool = False,
    run: Callable[[Sequence[str], str], CommandResult] = default_run,
    fetch: bool = True,
) -> BroadcastReport:
    """计算并可选执行 tip 广播。"""

    tip = fetch_tip(repo, remote, main, run, fetch=fetch)
    slug = repo_slug(repo, remote, run)
    prs = _with_behind(repo, tip, list_prs(repo, slug, run), run)
    if not apply:
        return BroadcastReport(tip=tip, prs=prs, dry_run=True)

    updated: list[PrTip] = []
    for pr in prs:
        if pr.behind is True:
            updated.append(_update_branch(repo, slug, pr, run))
        else:
            updated.append(pr)
    return BroadcastReport(tip=tip, prs=tuple(updated), dry_run=False)


def render(report: BroadcastReport) -> str:
    """人读报告；结论行在最后。"""

    lines = [f"tip = {report.tip}"]
    applicable = [pr for pr in report.prs if pr.applicable]
    if not applicable:
        lines.append("没有需要跟 tip 的开放卡分支 PR")
    for pr in applicable:
        if pr.behind is True:
            state = "落后"
        elif pr.behind is False:
            state = "已包含 tip"
        else:
            state = "判不了（merge-base 失败）"
        suffix = ""
        if pr.update_rc is not None:
            suffix = f"；update-branch rc={pr.update_rc}"
            if pr.update_rc != 0 and pr.update_detail:
                suffix += f"（{pr.update_detail}）"
        lines.append(f"#{pr.number} {pr.head_ref} | {state}{suffix}")

    if report.dry_run:
        lines.append("结论: dry-run，未更新分支（加 --apply 执行）")
    elif report.failed:
        lines.append(f"结论: {len(report.failed)} 条 update-branch 失败")
    else:
        lines.append(f"结论: 已更新 {len(report.updated)} 条分支")
    if report.errors:
        lines.append("错误: " + "；".join(report.errors))
    return "\n".join(lines)


def render_json(report: BroadcastReport) -> str:
    """机器可读报告。"""

    return json.dumps(
        {
            "tip": report.tip,
            "dry_run": report.dry_run,
            "prs": [
                {
                    "number": pr.number,
                    "head_ref": pr.head_ref,
                    "head_oid": pr.head_oid,
                    "base_ref": pr.base_ref,
                    "behind": pr.behind,
                    "update_rc": pr.update_rc,
                    "update_detail": pr.update_detail,
                }
                for pr in report.prs
            ],
            "errors": list(report.errors),
        },
        ensure_ascii=False,
    )
