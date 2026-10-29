"""``push-guard`` 判定本体：读 git pre-push 的 stdin 行，判「这次 push 放不放行」。

规则 B（checkout 偏移）是**事后**稽核（``branch-audit``）；本模块把它提前到
推之前拦住，四条判定（顺序固定 **P-1 → P-2 → P-3 → P-4**，不短路，全部命中
各出一条）：

- **P-1** 主 checkout 必须停在主分支（detached 也算偏移）；
- **P-2** 主 checkout 不许推送卡分支（``wt/t_xxxxxxxx``）；
- **P-3** worktree 侧一致性（只在「当前 checkout ≠ 主 checkout」时判）：
  a) HEAD 是卡分支而目录名 ≠ 卡号 / b) HEAD detached / c) 推了别的卡分支；
- **P-4** 原有 main / master 保护不丢（含「删远端分支」这种全零 sha）。

复用而非重写（硬要求）：卡分支正则、worktree 解析、主 checkout 定位、git
调用封装与异常全部来自 :mod:`team_board.branches` —— 本模块**不**有第二份
``subprocess`` 封装、第二份 ``worktree list --porcelain`` 解析、第二个异常类。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import branches

#: 卡分支正则：与 ``branches.BRANCH_RE`` **同一个对象**（不复制第二份）
CARD_BRANCH_RE = branches.BRANCH_RE

#: P-4 的保护名单（``remote_ref`` 精确命中即拒绝）
PROTECTED_MAIN_REFS = ("refs/heads/main", "refs/heads/master")

#: P-2 文案里固定的 worktree 落点前缀（与 pm 契约一致，逐字）
WORKTREE_HOME = "/home/liu/tb-wt"


@dataclass(frozen=True)
class PushRef:
    """pre-push stdin 的一行：``<local_ref> <local_sha> <remote_ref> <remote_sha>``。"""

    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str


@dataclass(frozen=True)
class Verdict:
    """一条判定：``blocked=True`` 是拦截项，``message`` 恒为**一行**人类可读文案。"""

    blocked: bool
    message: str


def parse_pre_push_lines(text: str) -> tuple[PushRef, ...]:
    """把 pre-push 的 stdin 全文解析成 :class:`PushRef` 元组。

    - 空行（含末尾没换行的最后一行）跳过；
    - 每行按空白切分，**字段数 ≠ 4** → ``AuditError``（带行号与字段数）；
    - 一条 ref 行都没有 → ``AuditError``（stdin 为空）。

    行号按原始行计（空行也占号），与「第 N 行」的字面义一致。
    """

    refs: list[PushRef] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        fields = raw.split()
        if len(fields) != 4:
            raise branches.AuditError(
                f"pre-push 输入格式非法（第 {lineno} 行只有 {len(fields)} 个字段）"
            )
        refs.append(PushRef(*fields))
    if not refs:
        raise branches.AuditError("没有收到任何要推送的 ref（stdin 为空）")
    return tuple(refs)


def _short_name(ref: str) -> str:
    """``refs/heads/x`` / ``refs/remotes/origin/x`` → ``x``（其它原样）。"""

    return branches._strip_ref_prefix(ref)


def _card_of(ref: str) -> str | None:
    """ref 是卡分支时返回卡号 ``t_xxxxxxxx``，否则 ``None``。"""

    match = CARD_BRANCH_RE.match(_short_name(ref))
    return match.group(1) if match else None


def _is_zero_sha(sha: str) -> bool:
    """``git push`` 删远端分支时 ``local_sha`` 是全零。"""

    return bool(sha) and not sha.strip("0")


def require_repo(repo: str | Path) -> None:
    """``--repo`` 的空串 / 纯空白是**参数错误**（rc2），不是「当前目录」。

    ``git -C ""`` 不报错（等于不动目录），于是一个空串会静默审到进程当前目录
    的仓库上 —— 这种「参数写错却给出结果」的口子必须在进 git 之前堵掉。
    """

    if not str(repo).strip():
        raise branches.AuditError("仓库路径不能为空（--repo）")


def _pushed_cards(refs: Sequence[PushRef]) -> list[str]:
    """本次 push 涉及的卡号（``t_xxxxxxxx``，去重保序；两侧 ref 都看）。"""

    cards: list[str] = []
    for ref in refs:
        for side in (ref.local_ref, ref.remote_ref):
            card = _card_of(side)
            if card is not None and card not in cards:
                cards.append(card)
    return cards


def judge(
    repo: str | Path, refs: Sequence[PushRef], main_branch: str = "main"
) -> list[Verdict]:
    """纯判定：返回判定列表；任一条 ``blocked`` → 整次拒绝。

    没有任何拦截时返回**恰好一条** ``blocked=False`` 的放行文案（含
    ``checkout=<root>`` 与 ``HEAD=<actual>``，供 CLI 原样写 stdout）。

    参数 / 环境级问题一律 ``branches.AuditError``（CLI 转 rc2）：主分支名为空
    或不存在、``--repo`` 不是工作树、worktree 列表里找不到当前 checkout。
    """

    if not main_branch or not main_branch.strip():
        raise branches.AuditError("主分支名不能为空（--main-branch）")
    if not refs:
        raise branches.AuditError("没有收到任何要推送的 ref（stdin 为空）")
    require_repo(repo)

    root = Path(
        branches._must_git(
            repo, "rev-parse", "--show-toplevel", what="定位当前 checkout 失败"
        ).strip()
    ).resolve()
    main_checkout = branches.main_checkout_path(repo)

    if not branches._ref_exists(root, f"refs/heads/{main_branch}"):
        raise branches.AuditError(f"主分支 {main_branch} 不存在（refs/heads/{main_branch}）")

    current = next(
        (entry for entry in branches.load_worktrees(root) if Path(entry.path).resolve() == root),
        None,
    )
    if current is None:
        raise branches.AuditError(f"worktree 列表里找不到当前 checkout {root}")

    expected_main_ref = f"refs/heads/{main_branch}"
    is_main_checkout = root == main_checkout
    if current.detached:
        actual = f"detached@{current.head[:7]}"
    else:
        actual = (current.branch or "").removeprefix("refs/heads/")
    own_card = None if current.detached else _card_of(f"refs/heads/{actual}")

    verdicts: list[Verdict] = []

    # P-1：主 checkout 必须停在主分支（detached 也算）
    if is_main_checkout and (
        current.detached or current.branch != expected_main_ref
    ):
        verdicts.append(
            Verdict(
                blocked=True,
                message=f"拒绝：主 checkout {root} 停在 {actual}，必须停 {main_branch}",
            )
        )

    # P-2：主 checkout 不许推卡分支（本侧 / 远端侧任一命中）
    if is_main_checkout:
        for card_id in _pushed_cards(refs):
            verdicts.append(
                Verdict(
                    blocked=True,
                    message=(
                        f"拒绝：主 checkout 不许推送卡分支 wt/{card_id}；"
                        f"做法：git -C {root} worktree add "
                        f"{WORKTREE_HOME}/{card_id} -b wt/{card_id} origin/main"
                    ),
                )
            )

    # P-3：worktree 侧一致性（主 checkout 不判）
    if not is_main_checkout:
        if own_card is not None and root.name != own_card:
            verdicts.append(
                Verdict(
                    blocked=True,
                    message=(
                        f"拒绝：worktree {root} 的目录名 {root.name} 与所推卡分支 "
                        f"{actual} 不一致（应为 {own_card}）"
                    ),
                )
            )
        if current.detached:
            verdicts.append(
                Verdict(
                    blocked=True,
                    message=(
                        f"拒绝：worktree {root} 处于 {actual}，无法归属到卡分支"
                    ),
                )
            )
        if own_card is not None:
            for card_id in _pushed_cards(refs):
                if card_id == own_card:
                    continue
                verdicts.append(
                    Verdict(
                        blocked=True,
                        message=(
                            f"拒绝：worktree 上的卡 {own_card} 不许推送别的卡分支 "
                            f"wt/{card_id}"
                        ),
                    )
                )

    # P-4：原有 main / master 保护不丢（含删远端分支）
    seen_protected: set[str] = set()
    for ref in refs:
        if ref.remote_ref not in PROTECTED_MAIN_REFS:
            continue
        name = _short_name(ref.remote_ref)
        if name in seen_protected:
            continue
        seen_protected.add(name)
        verdicts.append(
            Verdict(blocked=True, message=f"拒绝：不允许直接 push 到 {name}")
        )
        if _is_zero_sha(ref.local_sha):
            verdicts.append(
                Verdict(
                    blocked=False,
                    message="（这是删除远端分支的操作，同样禁止）",
                )
            )

    if verdicts:
        return verdicts
    return [
        Verdict(
            blocked=False,
            message=(
                f"push-guard: 放行 {len(refs)} 条 ref"
                f"（checkout={root}，HEAD={actual}）"
            ),
        )
    ]
