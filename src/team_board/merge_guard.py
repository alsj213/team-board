"""合并闸门：合并命令必须带 ``--delete-branch``，合并后必须确认远端源分支真的消失。

为什么要有这个模块
------------------
历史合并（PR #2–#14）全是「合完就走」，源分支堆在远端：板上看不出哪些卡的分支已经
合过，稽核又把已合并的旧分支当成「卡还在途」。靠自觉不行，所以把两件事做成机器判定：

1. **入口**：命令行没有 ``--delete-branch`` 就拒绝（rc2），一个 gh 写操作都不发。
   本工具**不提供**不带删除的合并路径 —— 没有 ``--no-delete-branch`` /
   ``--keep-branch``，也没有 ``--admin`` / ``--auto`` / ``--force`` 这类绕过 PR
   检查的口子。
2. **合并前**：先确认源分支在远端**存在**（``git ls-remote --exit-code --refs``，
   ``rc=0`` 才算存在）。不存在的分支名一律 rc2、stdout 空、一个 gh 调用都不发。
   没有这一步，出口判定会把「这个 ref 从来没存在过」读成「已删除」并 rc0 ——
   fork PR（gh 不删 fork 侧分支）与 ``--remote`` 指到另一个 remote 时必现。
3. **出口**：合并后只认 ``git ls-remote --exit-code --refs`` 的退出码：``0`` =
   分支仍在远端、``2`` = 没有这个 ref（已删除**或从未存在过**，单靠出口判不出来）、
   其它 = 判不了。分支还在就是 rc1，**绝不当成合并成功**。

``gh pr merge`` 返回非 0 **不等于**合并失败：gh 2.103.3 的 ``mergeRun`` 顺序是
``merge()`` → ``deleteLocalBranch()`` → ``deleteRemoteBranch()``，删本地分支失败就
``return`` 非 0，而那时远端分支还没删、PR 却已经合了。所以非 0 之后必须先
``gh pr view <n> --json state`` 反查：``MERGED`` = 合并已发生，继续做删除验证
（分支已删 → rc0、仍在 → rc1、判不了 → 判不了路径）；不是 ``MERGED`` 才是 rc1
「合并失败」。gh 自己的 stderr 警告（如 worktree 里 ``failed to delete local branch``）
同样不算失败 —— 判定只看远端。

合并前钉到提交，不只钉分支名
--------------------------
``gh pr view`` 除了 ``state`` / ``headRefName`` 还要读 ``headRefOid``（40 或 64 位十六
进制，缺字段或格式不对一律 rc2），并让 ``gh pr merge`` 带上
``--match-head-commit <sha>``。原因：预检（读到 ``OPEN`` + ``headRefName``）与 ``gh pr
merge`` 之间隔着一次网络往返，期间源分支可能被 force-push / 追加提交 —— 只比分支名
时，合进去的是预检**从未见过**的提交，工具却照样打「预检 | PASS」「合并 | PASS」+
rc0。预检通过时把 sha 记进证据（``head sha | PASS | <sha>``），合并时由 gh 自己比对：
head 变了 gh 直接拒，本工具拿不到 rc0。老版本 gh 报 ``unknown flag:
--match-head-commit`` → rc2、stderr 一行、stdout 空，**不许**静默降级成「不带 sha
合并」（那等于把 check-then-act 的洞重新打开）。

合并前必须已 rebase（base 必须是 head 的祖先）
--------------------------------------------
「分支落后 main 也照样合」是另一个 check-then-act 洞：只钉 head sha 不保证合进去的树
包含 main 尖端。所以预检之后、``gh pr merge`` 之前还有一道 rebase 闸门，判据是三条命令
（顺序固定；拒绝时 stdout 0 字节、一个 ``gh`` 写操作都不发）：

```
# ① 取远端真身：一次 fetch 两个 refspec（base 用预检读到的 baseRefName，不是写死的 main）
git -C <repo> fetch --no-tags --no-recurse-submodules --quiet <remote> \
    '+refs/heads/<base>:refs/merge-guard/<pr>/base' \
    '+refs/heads/<branch>:refs/merge-guard/<pr>/head'
# ② 交叉核对：远端 head 必须逐字等于预检读到的 headRefOid
git -C <repo> rev-parse refs/merge-guard/<pr>/head
# ③ 判定：base 是不是 head 的祖先
git -C <repo> merge-base --is-ancestor refs/merge-guard/<pr>/base refs/merge-guard/<pr>/head
```

- `<base>` 取 ``gh pr view`` 的 ``baseRefName``；形态非法（空串 / 含空白字符 / 带
  ``refs/`` 前缀）→ rc2，且**闸门自身不发任何 git 调用**（``fetch`` 之前就拦；此前已跑过
  一次 ``ls-remote`` 存在性探针，那是合并前的源分支存在闸门，不是本闸门发的）。
- **谁来做 rebase**（闸门只判「已 rebase 与否」，不做也不建议替你做）：rebase 必须在**这批
  提交作者的 bot 身份 worktree 里**做（``bash scripts/setup-bot-identity.sh <profile>
  <worktree>``），否则 committer 变成执行者、``commit-audit`` 判「committer 与 author 不一致」
  → ``commit-identity`` 红。**没有第二条路**：卡分支上不许出现没有认账字段的同步合并提交
  （把 base 同步合并进分支的那种）—— ``merge-audit`` 的 PR 形态（``--rev <head sha>``）把卡分支
  第一父链上每个合并提交都当「PR 合并提交」要求 ``merged-by`` / ``merge-card`` / ``merge-pr``
  三字段，同步合并提交没有，``merge-accountability`` 在 PR head 上必红（rc1）。
- ② 是必需的：预检到合并之间分支被推送时，判的必须是预检看到的那条 sha —— 与
  ``--match-head-commit`` 同一个理由。
- ③ ``rc=0`` = 已 rebase → ``rebase | PASS``；``rc=1`` = 落后或分叉 → **拒绝**；其它 rc
  （``128`` 对象缺失 / 通信错误…）= 判不了 → **同样拒绝**（不知道不等于通过）。
- **不用** ``--depth`` 浅抓：浅历史下 ``merge-base --is-ancestor`` 会假红
  （:mod:`team_board.branches` 已记同一个坑）。
- 私有 ref ``refs/merge-guard/<pr>/{base,head}`` 每次 ``+`` 覆盖，**不做清理**（刻意
  决定）：清理要多跑 ``git update-ref -d``，多出两条失败路径，而「清理失败不许翻状态」
  最容易出 P1。它不在 ``refs/heads`` / ``refs/remotes`` 下，稽核看不见、``git push``
  也不带出去，代价只是 pin 住两个提交对象。
- **没有跳过开关**：没有 ``--allow-stale`` / ``--skip-rebase-check`` / ``--force`` /
  ``--no-verify``。``--verify-only`` 路径也不加这一步（它一次 gh 都不调，不宣称合并前
  状态）。

合并前先过两道证据闸门（R17⑤ / R18④）
--------------------------------------
rebase 闸门之前还有两道**只读**闸门，顺序固定（拒绝时 stdout 0 字节、**一个 gh 写操作
都不发**）：

1. **认领唯一性**（R17⑤）：读看板库（``--db``，只读 ``mode=ro``）的未结卡，判「恰好一张
   卡在正文顶格写了 ``PR-CLAIM: #<pr>``」且**这张卡的卡号逐字等于 ``--card``**。判定
   逻辑全在 :func:`team_board.prclaim.audit` 与 ``Report.sole_claim_id``，本模块不复制
   正则、不另写一份 —— 与 ``team-board pr-claim-audit`` 共用同一份实现。三种拒绝文案
   一眼可分：``无未结卡认领该 PR`` / ``重复认领`` / ``认领卡 ≠ --card``。这一步一次 gh
   都不调（只读本地库）。
2. **双问绑定 head sha**（R18④）：用 ``--pr`` 与预检读到的 ``headRefOid`` 调
   :func:`team_board.reviewhead.fetch_bodies` / ``analyze``，要求该 sha 上 ``defect`` +
   ``regression`` 各 ≥1 条结构化标记（``REVIEW-HEAD`` / ``REVIEW-KIND``）。只发两条
   ``gh api GET``（``pulls/<n>/reviews`` + ``issues/<n>/comments``，``--paginate``），
   取数失败 / 证据不齐一律 rc2。三种拒绝文案：``双问缺证``（一条标记都没有） /
   ``双问陈旧``（有标记但都不是当前 head，即 head 变了没重跑双问） / ``双问缺类``
   （当前 head 上缺某一类）。
   取数前还有一道**仓库核对**：``reviewhead`` 的 ``gh api`` 路径前缀写死
   ``REPO = alsj213/team-board``、不看 ``--repo``，所以先用
   ``git config --get remote.<name>.url`` 解析 ``--repo`` 的 ``owner/name``，与 ``REPO`` 不一致
   （或不是 GitHub 形态 / 读不到 remote）→ rc2 拒绝。跨仓时它原本会去读**另一个仓库**
   同号 PR 的评审 —— 判的是别人的证据。

两道闸门都**没有**跳过开关（没有 ``--skip-claim`` / ``--skip-review-head``），也都不在
``--verify-only`` 路径上跑（那条路径不合并，不该要求认领与双问）。

合并方式只认一种
----------------
``--method`` 只接受 ``merge``（默认值就是 ``merge``，可省略）。``squash`` / ``rebase``
一律拒绝：rc2、stdout 空、stderr 一行 ``错误: …``，**一个 gh 调用都不发**。
依据是 R13（合并方式统一定一种 = create a merge commit，见 ``docs/merge-policy.md``）——
本工具不再把「用哪种方式」交给调用方决定。

认账三字段必须随合并命令一起走
------------------------------
``gh pr merge`` 不带 ``--subject`` / ``--body-file`` 时，合并提交 message 是 gh 自己的默认
文案（``Merge pull request #20 from alsj213/wt/t_xxxx``），**没有任何 trailer** —— 经本工具
合出来的提交必然过不了 ``merge-audit --require``（缺 ``merged-by`` / ``merge-card`` /
``merge-pr`` 三条）。所以合并模式多四个**必填**参数，全部原样转发给 ``gh pr merge``：

``gh pr merge <pr> -R <owner/name> --merge --delete-branch --match-head-commit <sha>
--subject <subject> --body-file <path>``

``-R <owner/name>`` 是仓库钉死（PR #46 双问缺陷 P2-2）：它必须与双问闸门核对过的仓库是
同一个值，于是「核对的是 A、操作的是 B」不成立。不钉时 gh 按 ``GH_REPO`` / cwd 选仓。

- ``--card`` 形如 ``t_[0-9a-f]{8}``、``--by`` 形如 ``<profile>/run<NNN>`` —— 用
  :mod:`team_board.merges` 的 ``MERGE_CARD_RE`` / ``MERGE_BY_RE`` 校验，**不复制正则**；
- ``--subject`` 单行非空白（含换行 → rc2）；
- ``--body-file`` 必须存在、是普通文件、严格 UTF-8、非空，且内容经
  ``merges.check_message(text, card=…, by=…, pr=…)`` 判过（缺字段 / 重复字段 / 格式非法 /
  与参数不一致都算不合格）；
- ``--verify-only`` 与这四个参数**互斥**（verify-only 不合并，给了 body 说明调用方误判）。

四条校验全部在 :func:`validate` 里判完 —— 也就是 ``ensure_branch_present()`` 之前，
零 ``gh`` 调用、零 ``git ls-remote``，任一不满足 → rc2、stdout 空、stderr 恰一行
``错误: …``。**任何「body 不合规」都不许落到 rc1**：rc1 是「未达成」专用码。

``--branch`` 只收裸分支名
------------------------
``--branch`` 在 ``validate`` 里（一切 ``gh`` / ``git`` 调用**之前**）就过形态校验：空串、
含任意空白字符、带 ``refs/`` 前缀 → rc2、stdout 空、stderr 恰一行 ``错误: …``。合并路径
与 ``--verify-only`` 走**同一套**校验，不许只修一半。

为什么不能「宽容一些」：探针入参是 ``refs/heads/<--branch>`` 直接拼出来的，于是
``--branch ''`` → ``refs/heads/``、``--branch ' wt/x'`` → ``refs/heads/ wt/x``、
``--branch 'refs/heads/wt/x'`` → ``refs/heads/refs/heads/wt/x`` —— 三者在远端都**不存在**，
``ls-remote`` 一律 ``rc=2``。修前 ``--verify-only`` 把 ``rc=2`` 读成「已删除」，给一个
从来没查过的 ref 打「分支删除 | PASS」+ rc0，等于伪造删除证明。

``--verify-only`` 的步骤行不写 PASS / FAIL
----------------------------------------
``--verify-only`` 既没看到「合并前分支在」，也没看到合并动作，``rc=2`` 分不出「已删除」
与「从未存在过」。所以它的步骤行只报 ref 现状：``远端 ref | 不存在 | … | rc=2 | …``
（``存在`` / ``不存在`` / ``无法判定``），``PASS`` / ``FAIL`` 与「分支已删除」字样一律
不出现。「删除」这个结论只由合并路径下 —— 合并前已确认分支存在 + 合并后探针 ``rc=2``
—— 才允许打出来。

退出码
------
- ``0``：合并完成，且已确认远端源分支删除（``--verify-only`` 时为「远端没有这个
  ref」—— 注意这一档**不能**证明「曾经存在并被删」，它只回答「现在在不在」）。
- ``1``：未达成 —— gh 合并失败（且 PR 状态不是 ``MERGED``） / 合并成功但源分支仍在
  远端 / ``--verify-only`` 时分支仍在远端或无法判定。
- ``2``：参数或环境错误：缺 ``--delete-branch``、``--method`` 不是 ``merge``（``squash``
  / ``rebase`` 一律拒）、合并模式缺 ``--card`` / ``--by`` / ``--subject`` / ``--body-file``
  任一项、``--card`` / ``--by`` 形态非法、``--subject`` 空 / 纯空白 / 含换行、``--body-file``
  路径为空 / 文件不存在 / 不是普通文件 / 非严格 UTF-8 / 空文件、body 经
  ``merges.check_message`` 判出问题（缺字段 / 重复 / 格式错 / 与参数不一致）、
  ``--verify-only`` 与这四个认账参数同给、``--pr`` / ``--timeout`` /
  ``--interval`` / ``--command-timeout`` 取值越界（``--pr`` 上限 ``MAX_PR``；
  ``--timeout`` / ``--interval`` / ``--command-timeout`` 上限 3600 秒，且
  ``--interval`` 不得大于 ``--timeout``）、``--branch`` 形态非法（空串 / 含空白字符 /
  带 ``refs/`` 前缀）、``--branch`` 是 ``main`` / ``master``
  或等于该 PR 的 ``baseRefName``、``--branch`` 与 PR 的 ``headRefName`` 不一致、
  **合并前认领闸门拒绝**（读不到看板库 / PR 无未结卡认领 / 重复认领 / 认领卡 ≠
  ``--card``）、**合并前双问绑定闸门拒绝**（取不到评审正文 / 双问缺证 / 双问陈旧 /
  双问缺类 / ``--repo`` 的 remote 读不到或不是 GitHub 形态 / 该仓库 ≠
  ``alsj213/team-board``）、
  **合并前 rebase 闸门拒绝**（``gh pr view`` 的 ``baseRefName`` 形态非法；``git fetch``
  取远端提交失败；远端 head 与预检的 ``headRefOid`` 不一致；``base`` 不是 ``head`` 的
  祖先；祖先关系判不了）、
  ``headRefOid`` 缺失或不是 40/64 位十六进制、gh 不支持 ``--match-head-commit``、
  源分支在远端**不存在**、PR 不是 ``OPEN``、``PATH`` 里没有 ``gh`` / ``git``、
  gh/git 输出不是 UTF-8、stdout 编码表示不了中文、子命令超时、合并后通信持续失败
  导致无法判定。

rc2 一律 stderr 一行 ``错误: …``、不冒 traceback，且**退出码绝不为 1**（rc1 是
「未达成」专用码：任何异常都不许落到它上面）。stdout 只有一种反例：**合并已经发生
之后**才出错时，stdout 仍保留已完成的「预检」「合并」两步证据 —— 后面的崩溃不能把
「合并成功了」这条证据一起丢掉，否则下一轮会把 rc2 读成「没合并、可以重跑」。

「写不出去」不许改变退出码
--------------------------
stdout 被提前关掉是常态（``… | head -0``、``1>&-``）。管道断开（``BrokenPipeError``
/ ``EPIPE``）、流已关闭（``ValueError``）、``sys.stdout is None`` 三种形态一律**不冒
traceback**：把 fd 1 接到 ``/dev/null`` 后按本次运行的**真实结论**返回 —— 本来 rc0
仍是 0、本来 rc1 仍是 1、本来 rc2 仍是 2，不许变成 120 / 141。只吞异常不够：缓冲区
里剩下的字节会在解释器退出时再 flush 一次，冒出 ``Exception ignored while flushing
sys.stdout`` 并把退出码改成 120，所以必须重定向 fd 1。

收到信号也要留住证据
--------------------
``main()`` 期间 ``SIGTERM`` / ``SIGINT`` / ``SIGHUP`` 一律转成「无法判定」收尾：
已完成的证据行照常写到 stdout，stderr 一行 ``错误: 收到信号 <SIGx>（退出码不是 1；
合并是否已发生请人工核对）``，退出码 ``2``（不是 124 / 130 / 143）。合并已经发生之后
被杀掉却留下 0 字节 stdout，下一轮就会把「合并成功」读成「没合并、可以重跑」。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any, NoReturn

from team_board import db, prclaim, reviewhead
from team_board.merges import MERGE_BY_RE, MERGE_CARD_RE, check_message
from team_board.model import DEFAULT_DB

#: 唯一合法的合并方式（R13：squash / rebase 一律不许，所以值域只有一个值）
METHODS: tuple[str, ...] = ("merge",)

#: ``--method`` 的默认值 —— 与 :data:`METHODS` 的唯一取值一致，可省略
DEFAULT_METHOD = "merge"

#: 永远不许删的主分支名
MAIN_BRANCHES: frozenset[str] = frozenset({"main", "master"})

#: ``--pr`` 的上限：PR 号是小正整数；13 位毫秒时间戳这类误用必须被拒
MAX_PR = 999_999_999

#: ``--timeout`` 的上限（秒）。没有上限时 ``clock() + timeout`` 会裸抛
#: ``OverflowError: int too large to convert to float``（且发生在合并之后）
MAX_TIMEOUT = 3600

#: ``--interval`` 的上限（秒），同时受 ``--timeout`` 约束（``interval <= timeout``）
MAX_INTERVAL = 3600

#: ``--command-timeout`` 的上限（秒）：单次 gh / git 子命令的硬超时
MAX_COMMAND_TIMEOUT = 3600

#: ``--command-timeout`` 的默认值（秒）
DEFAULT_COMMAND_TIMEOUT = 60

#: 探针（``git ls-remote``）超时用的哨兵退出码：必须能被
#: :func:`classify_probe_rc` 判成 :data:`PROBE_ERROR`（既不是 0 也不是 2）
PROBE_TIMEOUT_RC = -1

STEP_BRANCH_PRESENT = "分支存在"
STEP_PREFLIGHT = "预检"
#: 预检读到的 head 提交（钉住「合并的到底是哪个提交」，见模块 docstring）
STEP_HEAD_SHA = "head sha"
STEP_MERGE = "合并"
STEP_DELETE = "分支删除"
#: ``--verify-only`` 的步骤标签：只回答「远端有没有这个 ref」，不宣称删除
STEP_REMOTE_REF = "远端 ref"
#: 合并前 rebase 闸门的步骤标签（判据 = ``git merge-base --is-ancestor``）
STEP_REBASE = "rebase"
#: 合并前「认领唯一性」闸门的步骤标签（R17⑤；判据 = ``prclaim.audit``）
STEP_CLAIM = "认领唯一性"
#: 合并前「双问绑定 head sha」闸门的步骤标签（R18④；判据 = ``reviewhead.analyze``）
STEP_REVIEW_HEAD = "双问绑定"

#: 私有 ref 命名空间前缀：本工具把远端 base / head 抓到这里再判祖先关系。
#: 它**不在** ``refs/heads`` / ``refs/remotes`` 下 —— :mod:`team_board.branches`
#: 的稽核只枚举这两类，``git push`` 也不会把它带出去（见 :func:`rebase_fetch_argv`）。
MERGE_GUARD_REF_PREFIX = "refs/merge-guard"

#: :func:`classify_merge_base_rc` 的三档：祖先 / 不是祖先 / 判不了
ANCESTOR_YES = "yes"
ANCESTOR_NO = "no"
ANCESTOR_ERROR = "error"


OUT_DELETED = "deleted"
OUT_PRESENT = "present"
OUT_UNDETERMINED = "undetermined"

#: ``--verify-only`` 步骤行的状态字段：只报 ref 现状，**不用** ``PASS`` / ``FAIL``
VERIFY_STATE_TEXT: dict[str, str] = {
    OUT_DELETED: "不存在",
    OUT_PRESENT: "存在",
    OUT_UNDETERMINED: "无法判定",
}

#: ``--branch`` 形态规则（stderr 里逐条点明是哪一条，便于调用方改正）
BRANCH_RULE_EMPTY = "不能是空串"
BRANCH_RULE_WHITESPACE = "不能含空白字符"
BRANCH_RULE_REFS_PREFIX = "不能带 refs/ 前缀"

PROBE_PRESENT = OUT_PRESENT
PROBE_DELETED = OUT_DELETED
PROBE_ERROR = "error"

#: 缺 ``--delete-branch`` 时的固定文案（stderr 前面再加 ``错误: ``）
MISSING_DELETE_BRANCH = (
    "合并命令缺少 --delete-branch（合并卡必须带；本工具不提供不带删除的合并路径）"
)

#: 合并模式下**必填**的四个认账参数（顺序即校验与报错顺序）
ACCOUNTING_FLAGS: tuple[str, ...] = ("--card", "--by", "--subject", "--body-file")

#: 只认 ASCII 十进制：``int()`` 认空白、正负号、下划线与非 ASCII 数字
_DIGITS = re.compile(r"[0-9]+", re.ASCII)

#: ``gh pr view`` 要的字段：``headRefOid`` 是「合并前钉到提交」的凭据
_PR_VIEW_FIELDS = "number,state,headRefName,baseRefName,headRefOid"

#: 合法提交 SHA：40 位（SHA-1）或 64 位（SHA-256）十六进制
_HEAD_OID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


class MergeGuardError(Exception):
    """参数 / 环境级错误，CLI 转 rc2（stderr ``错误: …``，stdout 空）。"""


class UnsupportedMatchHeadCommit(MergeGuardError):
    """``gh pr merge`` 不认 ``--match-head-commit``（老版本 gh）。

    单独一个类是为了**不被** :func:`_keep_evidence` 包装成 :class:`PartialFailure`：
    gh 在解析 argv 阶段就退出，合并**一定没发生**，所以 stdout 必须保持空（rc2 档的
    契约），只留 stderr 一行。也**不许**降级成「不带 sha 合并」—— 那就把预检到合并
    之间 check-then-act 的洞又打开了。
    """


class CommandTimeout(MergeGuardError):
    """单次子命令在 ``--command-timeout`` 内没有返回。

    预检里抛出 → rc2（合并还没发生，stdout 保持空）；轮询里被
    :func:`make_probe` 吃掉并转成 :data:`PROBE_ERROR`（记一次通信错误继续轮询，
    **不许**当成「分支还在」或「已删」）。
    """


class Interrupted(MergeGuardError):
    """收到 ``SIGTERM`` / ``SIGINT`` / ``SIGHUP``：按「无法判定」收尾（rc2）。

    为什么单独一个类：被杀掉时进程**不能**直接消失。合并可能已经发生，证据还攒在
    ``lines`` 里；默认处理下 SIGTERM 让进程带走 rc=143（``timeout`` 报 124）、stdout
    0 字节，下一轮就会把「合并成功」读成「没合并、可以重跑」，再去合一次会撞 PR 已
    MERGED。所以信号转成异常，走 :class:`PartialFailure` 同一条收尾路径：
    先写已完成证据，再写 stderr 一行，退出码 2。

    作为 :class:`MergeGuardError` 的子类，它天然满足「异常不许落 rc1」这条：不论合并
    有没有发生，最终都是 rc2。
    """

    def __init__(self, signame: str) -> None:
        super().__init__(
            f"收到信号 {signame}（退出码不是 1；合并是否已发生请人工核对）"
        )
        self.signame = signame


class PartialFailure(MergeGuardError):
    """合并已经发生之后才出错：必须把已完成的步骤证据一起带到 stdout（仍是 rc2）。

    ``lines`` 是崩溃前已经攒下的输出行（至少含「预检 | PASS」「合并 | PASS」）；
    ``main`` 先把它们写到 stdout，再把 ``错误: …`` 写到 stderr，退出码 2 ——
    **绝不允许**落到 rc1，也不许只因后面的崩溃就让人读成「合并没发生、可以重跑」。
    """

    def __init__(self, message: str, lines: list[str] | None = None) -> None:
        super().__init__(message)
        self.lines: list[str] = list(lines or [])


@dataclass(frozen=True)
class CommandResult:
    """一次外部命令的原始结果。"""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def command(self) -> str:
        """命令原文（用于输出行里的 ``<命令原文>`` 段）。"""

        return " ".join(self.argv)


@dataclass(frozen=True)
class Preflight:
    """``gh pr view`` 里本工具关心的五个字段。"""

    number: int
    state: str
    head_ref: str
    base_ref: str
    #: 预检时源分支的 head 提交；合并命令要用它钉住「合的是哪一个提交」
    head_oid: str


@dataclass(frozen=True)
class Accounting:
    """合并提交的认账三字段 + 标题（全部原样转发给 ``gh pr merge``）。"""

    card: str
    by: str
    subject: str
    #: 认账 body 文件路径 —— **绝对路径**（:func:`read_body_file` 已拒掉相对路径），
    #: 校验与 ``gh pr merge --body-file`` 解析的是同一个文件（gh 的 cwd 是 ``--repo``，
    #: 而校验按进程 cwd 解析，相对路径会在两处指向不同文件）
    body_file: str


@dataclass(frozen=True)
class PollResult:
    """一次「远端分支是否已删」的轮询结果。"""

    outcome: str
    probes: int
    returncode: int
    key: str
    reason: str = ""


# --------------------------------------------------------------------- 输出

def _write_stderr_text(text: str) -> None:
    """往 stderr 写一段文本，**写不出去也不许抛**（否则 rc2 会变成 traceback + rc1）。

    与 ``cli._write_stderr_error`` 同款做法（刻意不 import cli：本模块独立可跑）：
    先问 stderr 的编码能不能表示整段，能就正常写，不能就按 UTF-8 字节直写
    ``stderr.buffer``，避免 ``UnicodeEncodeError`` 变成 traceback。

    另外三种「写不出去」也一并吞掉，它们都是同一个洞：``stderr`` 被关掉
    （``2>&-`` → ``sys.stderr is None``）、接的管道断开（``BrokenPipeError`` /
    ``EPIPE``）、流已关闭（``ValueError``）。这几种情况下抛出去只会让
    ``except MergeGuardError`` 自己崩掉 —— 结果是 traceback + rc1，正好撞上
    「任何异常都不许落 rc1」这条红线。stderr 崩了就是没人看得到，但退出码必须对。
    """

    stream = sys.stderr
    if stream is None:  # 2>&-
        return
    with suppress(OSError, ValueError, AttributeError):
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            text.encode(encoding)
        except UnicodeEncodeError:
            data = text.encode("utf-8")
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                buffer.write(data)
                buffer.flush()
            else:  # pragma: no cover - 没有 buffer 的替身对象（极少见）
                stream.write(data.decode("utf-8", "backslashreplace"))
            return
        stream.write(text)
        stream.flush()


def _write_stderr_error(message: str) -> None:
    """把 ``错误: …`` 写到 stderr，**在任何编码环境下都不崩**。"""

    _write_stderr_text(f"错误: {message}\n")


def _write_stderr_raw(text: str) -> None:
    """把外部命令的输出原文写 stderr（合并失败时要看原文）。"""

    if not text:
        return
    _write_stderr_text(text if text.endswith("\n") else f"{text}\n")


def _fileno(stream: object | None) -> int | None:
    """取流的 fd；没有 ``fileno`` / 已关闭 / 非真实流 → ``None``；``None`` 流当作 fd 1。

    ``sys.stdout is None`` 恰恰是 ``1>&-`` 的样子（Python 启动时 fd 1 已关），所以
    「没有流」= 目标就是 fd 1。
    """

    if stream is None:
        return 1
    fileno = getattr(stream, "fileno", None)
    if fileno is None:
        return None
    try:
        fd = fileno()
    except (OSError, ValueError):  # 已关闭 / 不支持 fileno（io.UnsupportedOperation ⊂ 两者）
        return None
    return fd if isinstance(fd, int) else None


def _silence_stdout(stream: object | None = None) -> None:
    """把 stdout 接到 ``/dev/null``：写不出去之后不再冒 traceback、不改退出码。

    只「吞掉异常」不够：``TextIOWrapper`` 缓冲区里剩下的字节会在解释器退出时再
    flush 一次，冒出 ``Exception ignored while flushing sys.stdout``，并且 Python 用
    **120** 作为「退出时 flush stdout 失败」的退出码 —— 结论被这套机制顶掉。所以按
    Python 官方建议（``signal`` 模块里 SIGPIPE 那一段）把 **fd 1 重定向到
    ``/dev/null``**，再把 ``sys.stdout`` 换成一个指向 ``/dev/null`` 的流：剩下的
    flush 写进空设备，退出码回到本次运行的真实结论。

    只在「失败的流真的是 fd 1」时动 fd 1 —— 测试里换成 ``StringIO`` 之类替身时
    不碰进程的 fd 1，免得把别的输出一起吞掉。stderr（fd 2）一律不动：``错误: …``
    必须还能被看到。
    """

    if _fileno(stream) == 1:
        devnull_fd = -1
        try:
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
        except OSError:  # pragma: no cover - 极端环境（/dev/null 打不开）
            devnull_fd = -1
        if devnull_fd >= 0:
            try:
                # fd 1 已关时 os.open 可能正好把它拿回来，那就已经指向 /dev/null 了
                if devnull_fd != 1:
                    os.dup2(devnull_fd, 1)
            except OSError:  # pragma: no cover
                pass
            finally:
                if devnull_fd > 1:
                    with suppress(OSError):
                        os.close(devnull_fd)
    with suppress(OSError, ValueError):
        # 故意不关：这个流要活到进程结束，退出时解释器 flush 它（写进 /dev/null）
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def _write_stdout(lines: list[str]) -> None:
    """一次性写出所有输出行；写不出去**不改变退出码**。

    - stdout 编码表示不了中文 → ``MergeGuardError``（rc2，与原口径一致，契约没变）；
    - 管道断开（``BrokenPipeError`` / ``OSError(EPIPE)``）、流已关闭
      （``ValueError``）、``sys.stdout is None``（``1>&-``）→ **不抛**：走
      :func:`_silence_stdout` 后静默返回，调用方按本次运行的**真实结论**返回退出码
      （本来 rc0 仍 0、本来 rc2 仍 2；不许变成 120 / 141，也不许落到 rc1）。
    """

    text = "\n".join(lines) + "\n"
    out = sys.stdout
    if out is None:
        _silence_stdout(out)
        return
    try:
        out.write(text)
        out.flush()
    except UnicodeEncodeError as exc:  # 必须排在 ValueError 前面：它是 ValueError 子类
        raise MergeGuardError(
            "当前 stdout 编码 "
            f"{getattr(sys.stdout, 'encoding', None)!r} 无法表示输出里的中文，"
            "请改用 UTF-8（例如 PYTHONIOENCODING=utf-8 / LC_ALL=C.UTF-8）"
        ) from exc
    except ValueError:  # 「I/O operation on closed file」
        _silence_stdout(out)
    except OSError:  # 含 BrokenPipeError / EPIPE / EBADF
        _silence_stdout(out)


def key_line(proc: CommandResult) -> str:
    """取一行「关键输出」：先 stdout 后 stderr 的最后一条非空行。"""

    for text in (proc.stdout, proc.stderr):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return "(无输出)"


def format_step(step: str, *, ok: bool, command: str, rc: int, key: str) -> str:
    """``<步骤> | <PASS|FAIL> | <命令原文> | rc=<n> | <关键输出一行>``。"""

    status = "PASS" if ok else "FAIL"
    return f"{step} | {status} | {command} | rc={rc} | {key}"


def format_verify_step(outcome: str, *, command: str, rc: int, key: str) -> str:
    """``远端 ref | <存在|不存在|无法判定> | <命令原文> | rc=<n> | <关键输出一行>``。

    为什么 ``--verify-only`` 这一步不用 ``PASS`` / ``FAIL``：它既没看到「合并前分支
    在」，也没看到合并动作，``ls-remote`` 的 ``rc=2`` 只说明「这个 remote 现在没有这个
    ref」，分不出「已删除」与「从未存在过」。修前这里打的是
    ``分支删除 | PASS | … | rc=2 | 分支已不在远端``：畸形 ``--branch``（空串 / 带空白 /
    带 ``refs/`` 前缀）拼出来的 ref 本来就查不到，工具却替它颁了「删除」证明。
    现在步骤行只报 ref 现状，``PASS`` / ``FAIL`` 与「已删」字样一律不出现。
    """

    state = VERIFY_STATE_TEXT.get(outcome, VERIFY_STATE_TEXT[OUT_UNDETERMINED])
    return f"{STEP_REMOTE_REF} | {state} | {command} | rc={rc} | {key}"


# --------------------------------------------------------------- 外部命令

def _run(argv: tuple[str, ...], *, cwd: str, timeout: float) -> CommandResult:
    """跑外部命令，不抛 CalledProcessError，交给调用方判 rc。

    ``timeout`` 是**单次子命令**的硬超时（``--command-timeout``）：一次卡住的
    ``gh`` / ``git`` 不许把整个合并卡拖到 worker 超时。超时抛
    :class:`CommandTimeout`（``MergeGuardError`` 子类，rc2）；轮询里的探针会把它
    转成 :data:`PROBE_ERROR`，继续轮询。

    ``PATH`` 里没有可执行文件、输出不是 UTF-8、其它 ``OSError`` → ``MergeGuardError``
    （rc2），不许冒 traceback，也不许落到 rc1。
    """

    try:
        # bandit B603/B607 假阳性：可执行文件名写死（"gh" / "git"，走 PATH），
        # 参数字符串以 argv 列表传入、shell=False，用户给的路径/分支名只作为独立
        # argv 元素，不经 shell 解释，无注入面
        proc = subprocess.run(  # nosec B603 B607
            list(argv),
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout(
            f"{argv[0]} 命令超时：{timeout:g} 秒内没有返回（见 --command-timeout）"
        ) from exc
    except FileNotFoundError as exc:
        raise MergeGuardError(
            f"找不到 {argv[0]} 可执行文件（PATH 里没有 {argv[0]}），无法合并/验证"
        ) from exc
    except UnicodeDecodeError as exc:
        raise MergeGuardError(
            f"{argv[0]} 输出不是 UTF-8，无法解析（第 {exc.start} 字节起解码失败）"
        ) from exc
    except OSError as exc:
        raise MergeGuardError(f"无法执行 {argv[0]}: {exc}") from exc
    return CommandResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def ls_remote_argv(repo: str, remote: str, branch: str) -> tuple[str, ...]:
    """判定「远端分支是否已删」的唯一命令。"""

    return (
        "git",
        "-C",
        repo,
        "ls-remote",
        "--exit-code",
        "--refs",
        remote,
        f"refs/heads/{branch}",
    )


def rebase_base_ref(pr: int) -> str:
    """本工具抓远端 base 提交的私有 ref：``refs/merge-guard/<pr>/base``。"""

    return f"{MERGE_GUARD_REF_PREFIX}/{pr}/base"


def rebase_head_ref(pr: int) -> str:
    """本工具抓远端 head 提交的私有 ref：``refs/merge-guard/<pr>/head``。"""

    return f"{MERGE_GUARD_REF_PREFIX}/{pr}/head"


def rebase_fetch_argv(
    repo: str, remote: str, pr: int, base: str, branch: str
) -> tuple[str, ...]:
    """判定「base 是不是 head 的祖先」的**第一条**命令：一次 fetch 抓两个 ref。

    为什么整条判定要落到私有 ref 上（不直接 ``git fetch`` 到 ``refs/remotes/<remote>/*``）：
    判定对象必须是**远端此刻**的提交，而不是本地某次 fetch 留下的旧对象。抓完之后
    ``rev-parse`` / ``merge-base`` 全部在这两个 ref 上做，判的就是「这一轮看到的远端」。

    ``--force`` 用 ``+`` 写在 refspec 里（``+refs/heads/<x>:refs/merge-guard/...``）：
    上一轮留下的 ref 可能与这一轮不同，不加 ``+`` 会非快进失败。

    **不清理**这两个 ref（刻意决定，理由见 :func:`rebase_gate` 的 docstring）。
    """

    return (
        "git",
        "-C",
        repo,
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--quiet",
        remote,
        f"+refs/heads/{base}:{rebase_base_ref(pr)}",
        f"+refs/heads/{branch}:{rebase_head_ref(pr)}",
    )


def rebase_rev_parse_argv(repo: str, ref: str) -> tuple[str, ...]:
    """``git rev-parse <ref>``：读远端抓下来的提交 SHA（交叉核对用）。"""

    return ("git", "-C", repo, "rev-parse", ref)


def rebase_merge_base_argv(repo: str, pr: int) -> tuple[str, ...]:
    """判定命令：``base`` 是不是 ``head`` 的祖先（rc0 是、rc1 不是、其它判不了）。"""

    return (
        "git",
        "-C",
        repo,
        "merge-base",
        "--is-ancestor",
        rebase_base_ref(pr),
        rebase_head_ref(pr),
    )


def classify_merge_base_rc(rc: int) -> str:
    """``merge-base --is-ancestor`` 的退出码分类：``0`` / ``1`` / 其它。

    ``0`` → :data:`ANCESTOR_YES`（base 是 head 的祖先 = 已 rebase，放行）；
    ``1`` → :data:`ANCESTOR_NO`（不是祖先 = 落后或分叉，拒绝）；
    其它（``128`` 对象缺失 / 通信错误 …）→ :data:`ANCESTOR_ERROR`，**同样拒绝**：
    判不了就不许合并，不许把「不知道」当成「通过」。
    """

    if rc == 0:
        return ANCESTOR_YES
    if rc == 1:
        return ANCESTOR_NO
    return ANCESTOR_ERROR


def merge_argv(
    pr: int,
    method: str,
    head_oid: str,
    subject: str,
    body_file: str,
    repo_slug: str,
) -> tuple[str, ...]:
    """合并 argv（顺序固定，不许改）。

    ``--merge`` / ``--delete-branch`` / ``--match-head-commit`` 三项语义一字不许动
    （R13 / R12 / 预检钉提交），认账的 ``--subject`` 与 ``--body-file`` 只能**追加**在
    ``--match-head-commit <sha>`` 之后：

        gh pr merge <pr> -R <owner/name> --merge --delete-branch
            --match-head-commit <sha> --subject <subject> --body-file <path>

    不带这两个参数时 gh 用默认文案生成合并提交 message，**没有任何 trailer**，
    经本工具合出来的提交必然过不了 ``merge-audit --require``（缺认账三字段）。

    ``-R <owner/name>`` 是**必须**的（PR #46 双问缺陷 P2-2）：不带 ``-R`` 时 gh 按
    ``GH_REPO`` 环境变量 / cwd 的 remote 选仓库，而本工具的仓库核对看的是
    ``git config --get remote.<name>.url`` —— 两者可以指向不同仓库，于是「核对的是 A、
    合并的是 B」。``-R`` 的优先级高于 ``GH_REPO``，钉住它才有「核对对象 = 操作对象」。
    """

    return (
        "gh",
        "pr",
        "merge",
        str(pr),
        "-R",
        repo_slug,
        f"--{method}",
        "--delete-branch",
        "--match-head-commit",
        head_oid,
        "--subject",
        subject,
        "--body-file",
        body_file,
    )


def pr_view_argv(
    pr: int, repo_slug: str, fields: str = _PR_VIEW_FIELDS
) -> tuple[str, ...]:
    """``gh pr view <n> -R <owner/name> --json <fields>``：仓库显式钉死。

    不带 ``-R`` 时 gh 按 ``GH_REPO`` 环境变量 / cwd 的 remote 选仓库（实测：``GH_REPO``
    指别仓时 ``gh pr view`` 读的是别仓的同号 PR，rc 仍是 0）—— 而本工具的仓库核对读的
    是 ``git config --get remote.<name>.url``。两者能各指一个仓库，于是「核对的是 A、
    操作的是 B」。``-R`` 的优先级高于 ``GH_REPO``，预检与合并都钉住它，
    「核对对象 = 操作对象」才成立（PR #46 双问缺陷 P2-2）。
    """

    return ("gh", "pr", "view", str(pr), "-R", repo_slug, "--json", fields)


class Probe:
    """一次 ``git ls-remote`` 探针；每次调用只肯等「剩余总预算」。

    ``--timeout`` 在 README 里是**总**等待预算（「总等待不超过 ``--timeout``」），而单次
    子命令只受 ``--command-timeout`` 约束 —— 两者一叠加，``--timeout 1
    --command-timeout 60`` 遇到一次卡 3 秒的 ``git`` 会实打实跑 3.1 秒，声明就不再成立。
    所以轮询每轮调 :meth:`with_limit`，把「还剩多少预算」交给探针，探针取
    ``min(--command-timeout, 剩余预算)`` 作为这一次的超时；剩余预算 ≤ 0 时轮询器根本
    不调用探针（不起子进程）。

    探针超时**不等于**判定：转成退出码 :data:`PROBE_TIMEOUT_RC` 的
    ``CommandResult``，由 :func:`classify_probe_rc` 判成 :data:`PROBE_ERROR`
    —— 记一次通信错误继续轮询，绝不当成「分支还在」（rc0）或「已删」（rc2）。
    其它环境错误（``git`` 不在 ``PATH``、输出不是 UTF-8）照旧 ``MergeGuardError``。
    """

    def __init__(self, repo: str, remote: str, branch: str, *, timeout: float) -> None:
        self.repo = repo
        self.remote = remote
        self.branch = branch
        #: ``--command-timeout``：单次子命令的硬上限（永远不越过它）
        self.timeout = timeout

    def with_limit(self, limit: float) -> Callable[[], CommandResult]:
        """返回一个「最多等 ``min(--command-timeout, limit)`` 秒」的无参探针。"""

        budget = min(float(self.timeout), max(0.0, float(limit)))
        argv = ls_remote_argv(self.repo, self.remote, self.branch)

        def probe() -> CommandResult:
            if budget <= 0:  # 兜底：调用方本该在预算耗尽时直接判「无法判定」
                return CommandResult(
                    argv=argv,
                    returncode=PROBE_TIMEOUT_RC,
                    stdout="",
                    stderr="--timeout 总预算已用完，未执行 git ls-remote\n",
                )
            try:
                return _run(argv, cwd=self.repo, timeout=budget)
            except CommandTimeout as exc:
                return CommandResult(
                    argv=argv,
                    returncode=PROBE_TIMEOUT_RC,
                    stdout="",
                    stderr=f"{exc}\n",
                )

        return probe

    def __call__(self) -> CommandResult:
        """按 ``--command-timeout`` 全量调用（没有预算约束时的用法）。"""

        return self.with_limit(self.timeout)()


def make_probe(repo: str, remote: str, branch: str, *, timeout: float) -> Probe:
    """造一个 :class:`Probe`（``timeout`` = ``--command-timeout``）。"""

    return Probe(repo, remote, branch, timeout=timeout)


def classify_probe_rc(rc: int) -> str:
    """``ls-remote --exit-code`` 的退出码 → 三种判定。

    - ``0``：ref 存在 → 分支仍在远端
    - ``2``：没有这个 ref → 已删（``--exit-code`` 的语义）
    - 其它：通信 / 仓库错误 → 判不了，记一次继续轮询
    """

    if rc == 0:
        return PROBE_PRESENT
    if rc == 2:
        return PROBE_DELETED
    return PROBE_ERROR


def _call_probe(probe: Callable[..., CommandResult], limit: float) -> CommandResult:
    """按剩余预算调用探针。

    有 ``with_limit``（本模块的 :class:`Probe`）就夹到剩余预算；测试里塞进来的裸函数
    照原样零参调用 —— 预算闸门由本函数的调用方（:func:`poll_branch_deleted`）先判一遍。
    """

    factory: Callable[[float], Callable[[], CommandResult]] | None = getattr(
        probe, "with_limit", None
    )
    if factory is not None:
        probe_fn = factory(limit)
        return probe_fn()
    return probe()


def poll_branch_deleted(
    probe: Callable[..., CommandResult],
    *,
    timeout: int,
    interval: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> PollResult:
    """轮询 ``probe`` 直到确认分支已删或总预算用完；**总等待不超过 ``timeout`` 秒**。

    ``timeout`` 是总预算，不是「每轮各等这么长」：

    - 每次探针的超时 = ``min(--command-timeout, 剩余预算)``（见 :class:`Probe`），
      所以一次卡住的 ``git`` 最多只吃掉剩余预算，不会把整条命令拖成
      ``--timeout + --command-timeout``；
    - 剩余预算 ≤ 0 时**不再起子进程**：已有明确结论（上一轮 ``rc=0`` = 分支还在）就按
      它返回，否则判「无法判定」并说清 ``--timeout`` 已用完。
    """

    deadline = clock() + timeout
    probes = 0
    errors = 0
    last: CommandResult | None = None
    while True:
        remaining = deadline - clock()
        if probes and remaining <= 0:
            # 预算已用完：不许再起子进程（超时传 0 的 subprocess 语义是「立即超时」，
            # 那只会把「判不了」伪装成一次通信错误）。
            last_rc = last.returncode if last is not None else PROBE_TIMEOUT_RC
            if last is not None and classify_probe_rc(last.returncode) == PROBE_PRESENT:
                return PollResult(
                    outcome=OUT_PRESENT,
                    probes=probes,
                    returncode=last_rc,
                    key=key_line(last),
                )
            return PollResult(
                outcome=OUT_UNDETERMINED,
                probes=probes,
                returncode=last_rc,
                key=key_line(last) if last is not None else "(无探针输出)",
                reason=(
                    f"共 {probes} 次检查、其中 {errors} 次通信错误，"
                    f"最后一次退出码 {last_rc}；"
                    f"--timeout={timeout} 秒总预算已用完，未再起新探针"
                ),
            )
        proc = _call_probe(probe, remaining)
        probes += 1
        last = proc
        kind = classify_probe_rc(proc.returncode)
        if kind == PROBE_DELETED:
            return PollResult(
                outcome=OUT_DELETED,
                probes=probes,
                returncode=proc.returncode,
                key="分支已不在远端",
            )
        if kind == PROBE_ERROR:
            errors += 1
        remaining = deadline - clock()
        if remaining <= 0:
            if kind == PROBE_PRESENT:
                return PollResult(
                    outcome=OUT_PRESENT,
                    probes=probes,
                    returncode=proc.returncode,
                    key=key_line(proc),
                )
            return PollResult(
                outcome=OUT_UNDETERMINED,
                probes=probes,
                returncode=proc.returncode,
                key=key_line(proc),
                reason=(
                    f"共 {probes} 次检查、其中 {errors} 次通信错误，"
                    f"最后一次退出码 {proc.returncode}；"
                    f"--timeout={timeout} 秒总预算已用完"
                ),
            )
        sleep(min(float(interval), remaining))


# --------------------------------------------------------------- 参数校验

def parse_positive_int(flag: str, raw: str, *, maximum: int | None = None) -> int:
    """``flag`` 的值必须是 ≥1 的 ASCII 十进制整数（可带 ``maximum`` 上限）。"""

    if not _DIGITS.fullmatch(raw):
        raise MergeGuardError(
            f"{flag} 必须是 ≥1 的 ASCII 十进制整数，实际是 {raw!r}"
        )
    value = int(raw)
    if value < 1:
        raise MergeGuardError(f"{flag} 必须 ≥1，实际是 {raw!r}")
    if maximum is not None and value > maximum:
        raise MergeGuardError(
            f"{flag} 超出取值范围（1–{maximum}），实际是 {raw!r}"
        )
    return value


def check_branch_is_not_main(branch: str) -> None:
    """拒绝 ``main`` / ``master``（合并后删除会毁掉主分支）。"""

    if branch in MAIN_BRANCHES:
        raise MergeGuardError(
            f"--branch {branch} 是主分支，拒绝合并（合并后删除会毁掉主分支）"
        )


def check_branch_shape(branch: str) -> None:
    """``--branch`` 必须是**裸分支名**：非空、不含空白字符、不带 ``refs/`` 前缀。

    为什么必须在 ``validate`` 里拦（一切 ``gh`` / ``git`` 调用之前）：探针入参是
    ``refs/heads/<--branch>`` 直接拼出来的，畸形输入拼出的 ref 在远端**永远不存在**，
    ``git ls-remote --exit-code`` 一律 ``rc=2`` —— 而 ``rc=2`` 的语义是「没有这个 ref」。
    合并路径至少还有「合并前分支必须在」那道闸门（也会误报成「不存在」但至少不合并），
    ``--verify-only`` 没有闸门：修前它把 ``rc=2`` 读成「已删除」并打「分支删除 | PASS」
    + rc0，等于替一个从没查过的 ref 颁了删除证明（pm 实测：``--branch ''`` /
    ``' wt/t_x'`` / ``'refs/heads/wt/t_x'`` 三种输入都是 rc0 + PASS）。

    三条规则都只有一行 ``错误: …``（不带换行），说清是哪条：
    :data:`BRANCH_RULE_EMPTY` / :data:`BRANCH_RULE_WHITESPACE` /
    :data:`BRANCH_RULE_REFS_PREFIX`。
    """

    if not branch:
        raise MergeGuardError(
            f"--branch {BRANCH_RULE_EMPTY}：要的是裸分支名（例 wt/t_1234abcd）；"
            "空串会拼成 refs/heads/ 这个不存在的 ref，ls-remote 的 rc=2 会被读成「已删除」"
        )
    for index, char in enumerate(branch):
        if char.isspace():
            raise MergeGuardError(
                f"--branch {branch!r} {BRANCH_RULE_WHITESPACE}"
                f"（第 {index + 1} 个字符是 {char!r}）：要的是裸分支名，"
                "空白不会被 Git 规范化掉，refs/heads/<含空白> 在远端永远查不到"
            )
    if branch.startswith("refs/"):
        raise MergeGuardError(
            f"--branch {branch!r} {BRANCH_RULE_REFS_PREFIX}：要的是裸分支名"
            "（例 wt/t_1234abcd），本工具自己拼 refs/heads/；"
            "重复前缀会查成 refs/heads/refs/… 而永远不存在"
        )


def check_base_ref_shape(base: str) -> None:
    """``gh pr view`` 读到的 ``baseRefName`` 必须是**裸分支名**。

    与 :func:`check_branch_shape` 同一条规矩、同三条判据（空串 / 含空白字符 / 带
    ``refs/`` 前缀），但文案**必须点名 ``baseRefName``**：这个值来自 gh 的预检输出，
    不是调用方给的 ``--branch``，复述 ``--branch`` 那套文案只会让人去改一个没问题的参数。

    为什么也要在 ``fetch`` 之前拦：``base`` 会被拼成 refspec 的源侧
    （``+refs/heads/<base>:…``），畸形值要么 fetch 直接失败（rc2 但报错指向 git），
    要么更糟 —— 拼出一个本地存在、远端却不存在的 ref。位置在 :func:`rebase_gate`
    第一步，畸形 ``baseRefName`` 闸门自身不发任何 git 调用（此前已跑过一次 ``ls-remote``
    存在性探针，那是合并前的源分支存在闸门）。
    """

    if not base:
        raise MergeGuardError(
            f"gh pr view 的 baseRefName {BRANCH_RULE_EMPTY}："
            "要的是裸分支名（例 main）；空串会拼成 refs/heads/ 这个不存在的 ref，"
            "无法判定 head 是否已 rebase 到 base，拒绝合并"
        )
    for index, char in enumerate(base):
        if char.isspace():
            raise MergeGuardError(
                f"gh pr view 的 baseRefName {base!r} {BRANCH_RULE_WHITESPACE}"
                f"（第 {index + 1} 个字符是 {char!r}）：要的是裸分支名，"
                "空白不会被 Git 规范化掉，refs/heads/<含空白> 在远端永远查不到，"
                "无法判定 head 是否已 rebase 到 base，拒绝合并"
            )
    if base.startswith("refs/"):
        raise MergeGuardError(
            f"gh pr view 的 baseRefName {base!r} {BRANCH_RULE_REFS_PREFIX}："
            "要的是裸分支名（例 main），本工具自己拼 refs/heads/；"
            "重复前缀会抓成 refs/heads/refs/… 而永远不存在，"
            "无法判定 head 是否已 rebase 到 base，拒绝合并"
        )


def check_subject(subject: str) -> str:
    """``--subject`` 必须是**单行非空白**的合并提交标题。

    三种非法形态各一行 ``错误: …``：空串、去掉首尾空白后为空、含换行（``\\n`` / ``\\r``）。
    标题为空时 git 的 ``%s`` 会把第一行 trailer（``merged-by: …``）当提交标题，
    认账块随之断掉；标题含换行时 gh 会把 ``--subject`` 当两行用，标题与正文边界不再确定。
    """

    if not subject:
        raise MergeGuardError(
            "--subject 不能为空：合并提交标题必填（空标题会让 git 把 merged-by 行当标题）"
        )
    for char in subject:
        if char in ("\n", "\r"):
            raise MergeGuardError(
                f"--subject {subject!r} 不能含换行字符：--subject 只收单行标题，"
                "正文（认账三字段）请放进 --body-file"
            )
    if not subject.strip():
        raise MergeGuardError(
            f"--subject {subject!r} 去掉首尾空白后为空：合并提交标题必填"
        )
    return subject


def read_body_file(raw: str) -> str:
    """读 ``--body-file`` 并校验：非空路径 / **必须是绝对路径** / 存在 / 普通文件 / 严格
    UTF-8 / 非空文件。

    为什么逐条判、不用 ``Path.read_text()`` 一把梭：``read_text`` 对「不存在」「是目录」
    「非 UTF-8」抛的是三种不同异常（``FileNotFoundError`` / ``IsADirectoryError`` /
    ``UnicodeDecodeError``），漏一种就冒 traceback；裸冒 traceback 是 rc1，而 rc1 是
    「未达成」专用码 —— 参数错的合并请求绝不许落到它上面。

    为什么必须绝对路径：本函数按**进程 cwd** 解析路径，而 ``gh pr merge`` 的 cwd 是
    ``--repo``（见 ``_run(..., cwd=opts.repo)``）。同一个相对路径在两处指向不同文件 ——
    校验读到 caller 的那份、gh 读 ``--repo`` 下那份：轻则「文件不存在」被 gh 判成合并
    失败、rc1 冒充「未达成」，重则 gh 静默合进另一份 body，合出**没有任何 trailer** 的
    提交（正是 R15-A 的红样例）。所以相对路径在这一步就 rc2 拒掉，交给 gh 的路径与
    校验的路径恒为同一个绝对路径。
    """

    if not raw:
        raise MergeGuardError(
            "--body-file 路径为空：要的是认账 body 文件（三行 trailer："
            "merged-by / merge-card / merge-pr）"
        )
    if not os.path.isabs(raw):
        raise MergeGuardError(
            f"--body-file 必须是绝对路径: {raw!r}（本工具按进程 cwd 校验、gh pr merge 的 "
            "cwd 是 --repo，同一个相对路径会解析成两个不同文件：轻则 gh 报读不到、rc1 冒充"
            "「未达成」，重则静默合进另一份 body、合出没有 trailer 的提交）"
        )
    try:
        info = os.stat(raw)
    except FileNotFoundError as exc:
        raise MergeGuardError(f"--body-file 文件不存在: {raw}") from exc
    except OSError as exc:
        raise MergeGuardError(f"--body-file 无法读取 {raw}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise MergeGuardError(
            f"--body-file 不是普通文件: {raw}（要的是含认账三字段的文本文件）"
        )
    try:
        with open(raw, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise MergeGuardError(f"--body-file 无法读取 {raw}: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MergeGuardError(
            f"--body-file 不是严格 UTF-8 文本: {raw}（第 {exc.start} 字节起解码失败）"
        ) from exc
    if not text:
        raise MergeGuardError(f"--body-file 是空文件: {raw}（认账 body 必须含三行 trailer）")
    return text


def validate_accounting(args: argparse.Namespace, pr: int) -> Accounting | None:
    """校验认账四参数；``--verify-only`` 时返回 ``None``（且一个都不许给）。

    全部在 :func:`validate` 里跑完 —— 即 ``ensure_branch_present()`` 之前，零 ``gh``
    调用、零 ``git ls-remote``。任一不满足 → ``MergeGuardError``（rc2、stdout 空、
    stderr 恰一行）。

    正则一律复用 :mod:`team_board.merges`（``MERGE_CARD_RE`` / ``MERGE_BY_RE``）与
    ``check_message``，**不在本模块复制一份** —— 两份口径迟早会分叉，而分叉的那一侧
    会让合并提交通过本工具、却被 ``merge-audit --require`` 判红。
    """

    given = [
        flag
        for flag, value in (
            ("--card", args.card),
            ("--by", args.by),
            ("--subject", args.subject),
            ("--body-file", args.body_file),
        )
        if value is not None
    ]
    if args.verify_only:
        if given:
            raise MergeGuardError(
                f"--verify-only 与 {'/'.join(given)} 互斥：verify-only 不合并、"
                "不产生合并提交，给了认账参数说明调用方误判"
            )
        return None

    for flag in ACCOUNTING_FLAGS:
        value = {
            "--card": args.card,
            "--by": args.by,
            "--subject": args.subject,
            "--body-file": args.body_file,
        }[flag]
        if value is None:
            raise MergeGuardError(
                f"合并命令缺少 {flag}（合并提交必须带认账三字段 merged-by / merge-card / "
                "merge-pr；没有 --subject / --body-file 时 gh 用默认文案，合出来的提交"
                "过不了 merge-audit --require）"
            )

    card = args.card
    if MERGE_CARD_RE.fullmatch(card) is None:
        raise MergeGuardError(
            f"--card 形态非法：{card!r}（应为 t_ 加 8 位小写十六进制，例 t_1234abcd）"
        )
    by = args.by
    if MERGE_BY_RE.fullmatch(by) is None:
        raise MergeGuardError(
            f"--by 形态非法：{by!r}（应为 <profile>/run<NNN>，例 pm/run200）"
        )
    subject = check_subject(args.subject)
    body_file = args.body_file
    body = read_body_file(body_file)

    problems = check_message(body, card=card, by=by, pr=pr)
    if problems:
        detail = "；".join(f"{problem.kind}：{problem.detail}" for problem in problems)
        raise MergeGuardError(
            f"--body-file 内容不合认账字段要求（{body_file}）：{detail}；"
            "可先用 team-board merge-record 生成 body"
        )
    return Accounting(card=card, by=by, subject=subject, body_file=body_file)


def parse_pr_view(proc: CommandResult, pr: int) -> Preflight:
    """解析 ``gh pr view --json number,state,headRefName,baseRefName``。"""

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MergeGuardError(
            f"gh pr view {pr} 的输出不是 JSON，无法解析: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise MergeGuardError(f"gh pr view {pr} 的输出不是 JSON 对象，无法解析")
    try:
        number = payload["number"]
        state = payload["state"]
        head_ref = payload["headRefName"]
        base_ref = payload["baseRefName"]
        head_oid = payload["headRefOid"]
    except KeyError as exc:
        raise MergeGuardError(
            f"gh pr view {pr} 的输出缺少字段 {exc.args[0] if exc.args else '?'}"
        ) from exc
    if not isinstance(number, int) or isinstance(number, bool):
        raise MergeGuardError(f"gh pr view {pr} 的 number 不是整数: {number!r}")
    if not isinstance(state, str) or not isinstance(head_ref, str) or not isinstance(
        base_ref, str
    ):
        raise MergeGuardError(f"gh pr view {pr} 的 state/headRefName/baseRefName 不是字符串")
    if not isinstance(head_oid, str) or not _HEAD_OID.fullmatch(head_oid):
        # 没有合法的 head sha 就没法钉住「合的是哪个提交」——不许退回「只比分支名」
        raise MergeGuardError(
            f"gh pr view {pr} 的 headRefOid 不是 40/64 位十六进制提交 SHA: {head_oid!r}"
        )
    return Preflight(
        number=number,
        state=state,
        head_ref=head_ref,
        base_ref=base_ref,
        head_oid=head_oid,
    )


# ---------------------------------------------------- 合并前的两道证据闸门

def _claim_row_ids(rows: tuple[prclaim.ClaimRow, ...]) -> str:
    """认领卡的卡号清单（拒绝文案里用；0 张时是 ``(无)``）。"""

    return ",".join(row.id for row in rows) if rows else "(无)"


#: GitHub remote URL → ``owner/name``。两种形态都要认（本仓 ``origin`` 是 scp 形态的
#: ``git@github.com:alsj213/team-board.git``，``origin-https`` 是 https 形态，见 R10）：
#:   ``[user@]github.com:owner/name(.git)``            —— scp 形态（ssh）
#:   ``{ssh,git,http,https}://[user@]github.com[:port]/owner/name(.git)`` —— URL 形态
_REMOTE_SLUG_RE = re.compile(
    r"""
    (?:
        ^(?:[A-Za-z0-9._-]+@)?github\.com:
      |
        ^(?:ssh|git|https?)://(?:[A-Za-z0-9._-]+@)?github\.com(?::\d+)?/
    )
    (?P<slug>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)
    (?:\.git)?
    /?
    $
    """,
    re.VERBOSE | re.ASCII,
)


def parse_github_slug(url: str) -> str | None:
    """remote URL → ``owner/name``（小写）；不是 GitHub 形态 → ``None``。

    纯函数：不碰文件系统、不调外部命令。只认 GitHub —— 别的 host（GitLab / 本地裸库 /
    ``file://``）一律 ``None``，由调用方按「判不了」拒绝（不知道 ≠ 通过）。
    """

    match = _REMOTE_SLUG_RE.match((url or "").strip())
    if match is None:
        return None
    return match.group("slug").lower()


#: remote URL 里的 userinfo 段（``user:token@`` / ``user@``）。报错正文只许说「不是
#: GitHub 形态」，**不许**把凭据回显出去：stderr 会进卡片、CI 日志、终端 scrollback，
#: 一次回显就等于把 token 抄了一遍（PR #46 双问缺陷 P2-1，实测报错正文含 token）。
#:
#: 判据按**位置**而不按字符集：``://`` 之后到该串**最后一个** ``@`` 之前的全部内容都是
#: userinfo。用字符集（旧版 ``[^@/]*``）会漏两种真实现场（PR #46 复验 P2，head
#: ``edb3c2c8`` 实测）：密码含 ``/``（``a55w/0rdTOKEN`` → 整段不匹配、原样回显）与密码
#: / 用户名含 ``@``（``p@ssword9@`` → 只换掉首个 ``@`` 前的部分，尾部 ``ssword9`` 残留）。
_SCHEME_PREFIX_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)")
_SCP_USERINFO_RE = re.compile(r"^(?P<user>[^@/\s]+)@(?=[A-Za-z0-9.-]+:)")


def redact_url(url: str) -> str:
    """把 URL 的 userinfo 段换成 ``***``（纯函数，不碰文件系统、不调外部命令）。

    两种形态都脱敏：

    - 带 scheme 的：``://`` 之后到该串**最后一个** ``@`` 之前的整段 → ``***``，
      所以 ``https://user:token@github.com/o/n.git`` → ``https://***@github.com/o/n.git``，
      密码里含 ``/`` 或 ``@``（``a55w/0rdTOKEN`` / ``p@ssword9``）、多个 ``@`` 混合同样整段
      换成 ``***``；没有 ``@`` 的 URL 原样返回，host / path 不动。
    - scp 形态：``user@github.com:o/n.git`` → ``***@github.com:o/n.git``。

    本函数只负责「别把凭据抄出去」，不负责判定形态。
    """

    text = (url or "").strip()
    prefix = _SCHEME_PREFIX_RE.match(text)
    if prefix is not None:
        rest = text[prefix.end() :]
        at = rest.rfind("@")
        if at < 0:
            return text
        return f"{prefix.group('scheme')}***{rest[at:]}"
    return _SCP_USERINFO_RE.sub("***@", text, count=1)


def remote_repo_slug(opts: Options) -> str:
    """``--repo`` + ``--remote`` 指向的 GitHub 仓库 ``owner/name``；判不了 → rc2。

    读的是**配置里的原始 URL**（``git config --get remote.<name>.url``），不是
    ``git remote get-url``：后者会把 ``url.<base>.insteadOf`` 的改写结果吐出来，
    镜像 / 转发的 URL 看起来就不是 GitHub 的了，而「这是哪个仓库」该按配置回答。

    只读一条 ``git config --get``（不联网、不写）。读不到 / 不是 GitHub URL 都拒绝：
    双问闸门要核对的正是「读的是不是这个仓库的评审」，判不了就不许放行。

    调用方必须把结果显式钉进**每一条** gh 调用（``gh pr view <n> -R <slug>`` /
    ``gh pr merge <n> -R <slug>``）：不钉 ``-R`` 时 gh 按 ``GH_REPO`` / cwd 选仓库，
    于是核对的仓库与操作的仓库可以是两个（PR #46 双问缺陷 P2-2）。
    """

    argv = ("git", "-C", opts.repo, "config", "--get", f"remote.{opts.remote}.url")
    proc = _run(argv, cwd=opts.repo, timeout=opts.command_timeout)
    if proc.returncode != 0:
        raise MergeGuardError(
            f"读不到 --repo {opts.repo} 的 remote {opts.remote} 配置"
            f"（git config --get remote.{opts.remote}.url rc={proc.returncode}："
            f"{key_line(proc)}），无法判定双问绑定的仓库就是 {reviewhead.REPO}；拒绝合并"
        )
    url = proc.stdout.strip()
    slug = parse_github_slug(url)
    if slug is None:
        # 配置里可能带凭据（``https://x-access-token:<token>@github.com/...``），
        # 报错正文只许出现脱敏形态：stderr 会进卡片 / CI 日志 / scrollback。
        raise MergeGuardError(
            f"--repo {opts.repo} 的 remote {opts.remote} 不是 GitHub 形态"
            f"（git config --get remote.{opts.remote}.url 输出 {redact_url(url) or '(空)'!r}；"
            "URL 里的 userinfo 已脱敏为 ***），"
            f"无法判定双问绑定的仓库就是 {reviewhead.REPO}；拒绝合并"
            "（跨仓 / 非 GitHub 远端时不许拿别的仓库的评审判定）"
        )
    return slug


def claim_gate(opts: Options, info: Preflight) -> str:
    """**合并前认领唯一性闸门**（R17⑤）：``PR-CLAIM`` 唯一且就是 ``--card``，否则 rc2。

    判据直接调 :func:`team_board.prclaim.audit`（``pr-claim-audit`` 子命令的同一份实现，
    禁止第二份判定逻辑）与 ``Report.sole_claim_id``（恰好一张才给卡号，0 张或 ≥2 张给
    ``None``）—— 本模块**不**从渲染文本里抠卡号。

    三种拒绝文案一眼可分（都是一行 ``错误: …``、stdout 0 字节）：

    - 0 张认领 → ``无未结卡认领该 PR``；
    - ≥2 张认领 → ``重复认领``（附卡号清单）；
    - 恰好一张但不是 ``--card`` → ``认领卡 ≠ --card``。

    这一步一次 gh 都不调（只读看板库，``mode=ro``）。库不可用同样是 rc2 且要在拒绝前
    就把原因说清 —— 「读不到库」不许被读成「没有卡认领」。
    """

    accounting = opts.accounting
    if accounting is None:  # pragma: no cover - validate 已拦住（合并模式必填）
        raise MergeGuardError("合并模式缺少认账参数（内部错误）")
    card = accounting.card
    source = f"看板库 {opts.db_path}（未结卡，mode=ro）"
    try:
        conn = db.connect(opts.db_path)
    except (db.BoardError, OSError) as exc:
        # 点出参数名 --db：``merge-record`` 打印的合并命令**不带** --db（走默认值），
        # 默认路径换机/换 HERMES_HOME 就失效；只写「读看板库失败」时调用方不知道
        # 该改哪个参数（PR #45 第二视角缺陷项 9 / 回归项 2）。
        raise MergeGuardError(
            f"读看板库失败（--db {opts.db_path}），无法判定 PR #{opts.pr} 的认领唯一性："
            f"{exc}；拒绝合并（认领闸门只读 --db 指向的库，缺省 {DEFAULT_DB}）"
        ) from exc
    try:
        rows = db.iter_claim_rows(conn)
    except (db.BoardError, OSError) as exc:
        raise MergeGuardError(
            f"读未结卡正文失败，无法判定 PR #{opts.pr} 的认领唯一性：{exc}；拒绝合并"
        ) from exc
    finally:
        conn.close()

    report = prclaim.audit(rows, opts.pr)
    if not report.ok:
        if not report.claims:
            raise MergeGuardError(
                f"认领闸门拦下 PR #{opts.pr}：{prclaim.VERDICT_NONE}；"
                "拒绝合并（R17：一次只许一张卡对同一 PR 发 gh 写操作）"
            )
        raise MergeGuardError(
            f"认领闸门拦下 PR #{opts.pr}：{prclaim.VERDICT_DUPE}"
            f"（认领卡 {_claim_row_ids(report.claims)}）；拒绝合并"
        )
    sole = report.sole_claim_id
    if sole != card:
        raise MergeGuardError(
            f"认领闸门拦下 PR #{opts.pr}：认领卡 {sole} ≠ --card {card}"
            "（认领卡号必须与 --card 逐字一致，R17）；拒绝合并"
        )
    return format_step(
        STEP_CLAIM,
        ok=True,
        # 这一步没有外部命令：判据是本地只读库 + 纯函数，命令格写明数据来源
        command=f"prclaim.audit({source}, pr=#{opts.pr})",
        rc=0,
        key=f"认领唯一 → {sole}（与 --card 逐字一致）",
    )


def review_head_gate(opts: Options, info: Preflight, repo_slug: str) -> str:
    """**合并前双问绑定闸门**（R18④）：该 head sha 上 defect + regression 各 ≥1 条。

    取数与判定全调 :mod:`team_board.reviewhead`（``pr-review-head-audit`` 子命令的同一份
    实现）：``fetch_bodies(pr)`` 只发两条 ``gh api GET``（``--paginate``，只看 body），
    ``analyze(head_oid, bodies)`` 是纯函数。取数失败（gh 不在 PATH / 非 0 / 超时 / 输出
    不是 JSON）一律 rc2 —— 「读不到评审」不许被读成「双问没做」（那是 rc1 语义，本工具
    只有 rc2 这一档拒绝）。

    ``repo_slug`` 由调用方在**预检 gh 调用之前**算好（:func:`remote_repo_slug`，即
    ``--repo`` 的 ``git config --get remote.<name>.url`` 解析出的 ``owner/name``）：

    ``reviewhead`` 的 ``gh api`` 路径前缀写死 ``REPO = alsj213/team-board``，不看
    ``--repo``。跨仓时它会去读**另一个仓库**同号 PR 的 Review —— 方向虽是拒绝（几乎必
    「双问缺证」），但判的是别人的评审，不是本次合并对象的。所以这里先与
    ``reviewhead.REPO`` 对齐，不一致（或 URL 解析不出 GitHub 形态）就 rc2 拒绝。
    同一个 slug 也正是调用方钉进 ``gh pr view -R`` / ``gh pr merge -R`` 的那个值：
    「核对的是 A、操作的是 B」不许成立。

    三种拒绝文案一眼可分：

    - 一条结构化标记都没有 → ``双问缺证``；
    - 有标记但都不在当前 head 上 → ``双问陈旧``（head 变了没重跑双问）；
    - 当前 head 上缺某一类 → ``双问缺类``（附缺的类）。
    """

    if repo_slug != reviewhead.REPO.lower():
        raise MergeGuardError(
            f"双问绑定闸门拦下 PR #{opts.pr}：--repo {opts.repo}（remote {opts.remote}）"
            f"对应仓库 {repo_slug} ≠ 双问判定仓库 {reviewhead.REPO}"
            "（reviewhead 的 gh api 路径前缀写死，跨仓会拿别的仓库同号 PR 的评审当证据）；拒绝合并"
        )
    head = info.head_oid
    short = head[:12]
    try:
        bodies = reviewhead.fetch_bodies(opts.pr)
    except reviewhead.ReviewHeadError as exc:
        raise MergeGuardError(
            f"取 PR #{opts.pr} 的评审正文失败，无法判定双问是否绑定 head {short}："
            f"{exc}；拒绝合并"
        ) from exc
    report = reviewhead.analyze(head, bodies)
    if not report.ok:
        if not report.marks:
            raise MergeGuardError(
                f"双问缺证：PR #{opts.pr} 的评审正文（{report.bodies_scanned} 条）里"
                "没有任何 REVIEW-HEAD / REVIEW-KIND 标记"
                f"（{reviewhead.CONCLUSION_NO_MARK}）；拒绝合并"
            )
        if not report.kinds_on_head:
            raise MergeGuardError(
                f"双问陈旧：PR #{opts.pr} 有 {len(report.marks)} 条结构化标记，"
                f"但都不是当前 head {short}（head 变了要重跑双问）；拒绝合并"
            )
        raise MergeGuardError(
            "双问缺类："
            f"{reviewhead.CONCLUSION_MISSING.format(short=short, missing=','.join(report.missing))}"
            "；拒绝合并"
        )
    return format_step(
        STEP_REVIEW_HEAD,
        ok=True,
        command=(
            f"gh api repos/{reviewhead.REPO}/pulls/{opts.pr}/reviews --paginate"
            f" + gh api repos/{reviewhead.REPO}/issues/{opts.pr}/comments --paginate"
        ),
        rc=0,
        key=(
            f"head {short} 上 defect + regression 各 >=1 条"
            f"（扫描正文 {report.bodies_scanned} 条、带标记 {len(report.marks)} 条）"
        ),
    )


# ------------------------------------------------------------------ 命令行

class _Parser(argparse.ArgumentParser):
    """argparse 的报错也走 ``MergeGuardError``：rc2 只留一行 ``错误: …``。"""

    def error(self, message: str) -> NoReturn:
        raise MergeGuardError(message)


def build_parser() -> argparse.ArgumentParser:
    """本工具的命令行定义（没有关掉删除的开关，也没有绕过 PR 检查的口子）。"""

    parser = _Parser(
        prog="python3 -m team_board.merge_guard",
        description=(
            "合并闸门：合并命令必须带 --delete-branch，"
            "并在合并后用 git ls-remote 机器确认远端源分支真的消失。"
        ),
    )
    parser.add_argument(
        "--pr", required=True, metavar="N", help=f"PR 号（1–{MAX_PR} 的整数）"
    )
    parser.add_argument(
        "--branch",
        required=True,
        metavar="NAME",
        help="PR 的 head 分支名（必须与 PR 的 headRefName 逐字一致）",
    )
    parser.add_argument(
        "--method",
        default=DEFAULT_METHOD,
        choices=METHODS,
        help=(
            f"合并方式（只允许 {DEFAULT_METHOD}，默认 {DEFAULT_METHOD}，可省略；"
            "squash / rebase 一律拒绝）"
        ),
    )
    parser.add_argument(
        "--delete-branch",
        action="store_true",
        help="必填：合并后删除远端源分支（缺了就拒绝，本工具没有不删的路径）",
    )
    parser.add_argument("--repo", default=".", metavar="PATH", help="仓库路径（默认 .）")
    parser.add_argument("--remote", default="origin", metavar="NAME", help="远端名（默认 origin）")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        metavar="PATH",
        help=(
            "看板 SQLite 路径，只读（认领唯一性闸门用；"
            f"默认 {DEFAULT_DB}）"
        ),
    )
    parser.add_argument(
        "--timeout",
        default="60",
        metavar="SEC",
        help=(
            "删除验证的**总**等待秒数（默认 60，1–3600）；每次探针的超时取"
            " min(--command-timeout, 剩余预算)，预算用完即判「无法判定」"
        ),
    )
    parser.add_argument(
        "--interval",
        default="2",
        metavar="SEC",
        help=(
            f"删除验证的轮询间隔秒数（默认 2，1–{MAX_INTERVAL}，且不得大于 --timeout）"
        ),
    )
    parser.add_argument(
        "--command-timeout",
        default=str(DEFAULT_COMMAND_TIMEOUT),
        metavar="SEC",
        help=(
            "单次 gh / git 子命令的超时秒数"
            f"（默认 {DEFAULT_COMMAND_TIMEOUT}，1–{MAX_COMMAND_TIMEOUT}）"
        ),
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="不合并，只验证远端分支是否已删（巡检 / 取红样例用）",
    )
    parser.add_argument(
        "--card",
        default=None,
        metavar="CARD",
        help=(
            "必填（合并模式）：合并提交的 merge-card（形如 t_1234abcd）；"
            "--verify-only 时不许给"
        ),
    )
    parser.add_argument(
        "--by",
        default=None,
        metavar="WHO",
        help=(
            "必填（合并模式）：合并提交的 merged-by（形如 pm/run200）；"
            "--verify-only 时不许给"
        ),
    )
    parser.add_argument(
        "--subject",
        default=None,
        metavar="TEXT",
        help=(
            "必填（合并模式）：合并提交标题（单行非空白，含换行 → rc2）；"
            "--verify-only 时不许给"
        ),
    )
    parser.add_argument(
        "--body-file",
        default=None,
        metavar="PATH",
        help=(
            "必填（合并模式）：认账 body 文件（含 merged-by / merge-card / merge-pr 三行 "
            "trailer，由 team-board merge-record --body-out 生成）；--verify-only 时不许给"
        ),
    )
    return parser


@dataclass(frozen=True)
class Options:
    """校验通过的参数集合。"""

    pr: int
    branch: str
    method: str
    repo: str
    remote: str
    #: 看板 SQLite 路径（认领唯一性闸门的数据源，只读打开）
    db_path: str
    timeout: int
    interval: int
    command_timeout: int
    verify_only: bool
    #: 认账四参数（仅合并模式有值；``--verify-only`` 恒为 ``None``）
    accounting: Accounting | None = None


def _parse(argv: list[str] | None) -> argparse.Namespace:
    """argparse 解析（``--help`` 仍然是 argparse 自己那套，rc0）。"""

    return build_parser().parse_args(argv)


def validate(args: argparse.Namespace) -> Options:
    """把 argparse 的结果校验成 ``Options``；任何越界 → rc2。"""

    if not args.delete_branch:
        raise MergeGuardError(MISSING_DELETE_BRANCH)
    branch = args.branch
    # 形态校验排在 --pr / --repo / 一切 gh / git 调用之前：畸形分支名一条外部命令都不发
    check_branch_shape(branch)
    pr = parse_positive_int("--pr", args.pr, maximum=MAX_PR)
    # 认账四参数：形态、body 文件、body 内容全部在这里判完 —— 即一次 gh / git 调用之前。
    # --verify-only 与它们互斥（见 validate_accounting）。
    accounting = validate_accounting(args, pr)
    check_branch_is_not_main(branch)
    timeout = parse_positive_int("--timeout", args.timeout, maximum=MAX_TIMEOUT)
    interval = parse_positive_int("--interval", args.interval, maximum=MAX_INTERVAL)
    if interval > timeout:
        raise MergeGuardError(
            f"--interval 不能大于 --timeout（interval={interval}, timeout={timeout}）"
        )
    command_timeout = parse_positive_int(
        "--command-timeout", args.command_timeout, maximum=MAX_COMMAND_TIMEOUT
    )
    repo = args.repo
    if not os.path.isdir(repo):
        raise MergeGuardError(f"仓库路径不存在或不是目录: {repo}")
    method = args.method
    if method not in METHODS:
        raise MergeGuardError(
            f"--method 只接受 {'/'.join(METHODS)}（R13：合并方式统一定一种），"
            f"实际是 {method!r}"
        )
    return Options(
        pr=pr,
        branch=branch,
        method=method,
        repo=repo,
        remote=args.remote,
        db_path=args.db,
        timeout=timeout,
        interval=interval,
        command_timeout=command_timeout,
        verify_only=bool(args.verify_only),
        accounting=accounting,
    )


# -------------------------------------------------------------------- 主流程

#: 收到就要收尾的信号名（SIGHUP = 终端关掉；三个都按「无法判定」处理）
INTERRUPT_SIGNAL_NAMES: tuple[str, ...] = ("SIGTERM", "SIGINT", "SIGHUP")


def _interrupt_signals() -> tuple[int, ...]:
    """本平台认得的收尾信号编号（Windows 上 SIGHUP / SIGTERM 不全，缺就跳过）。"""

    return tuple(
        signum
        for signum in (getattr(signal, name, None) for name in INTERRUPT_SIGNAL_NAMES)
        if isinstance(signum, int)
    )


def signal_name(signum: int) -> str:
    """``15`` → ``SIGTERM``；平台专有信号回落到数字（stderr 那行要能读懂）。"""

    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover - 平台专有信号
        return f"信号 {signum}"


@contextmanager
def handle_signals() -> Iterator[None]:
    """``SIGTERM`` / ``SIGINT`` / ``SIGHUP`` → :class:`Interrupted`；退出时恢复原 handler。

    没有这一层时：SIGTERM 直接杀进程（rc=143，``timeout`` 报 124）、SIGINT 冒
    ``KeyboardInterrupt`` traceback —— 两种都是 **0 字节 stdout**，而 ``lines`` 里可能
    已经攒着「合并 | PASS」。下一轮读板的人会把 rc 读成「没合并、可以重跑」。

    转成异常后走的是和 :class:`PartialFailure` 同一条路径：先写已完成证据、再写
    stderr 一行、退出码 2。**只转第一次信号**：收尾过程中再来一次信号直接忽略，
    免得证据写一半又被打断。

    ``signal.signal`` 只能在主线程调用：拿不到就跳过（测试里常从辅助线程跑
    ``main()``），此时行为回落到默认处理，不影响其余口径。
    """

    fired = False

    def _handler(signum: int, _frame: object) -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        raise Interrupted(signal_name(signum))

    saved: list[tuple[int, Any]] = []
    for signum in _interrupt_signals():
        previous = signal.getsignal(signum)
        try:
            signal.signal(signum, _handler)
        except (OSError, RuntimeError, ValueError):  # 非主线程 / 平台不支持
            continue
        saved.append((signum, previous))
    try:
        yield
    finally:
        for signum, previous in reversed(saved):
            with suppress(OSError, RuntimeError, ValueError):
                signal.signal(signum, previous)


@contextmanager
def _keep_evidence(lines: list[str]) -> Iterator[None]:
    """合并已经发生之后的兜底：任何异常都带上已完成的步骤证据走 rc2。

    为什么必须有这一层：``poll_branch_deleted`` 是在 ``gh pr merge`` **之后**才跑的，
    后面的崩溃（参数越界的 ``OverflowError``、探针环境故障、意外的 ``TypeError``…）
    如果直接冒到解释器，退出码是 1，而且 ``main`` 里的 ``lines`` 一条都打不出来 ——
    下一轮会把它读成「合并没发生、可以重跑」，再去合一次会撞 PR 已 MERGED。

    所以这里把异常统一转成 :class:`PartialFailure`（携带 ``lines``），``main`` 先把
    已完成的「预检」「合并」两步写到 stdout，再写 stderr 一行，退出码固定 2。

    收到信号（:class:`Interrupted`）走同一条路：``str(exc)`` 就是那行「收到信号 …」，
    证据照常先写出去。
    """

    try:
        yield
    except PartialFailure:
        raise
    except UnsupportedMatchHeadCommit:
        # gh 在解析 argv 时就拒了 --match-head-commit，合并一定没发生：
        # 证据行（分支存在 / 预检 / head sha）留着只会让人读成「合了一半」，
        # 所以原样抛出，main 按「合并前 rc2」处理（stdout 空、stderr 一行）。
        raise
    except CommandTimeout as exc:
        raise PartialFailure(
            f"{exc}；合并是否已发生无法确认，请人工核对 PR 状态，不要直接重跑合并",
            lines,
        ) from exc
    except MergeGuardError as exc:
        raise PartialFailure(str(exc), lines) from exc
    except Exception as exc:  # 兜底：意外异常不许冒 traceback、不许落 rc1
        detail = " ".join(str(exc).split())
        raise PartialFailure(
            f"合并后出现意外错误（{type(exc).__name__}: {detail}）；"
            "合并是否已发生无法确认，请人工核对 PR 状态，不要直接重跑合并",
            lines,
        ) from exc


def rebase_gate(opts: Options, info: Preflight) -> str:
    """**合并前 rebase 闸门**：``origin/main`` 不是 head 的祖先就 rc2 拦下。

    判据是三条命令，顺序固定（全部在 ``gh pr merge`` 之前跑，拒绝时一个 ``gh`` 写操作
    都不发、stdout 保持 0 字节）：

    1. :func:`rebase_fetch_argv` —— 一次 ``git fetch`` 把远端 ``base`` 与 ``branch``
       抓进私有 ref ``refs/merge-guard/<pr>/{base,head}``；base 取 ``gh pr view`` 读到
       的 ``baseRefName``（不写死 ``main``），``+`` 强制覆盖上一轮的 ref；
    2. :func:`rebase_rev_parse_argv` —— 读 ``refs/merge-guard/<pr>/head``，必须**逐字
       等于**预检读到的 ``headRefOid``（预检到合并之间分支被推送时，判的就该是预检看到
       的那条 sha，与 ``--match-head-commit`` 同一个理由）；
    3. :func:`rebase_merge_base_argv` —— ``git merge-base --is-ancestor base head``：
       ``rc=0`` = 已 rebase（PASS）、``rc=1`` = 落后或分叉（拒绝）、其它 = 判不了（拒绝）。

    为什么**不清理** ``refs/merge-guard/<pr>/{base,head}``（刻意决定，不是漏了）：
    清理要多跑一条 ``git update-ref -d``，于是多出两条失败路径（``update-ref`` 失败、
    清理与下一次 fetch 之间被别的进程插队），而「清理失败不许翻状态」正是最容易出 P1
    的地方 —— 合并已经成功却因为清理报错被读成未完成。留下的代价仅是两个提交对象被
    pin 住：这两个 ref 不在 ``refs/heads`` / ``refs/remotes`` 下，:mod:`team_board.branches`
    的稽核（只枚举这两类）看不见、``git push`` 也不会把它们带出去。

    不做 ``--depth`` 浅抓（``branches.py`` 已记同一个坑）：浅历史下
    ``merge-base --is-ancestor`` 会假红。

    通过时返回要打到 stdout 的 ``rebase | PASS | …`` 证据行（由 :func:`_run_merge`
    插在 ``head sha`` 之后）；拒绝时抛 :class:`MergeGuardError`（rc2、stdout 空、
    stderr 恰一行）。
    """

    base = info.base_ref
    # 畸形 baseRefName 在 fetch 之前拦：闸门自身不发任何 git 调用
    # （此前已跑过一次 ls-remote 存在性探针，那是合并前的源分支存在闸门）
    check_base_ref_shape(base)
    base_text = base if opts.remote == "origin" else f"{opts.remote}/{base}"

    fetch = _run(
        rebase_fetch_argv(opts.repo, opts.remote, opts.pr, base, opts.branch),
        cwd=opts.repo,
        timeout=opts.command_timeout,
    )
    if fetch.returncode != 0:
        raise MergeGuardError(
            f"取远端提交失败（git fetch rc={fetch.returncode}）：{key_line(fetch)}；"
            f"无法判定 head 是否已 rebase 到 {base_text}，拒绝合并"
        )

    head_ref = rebase_head_ref(opts.pr)
    rev = _run(
        rebase_rev_parse_argv(opts.repo, head_ref),
        cwd=opts.repo,
        timeout=opts.command_timeout,
    )
    # rev-parse 失败时拿不到 sha：把它的关键输出填进「远端 head 提交」这一格，
    # 于是仍然落同一条「不一致」文案（比编一个新退出码路径好判）
    remote_head = rev.stdout.strip() if rev.returncode == 0 else key_line(rev)
    if remote_head != info.head_oid:
        raise MergeGuardError(
            f"远端 head 提交 {remote_head} 与 gh pr view 的 headRefOid "
            f"{info.head_oid} 不一致：预检之后分支被推送，拒绝合并"
        )

    ancestor = _run(
        rebase_merge_base_argv(opts.repo, opts.pr),
        cwd=opts.repo,
        timeout=opts.command_timeout,
    )
    kind = classify_merge_base_rc(ancestor.returncode)
    if kind == ANCESTOR_NO:
        raise MergeGuardError(
            f"分支 {opts.branch} 落后 {base_text}（{base_text} 不是 head 的祖先，"
            f"git merge-base --is-ancestor rc=1）：合并前必须先 rebase 到 {base_text}"
            "（必须在作者的 bot 身份 worktree 里做：bash scripts/setup-bot-identity.sh <profile>"
            " <worktree>，否则 committer≠author 会被 commit-identity 判红；卡分支上不许出现没有"
            "认账字段的同步合并提交，否则 merge-accountability 必红），拒绝合并"
        )
    if kind == ANCESTOR_ERROR:
        raise MergeGuardError(
            f"无法判定 {base_text} 是否为 head 的祖先"
            f"（git merge-base rc={ancestor.returncode}）：{key_line(ancestor)}；拒绝合并"
        )

    return format_step(
        STEP_REBASE,
        ok=True,
        command=ancestor.command,
        rc=ancestor.returncode,
        # merge-base --is-ancestor 成功时没有输出，所以 key 由本工具合成
        key=(
            f"{_short_ref(opts, rebase_base_ref(opts.pr), fallback=base_text)} "
            f"是 head {info.head_oid[:7]} 的祖先（已 rebase 到 {base_text}）"
        ),
    )


def _short_ref(opts: Options, ref: str, *, fallback: str) -> str:
    """读 ``ref`` 的提交 sha 前 7 位（仅用于证据行；读不到就退回 ``fallback``）。"""

    proc = _run(
        rebase_rev_parse_argv(opts.repo, ref),
        cwd=opts.repo,
        timeout=opts.command_timeout,
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not _HEAD_OID.fullmatch(sha):
        return fallback
    return sha[:7]


def ensure_branch_present(opts: Options) -> CommandResult:
    """**合并前**确认源分支在远端存在（rc=0 才算存在）；否则 ``MergeGuardError`` → rc2。

    为什么必须有这一步：出口判定只认 ``ls-remote`` 的 ``rc=2``，而 ``rc=2`` 只说明
    「现在这个 remote 上没有这个 ref」，不说明「曾经在、并且被删」。少了合并前的这一
    次确认，「从来没存在过的分支名」在合并路径与 ``--verify-only`` 路径上都会被读成
    「已删除」并 rc0（PR 来自 fork、``--remote`` 指到另一个 remote 时必现）。

    任何非 0 的 rc（含 ``2`` = 没有这个 ref、``128`` = 通信错误）都算「合并前源分支
    不存在」→ rc2、stderr 一行、stdout 空、**一个 gh 调用都不发**（此函数在
    ``gh pr view`` 之前跑）。子命令超时由 :func:`_run` 抛
    :class:`CommandTimeout`（也是 rc2，合并未发生）。
    """

    argv = ls_remote_argv(opts.repo, opts.remote, opts.branch)
    proc = _run(argv, cwd=opts.repo, timeout=opts.command_timeout)
    if proc.returncode != 0:
        raise MergeGuardError(
            f"源分支 {opts.branch} 不在 {opts.remote} 远端（rc={proc.returncode}）："
            f"{key_line(proc)}；合并前源分支必须先存在，拒绝合并"
        )
    return proc


#: gh / cobra 报「不认识这个 flag」的几种原文（大小写不敏感）
_UNSUPPORTED_FLAG_HINTS: tuple[str, ...] = (
    "unknown flag",
    "unknown shorthand flag",
    "flag provided but not defined",
)


def _match_head_unsupported(proc: CommandResult) -> bool:
    """``gh pr merge`` 是否明确报了「不认 ``--match-head-commit``」。

    只在**同一次输出里**同时出现 flag 名与「unknown flag」这类原文时才判真：
    普通的合并失败（分支保护、非快进、删本地分支失败）不许被误判成环境错误，
    那会把 rc1「合并失败」改成 rc2。
    """

    text = f"{proc.stdout}\n{proc.stderr}".lower()
    if "--match-head-commit" not in text:
        return False
    return any(hint in text for hint in _UNSUPPORTED_FLAG_HINTS)


def fetch_pr_state(opts: Options, repo_slug: str) -> str | None:
    """``gh pr view <n> -R <slug> --json state``：反查 gh 非 0 时 PR 的真实状态。

    只判 ``state`` 一个字段：``MERGED`` = 合并已经发生（gh 的非 0 来自合并**之后**的
    步骤，比如删本地分支失败），调用方要继续做删除验证；其它值 = 合并没发生。

    ``-R`` 与预检 / 合并用的是**同一个** slug（不是 ``GH_REPO``）：反查必须看同一个
    仓库，否则「合并失败」可能被别仓同号 PR 的 ``MERGED`` 读成「合并已发生」。

    反查本身失败（gh 不在 PATH、非 0、输出不是 JSON / 不是对象 / 没有 string 类型的
    ``state``）一律返回 ``None`` —— 反查是补充证据，不是新的失败源，**不许**把 rc1
    「合并失败」悄悄变成 rc2。
    """

    argv = pr_view_argv(opts.pr, repo_slug, "state")
    try:
        proc = _run(argv, cwd=opts.repo, timeout=opts.command_timeout)
    except MergeGuardError:
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    state = payload.get("state")
    return state if isinstance(state, str) else None


def _run_verify_only(opts: Options) -> tuple[list[str], int]:
    """``--verify-only``：只验证「远端分支现在在不在」，一次 gh 都不调。

    措辞口径（本工具的硬规矩）：``rc=0`` 只能说「远端没有这个 ref（已删除，或从未
    存在过）」。``--verify-only`` 既没看到过「合并前分支在」，也没看到过合并动作，
    所以**不许**下「分支已删除」这种结论 —— 那要靠合并路径的「分支存在」预检 +
    「分支删除」验证两步合起来才成立。退出码语义不变：``ls-remote`` 的 ``rc=2`` 仍
    映到本命令的 rc0。

    步骤行走 :func:`format_verify_step`（``远端 ref | 不存在 | …``），**不写**
    ``PASS`` / ``FAIL``：``PASS`` 那一列会被抄进卡里当「删除已验证」的证据，而这条
    路径恰恰没有验证过任何删除。
    """

    result = poll_branch_deleted(
        make_probe(opts.repo, opts.remote, opts.branch, timeout=opts.command_timeout),
        timeout=opts.timeout,
        interval=opts.interval,
    )
    lines = [
        format_verify_step(
            result.outcome,
            command=" ".join(ls_remote_argv(opts.repo, opts.remote, opts.branch)),
            rc=result.returncode,
            key=result.key,
        )
    ]
    if result.outcome == OUT_DELETED:
        lines.append("结论：远端没有这个 ref（已删除，或从未存在过）")
        return lines, 0
    if result.outcome == OUT_PRESENT:
        lines.append("结论：分支仍在远端")
        return lines, 1
    lines.append(f"结论：无法判定（{result.reason}）")
    return lines, 1


def _run_merge(opts: Options) -> tuple[list[str], int]:
    """分支存在预检 → gh 预检 → 合并 → 删除验证。"""

    accounting = opts.accounting
    if accounting is None:  # pragma: no cover - validate 已拦住（合并模式必填）
        raise MergeGuardError(
            "合并模式缺少认账参数 --card/--by/--subject/--body-file（内部错误）"
        )
    # 合并**前**的「源分支必须存在」闸门：排在所有 gh 调用之前，失败即 rc2 且 stdout 空。
    presence = ensure_branch_present(opts)
    # 仓库身份在这一步算定（只读一条 git config，不发 gh）：此后每一条 gh 调用都用这
    # 同一个 slug 显式钉 `-R`，闸门核对的对象与实际操作的仓库因此是同一个（P2-2）。
    repo_slug = remote_repo_slug(opts)
    view = _run(pr_view_argv(opts.pr, repo_slug), cwd=opts.repo, timeout=opts.command_timeout)
    if view.returncode != 0:
        raise MergeGuardError(
            f"gh pr view {opts.pr} 失败（rc={view.returncode}）: {key_line(view)}"
        )
    info = parse_pr_view(view, opts.pr)
    if info.state != "OPEN":
        raise MergeGuardError(
            f"PR {opts.pr} 的状态是 {info.state}，只有 OPEN 的 PR 能合并"
        )
    if info.head_ref != opts.branch:
        raise MergeGuardError(
            f"--branch {opts.branch} 与 PR {opts.pr} 的 headRefName "
            f"{info.head_ref} 不一致"
        )
    if opts.branch == info.base_ref:
        raise MergeGuardError(
            f"--branch {opts.branch} 等于 PR {opts.pr} 的 baseRefName，"
            "拒绝合并（合并后删除会毁掉主分支）"
        )
    # 合并**前**的两道证据闸门（顺序固定，见模块 docstring）：
    #   ① 认领唯一性（R17⑤）——只读看板库，一次 gh 都不调；
    #   ② 双问绑定 head sha（R18④）——只发两条 gh api GET。
    # 两者都在 rebase 闸门之前、gh pr merge 之前：拒绝时 stdout 0 字节、零 gh 写操作。
    claim_line = claim_gate(opts, info)
    review_line = review_head_gate(opts, info, repo_slug)
    # 合并**前**的 rebase 闸门：base 不是 head 的祖先 → rc2、stdout 空、零 gh 写操作。
    # 排在这里（而不是 lines 攒完之后）是刻意的：拒绝时 stdout 必须 0 字节，
    # 否则下游会把半截证据行读成「已经走到合并了」。PASS 行插在 head sha 之后。
    rebase_line = rebase_gate(opts, info)

    lines = [
        format_step(
            STEP_BRANCH_PRESENT,
            ok=True,
            command=presence.command,
            rc=presence.returncode,
            key=key_line(presence),
        ),
        format_step(
            STEP_PREFLIGHT,
            ok=True,
            command=view.command,
            rc=view.returncode,
            key=(
                f"state={info.state}, headRefName={info.head_ref}, "
                f"baseRefName={info.base_ref}"
            ),
        ),
        # 预检钉到**提交**：这条 sha 就是合并时交给 --match-head-commit 的那个值。
        f"{STEP_HEAD_SHA} | PASS | {info.head_oid}",
        claim_line,
        review_line,
        rebase_line,
    ]

    merge_argv_value = merge_argv(
        opts.pr,
        opts.method,
        info.head_oid,
        accounting.subject,
        accounting.body_file,
        repo_slug,
    )
    # 从这一行起合并随时可能已经发生：任何异常都必须带着上面那条「预检 | PASS」
    # （以及合并成功后的「合并 | PASS」）走 rc2，不许落 rc1、不许冒 traceback。
    with _keep_evidence(lines):
        merge = _run(merge_argv_value, cwd=opts.repo, timeout=opts.command_timeout)
        if merge.returncode != 0:
            if _match_head_unsupported(merge):
                # 老版本 gh 不认这个 flag：它在解析 argv 时就退出，**合并没发生**。
                # 先反查一次 PR 状态兜底（万一真是合并后报的这句），仍不是 MERGED
                # 就按「合并前 rc2」处理 —— 绝不允许退回「不带 sha 合并」。
                state = fetch_pr_state(opts, repo_slug)
                if state != "MERGED":
                    raise UnsupportedMatchHeadCommit(
                        "gh pr merge 不接受 --match-head-commit"
                        f"（{key_line(merge)}）：本工具不提供不带 head sha 的合并路径，"
                        "请升级 gh（2.103.3 起支持）或人工核对 PR 状态"
                    )
            # gh 非 0 **不等于**合并失败：2.103.3 的 mergeRun 是 merge() → 删本地分支
            # → 删远端分支，删本地分支失败就返回非 0，那时 PR 已合、远端分支还没删。
            # 所以先反查 PR 状态，再决定是「合并失败」还是「合并已发生」。
            _write_stderr_raw(merge.stdout)
            _write_stderr_raw(merge.stderr)
            state = fetch_pr_state(opts, repo_slug)
            state_text = state if state is not None else "反查失败"
            if state != "MERGED":
                lines.append(
                    format_step(
                        STEP_MERGE,
                        ok=False,
                        command=merge.command,
                        rc=merge.returncode,
                        key=key_line(merge),
                    )
                )
                lines.append(
                    f"结论：合并失败（gh pr merge 退出码 {merge.returncode}，"
                    f"PR 状态 {state_text}，未做分支删除验证）"
                )
                return lines, 1
            # MERGED：合并已发生，gh 的非 0 来自合并之后的步骤 —— 继续做删除验证。
            lines.append(
                format_step(
                    STEP_MERGE,
                    ok=True,
                    command=merge.command,
                    rc=merge.returncode,
                    key=(
                        f"gh 退出码 {merge.returncode}，但 PR 状态已是 MERGED；"
                        f"{key_line(merge)}"
                    ),
                )
            )
        else:
            lines.append(
                format_step(
                    STEP_MERGE,
                    ok=True,
                    command=merge.command,
                    rc=merge.returncode,
                    key=key_line(merge),
                )
            )

        result = poll_branch_deleted(
            make_probe(
                opts.repo, opts.remote, opts.branch, timeout=opts.command_timeout
            ),
            timeout=opts.timeout,
            interval=opts.interval,
        )
        lines.append(
            format_step(
                STEP_DELETE,
                ok=result.outcome == OUT_DELETED,
                command=" ".join(ls_remote_argv(opts.repo, opts.remote, opts.branch)),
                rc=result.returncode,
                key=result.key,
            )
        )
        if result.outcome == OUT_DELETED:
            lines.append(
                f"结论：合并完成，源分支 {opts.branch} 已从 {opts.remote} 删除"
            )
            return lines, 0
        if result.outcome == OUT_PRESENT:
            lines.append("结论：合并成功但源分支仍在远端（合并不算完成）")
            return lines, 1
        # 合并已经发生，但「分支是否真的删了」判不了 —— 明确未通过，绝不当绿。
        # 走 PartialFailure：预检 / 合并两步证据留在 stdout，退出码 2（不是 1）。
        raise MergeGuardError(
            f"合并已完成，但无法判定远端分支 {opts.branch} 是否已删除"
            f"（{result.reason}）；请人工核对"
        )


def run(args: argparse.Namespace) -> tuple[list[str], int]:
    """校验参数并执行；返回（要打到 stdout 的行, 退出码）。"""

    opts = validate(args)
    if opts.verify_only:
        return _run_verify_only(opts)
    return _run_merge(opts)


def main(argv: list[str] | None = None) -> int:
    """``python -m team_board.merge_guard`` 入口。

    退出码口径：**任何异常都不许落 rc1**（rc1 只表示「未达成」）。合并已发生之后的
    失败走 :class:`PartialFailure`：先把已完成的步骤证据写到 stdout，再把
    ``错误: …`` 写到 stderr，返回 2 —— 崩溃既不能丢证据，也不能被读成「可以重跑」。
    收到 SIGTERM / SIGINT / SIGHUP 同样走这条路径（:func:`handle_signals`）。
    """

    try:
        args = _parse(argv)
    except MergeGuardError as exc:
        _write_stderr_error(str(exc))
        return 2
    except SystemExit as exc:  # --help / --version：交给 argparse 的退出码
        code = exc.code
        return code if isinstance(code, int) else 2

    try:
        with handle_signals():
            lines, rc = run(args)
    except PartialFailure as exc:
        if exc.lines:
            # stdout 编码表示不了中文时证据写不出，但仍然是 rc2（不许落 rc1）
            with suppress(MergeGuardError):
                _write_stdout(exc.lines)
        _write_stderr_error(str(exc))
        return 2
    except MergeGuardError as exc:
        _write_stderr_error(str(exc))
        return 2
    except Exception as exc:  # 兜底：意外异常不许冒 traceback、不许落 rc1
        detail = " ".join(str(exc).split())
        _write_stderr_error(
            f"内部错误：{type(exc).__name__}: {detail}"
            "（退出码不是 1；合并是否已发生请人工核对）"
        )
        return 2

    try:
        _write_stdout(lines)
    except MergeGuardError as exc:
        _write_stderr_error(str(exc))
        return 2
    return rc


if __name__ == "__main__":  # pragma: no cover - 由 python -m 触发
    raise SystemExit(main())
