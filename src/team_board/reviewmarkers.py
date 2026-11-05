"""让 GitHub required check ``review-markers (双问绑定)`` 在当前 head 上重新评估。

为什么需要它（2026-11-06 实测）：workflow 只在 ``pull_request`` / ``push`` 上触发，
qa 的双问是 PR 评论（issue comment），贴完不会触发任何 workflow。于是 PR 首次
push 时必然失败的 ``review-markers`` 一直红着，``merge_guard`` 的 CI 全绿闸门
会拒绝合并 —— 合并路径死锁。本模块在合并命令之前重跑失败的那个 job，让同一段
CI 逻辑重新读一次评论；**不伪造 check 结论**。

用法（合并卡第 2 步命令的前缀）::

    python3 -m team_board.reviewmarkers --pr 108 --wait 240

退出码：0 = required check 已绿；1 = 重跑后仍失败 / 找不到 run / 超时。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 - 只起常量 argv 的 gh 子进程
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "CHECK_NAME",
    "DEFAULT_POLL",
    "DEFAULT_SLUG",
    "DEFAULT_WAIT",
    "Check",
    "PrInfo",
    "RefreshResult",
    "ReviewMarkersError",
    "main",
    "parse_checks",
    "parse_pr",
    "refresh",
]

#: GitHub required check 的 exact context 名（ruleset 里按这个名字要）
CHECK_NAME = "review-markers (双问绑定)"

#: 仓库钉死：与 merge_guard / reviewhead 的 ``-R`` 口径一致
DEFAULT_SLUG = "alsj213/team-board"

#: 重跑后最多等多久（秒）；单 job 实测 ~10s，留足 API 排队余量
DEFAULT_WAIT = 240

#: 轮询间隔（秒）
DEFAULT_POLL = 5

#: check 还在跑的状态
_IN_PROGRESS = frozenset(
    {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
)

#: 视为失败、需要重跑的状态
_FAILED = frozenset(
    {
        "FAILURE",
        "CANCELLED",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "SKIPPED",
        "NEUTRAL",
    }
)

#: check link 里的 run / job id（``.../actions/runs/123/job/456``）
_LINK_RE = re.compile(r"/actions/runs/([0-9]+)/job/([0-9]+)", re.ASCII)

#: 40 位 sha1 或 64 位 sha256
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.ASCII)

#: 重跑触发后，API 仍回旧 FAILURE 的宽限期（秒）
_RERUN_GRACE = 60.0


class ReviewMarkersError(Exception):
    """刷新失败（CLI 渲染成 rc1，不冒 traceback）。"""


class Runner(Protocol):
    """gh 子进程适配点（单测注入假 runner）。"""

    def __call__(self, argv: Sequence[str]) -> Any: ...


@dataclass(frozen=True)
class PrInfo:
    """``gh pr view`` 里本模块关心的三个字段。"""

    head_oid: str
    head_ref: str
    state: str


@dataclass(frozen=True)
class Check:
    """``gh pr checks --json`` 的一行。"""

    name: str
    state: str
    link: str


@dataclass(frozen=True)
class RefreshResult:
    """一次刷新的终态；``reran`` 表示是否真的触发过重跑。"""

    pr: int
    head_oid: str
    state: str
    run_id: int | None = None
    job_id: int | None = None
    reran: bool = False


def _default_runner(argv: Sequence[str]) -> Any:
    """默认执行器：捕获 stdout/stderr，rc 交给调用方判定。"""

    return subprocess.run(  # nosec B603 - argv 是常量元组，不经 shell
        tuple(argv), capture_output=True, text=True, check=False
    )


def _proc_text(proc: Any) -> tuple[int, str, str]:
    return (
        int(getattr(proc, "returncode", 1)),
        str(getattr(proc, "stdout", "") or ""),
        str(getattr(proc, "stderr", "") or ""),
    )


def _gh(*args: str, runner: Runner) -> tuple[int, str, str]:
    return _proc_text(runner(("gh", *args)))


def parse_pr(stdout: str, *, pr: int) -> PrInfo:
    """解析 ``gh pr view --json headRefOid,headRefName,state``。"""

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ReviewMarkersError(f"PR #{pr} 的 gh pr view 输出不是 JSON：{exc.msg}") from None
    if not isinstance(payload, dict):
        raise ReviewMarkersError(f"PR #{pr} 的 gh pr view 输出不是 JSON 对象")
    head_oid = payload.get("headRefOid")
    head_ref = payload.get("headRefName")
    state = payload.get("state")
    if not isinstance(head_oid, str) or not _SHA_RE.fullmatch(head_oid):
        raise ReviewMarkersError(f"PR #{pr} 的 headRefOid 不是 40/64 位小写十六进制：{head_oid!r}")
    if not isinstance(head_ref, str) or not head_ref:
        raise ReviewMarkersError(f"PR #{pr} 的 headRefName 不是非空字符串：{head_ref!r}")
    if not isinstance(state, str) or not state:
        raise ReviewMarkersError(f"PR #{pr} 的 state 不是非空字符串：{state!r}")
    return PrInfo(head_oid=head_oid, head_ref=head_ref, state=state)


def parse_checks(stdout: str) -> tuple[Check, ...]:
    """解析 ``gh pr checks --json name,state,link``；空输出视为没有 check。"""

    if not stdout.strip():
        return ()
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ReviewMarkersError(f"gh pr checks 输出不是 JSON：{exc.msg}") from None
    if not isinstance(payload, list):
        raise ReviewMarkersError("gh pr checks 输出不是 JSON 数组")
    checks: list[Check] = []
    for row in payload:
        if not isinstance(row, dict):
            raise ReviewMarkersError("gh pr checks 的每一行必须是 JSON 对象")
        name = row.get("name")
        state = row.get("state")
        link = row.get("link")
        if not isinstance(name, str) or not isinstance(state, str):
            raise ReviewMarkersError("gh pr checks 的 name/state 必须是字符串")
        checks.append(Check(name=name, state=state, link=link if isinstance(link, str) else ""))
    return tuple(checks)


def find_check(checks: Sequence[Check]) -> Check | None:
    """按 exact context 名找 required check。"""

    for check in checks:
        if check.name == CHECK_NAME:
            return check
    return None


def _read_pr(pr: int, slug: str, runner: Runner) -> PrInfo:
    rc, out, err = _gh(
        "pr",
        "view",
        str(pr),
        "-R",
        slug,
        "--json",
        "headRefOid,headRefName,state",
        runner=runner,
    )
    if rc != 0:
        raise ReviewMarkersError(f"gh pr view #{pr} 失败（rc={rc}）：{err.strip()}")
    return parse_pr(out, pr=pr)


def _read_checks(pr: int, slug: str, runner: Runner) -> tuple[Check, ...]:
    # gh pr checks 在有检查失败时 rc=1；rc>1 才是取数失败，JSON 仍可能有效
    rc, out, err = _gh(
        "pr",
        "checks",
        str(pr),
        "-R",
        slug,
        "--json",
        "name,state,link",
        runner=runner,
    )
    if rc > 1 or (rc == 1 and not out.strip()):
        raise ReviewMarkersError(f"gh pr checks #{pr} 失败（rc={rc}）：{err.strip()}")
    return parse_checks(out)


def _rerun(run_id: int, job_id: int, slug: str, runner: Runner) -> None:
    rc, out, err = _gh(
        "run",
        "rerun",
        str(run_id),
        "--job",
        str(job_id),
        "-R",
        slug,
        runner=runner,
    )
    if rc != 0:
        detail = (err or out).strip()
        raise ReviewMarkersError(f"gh run rerun {run_id} --job {job_id} 失败（rc={rc}）：{detail}")


def refresh(
    pr: int,
    *,
    slug: str = DEFAULT_SLUG,
    wait: int = DEFAULT_WAIT,
    poll: int = DEFAULT_POLL,
    runner: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> RefreshResult:
    """把 ``review-markers`` 刷新到绿；失败抛 :class:`ReviewMarkersError`。"""

    if pr < 1:
        raise ReviewMarkersError(f"--pr 必须是正整数，实际是 {pr}")
    if wait < 0:
        raise ReviewMarkersError(f"--wait 不能是负数，实际是 {wait}")
    if poll < 1:
        raise ReviewMarkersError(f"--poll 必须 ≥1，实际是 {poll}")

    run = runner or _default_runner
    info = _read_pr(pr, slug, run)
    if info.state != "OPEN":
        raise ReviewMarkersError(f"PR #{pr} 状态是 {info.state}，不是 OPEN，不需要刷新 required check")

    deadline = monotonic() + wait
    run_id: int | None = None
    job_id: int | None = None
    reran = False
    rerun_at = 0.0
    seen_in_progress = False

    while True:
        checks = _read_checks(pr, slug, run)
        check = find_check(checks)
        now = monotonic()

        if check is not None and check.state == "SUCCESS":
            return RefreshResult(
                pr=pr,
                head_oid=info.head_oid,
                state=check.state,
                run_id=run_id,
                job_id=job_id,
                reran=reran,
            )

        if check is None:
            if now >= deadline:
                raise ReviewMarkersError(
                    f"PR #{pr} 上找不到 required check「{CHECK_NAME}」"
                    "（workflow 还没跑或 head 已变）；不重跑，交给人看"
                )
            sleep(poll)
            continue

        if check.state in _IN_PROGRESS:
            seen_in_progress = True
            if now >= deadline:
                raise ReviewMarkersError(
                    f"等待「{CHECK_NAME}」超时（{wait}s，当前 {check.state}）：{check.link}"
                )
            sleep(poll)
            continue

        if check.state not in _FAILED:
            raise ReviewMarkersError(
                f"「{CHECK_NAME}」状态 {check.state!r} 不认识，不猜，先停手：{check.link}"
            )

        if reran:
            # 重跑触发后 API 可能短暂回旧 FAILURE；宽限期内继续等，见到新 attempt 的
            # IN_PROGRESS 后仍 FAILURE 才判失败。
            if seen_in_progress or now - rerun_at >= _RERUN_GRACE:
                raise ReviewMarkersError(
                    f"「{CHECK_NAME}」重跑后仍 {check.state}：{check.link}"
                )
            if now >= deadline:
                raise ReviewMarkersError(
                    f"等待重跑结果超时（{wait}s，当前 {check.state}）：{check.link}"
                )
            sleep(poll)
            continue

        match = _LINK_RE.search(check.link)
        if match is None:
            raise ReviewMarkersError(
                f"「{CHECK_NAME}」是 {check.state}，但 link 里没有 run/job id，无法定点重跑："
                f"{check.link or '<空>'}"
            )
        run_id, job_id = int(match.group(1)), int(match.group(2))
        _rerun(run_id, job_id, slug, run)
        reran = True
        rerun_at = now
        sleep(poll)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m team_board.reviewmarkers",
        description="重跑 GitHub required check review-markers，直到它在当前 head 上出结论。",
    )
    parser.add_argument("--pr", type=int, required=True, help="PR 号")
    parser.add_argument("--slug", default=DEFAULT_SLUG, help=f"owner/name（默认 {DEFAULT_SLUG}）")
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT, help=f"最多等多少秒（默认 {DEFAULT_WAIT}）")
    parser.add_argument("--poll", type=int, default=DEFAULT_POLL, help=f"轮询间隔秒（默认 {DEFAULT_POLL}）")
    args = parser.parse_args(argv)

    try:
        result = refresh(args.pr, slug=args.slug, wait=args.wait, poll=args.poll)
    except ReviewMarkersError as exc:
        print(f"review-markers: FAIL | {exc}", file=sys.stderr)
        return 1

    rerun = f" | rerun run {result.run_id} job {result.job_id}" if result.reran else ""
    print(
        f"review-markers: PASS | PR #{result.pr} head {result.head_oid[:12]} "
        f"{result.state}{rerun}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
