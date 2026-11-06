"""机械预批准的「纯 rebase 代推」：R20 的自动通道。

背景（2026-11-06 实测）：main 前移后卡分支必须 rebase，但 bot 在无人值守会话里
不许 force push（R20），每次都要人工代推；如果 main 在卡进行中前移，整条链就卡住。
本工具把「纯 rebase」变成可机械判定的条件：

1. 远端 head 与本地 HEAD 的 **patch 指纹**相同（内容没变）；
2. 本地 HEAD 包含 ``origin/main``（已 rebase，且不是分叉）；
3. 区间内没有 merge commit；
4. 每个非 merge 提交的 ``author == committer``，且 committer 是 ``bots.tsv`` 里的 bot；
5. 工作区没有 tracked 改动。

全部通过才执行 ``git push --force-with-lease=<branch>:<旧远端 head>``。任何一项不满足
都 rc1，不推、不猜。

用法（在 engineer 的 worktree 里）::

    PYTHONPATH=<主仓>/src python3 -m team_board.rebasepush --card t_1234abcd --apply

不带 ``--apply`` 是 dry-run，只打印判定结果。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404 - 只起常量 argv 的 git 子进程
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import commits, reviewhead

__all__ = [
    "CARD_RE",
    "Plan",
    "RebasePushError",
    "apply",
    "main",
    "plan",
]

#: 卡号形态（与 team_board 其它模块同口径）
CARD_RE = re.compile(r"^t_[0-9a-f]{8}$", re.ASCII)

#: 40 位 sha1 或 64 位 sha256
SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.ASCII)

#: 默认远端与主分支
DEFAULT_REMOTE = "origin"
DEFAULT_MAIN = "main"


class RebasePushError(Exception):
    """机械判定不通过（CLI 渲染成 rc1，不冒 traceback）。"""


class Runner(Protocol):
    """git 子进程适配点（单测注入假 runner）。"""

    def __call__(self, argv: Sequence[str], cwd: str) -> Any: ...


@dataclass(frozen=True)
class Plan:
    """一次代推的完整判定结果。"""

    card: str
    branch: str
    old_head: str
    new_head: str
    patch: str
    commit_count: int
    base: str

    @property
    def needs_push(self) -> bool:
        return self.old_head != self.new_head


def _default_runner(argv: Sequence[str], cwd: str) -> Any:
    return subprocess.run(  # nosec B603 - argv 是常量元组，不经 shell
        tuple(argv), cwd=cwd, capture_output=True, text=True, check=False
    )


def _text(proc: Any) -> tuple[int, str, str]:
    return (
        int(getattr(proc, "returncode", 1)),
        str(getattr(proc, "stdout", "") or ""),
        str(getattr(proc, "stderr", "") or ""),
    )


def _git(cwd: str, *args: str, runner: Runner) -> tuple[int, str, str]:
    return _text(runner(("git", *args), cwd))


def _must(cwd: str, *args: str, runner: Runner, what: str) -> str:
    rc, out, err = _git(cwd, *args, runner=runner)
    if rc != 0:
        raise RebasePushError(f"{what}失败（git {' '.join(args)} rc={rc}）：{err.strip()}")
    return out.strip()


def _sha(text: str, what: str) -> str:
    value = text.strip()
    if not SHA_RE.fullmatch(value):
        raise RebasePushError(f"{what}不是 40/64 位小写十六进制：{value!r}")
    return value


def plan(
    card: str,
    worktree: str = ".",
    *,
    remote: str = DEFAULT_REMOTE,
    main_branch: str = DEFAULT_MAIN,
    runner: Runner | None = None,
    fingerprint: Callable[..., reviewhead.Fingerprint] = reviewhead.fingerprint,
    audit: Callable[..., commits.CommitReport] = commits.audit_commits,
) -> Plan:
    """做完全部机械判定，返回待推计划；任何一项不满足抛 :class:`RebasePushError`。"""

    if CARD_RE.fullmatch(card) is None:
        raise RebasePushError(f"卡号非法：{card!r}（应形如 t_1234abcd）")
    repo = os.path.realpath(worktree)
    if not os.path.isdir(repo):
        raise RebasePushError(f"worktree 不存在：{repo}")
    run = runner or _default_runner
    branch = f"wt/{card}"

    dirty = _must(repo, "status", "--porcelain", "--untracked-files=no", runner=run, what="读工作区状态")
    if dirty:
        raise RebasePushError(f"worktree 有未提交的 tracked 改动，不代推：\n{dirty}")

    _must(
        repo,
        "fetch",
        "--quiet",
        remote,
        main_branch,
        f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        runner=run,
        what="fetch 远端",
    )
    old_head = _sha(
        _must(repo, "rev-parse", f"refs/remotes/{remote}/{branch}", runner=run, what="读远端 head"),
        "远端 head",
    )
    new_head = _sha(_must(repo, "rev-parse", "HEAD", runner=run, what="读本地 HEAD"), "本地 HEAD")

    if old_head == new_head:
        return Plan(card, branch, old_head, new_head, "", 0, f"{remote}/{main_branch}")

    rc, _out, _err = _git(
        repo, "merge-base", "--is-ancestor", f"{remote}/{main_branch}", "HEAD", runner=run
    )
    if rc != 0:
        raise RebasePushError(
            f"{remote}/{main_branch} 不是 HEAD 的祖先（git merge-base --is-ancestor rc={rc}）："
            "先在 worktree 里 git rebase origin/main，再跑本工具"
        )

    merges = _must(
        repo,
        "rev-list",
        "--merges",
        f"{remote}/{main_branch}..HEAD",
        runner=run,
        what="检查 merge commit",
    )
    if merges:
        raise RebasePushError(
            "卡分支上存在 merge commit，不代推（merge-accountability 会红）："
            f"{merges.splitlines()[0]}"
        )

    try:
        old_fp = fingerprint(repo, f"{remote}/{main_branch}", old_head)
        new_fp = fingerprint(repo, f"{remote}/{main_branch}", new_head)
    except (reviewhead.PatchIdError, reviewhead.UnrelatedHistoryError) as exc:
        raise RebasePushError(f"算 patch 指纹失败：{exc}") from exc
    if old_fp.kind != "patch" or new_fp.kind != "patch":
        raise RebasePushError(
            f"不是可代推的 patch（old={old_fp.kind} new={new_fp.kind}）："
            "空改动 / 二进制文件都不能用机械通道"
        )
    old_patch, new_patch = old_fp.value, new_fp.value
    if old_patch is None or new_patch is None:
        raise RebasePushError("patch 指纹缺失（kind=patch 但没有 value），不代推")
    if old_patch != new_patch:
        raise RebasePushError(
            f"内容变了，不是纯 rebase：old patch={old_patch} new patch={new_patch}"
        )

    try:
        report = audit(repo, card=card, rev=new_head, main_branch=main_branch)
    except commits.CommitAuditError as exc:
        raise RebasePushError(f"提交身份稽核失败：{exc}") from exc
    if not report.ok:
        raise RebasePushError(
            "提交身份稽核不通过，不代推：\n" + commits.render_report(report)
        )

    return Plan(
        card=card,
        branch=branch,
        old_head=old_head,
        new_head=new_head,
        patch=new_patch,
        commit_count=len(report.commits),
        base=f"{remote}/{main_branch}",
    )


def apply(
    result: Plan,
    worktree: str = ".",
    *,
    remote: str = DEFAULT_REMOTE,
    runner: Runner | None = None,
) -> None:
    """执行带 lease 的 force push；已是最新则直接返回。"""

    if not result.needs_push:
        return
    repo = os.path.realpath(worktree)
    run = runner or _default_runner
    push_argv = (
        "push",
        f"--force-with-lease=refs/heads/{result.branch}:{result.old_head}",
        remote,
        f"HEAD:refs/heads/{result.branch}",
    )
    rc, out, err = _git(repo, *push_argv, runner=run)
    if rc != 0:
        raise RebasePushError(
            f"git push --force-with-lease 失败（rc={rc}）：{(err or out).strip()}"
        )
    after = _sha(
        _must(
            repo,
            "rev-parse",
            f"refs/remotes/{remote}/{result.branch}",
            runner=run,
            what="回读 push 后的远端 head",
        ),
        "推送后的远端 head",
    )
    if after != result.new_head:
        raise RebasePushError(
            f"推送后远端 head {after} != 本地 HEAD {result.new_head}；分支可能被并发推送，先停手"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m team_board.rebasepush",
        description="机械校验纯 rebase 后执行 --force-with-lease（R20 自动通道）。",
    )
    parser.add_argument("--card", required=True, help="卡号（t_ 加 8 位小写十六进制）")
    parser.add_argument("--worktree", default=".", help="卡分支的 worktree 路径（默认当前目录）")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help=f"远端（默认 {DEFAULT_REMOTE}）")
    parser.add_argument("--main", dest="main_branch", default=DEFAULT_MAIN, help=f"主分支（默认 {DEFAULT_MAIN}）")
    parser.add_argument("--apply", action="store_true", help="真推；缺省只做 dry-run")
    args = parser.parse_args(argv)

    try:
        result = plan(
            args.card,
            args.worktree,
            remote=args.remote,
            main_branch=args.main_branch,
        )
        print(
            f"rebasepush: card={result.card} branch={result.branch} "
            f"old={result.old_head[:12]} new={result.new_head[:12]} base={result.base}"
        )
        if not result.needs_push:
            print("rebasepush: 远端已是最新 head，无需推送")
            return 0
        print(
            f"rebasepush: patch={result.patch} commits={result.commit_count} "
            "身份/祖先/无 merge commit PASS"
        )
        if not args.apply:
            print("rebasepush: DRY-RUN（加 --apply 执行 --force-with-lease）")
            return 0
        apply(result, args.worktree, remote=args.remote)
    except RebasePushError as exc:
        print(f"rebasepush: FAIL | {exc}", file=sys.stderr)
        return 1

    print(f"rebasepush: PUSHED {result.new_head}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
