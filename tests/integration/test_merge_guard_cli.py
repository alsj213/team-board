"""merge_guard 的集成测试：走 ``main(argv)``，gh / git 一律用临时目录里的假脚本。

**禁止联调**：测试里出现的 ``gh`` / ``git`` 全部是本目录 fixture 造的可执行假脚本，
塞在 ``PATH`` 最前面，调用记录写进 ``$FAKE_LOG``；既不打真实 GitHub，也不碰真远端。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from team_board import cli as board_cli
from team_board import merge_guard as mg

CARD_BRANCH = "wt/t_bbeab29f"
#: 认账四参数的默认取值（合并模式必填；body 文件由 fixture 写在临时目录里）
MERGE_CARD = "t_bbeab29f"
MERGE_BY = "repo/run200"
MERGE_SUBJECT = "Merge PR #9（t_bbeab29f）"
#: 合法 body：恰好三行 trailer（与 ``team-board merge-record --body-out`` 的输出同形）
BODY_TEXT = (
    f"merged-by: {MERGE_BY}\nmerge-card: {MERGE_CARD}\nmerge-pr: #9\n"
)

#: 假 gh 的 ``pr view`` 报出来的 head 提交（40 位十六进制）：合并 argv 必须带它
HEAD_OID = "4f2a9c0e5b1d3a7c8e6f90b2d4a1c3e5f7b9d0a2"
#: 假 git 在 ``refs/merge-guard/<pr>/base`` 上读到的 base 提交（rebase PASS 行的 key 用它前 7 位）
BASE_OID = "b3c1d2e4f5a6978899aabbccddeeff0011223344"

#: 双问绑定闸门的仓库核对调用（``--repo`` 的 remote 配置 URL 必须指向同一仓库）
REMOTE_CALL = "git -C {repo} config --get remote.origin.url"

#: ``gh pr view`` 要的字段（``headRefOid`` 是「预检钉到提交」的凭据）
VIEW_FIELDS = "number,state,headRefName,baseRefName,headRefOid"
#: 仓库身份（``reviewhead.REPO`` 的小写形态）：合并模式的每一条 gh 调用都必须钉它
REPO_SLUG = "alsj213/team-board"
VIEW_CALL = f"gh pr view 9 -R {REPO_SLUG} --json {VIEW_FIELDS}"
#: 合并失败后的状态反查（同一个 ``-R``，不看 ``GH_REPO``）
STATE_CALL = f"gh pr view 9 -R {REPO_SLUG} --json state"


def merge_call(
    stub: Stub,
    *,
    subject: str = MERGE_SUBJECT,
    body_file: Path | None = None,
) -> str:
    """合并命令原文：``--merge --delete-branch --match-head-commit`` 原位 + 两个认账参数。"""

    path = stub.body_file if body_file is None else body_file
    return (
        f"gh pr merge 9 -R {REPO_SLUG} --merge --delete-branch --match-head-commit {HEAD_OID} "
        f"--subject {subject} --body-file {path}"
    )


def mark_body(sha: str, kind: str) -> str:
    """一条带结构化双问标记的评审正文（``REVIEW-HEAD`` + ``REVIEW-KIND``，顶格）。"""

    return f"REVIEW-HEAD: {sha}\nREVIEW-KIND: {kind}\n"


def rebase_call(stub: Stub) -> str:
    """rebase 闸门的判定命令原文（私有 ref 命名空间 ``refs/merge-guard/<pr>/*``）。"""

    return (
        f"git -C {stub.repo} merge-base --is-ancestor "
        "refs/merge-guard/9/base refs/merge-guard/9/head"
    )


def rebase_line(stub: Stub) -> str:
    """rebase 闸门通过时的那一行证据（``merge-base`` 成功没有输出，key 由工具合成）。"""

    return (
        f"rebase | PASS | {rebase_call(stub)} | rc=0 | "
        f"{BASE_OID[:7]} 是 head {HEAD_OID[:7]} 的祖先（已 rebase 到 main）"
    )


#: 双问绑定闸门的两条只读调用（顺序固定：``reviews`` 先、``comments`` 后）
API_CALLS = [
    "gh api repos/alsj213/team-board/pulls/9/reviews --paginate",
    "gh api repos/alsj213/team-board/issues/9/comments --paginate",
]


def claim_line(stub: Stub) -> str:
    """认领唯一性闸门通过时的那一行证据（判据是本地只读库 + 纯函数，没有外部命令）。"""

    return (
        f"认领唯一性 | PASS | prclaim.audit(看板库 {stub.board_db}（未结卡，mode=ro）, pr=#9)"
        f" | rc=0 | 认领唯一 → {MERGE_CARD}（与 --card 逐字一致）"
    )


def review_line(stub: Stub) -> str:
    """双问绑定闸门通过时的那一行证据（默认 fixture：两个端点各一条标记）。

    key 的格式由 E4 定死：``内容指纹 <pid12> 上 defect + regression 各 >=1 条（head <sha12>，
    扫描正文 N 条、带标记 M 条）`` —— 绑定对象是内容指纹，head 只是附注。
    """

    return (
        f"双问绑定 | PASS | {API_CALLS[0]} + {API_CALLS[1]} | rc=0 | "
        f"内容指纹 {stub.pid[:12]} 上 defect + regression 各 >=1 条"
        f"（head {HEAD_OID[:12]}，扫描正文 2 条、带标记 2 条）"
    )


def evidence_line(stub: Stub) -> str:
    """证据状态机闸门通过时的那一行证据（默认 fixture：四槽齐、全绑 ``HEAD_OID``）。

    判据是本地只读库 + ``evidence.collect`` / ``judge`` 纯函数，没有外部命令；命令格写
    数据来源与实现卡，最后一格是四槽摘要（``qa_pass`` 按契约示例打码成 ``***``）。
    """

    return (
        f"证据状态机 | PASS | evidence.audit(card={MERGE_CARD}, head={HEAD_OID}) | rc=0 | "
        "tier=B, qa_pass=*** review=defect+regression"
    )


GH_STUB = """#!/bin/sh
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = "api" ]; then
  # 双问绑定闸门只发两条 GET（pulls/<n>/reviews + issues/<n>/comments）——按端点分开给
  case "$2" in
    */pulls/*/reviews) printf '%s' "$FAKE_REVIEWS_JSON" ;;
    */issues/*/comments) printf '%s' "$FAKE_COMMENTS_JSON" ;;
    *) printf 'fake gh: unexpected api endpoint: %s\\n' "$2" >&2; exit 9 ;;
  esac
  exit "${FAKE_API_RC:-0}"
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  # 合并之后（marker 存在）的反查走另一份固定输出：合并前必须报 OPEN，
  # 合并后要能报 MERGED（gh 非 0 时的 state 反查用例）
  if [ -n "$FAKE_MERGED_MARK" ] && [ -f "$FAKE_MERGED_MARK" ] && [ -n "$FAKE_VIEW_JSON_AFTER" ]; then
    printf '%s' "$FAKE_VIEW_JSON_AFTER"
    exit "${FAKE_VIEW_RC_AFTER:-0}"
  fi
  if [ -n "$FAKE_VIEW_SLEEP" ]; then sleep "$FAKE_VIEW_SLEEP"; fi
  printf '%s' "$FAKE_VIEW_JSON"
  exit "${FAKE_VIEW_RC:-0}"
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  if [ -n "$FAKE_MERGED_MARK" ]; then : > "$FAKE_MERGED_MARK"; fi
  if [ -n "$FAKE_MERGE_OUT" ]; then printf '%s\\n' "$FAKE_MERGE_OUT"; fi
  if [ -n "$FAKE_MERGE_ERR" ]; then printf '%s\\n' "$FAKE_MERGE_ERR" >&2; fi
  exit "${FAKE_MERGE_RC:-0}"
fi
printf 'fake gh: unexpected args: %s\\n' "$*" >&2
exit 9
"""

GIT_STUB = """#!/bin/sh
printf 'git %s\\n' "$*" >> "$FAKE_LOG"
for arg in "$@"; do
  case "$arg" in
    ls-remote|fetch|rev-parse|merge-base|config|diff|patch-id|cat-file) sub="$arg"; break ;;
  esac
done
case "$sub" in
  ls-remote)
    # 只有 ls-remote 计次：第 1 次 = 合并前的「源分支存在」闸门，其余 = 合并后的删除探针
    n=0
    if [ -n "$FAKE_GIT_COUNT" ] && [ -f "$FAKE_GIT_COUNT" ]; then n=$(cat "$FAKE_GIT_COUNT"); fi
    n=$((n + 1))
    if [ -n "$FAKE_GIT_COUNT" ]; then printf '%s' "$n" > "$FAKE_GIT_COUNT"; fi
    if [ "$n" = "1" ] && [ -n "$FAKE_LS_REMOTE_FIRST_RC" ]; then
      exit "$FAKE_LS_REMOTE_FIRST_RC"
    fi
    if [ -n "$FAKE_GIT_SLEEP" ]; then sleep "$FAKE_GIT_SLEEP"; fi
    exit "${FAKE_LS_REMOTE_RC:-2}"
    ;;
  fetch)
    if [ -n "$FAKE_FETCH_ERR" ]; then printf '%s\\n' "$FAKE_FETCH_ERR" >&2; fi
    exit "${FAKE_FETCH_RC:-0}"
    ;;
  rev-parse)
    ref=""
    for arg in "$@"; do
      case "$arg" in refs/*) ref="$arg" ;; esac
    done
    case "$ref" in
      */base) out="$FAKE_REV_PARSE_BASE_OUT" ;;
      *) out="$FAKE_REV_PARSE_OUT" ;;
    esac
    rc="${FAKE_REV_PARSE_RC:-0}"
    if [ -n "$FAKE_REV_PARSE_ERR" ]; then printf '%s\\n' "$FAKE_REV_PARSE_ERR" >&2; fi
    if [ "$rc" = "0" ] && [ -n "$out" ]; then printf '%s\\n' "$out"; fi
    exit "$rc"
    ;;
  merge-base)
    # 两种用法分开：``--is-ancestor`` = rebase 闸门的判定（rc 可注入）；
    # 其余 = 内容指纹的 merge-base，落到真 git 库上算（假脚本不伪造 patch-id）
    case " $* " in
      *" --is-ancestor "*)
        if [ -n "$FAKE_MERGE_BASE_ERR" ]; then printf '%s\\n' "$FAKE_MERGE_BASE_ERR" >&2; fi
        exit "${FAKE_MERGE_BASE_RC:-0}"
        ;;
    esac
    exec "$FAKE_REAL_GIT" "$@"
    ;;
  diff|patch-id|cat-file)
    # 内容指纹（``reviewhead.fingerprint``）只跑这三类命令：一律交给真 git，
    # 作用在 fixture 造的真仓库上 —— 指纹值必须是真 patch-id，不许 stub 伪造
    exec "$FAKE_REAL_GIT" "$@"
    ;;
  config)
    # 双问绑定闸门的仓库核对：``git config --get remote.<name>.url`` → 假 remote URL
    if [ -n "$FAKE_REMOTE_URL" ]; then printf '%s\\n' "$FAKE_REMOTE_URL"; fi
    if [ -n "$FAKE_REMOTE_ERR" ]; then printf '%s\\n' "$FAKE_REMOTE_ERR" >&2; fi
    exit "${FAKE_REMOTE_RC:-0}"
    ;;
  *)
    printf 'fake git: unexpected args: %s\\n' "$*" >&2
    exit 9
    ;;
esac
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


#: 真 git 的绝对路径（假脚本里要代理内容指纹的三类命令，不能被 PATH 里的假 git 抢走）
REAL_GIT = shutil.which("git") or "git"

#: 内容指纹用的私有 ref（命名与 :func:`mg.rebase_base_ref` / :func:`mg.rebase_head_ref` 一致）
MG_BASE_REF = mg.rebase_base_ref(9)
MG_HEAD_REF = mg.rebase_head_ref(9)


def _set_ref(repo: Path, ref: str, sha: str) -> None:
    """把 ``ref`` 指到 ``sha``（fixture 预置 = 闸门那次 fetch 会写下的东西）。"""

    subprocess.run([REAL_GIT, "-C", str(repo), "update-ref", ref, sha], check=True)


def _commit(repo: Path, files: dict[str, bytes], message: str) -> str:
    """在真仓库里提交一次改动（``files`` 是 路径 → 字节），返回新提交 sha。"""

    for name, data in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            REAL_GIT,
            "-C",
            str(repo),
            "-c",
            "user.name=probe",
            "-c",
            "user.email=probe@local",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            message,
        ],
        check=True,
    )
    return subprocess.run(
        [REAL_GIT, "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_content_repo(repo: Path) -> tuple[str, str]:
    """把 ``--repo`` 目录造成**真 git 库**，并预置两条私有 ref。

    ``refs/merge-guard/9/{base,head}`` 就是 :func:`mg.ensure_pr_refs` 那次 ``git fetch``
    会写下的两条 ref（fetch 本身仍是假脚本）：base = 基线提交，head = 卡分支上的改动提交。
    内容指纹的三类命令（``merge-base`` / ``diff`` / ``patch-id`` / ``cat-file``）由假脚本
    **代理给真 git**，于是指纹值是真 patch-id —— 桩不伪造哈希。
    """

    subprocess.run([REAL_GIT, "init", "-q", "-b", "main", str(repo)], check=True)
    base_sha = _commit(repo, {"base.txt": b"base\n"}, "base")
    head_sha = _commit(repo, {"feature.txt": b"feature\n"}, "feat: content")
    _set_ref(repo, MG_BASE_REF, base_sha)
    _set_ref(repo, MG_HEAD_REF, head_sha)
    return base_sha, head_sha


def _replay_on(repo: Path, parent: str, files: dict[str, bytes], message: str) -> str:
    """把 ``files`` 那份改动**重放到** ``parent`` 上（父提交换了、内容不变）= 纯 rebase 形态。

    真实 rebase 的语义就是这个：分支提交被重放，树的差异不变、提交 sha 变了。
    """

    subprocess.run(
        [REAL_GIT, "-C", str(repo), "checkout", "-q", "-B", "rebase-probe", parent],
        check=True,
    )
    return _commit(repo, files, message)


def patch_body(pid: str, kind: str) -> str:
    """一条绑定**内容指纹**的双问标记（``REVIEW-PATCH`` + ``REVIEW-KIND``，顶格）。"""

    return f"REVIEW-PATCH: {pid}\nREVIEW-KIND: {kind}\n"

def default_evidence_rows(head: str = HEAD_OID) -> list[tuple[str, str, int]]:
    """证据状态机闸门要的默认四槽（tier / qa_pass / review:defect / review:regression）。

    四条全绑 ``head``（= 预检的 ``headRefOid``），写入 run 的 profile 落在角色矩阵里
    （``tier`` pm / ``qa_pass`` qa / ``review`` pm+qa）。缺省行就是「四槽齐、head 一致」，
    各用例按需用 :meth:`Stub.set_evidence` 换成「缺一槽 / 绑旧 head / tier=C」等红样。
    """

    return [
        (
            "tier",
            json.dumps({"tier": "B", "reason": "文档 + 单测", "head_sha": head}, ensure_ascii=False),
            1,
        ),
        ("qa_pass", json.dumps({"head_sha": head, "run_id": 2}), 2),
        (
            "review",
            json.dumps({"kind": "defect", "head_sha": head, "verdict": "pass"}),
            2,
        ),
        (
            "review",
            json.dumps({"kind": "regression", "head_sha": head, "verdict": "pass"}),
            1,
        ),
    ]


def make_board_db(
    path: Path,
    cards: list[tuple[str, str, str]],
    *,
    evidence_card: str | None = None,
    evidence: list[tuple[str, str, int | None]] | None = None,
) -> Path:
    """造一个最小看板库（``tasks`` 四列 + evidence 事件的两张表）。

    ``cards`` 是 ``(卡号, status, body)``；``assignee`` 一律写 ``engineer``（认领闸门不
    看它）。表结构照真库的子集：``db.iter_claim_rows`` 只读
    ``id, status, assignee, body`` + ``status NOT IN ('done','archived')``；
    ``evidence.collect`` 读 ``task_events(id,task_id,kind,payload,run_id,created_at)``
    并 ``LEFT JOIN task_runs`` 取 profile。

    ``evidence_card`` 非空时把 ``evidence``（缺省 = :func:`default_evidence_rows` 四槽齐）
    写进该卡的 evidence 事件 —— 证据状态机闸门按 ``--branch`` 取实现卡号，所以这里的
    卡号必须是 ``wt/t_<卡号>`` 的那个实现卡。
    """

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, assignee TEXT, body TEXT)")
        conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, profile TEXT)")
        conn.execute(
            "CREATE TABLE task_events ("
            "id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, "
            "run_id INTEGER, created_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO tasks (id, status, assignee, body) VALUES (?, ?, ?, ?)",
            [(card, status, "engineer", body) for card, status, body in cards],
        )
        conn.executemany(
            "INSERT INTO task_runs (id, profile) VALUES (?, ?)",
            [(1, "pm"), (2, "qa"), (3, "paimeng")],
        )
        if evidence_card is not None:
            rows = default_evidence_rows() if evidence is None else evidence
            conn.executemany(
                "INSERT INTO task_events (task_id, kind, payload, run_id, created_at) "
                "VALUES (?, ?, ?, ?, 0)",
                [(evidence_card, kind, payload, run_id) for kind, payload, run_id in rows],
            )
        conn.commit()
    finally:
        conn.close()
    return path


@dataclass
class Stub:
    """一次性环境：假 gh / 假 git + 调用日志 + 真 git 仓库 + 认领闸门用的看板库。"""

    bindir: Path
    log: Path
    repo: Path
    body_file: Path
    setenv: Callable[[str, str], None]
    #: 认领唯一性闸门读的看板库（各用例按需换成本地 fixture；缺省用真库——手搭 argv 的
    #: 用例可以显式给 ``--db``，也可以换掉这个字段）
    board_db: Path = Path(mg.DEFAULT_DB)
    #: 真仓库里 ``refs/merge-guard/9/base`` 指向的基线提交（= 闸门那次 fetch 的结果）
    base_sha: str = ""
    #: 真仓库里 ``refs/merge-guard/9/head`` 指向的改动提交
    head_sha: str = ""
    #: 两条 ref 之间的内容指纹（``reviewhead.fingerprint`` 真算出来的 40 hex）
    pid: str = ""

    def set_body(self, text: str) -> None:
        """改 body 文件内容（坏输入用例复用同一路径，``--body-file`` 不必变）。"""

        self.body_file.write_text(text, encoding="utf-8")

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def gh_calls(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("gh ")]

    def api_calls(self) -> list[str]:
        return [call for call in self.gh_calls() if call.startswith("gh api ")]

    def set_review_bodies(self, reviews: list[str], comments: list[str]) -> None:
        """喂给假 ``gh api`` 的两份正文（双问绑定闸门只看 body）。"""

        self.setenv("FAKE_REVIEWS_JSON", json.dumps([{"body": b} for b in reviews]))
        self.setenv("FAKE_COMMENTS_JSON", json.dumps([{"body": b} for b in comments]))

    def set_claims(self, cards: list[tuple[str, str]]) -> None:
        """重写 fixture 看板库里的未结卡（``(卡号, 正文)``，status 一律 ``running``）。

        重建库时把默认四槽证据一起写回实现卡（``--branch`` 对应的卡号）：认领类用例
        未必走到证据闸门，但走到时不该因为 fixture 少了 ``task_events`` 而假红。
        """

        self.board_db.unlink(missing_ok=True)
        make_board_db(
            self.board_db,
            [(card, "running", body) for card, body in cards],
            evidence_card=MERGE_CARD,
        )

    def add_content(self, files: dict[str, bytes]) -> str:
        """在真仓库上追加一个改动提交，把 head ref 挪过去（内容指纹随之改变）。

        用途：造「真改内容」/ 二进制 / 纯 rebase 之后新 head 三种形态 —— 指纹由真 git 算，
        stub 不伪造 patch-id。返回新 head sha。
        """

        sha = _commit(self.repo, files, f"probe: {sorted(files)}")
        _set_ref(self.repo, MG_HEAD_REF, sha)
        self.head_sha = sha
        return sha

    def point_head_at_base(self) -> None:
        """把 head ref 挪到 base 提交（= 分支相对 base 无差异，空 diff 形态）。"""

        _set_ref(self.repo, MG_HEAD_REF, self.base_sha)
        self.head_sha = self.base_sha

    def reset_ls_remote_count(self) -> None:
        """清掉 ``ls-remote`` 计次文件（同一用例里跑第二次完整合并时用）。"""

        Path(os.environ["FAKE_GIT_COUNT"]).unlink(missing_ok=True)

    def alias_pr(self, pr: int) -> None:
        """把 fixture 的两条私有 ref 复制到另一个 PR 号下（``--pr`` 换号的用例用）。

        闸门读的是 ``refs/merge-guard/<pr>/{base,head}``；fixture 预置的是 PR #9 那一份。
        """

        _set_ref(self.repo, mg.rebase_base_ref(pr), self.base_sha)
        _set_ref(self.repo, mg.rebase_head_ref(pr), self.head_sha)

    def fingerprint(self) -> str:
        """当前两条 ref 之间的内容指纹（真 git 算；stub 只把命令代理给真 git）。

        预计算走的也是假脚本，会往 ``FAKE_LOG`` 记几条调用 —— 算完把日志还原成算之前的样子，
        免得用例的「调用序列」断言被 fixture 自身的预计算污染。
        """

        before = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        value = mg.reviewhead.fingerprint(self.repo, MG_BASE_REF, MG_HEAD_REF).value
        if before:
            self.log.write_text(before, encoding="utf-8")
        else:
            self.log.unlink(missing_ok=True)
        assert value is not None, "fixture 的 head ref 应能算出 patch 指纹"
        return value

    def set_evidence(
        self,
        rows: list[tuple[str, str, int | None]] | None = None,
        *,
        card: str = MERGE_CARD,
    ) -> None:
        """重写 fixture 库里的 evidence 事件（``None`` = 恢复默认四槽齐、绑 ``HEAD_OID``）。

        证据状态机闸门按 ``--branch``（``wt/t_<卡号>``）取实现卡号，``card`` 默认就是
        ``MERGE_CARD``（= 本 fixture 的 ``--card`` 与 ``CARD_BRANCH`` 的卡号）。
        """

        conn = sqlite3.connect(str(self.board_db))
        try:
            with conn:
                conn.execute("DELETE FROM task_events")
                events = default_evidence_rows() if rows is None else rows
                conn.executemany(
                    "INSERT INTO task_events (task_id, kind, payload, run_id, created_at) "
                    "VALUES (?, ?, ?, ?, 0)",
                    [(card, kind, payload, run_id) for kind, payload, run_id in events],
                )
        finally:
            conn.close()

    def git_calls(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("git ")]

    def view(
        self,
        *,
        state: str = "OPEN",
        head: str = CARD_BRANCH,
        base: str = "main",
        head_oid: str | None = HEAD_OID,
    ) -> None:
        payload: dict[str, object] = {
            "number": 9,
            "state": state,
            "headRefName": head,
            "baseRefName": base,
        }
        if head_oid is not None:  # None = 故意让假 gh 少报 headRefOid
            payload["headRefOid"] = head_oid
        self.setenv("FAKE_VIEW_JSON", json.dumps(payload))

    def view_after_merge(self, *, state: str = "MERGED", rc: int = 0) -> None:
        """合并之后（``gh pr merge`` 已经跑过）的 ``gh pr view --json state`` 输出。"""

        self.setenv("FAKE_VIEW_JSON_AFTER", json.dumps({"state": state}))
        self.setenv("FAKE_VIEW_RC_AFTER", str(rc))


@pytest.fixture()
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir()
    _write_exe(bindir / "gh", GH_STUB)
    _write_exe(bindir / "git", GIT_STUB)
    log = tmp_path / "calls.log"
    repo = tmp_path / "repo"
    repo.mkdir()
    # 认账 body：合并模式必填，内容恰好是合法三行 trailer（坏输入用例用 Stub.set_body 覆盖）
    body_file = tmp_path / "merge-body.txt"
    body_file.write_text(BODY_TEXT, encoding="utf-8")
    # 认领闸门的 fixture 库：恰好一张未结卡认领 PR #9，且卡号就是 --card
    board_db = make_board_db(
        tmp_path / "board.db",
        [(MERGE_CARD, "running", "PR-CLAIM: #9\n")],
        evidence_card=MERGE_CARD,
    )
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_GIT_COUNT", str(tmp_path / "git.count"))
    monkeypatch.setenv("FAKE_MERGED_MARK", str(tmp_path / "merged.mark"))
    # 假 git 里「内容指纹」那三类命令代理给真 git（绝对路径，绕开 PATH 上的假脚本）
    monkeypatch.setenv("FAKE_REAL_GIT", REAL_GIT)
    monkeypatch.setenv("FAKE_VIEW_RC", "0")
    monkeypatch.setenv("FAKE_MERGE_RC", "0")
    # 默认：合并前的「源分支存在」闸门通过（分支在），合并后的探针报「没有这个 ref」
    monkeypatch.setenv("FAKE_LS_REMOTE_FIRST_RC", "0")
    monkeypatch.setenv("FAKE_LS_REMOTE_RC", "2")
    # 默认：rebase 闸门的三个子命令都成功 —— fetch 成功、远端 head 就是预检的 headRefOid、
    # base 是 head 的祖先（已 rebase）。各用例按需覆盖成红样。
    monkeypatch.setenv("FAKE_FETCH_RC", "0")
    monkeypatch.setenv("FAKE_REV_PARSE_OUT", HEAD_OID)
    monkeypatch.setenv("FAKE_REV_PARSE_BASE_OUT", BASE_OID)
    monkeypatch.setenv("FAKE_MERGE_BASE_RC", "0")
    # 默认：``--repo`` 的 remote 指向本仓（双问绑定闸门的仓库核对通过）
    monkeypatch.setenv("FAKE_REMOTE_URL", "git@github.com:alsj213/team-board.git")
    monkeypatch.setenv("FAKE_REMOTE_RC", "0")
    monkeypatch.setenv("FAKE_API_RC", "0")
    # ``--repo`` 是**真 git 库**（E4 起双问闸门绑内容指纹）：基线提交 + 改动提交，
    # 两条私有 ref 预置好（= 闸门那次 fetch 的结果）
    base_sha, head_sha = _init_content_repo(repo)
    out = Stub(
        bindir=bindir,
        log=log,
        repo=repo,
        body_file=body_file,
        setenv=monkeypatch.setenv,
        board_db=board_db,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    out.view()
    # 指纹由**真 git** 算出（假脚本只把 merge-base / diff / patch-id / cat-file 代理过去）：
    # 预计算这几条命令也会被假脚本记进 FAKE_LOG，算完清掉，免得污染用例的调用断言
    out.pid = out.fingerprint()
    log.unlink(missing_ok=True)
    # 默认：双问绑定闸门通过 —— 两个端点各一条标记，都绑在 fixture 那份内容的指纹上
    out.set_review_bodies(
        [patch_body(out.pid, "defect")], [patch_body(out.pid, "regression")]
    )
    return out


def args_for(stub: Stub, *extra: str, delete_branch: bool = True) -> list[str]:
    """一条能走通的基本命令；``extra`` 追加在最后（覆盖同名参数）。

    合并模式必填的认账四参数默认带上（``--card`` / ``--by`` / ``--subject`` /
    ``--body-file``）；``--verify-only`` 的用例请改用 :func:`args_for_verify` ——
    它与这四个参数互斥，一起给就是 rc2。
    """

    base = [
        "--pr",
        "9",
        "--branch",
        CARD_BRANCH,
        "--method",
        "merge",
        "--repo",
        str(stub.repo),
        "--db",
        str(stub.board_db),
        "--timeout",
        "1",
        "--interval",
        "1",
        "--card",
        MERGE_CARD,
        "--by",
        MERGE_BY,
        "--subject",
        MERGE_SUBJECT,
        "--body-file",
        str(stub.body_file),
    ]
    if delete_branch:
        base.append("--delete-branch")
    return base + list(extra)


def accounting_args(stub: Stub) -> list[str]:
    """认账四参数（合并模式必填）；手搭 argv 的用例复用它。"""

    return [
        "--card",
        MERGE_CARD,
        "--by",
        MERGE_BY,
        "--subject",
        MERGE_SUBJECT,
        "--body-file",
        str(stub.body_file),
    ]


def args_for_verify(stub: Stub, *extra: str, delete_branch: bool = True) -> list[str]:
    """``--verify-only`` 的基本命令：**不带**认账四参数（它们与 verify-only 互斥）。"""

    base = [
        "--pr",
        "9",
        "--branch",
        CARD_BRANCH,
        "--method",
        "merge",
        "--repo",
        str(stub.repo),
        "--timeout",
        "1",
        "--interval",
        "1",
    ]
    if delete_branch:
        base.append("--delete-branch")
    return [*base, "--verify-only", *extra]


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = mg.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ------------------------------------------------ 1. 缺 --delete-branch 被拦

def test_missing_delete_branch_rc2_and_no_gh_call(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, err = run(args_for(stub, delete_branch=False), capsys)

    assert rc == 2
    assert out == ""  # stdout 必须空
    assert err.startswith("错误: 合并命令缺少 --delete-branch")
    assert err.count("\n") == 1  # stderr 只有一行
    assert "本工具不提供不带删除的合并路径" in err
    assert stub.calls() == []  # 一个 gh（乃至 git）写操作都没发


def test_no_way_to_turn_off_delete_branch(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """没有关掉删除的开关，也没有绕过 PR 检查的口子。"""

    for flag in ("--no-delete-branch", "--keep-branch", "--admin", "--auto", "--force"):
        rc, out, err = run(args_for(stub, flag), capsys)
        assert rc == 2, flag
        assert out == "", flag
        assert err.startswith("错误: "), flag
    assert stub.calls() == []


# --------------- 1b. 合并前源分支不存在：rc2、stdout 空、零 gh 调用（新闸门）

def test_merge_path_branch_never_existed_rc2_and_zero_gh_calls(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """「这个 ref 从来没存在过」不许被读成「已删除」：合并前就 rc2。

    依据（pm 实跑）：``--verify-only --branch wt/t_never_existed_zzz`` 修前打
    「结论：分支已删除」+ rc0。合并路径同理 —— 闸门必须在 ``gh pr view`` 之前。
    """

    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "2")  # 合并前就「没有这个 ref」

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""  # 合并还没发生 → 一个字的证据都不该有
    assert err.startswith("错误: 源分支 wt/t_bbeab29f 不在 origin 远端（rc=2）")
    assert err.count("\n") == 1  # stderr 恰一行
    assert stub.gh_calls() == []  # 零 gh 调用（写操作更是一个都没有）
    assert len(stub.git_calls()) == 1


def test_merge_path_branch_probe_error_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """合并前的探针通信错误（rc=128）同样是 rc2，不许当成「分支在」继续合并。"""

    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "128")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "rc=128" in err
    assert stub.gh_calls() == []


# ------ 1d. --branch 形态校验（P2-1）：空串 / 含空白 / refs/ 前缀 → rc2、零外部调用
#
# 修前（pm 实测）：--verify-only 对这三种输入都打「分支删除 | PASS」+ rc0。原因是
# ls-remote 的入参是 refs/heads/<--branch> 直接拼的：'' → refs/heads/、
# ' wt/x' → refs/heads/ wt/x、'refs/heads/wt/x' → refs/heads/refs/heads/wt/x，
# 三者在远端都不存在 → rc=2 → 被读成「已删除」。合并路径与 --verify-only 必须同一套校验。

MALFORMED_BRANCHES: list[tuple[str, str, str]] = [
    ("空串", "", mg.BRANCH_RULE_EMPTY),
    ("前导空格", f" {CARD_BRANCH}", mg.BRANCH_RULE_WHITESPACE),
    ("中间制表符", "wt/\tt_bbeab29f", mg.BRANCH_RULE_WHITESPACE),
    ("全角空格", f"　{CARD_BRANCH}", mg.BRANCH_RULE_WHITESPACE),
    ("尾随换行", f"{CARD_BRANCH}\n", mg.BRANCH_RULE_WHITESPACE),
    ("refs/ 前缀", f"refs/heads/{CARD_BRANCH}", mg.BRANCH_RULE_REFS_PREFIX),
]


@pytest.mark.parametrize(("label", "bad_branch", "rule"), MALFORMED_BRANCHES)
@pytest.mark.parametrize("verify_only", [False, True])
def test_malformed_branch_rc2_with_zero_calls(
    stub: Stub,
    capsys: pytest.CaptureFixture[str],
    label: str,
    bad_branch: str,
    rule: str,
    verify_only: bool,
) -> None:
    """畸形 ``--branch``：两条路径都 rc2、stdout 空、stderr 恰一行、零 gh / 零 git。"""

    extra = ["--branch", bad_branch]
    builder = args_for_verify if verify_only else args_for
    rc, out, err = run(builder(stub, *extra), capsys)

    assert rc == 2, label
    assert out == "", label  # 一个字的证据都没有（更不许有 PASS 行）
    assert err.startswith("错误: --branch "), label
    assert rule in err, label  # 说清是哪条规则
    assert err.count("\n") == 1, label  # stderr 严格一行
    assert stub.calls() == [], label  # 零 gh、零 git


# ------------------------------------- 2. 合并成功但分支仍在 → rc1（不算成功）

def test_merge_ok_but_branch_remains_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "0")  # ref 仍在

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 1
    assert err == ""
    assert "结论：合并成功但源分支仍在远端（合并不算完成）" in out
    assert "分支删除 | FAIL" in out
    assert out.splitlines()[0].startswith("分支存在 | PASS | git -C ")
    assert merge_call(stub) in stub.gh_calls()
    assert stub.git_calls()  # 确实做了远端判定


# ------------------------------------------ 3. 合并成功 + ls-remote rc2 → rc0

def test_merge_ok_and_branch_deleted_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "2")  # 没有这个 ref

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0
    assert err == ""
    lines = out.splitlines()
    assert lines[0].startswith(
        f"分支存在 | PASS | git -C {stub.repo} ls-remote --exit-code --refs "
        f"origin refs/heads/{CARD_BRANCH} | rc=0 | "
    )
    assert lines[1].startswith(f"预检 | PASS | {VIEW_CALL} | rc=0 | ")
    # 预检钉到提交：新增一行 head sha 证据（值是假 gh 报的 headRefOid）
    assert lines[2] == f"head sha | PASS | {HEAD_OID}"
    # 预检之后、rebase 之前：三道证据闸门（认领唯一性 → 证据状态机 → 双问绑定 head sha）
    assert lines[3] == claim_line(stub)
    assert lines[4] == evidence_line(stub)
    assert lines[5] == review_line(stub)
    # 然后才是 rebase 闸门（base 必须是 head 的祖先）
    assert lines[6] == rebase_line(stub)
    assert lines[7].startswith(f"合并 | PASS | {merge_call(stub)} | rc=0 | ")
    assert lines[8].startswith(
        f"分支删除 | PASS | git -C {stub.repo} ls-remote --exit-code --refs "
        f"origin refs/heads/{CARD_BRANCH} | rc=2 | "
    )
    assert lines[-1] == f"结论：合并完成，源分支 {CARD_BRANCH} 已从 origin 删除"
    assert stub.gh_calls() == [
        VIEW_CALL,
        *API_CALLS,
        merge_call(stub),
    ]


# -------- 1c. 预检钉到提交（P2-3）：--match-head-commit + head sha 证据行
#
# 修之前：预检只比分支名，预检到合并之间被 force-push / 追加提交时，合的是预检从未
# 见过的提交，工具照样 PASS + rc0。现在 gh pr view 读 headRefOid、合并命令带
# --match-head-commit，预检通过后把 sha 记进证据。

def test_merge_pins_head_sha(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """``gh pr merge`` 必须收到 ``--match-head-commit <headRefOid>``，并留一行 head sha 证据。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0
    assert err == ""
    assert f"head sha | PASS | {HEAD_OID}" in out  # 预检钉到提交的证据行
    assert merge_call(stub) in stub.gh_calls()
    # 每一次合并调用都必须带 sha：一条不带 sha 的重试都不许有
    merge_calls = [call for call in stub.gh_calls() if call.startswith("gh pr merge")]
    assert merge_calls == [merge_call(stub)]


def test_view_missing_head_oid_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """假 gh 少报 ``headRefOid`` → rc2、stdout 空、stderr 一行、绝不合并。"""

    stub.view(head_oid=None)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "headRefOid" in err
    assert "gh pr merge" not in " ".join(stub.gh_calls())


@pytest.mark.parametrize(
    "head_oid",
    ["4f2a9c0", "zz" * 20, ""],
    ids=["short", "non-hex", "empty"],
)
def test_view_bad_head_oid_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], head_oid: str
) -> None:
    """``headRefOid`` 不是 40/64 位十六进制 → rc2（没有合法 sha 就不许合并）。"""

    stub.view(head_oid=head_oid)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "gh pr merge" not in " ".join(stub.gh_calls())


def test_old_gh_unknown_match_head_commit_rc2_no_downgrade(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """老版本 gh 报 ``unknown flag: --match-head-commit`` → rc2，**不许**降级合并。

    判据：stdout 空、stderr 恰一行、只发过一次合并调用（就是带 sha 那次）、
    合并后的删除验证一次都没跑。
    """

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.setenv("FAKE_MERGE_ERR", "unknown flag: --match-head-commit")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1：环境不支持，判不了
    assert out == ""  # 合并一定没发生 → 一个字的证据都不该有
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "--match-head-commit" in err
    assert "不提供不带 head sha 的合并路径" in err
    merge_calls = [call for call in stub.gh_calls() if call.startswith("gh pr merge")]
    assert merge_calls == [merge_call(stub)]  # 没有「不带 sha 再试一次」
    # 只有合并前那次存在性探针，没有删除验证（rebase 闸门的 fetch / rev-parse /
    # merge-base 是另外三条子命令，按子命令分派计数，不混进 ls-remote 的次数里）
    assert len([call for call in stub.git_calls() if " ls-remote " in call]) == 1


# ------------------- 1d. --timeout 是总预算（P2-1）：单次卡住也不许击穿它
#
# 修之前：单次探针只受 --command-timeout 约束，--timeout 1 + 卡 3 秒的 git → 3.1s。

def test_verify_only_timeout_is_total_budget(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--timeout 1`` + 单次 probe 3 秒 → 总耗时 ≤ ~1.5s，结论「无法判定」rc1。"""

    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")  # 别让「第一次」跳过 sleep
    stub.setenv("FAKE_GIT_SLEEP", "3")
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    started = time.monotonic()
    rc, out, err = run(
        args_for_verify(
            stub,
            "--timeout",
            "1",
            "--interval",
            "1",
            "--command-timeout",
            "60",
        ),
        capsys,
    )
    elapsed = time.monotonic() - started

    assert rc == 1  # --verify-only 的「无法判定」按现有口径是 1
    assert err == ""
    assert elapsed <= 1.5  # 不许跑满 3 秒
    assert "结论：无法判定" in out
    assert "总预算" in out


def test_merge_path_timeout_is_total_budget_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """合并已发生后删除验证卡住 + ``--timeout 1`` → rc2（判不了），耗时 ≈ 1 秒。"""

    stub.setenv("FAKE_GIT_SLEEP", "3")
    stub.setenv("FAKE_LS_REMOTE_RC", "128")  # 第 2 次起的探针：睡满再报通信错误

    started = time.monotonic()
    rc, out, err = run(
        args_for(stub, "--timeout", "1", "--interval", "1", "--command-timeout", "60"),
        capsys,
    )
    elapsed = time.monotonic() - started

    assert rc == 2  # 合并已发生但判不了 → rc2，不是 1
    assert elapsed <= 1.5
    assert f"合并 | PASS | {merge_call(stub)}" in out  # 证据必须留住
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "无法判定" in err
    assert "总预算" in err


# ------------------------------- 3b. --method 只认 merge（R13）：默认值 + 拒绝两值

def test_method_defaults_to_merge_without_flag(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """**完全不传 ``--method``**：gh 收到的 argv 里必须是 ``--merge``。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    argv = [
        "--pr",
        "9",
        "--branch",
        CARD_BRANCH,
        "--repo",
        str(stub.repo),
        "--db",
        str(stub.board_db),
        "--delete-branch",
        "--timeout",
        "1",
        "--interval",
        "1",
        *accounting_args(stub),
    ]
    assert "--method" not in argv  # 确保真的没传

    rc, out, err = run(argv, capsys)

    assert rc == 0
    assert err == ""
    assert f"合并 | PASS | {merge_call(stub)} | rc=0 | " in out
    assert stub.gh_calls() == [
        VIEW_CALL,
        *API_CALLS,
        merge_call(stub),
    ]


@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_squash_and_rebase_rejected_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], method: str
) -> None:
    """R13：squash / rebase 一律 rc2、stdout 空、stderr 恰一行、**零 gh/git 调用**。"""

    rc, out, err = run(args_for(stub, "--method", method), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert method in err  # 说清实际收到的值
    assert stub.calls() == []  # 连「分支存在」预检都没跑


def test_gh_stderr_warning_is_not_a_failure(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 的 stderr 警告（worktree 里删不掉本地分支）不算失败，判定只看远端。"""

    stub.setenv("FAKE_MERGE_ERR", "failed to delete local branch wt/t_bbeab29f")
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, _err = run(args_for(stub), capsys)

    assert rc == 0
    assert "结论：合并完成" in out


# ------------------------------- 4. gh pr merge 非 0：先反查 state，再决定 rc
#
# gh 2.103.3 的 mergeRun 顺序是 merge() → deleteLocalBranch() → deleteRemoteBranch()：
# 删本地分支失败就 return 非 0，而那时 PR 已合、远端分支还没删。所以「gh 非 0」不能
# 一律当「合并失败」。

def test_merge_failure_rc1_and_no_verification(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 非 0 + state 不是 MERGED → rc1「合并失败」，且不跑删除验证。"""

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.setenv("FAKE_MERGE_ERR", "pull request is not mergeable: required check failing")
    stub.view_after_merge(state="OPEN")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 1
    # 只有合并前那一次「源分支存在」探针，合并后一次删除验证都没跑（rebase 闸门的
    # fetch / rev-parse / merge-base 另算：这里是按 ls-remote 次数判「没做删除验证」）
    assert len([call for call in stub.git_calls() if " ls-remote " in call]) == 1
    assert stub.git_calls()[0].startswith("git -C ")
    assert merge_call(stub) in stub.gh_calls()
    assert STATE_CALL in stub.gh_calls()  # 做了状态反查（同一个 -R）
    assert "合并 | FAIL" in out
    assert "合并失败" in out
    assert "PR 状态 OPEN" in out
    assert "合并成功" not in out
    assert not [line for line in out.splitlines() if line.startswith("分支删除 | ")]
    assert "required check failing" in err  # gh 输出原文写出来了


def test_merge_gh_nonzero_but_state_merged_then_verification_deleted_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 非 0 + state=MERGED + 远端分支已删 → rc0，**不许**说「合并失败」。

    真实触发：``gh pr merge --delete-branch`` 删本地分支失败（分支被别处 worktree
    / checkout 占着）→ 非 0 退出，但 PR 已经合并、远端分支还没删。
    """

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.setenv("FAKE_MERGE_ERR", "failed to delete local branch wt/t_bbeab29f")
    stub.view_after_merge(state="MERGED")
    stub.setenv("FAKE_LS_REMOTE_RC", "2")  # 远端已经没有这个 ref

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0
    assert "合并失败" not in out
    assert STATE_CALL in stub.gh_calls()
    assert f"合并 | PASS | {merge_call(stub)} | rc=1 | " in out
    assert "PR 状态已是 MERGED" in out
    assert "分支删除 | PASS | " in out
    assert out.splitlines()[-1] == f"结论：合并完成，源分支 {CARD_BRANCH} 已从 origin 删除"
    # gh 的退出码与输出原文都留了证
    assert "rc=1" in out
    assert "failed to delete local branch" in err


def test_merge_gh_nonzero_state_merged_branch_present_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 非 0 + state=MERGED + 分支仍在远端 → rc1（合并成功但没删干净）。"""

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.view_after_merge(state="MERGED")
    stub.setenv("FAKE_LS_REMOTE_RC", "0")

    rc, out, _err = run(args_for(stub), capsys)

    assert rc == 1
    assert "合并失败" not in out
    assert out.splitlines()[-1] == "结论：合并成功但源分支仍在远端（合并不算完成）"


def test_merge_gh_nonzero_state_undetermined_rc2_keeps_evidence(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh 非 0 + MERGED + 删除验证判不了 → 走「合并已完成但无法判定」rc2。"""

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.view_after_merge(state="MERGED")
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1
    assert "合并失败" not in out
    assert out.splitlines()[0].startswith("分支存在 | PASS | ")
    assert f"合并 | PASS | {merge_call(stub)}" in out
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "无法判定" in err


def test_merge_gh_nonzero_state_query_fails_stays_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """反查本身失败 → 退回旧口径 rc1「合并失败」（反查不是新的失败源，不许变 rc2）。"""

    stub.setenv("FAKE_MERGE_RC", "1")
    stub.view_after_merge(state="MERGED", rc=1)  # 反查命令自己失败

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 1
    assert "合并失败" in out
    assert "PR 状态 反查失败" in out
    assert err.count("\n") == 0 or "错误: " not in err


# ------------------------------------------- 5. --verify-only（一次 gh 都不调）

def test_verify_only_branch_present_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "0")

    rc, out, err = run(args_for_verify(stub), capsys)

    assert rc == 1
    assert err == ""
    assert out.splitlines()[-1] == "结论：分支仍在远端"
    # 步骤行只报 ref 现状：--verify-only 没有「删除」这一步，不许打 PASS / FAIL
    assert out.splitlines()[0].startswith("远端 ref | 存在 | ")
    assert "PASS" not in out and "FAIL" not in out
    assert stub.gh_calls() == []


def test_verify_only_branch_deleted_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """远端没有这个 ref → rc0，但结论**只能说「没有」，不许说「分支已删除」**。

    因为 ``--verify-only`` 不知道这个分支是「删掉了」还是「从来没存在过」——
    后者正是修前把 ``wt/t_never_existed_zzz`` 读成 rc0「已删除」的那条路。

    步骤行同理：修前打 ``分支删除 | PASS | … | rc=2 | 分支已不在远端``，那一列会被
    抄进卡当「删除已验证」的证据，而这条路径一次删除都没验证过。
    """

    stub.setenv("FAKE_LS_REMOTE_RC", "2")
    # --verify-only 没有「合并前存在性」那道闸门：别让第一次调用被 FIRST_RC 抢走
    # （否则第一次报「分支在」，--timeout 1 的预算就只够一次探针，结论变成 rc1）
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")

    rc, out, err = run(args_for_verify(stub), capsys)

    assert rc == 0  # rc 语义不变
    assert err == ""
    assert out.splitlines()[-1] == "结论：远端没有这个 ref（已删除，或从未存在过）"
    assert "分支已删除" not in out  # 旧措辞（会掩盖「从未存在过」）
    assert "或从未存在过" in out
    # 步骤行：只报「远端 ref 不存在」，既不写 PASS，也不写「分支删除」
    steps = [line for line in out.splitlines() if line.startswith("远端 ref | ")]
    assert len(steps) == 1
    assert steps[0].startswith("远端 ref | 不存在 | ")
    assert "rc=2" in steps[0]
    assert "PASS" not in out
    assert "分支删除" not in out
    assert stub.gh_calls() == []


def test_verify_only_legal_branch_rc2_never_claims_delete(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """合法 ``--branch`` + ``--verify-only`` + ``ls-remote rc=2``：输出里没有宣称删除的 PASS。

    这是本卡的验收判据②：``rc=2`` 只能说明「这个 remote 现在没有这个 ref」。
    同时钉住探针查的 ref **就是裸分支名拼出来的那一个** —— 不许出现
    ``refs/heads/refs/heads/…`` 或带空白的 ref（那才是修前「假绿」的来源）。
    """

    stub.setenv("FAKE_LS_REMOTE_RC", "2")
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")  # --verify-only 没有「合并前存在」闸门

    rc, out, err = run(args_for_verify(stub), capsys)

    assert rc == 0
    assert err == ""
    for line in out.splitlines():
        if "PASS" in line:
            assert "删除" not in line, line  # 任何 PASS 行都不许宣称删除
    assert "分支删除" not in out
    assert "已删除 |" not in out  # 步骤行的状态字段只有 存在 / 不存在 / 无法判定
    assert stub.git_calls() == [
        f"git -C {stub.repo} ls-remote --exit-code --refs origin refs/heads/{CARD_BRANCH}"
    ]


def test_verify_only_undetermined_rc1(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "128")
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")  # 同上：第一次就该是 128，不是「分支在」

    rc, out, _err = run(args_for_verify(stub), capsys)

    assert rc == 1
    assert out.splitlines()[-1].startswith("结论：无法判定（")


# --------------------------------------- 6. 参数取值域越界（全部 rc2、stdout 空）

@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--pr", "0"),
        ("--pr", "-1"),
        ("--pr", "1793160000000"),  # 13 位毫秒
        ("--pr", "1000000000"),
        ("--pr", "abc"),
        ("--pr", ""),
        ("--timeout", "0"),
        ("--timeout", "-1"),
        ("--timeout", "1_000"),
        ("--timeout", ""),
        ("--timeout", "abc"),
        ("--interval", "0"),
        ("--interval", "-5"),
        ("--interval", ""),
        ("--method", "fast-forward"),
        ("--method", ""),
    ],
)
def test_bad_values_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    rc, out, err = run(args_for(stub, flag, value), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert stub.calls() == []


def test_pr_boundary_accepted(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """上界 999999999 本身是合法值（不是越界）。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")
    # body 里的 merge-pr 必须与 --pr 一致（check_message 会逐字段比对）
    stub.set_body(
        f"merged-by: {MERGE_BY}\nmerge-card: {MERGE_CARD}\nmerge-pr: #999999999\n"
    )
    # 认领闸门要读到「恰好一张卡认领 #999999999，且就是 --card」
    stub.set_claims([(MERGE_CARD, "PR-CLAIM: #999999999\n")])
    # 双问闸门读的是 refs/merge-guard/<pr>/*：把 fixture 的两条私有 ref 复制到这个 PR 号下
    stub.alias_pr(999999999)

    rc, out, _err = run(
        [
            "--pr",
            "999999999",
            "--branch",
            CARD_BRANCH,
            "--delete-branch",
            "--repo",
            str(stub.repo),
            "--db",
            str(stub.board_db),
            "--timeout",
            "1",
            "--interval",
            "1",
            *accounting_args(stub),
        ],
        capsys,
    )

    assert rc == 0
    # 未传 --method → 默认 merge（gh 收到的是 --merge）；认账两参数追加在后面
    merge_calls = [call for call in stub.gh_calls() if call.startswith("gh pr merge")]
    assert len(merge_calls) == 1
    assert merge_calls[0].startswith(
        f"gh pr merge 999999999 -R {REPO_SLUG} --merge --delete-branch"
        f" --match-head-commit {HEAD_OID} "
    )
    assert f"--subject {MERGE_SUBJECT} --body-file {stub.body_file}" in merge_calls[0]
    assert "结论：合并完成" in out


# ----------------------------------------- 7. 安全：不许删主分支 / base 分支

def test_branch_main_rejected(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--pr",
        "9",
        "--branch",
        "main",
        "--method",
        "merge",
        "--delete-branch",
        "--repo",
        str(stub.repo),
        *accounting_args(stub),
    ]

    rc, out, err = run(argv, capsys)

    assert rc == 2
    assert out == ""
    assert "主分支" in err
    assert stub.calls() == []  # 连 preflight 都没跑


def test_branch_master_rejected(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--pr",
        "9",
        "--branch",
        "master",
        "--method",
        "merge",
        "--delete-branch",
        "--repo",
        str(stub.repo),
        *accounting_args(stub),
    ]

    rc, out, err = run(argv, capsys)

    assert rc == 2
    assert out == ""
    assert "主分支" in err


def test_branch_equals_base_ref_rejected(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.view(head="release", base="release")

    rc, out, err = run(args_for(stub, "--branch", "release"), capsys)

    assert rc == 2
    assert out == ""
    assert "baseRefName" in err
    # 只跑了 preflight，没有发起合并（认领闸门在 head/branch 校验之后才跑，这里轮不到）
    assert stub.gh_calls() == [VIEW_CALL]


# ------------------------------- 8. --branch 与 PR headRefName 不一致 → rc2

def test_branch_mismatch_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    stub.view(head="wt/t_aaaaaaaa")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert "headRefName" in err
    assert "wt/t_aaaaaaaa" in err  # 说清实际值
    assert "gh pr merge" not in " ".join(stub.gh_calls())


def test_pr_not_open_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    stub.view(state="MERGED")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert "MERGED" in err
    assert "gh pr merge" not in " ".join(stub.gh_calls())


# ------------------------------------------- 9. PATH 里没有 gh → rc2

def test_missing_gh_rc2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "git-only-bin"
    bindir.mkdir()
    _write_exe(bindir / "git", GIT_STUB)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("FAKE_GIT_COUNT", str(tmp_path / "git.count"))
    monkeypatch.setenv("FAKE_LS_REMOTE_FIRST_RC", "0")  # 分支存在闸门先过
    monkeypatch.setenv("FAKE_FETCH_RC", "0")
    monkeypatch.setenv("FAKE_REV_PARSE_OUT", HEAD_OID)
    repo = tmp_path / "repo"
    repo.mkdir()
    body_file = tmp_path / "merge-body.txt"
    body_file.write_text(BODY_TEXT, encoding="utf-8")
    # 认领闸门与证据状态机闸门都先过（都只读本地库，不需要 gh），才会走到「取评审正文时
    # gh 不见了」
    board_db = make_board_db(
        tmp_path / "board.db",
        [(MERGE_CARD, "running", "PR-CLAIM: #9\n")],
        evidence_card=MERGE_CARD,
    )
    stub = Stub(bindir, tmp_path / "calls.log", repo, body_file, monkeypatch.setenv, board_db)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "gh" in err


def test_missing_git_rc2_before_any_gh_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git`` 不可用 → 合并前的「源分支存在」闸门就 rc2：零 gh 调用、stdout 空。

    修前这一步不存在，git 缺失只能在**合并之后**才被发现（rc2 但 PR 已被合并）。
    现在往前挪到合并之前，这个风险整条消失。
    """

    bindir = tmp_path / "gh-only-bin"
    bindir.mkdir()
    _write_exe(bindir / "gh", GH_STUB)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("PATH", str(bindir))  # 没有 git
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_MERGED_MARK", str(tmp_path / "merged.mark"))
    monkeypatch.setenv("FAKE_VIEW_RC", "0")
    monkeypatch.setenv("FAKE_MERGE_RC", "0")
    monkeypatch.setenv(
        "FAKE_VIEW_JSON",
        json.dumps(
            {"number": 9, "state": "OPEN", "headRefName": CARD_BRANCH, "baseRefName": "main"}
        ),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    body_file = tmp_path / "merge-body.txt"
    body_file.write_text(BODY_TEXT, encoding="utf-8")

    rc, out, err = run(args_for(Stub(bindir, log, repo, body_file, monkeypatch.setenv)), capsys)

    assert rc == 2  # 不是 1
    assert out == ""  # 合并没发生 → 没有证据要保
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "git" in err
    assert not log.exists() or "gh " not in log.read_text(encoding="utf-8")


def test_git_vanishes_after_merge_keeps_evidence(
    stub: Stub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并成功后 git 突然不可用（容器重建 / PATH 被改）→ rc2，且必须保住已完成证据。

    合并前的「分支存在」闸门用的是同一次 ``_run``，所以这里只让**合并后的探针**
    失败（``make_probe`` 返回的探针抛 ``MergeGuardError``），复现「合并已发生、验证
    做不了」这一状态。
    """

    def make_broken_probe(*_args: object, **_kwargs: object) -> Callable[[], mg.CommandResult]:
        def probe() -> mg.CommandResult:
            raise mg.MergeGuardError("找不到 git 可执行文件（PATH 里没有 git），无法合并/验证")

        return probe

    monkeypatch.setattr(mg, "make_probe", make_broken_probe)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1
    lines = out.splitlines()
    assert lines[0].startswith("分支存在 | PASS | ")
    assert lines[1].startswith("预检 | PASS | ")
    assert lines[3] == claim_line(stub)  # 预检之后：认领唯一性闸门
    assert lines[4] == evidence_line(stub)  # 再是证据状态机闸门（四槽绑当前 head）
    assert lines[5] == review_line(stub)  # 再是双问绑定闸门
    assert lines[6] == rebase_line(stub)  # 预检之后、合并之前：rebase 闸门
    assert lines[7].startswith(f"合并 | PASS | {merge_call(stub)} | ")
    assert "Traceback" not in out + err
    assert err.startswith("错误: ")
    assert err.count("\n") == 1  # stderr 只有一行
    assert "git" in err
    assert merge_call(stub) in stub.gh_calls()


# ------------------------------- 合并后通信一直失败 → rc2（明确未通过，不当绿）

def test_merge_ok_but_undetermined_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1：判不了就是未通过
    lines = out.splitlines()
    assert lines[0].startswith("分支存在 | PASS | ")  # 合并已发生 → 证据必须留下
    assert lines[1].startswith("预检 | PASS | ")
    assert lines[3] == claim_line(stub)  # 预检之后：认领唯一性闸门
    assert lines[4] == evidence_line(stub)  # 再是证据状态机闸门（四槽绑当前 head）
    assert lines[5] == review_line(stub)  # 再是双问绑定闸门
    assert lines[6] == rebase_line(stub)  # 预检之后、合并之前：rebase 闸门
    assert lines[7].startswith(f"合并 | PASS | {merge_call(stub)}")
    assert "Traceback" not in out + err
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "无法判定" in err


# --------------------------------------------------- 10. 回归：--help rc0

def test_help_rc0(capsys: pytest.CaptureFixture[str]) -> None:
    rc = mg.main(["--help"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "--delete-branch" in captured.out
    assert "--verify-only" in captured.out
    assert "--command-timeout" in captured.out
    # R13：--method 值域只剩 merge，且默认值要写出来（不再要求调用方必传）
    assert "--method {merge}" in captured.out
    assert "默认 merge" in captured.out
    assert "squash" in captured.out  # 只出现在「一律拒绝」的说明里
    assert captured.err == ""


# --------------------- 11. 三个时长参数的取值域上界（max 3600，越界一律 rc2）

BOUNDARY_VALUES = [
    "0",
    "-1",
    "1793160000000",  # 13 位毫秒：修好之前它会让 poll 挂死（rc=124）
    "9" * 400,  # 天文数字：修好之前它会在 clock() + timeout 上裸抛 OverflowError
    "",
]


@pytest.mark.parametrize("flag", ["--timeout", "--interval", "--command-timeout"])
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_duration_flags_out_of_range_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    rc, out, err = run(args_for(stub, flag, value), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1  # stderr 恰一行
    assert "Traceback" not in err
    assert stub.calls() == []  # 越界在调 gh / git 之前就被拒


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "3601"),
        ("--interval", "3601"),
        ("--command-timeout", "3601"),
    ],
)
def test_duration_upper_bound_exceeded_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    rc, out, err = run(args_for(stub, flag, value), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert "3600" in err  # 说清上界
    assert stub.calls() == []


def test_duration_upper_bound_itself_accepted(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """上界 3600 本身合法（不是越界）；假 git 第一次就说「已删」，不会真等。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(
        args_for(
            stub,
            "--timeout",
            "3600",
            "--interval",
            "3600",
            "--command-timeout",
            "3600",
        ),
        capsys,
    )

    assert rc == 0
    assert err == ""
    assert "结论：合并完成" in out


def test_interval_greater_than_timeout_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``interval <= timeout`` 是硬约束：轮询间隔比总预算还长等于没预算。"""

    rc, out, err = run(args_for(stub, "--timeout", "3", "--interval", "10"), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "--interval" in err
    assert stub.calls() == []


# ------------------- 12. 子命令超时（--command-timeout）：预检超时 → rc2

def test_preflight_timeout_rc2_and_no_merge(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``gh pr view`` 卡住 → rc2，且**不许**再发合并命令。"""

    stub.setenv("FAKE_VIEW_SLEEP", "5")

    rc, out, err = run(args_for(stub, "--command-timeout", "1"), capsys)

    assert rc == 2
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "超时" in err
    assert "Traceback" not in err
    assert "gh pr merge" not in " ".join(stub.gh_calls())


# ------------------- 13. 子命令超时：合并后的探针超时 → 不是 rc0

def test_probe_timeout_after_merge_is_not_rc0(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ls-remote`` 卡住不许当成「分支还在」（rc1）或「已删」（rc0）。

    超时按通信错误记一次、继续轮询，预算耗尽后判「无法判定」→ rc2（≠0，也≠1），
    且因为合并已发生，stdout 必须保住「预检 | PASS」「合并 | PASS」。
    """

    stub.setenv("FAKE_GIT_SLEEP", "5")

    rc, out, err = run(
        args_for(stub, "--timeout", "1", "--interval", "1", "--command-timeout", "1"),
        capsys,
    )

    assert rc != 0  # 绝不把超时读成「合并完成」
    assert rc == 2
    assert out.splitlines()[0].startswith("分支存在 | PASS | ")
    assert "预检 | PASS | " in out
    assert f"合并 | PASS | {merge_call(stub)}" in out
    assert "分支删除 | FAIL" in out  # 超时不算 PASS
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "无法判定" in err


# ---------- 14. 非 MergeGuardError 异常：不许落 rc1、不许丢已完成步骤证据

def test_unexpected_exception_after_merge_keeps_evidence(
    stub: Stub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> mg.PollResult:
        raise ValueError("模拟意外崩溃")

    monkeypatch.setattr(mg, "poll_branch_deleted", boom)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 不是 1
    lines = out.splitlines()
    assert lines[0].startswith("分支存在 | PASS | ")
    assert lines[1].startswith("预检 | PASS | ")
    assert lines[3] == claim_line(stub)  # 预检之后：认领唯一性闸门
    assert lines[4] == evidence_line(stub)  # 再是证据状态机闸门（四槽绑当前 head）
    assert lines[5] == review_line(stub)  # 再是双问绑定闸门
    assert lines[6] == rebase_line(stub)  # 预检之后、合并之前：rebase 闸门
    assert lines[7].startswith(f"合并 | PASS | {merge_call(stub)} | ")
    assert "Traceback" not in out + err
    assert err.startswith("错误: ")
    assert err.count("\n") == 1  # 一行，没有 traceback
    assert "ValueError" in err
    assert "人工核对" in err


def test_unexpected_exception_before_merge_rc2_stdout_empty(
    stub: Stub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并之前的意外异常：rc2、stdout 空、stderr 一行（没有 traceback）。"""

    def boom(*_args: object, **_kwargs: object) -> mg.CommandResult:
        raise RuntimeError("模拟预检崩溃")

    monkeypatch.setattr(mg, "_run", boom)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert "Traceback" not in err
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "RuntimeError" in err


# --------------------------------- 12. 文档里的命令必须与 CLI 一致（可机器核对）

MERGE_POLICY = Path(__file__).resolve().parents[2] / "docs" / "merge-policy.md"


def _documented_merge_commands() -> list[list[str]]:
    """把 ``docs/merge-policy.md`` 里每行 ``python -m team_board.merge_guard …`` 拆成 argv。

    文档与 CLI 分叉过一次（文档缺 ``--branch`` → 照抄它的合并卡一律 rc2），所以这里
    不靠人眼对：把文档里的命令原文抠出来直接喂给 ``main()``。
    """

    argvs: list[list[str]] = []
    for line in MERGE_POLICY.read_text(encoding="utf-8").splitlines():
        at = line.find("python -m team_board.merge_guard")
        if at < 0:
            continue
        parts = shlex.split(line[at:].split(";")[0].strip())
        assert parts[:3] == ["python", "-m", "team_board.merge_guard"], line
        argvs.append(parts[3:])
    return argvs


def test_documented_commands_carry_required_branch() -> None:
    """条文 ② 与模板 ⑦ 两条命令都必须带 ``--branch wt/<卡号>``（``--branch`` 必填），
    并且必须带齐认账四参数（缺任一项 → 经它合出的提交过不了 ``merge-audit --require``）。"""

    argvs = _documented_merge_commands()

    assert len(argvs) == 2  # 文档里只有这两条 merge_guard 命令；增删都得跟着改这里
    for argv in argvs:
        assert "--branch" in argv, argv
        assert "wt/<卡号>" in argv, argv
        for flag in ("--card", "--by", "--subject", "--body-file"):
            assert flag in argv, (flag, argv)


def _fill_documented(argv: list[str], stub: Stub) -> list[str]:
    """把文档命令里的占位符换成合法值（``<body-file>`` 换成 fixture 造的 body 文件）。"""

    return [
        arg.replace("<n>", "9")
        .replace("<卡号>", MERGE_CARD)
        .replace("<NNN>", "200")
        .replace("<body-file>", str(stub.body_file))
        # R15：执行者只能是 repo。docs/merge-policy.md 的样例行仍写旧 profile
        # （改它归 G1-b），这里按 repo 填，否则占位值自己就把命令挡在预检之前 ——
        # 那不再是「文档命令能不能跑」的判据。G1-b 改完文档后这两条 replace 变成空操作。
        .replace("pm/run", "repo/run")
        for arg in argv
    ]


def test_documented_command_runs_to_preflight_and_merge(
    stub: Stub, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """照抄文档那条命令（占位符填掉）→ 跑通到预检并合并，而不是 rc2 于缺参数。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    for index, argv in enumerate(_documented_merge_commands()):
        # 每次跑都是一个新进程视角：换一份「第几次 git 调用」计数，
        # 让「分支存在」闸门重新走 FAKE_LS_REMOTE_FIRST_RC=0 那条
        stub.setenv("FAKE_GIT_COUNT", str(tmp_path / f"git-{index}.count"))
        filled = _fill_documented(argv, stub)
        rc, out, err = run(
            [
                *filled,
                "--repo",
                str(stub.repo),
                # 认领闸门读的库：文档命令走默认（真库），测试里换成 fixture 库
                "--db",
                str(stub.board_db),
                "--timeout",
                "1",
                "--interval",
                "1",
            ],
            capsys,
        )

        assert rc == 0, (filled, err)
        assert err == ""
        assert "分支存在 | PASS | " in out
        assert "预检 | PASS | " in out
        assert merge_call(stub) in stub.gh_calls()


# ------------------------- 12b. README 里的样例行同样必须可跑（文档守卫的另一半）
#
# 只在 docs/merge-policy.md 上做守卫是不够的：PR #30 把 ``--card`` / ``--by`` /
# ``--subject`` / ``--body-file`` 变成合并模式必填之后，README 那条「源分支不存在」
# 的样例照抄已经拿不到文档写的报错（先撞「缺少 --card」），而 808 条全绿照不到。
# 这里把 README.md 的每条样例行连同它下面那行 ``# stderr: …`` 一起抠出来真跑：
# 期望文案一律取 README 原文，**不在测试里再抄一遍**（两边一起改就永远绿）。

README = Path(__file__).resolve().parents[2] / "README.md"

#: README 里的样例行前缀（``python3`` / ``python`` 两种写法都认）；不带 ``$ `` 的用法模板不算
_README_CMD_RE = re.compile(r"^\$ python3? -m team_board\.merge_guard(?:\s|$)")
#: 样例行下面那行期望 stderr
_README_STDERR_RE = re.compile(r"^# stderr: (?P<text>.+)$")


def _readme_guard_commands() -> list[tuple[list[str], str, str]]:
    """把 README 每条样例行配成 ``(argv, 文档期望的 stderr, 样例前那段说明文字)``。

    反斜杠续行拼回一条命令；样例下面必须紧跟 ``# stderr: …``（不跟 = 样例行没法判，
    按文档 bug 处理）。``argv`` 已去掉 ``python3 -m team_board.merge_guard`` 前缀。
    """

    pairs: list[tuple[list[str], str, str]] = []
    lines = README.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if _README_CMD_RE.match(line) is None:
            index += 1
            continue
        start = index
        command = line[1:].strip()  # 去掉开头的 "$ "
        while command.endswith("\\"):
            index += 1
            command = f"{command[:-1].strip()} {lines[index].strip()}"
        argv = shlex.split(command)
        assert argv[:3] == ["python3", "-m", "team_board.merge_guard"], line
        prefix: list[str] = []
        back = start - 1
        while back >= 0 and len(prefix) < 3:  # 只收样例上方那一段说明（跨过围栏与空行）
            text = lines[back].strip()
            if text and not text.startswith("```"):
                prefix.insert(0, text)
            back -= 1
        index += 1
        match = _README_STDERR_RE.match(lines[index]) if index < len(lines) else None
        assert match is not None, f"README 的样例行下面没有 `# stderr: …`：{line}"
        pairs.append((argv[3:], match.group("text"), " ".join(prefix)))
        index += 1
    return pairs


def test_readme_samples_are_all_guarded() -> None:
    """README 的 4 条样例行（1 条合并模式 + 3 条 ``--verify-only``）一条不漏地被抠出来。"""

    pairs = _readme_guard_commands()

    assert len(pairs) == 4  # README 增删样例都必须跟着改这里，否则守卫悄悄瞎掉
    occurrences = [
        line
        for line in README.read_text(encoding="utf-8").splitlines()
        if "-m team_board.merge_guard" in line
    ]
    # 多出来的那一处是不带 "$ " 的用法模板（[--verify-only] 那行）
    assert len(occurrences) == len(pairs) + 1


def test_readme_merge_mode_sample_carries_accounting_flags() -> None:
    """README 那条合并模式样例必须保持「合并模式 + 认账四参数」形态，不许降级成 ``--verify-only``，
    并且样例前面写清了 body 先由 ``merge-record --body-out`` 生成。"""

    merge_mode = [
        (argv, prefix)
        for argv, _text, prefix in _readme_guard_commands()
        if "--verify-only" not in argv
    ]

    assert len(merge_mode) == 1
    argv, prefix = merge_mode[0]
    assert "--branch" in argv
    assert "wt/t_never_existed_zzz" in argv
    assert "--method" in argv and "merge" in argv
    assert "--delete-branch" in argv
    for flag in ("--card", "--by", "--subject", "--body-file"):
        assert flag in argv, (flag, argv)
    assert "merge-record" in prefix, prefix
    assert "--body-out" in prefix, prefix


def _fill_readme(argv: list[str], stub: Stub) -> list[str]:
    """把 README 样例里的占位符换成合法值；留下任何 ``<…>`` 都算文档 bug。"""

    mapping = {
        "<卡号>": MERGE_CARD,
        # R15：合并权归 repo bot，``--by`` 的 profile 只能是 repo（``<profile>`` 占位符
        # 现在只有一个合法展开值）
        "<profile>": "repo",
        "<NNN>": "200",
        "<subject>": MERGE_SUBJECT,
        "<body文件>": str(stub.body_file),
    }
    filled: list[str] = []
    for arg in argv:
        for token, value in mapping.items():
            arg = arg.replace(token, value)
        assert re.search(r"<[^<>]*>", arg) is None, f"README 样例行里有没填的占位符：{arg}"
        filled.append(arg)
    return filled


def test_readme_samples_run_and_match_documented_stderr(
    stub: Stub, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """照抄 README 每条样例行 → rc2、stdout 0 字节、stderr 与 README 的 ``# stderr:`` 逐字相同。

    这条就是 README「合并前源分支不存在」样例的守卫：文档写的报错一旦被参数校验挡在
    前面（PR #30 的 P2），这里立刻红。
    """

    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "2")  # 合并前的「源分支存在」闸门报不存在

    pairs = _readme_guard_commands()
    assert pairs
    for index, (argv, documented, _prefix) in enumerate(pairs):
        # 每条样例都是新进程视角：换一份「第几次 git 调用」计数
        stub.setenv("FAKE_GIT_COUNT", str(tmp_path / f"readme-git-{index}.count"))
        filled = [
            *_fill_readme(argv, stub),
            "--repo",
            str(stub.repo),
            "--timeout",
            "1",
            "--interval",
            "1",
        ]
        rc, out, err = run(filled, capsys)

        assert rc == 2, (filled, out, err)
        assert out == ""  # stdout 0 字节
        assert err.strip() == documented.strip(), (filled, err, documented)
        if "--verify-only" not in argv:
            assert stub.gh_calls() == []  # 合并模式：一个 gh 调用都没发


# ---------------- 15. stdout 断管 / 关闭：rc 必须是本次运行的真实结论
#
# 断管不是运行结论：``… | head -0`` 与 ``1>&-`` 下不许冒 traceback，也不许把
# rc0/rc1 改成 120/141。这里两种形态各覆盖一遍。

class DeadStdout:
    """写就 EPIPE 的替身 stdout（没有 fileno：不碰进程的 fd 1）。"""

    encoding = "utf-8"

    def write(self, text: str) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.mark.parametrize(
    ("probe_rc", "expected"),
    [("0", 1), ("2", 0)],
    ids=["branch-present-rc1", "branch-deleted-rc0"],
)
def test_broken_pipe_keeps_true_rc(
    stub: Stub,
    monkeypatch: pytest.MonkeyPatch,
    probe_rc: str,
    expected: int,
) -> None:
    """合并路径上 stdout 断管：rc1 仍是 1、rc0 仍是 0，一条 traceback 都没有。"""

    stub.setenv("FAKE_LS_REMOTE_RC", probe_rc)
    monkeypatch.setattr(sys, "stdout", DeadStdout())

    rc = mg.main(args_for(stub))  # 不抛

    assert rc == expected
    assert len(stub.gh_calls()) == 4  # 预检 + 两条只读 api + 合并都照常发生
    assert merge_call(stub) in stub.gh_calls()


def test_broken_pipe_keeps_rc2_with_evidence(
    stub: Stub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """合并已发生 + 判不了 + stdout 断管：rc 仍 2（不是 1），stderr 一行仍在。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "128")
    monkeypatch.setattr(sys, "stdout", DeadStdout())

    rc = mg.main(args_for(stub))
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""  # 写不出来就是写不出来
    assert captured.err.startswith("错误: ")
    assert captured.err.count("\n") == 1
    assert "无法判定" in captured.err


def _spawn(argv: list[str], *, cwd: Path) -> subprocess.Popen[str]:
    """跑一个真子进程的 merge_guard（``python -m``），stdout/stderr 走管道。"""

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(mg.__file__).resolve().parents[1])
    return subprocess.Popen(
        [sys.executable, "-m", "team_board.merge_guard", *argv],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("等待前置条件超时（子进程没有走到预期位置）")


def test_closed_stdout_keeps_true_rc(
    stub: Stub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``1>&-``（真关掉 fd 1）：rc 仍是真实结论，stderr 照常可读。

    用 ``bash -c 'exec 1>&-; exec …'`` —— 这是 ``1>&-`` 的真实形态，不是
    ``>/dev/null``（那只是丢弃输出，fd 1 还开着，压根走不到这条分支）。
    """

    stub.setenv("PYTHONPATH", str(Path(mg.__file__).resolve().parents[1]))
    # ① 分支一直在远端 → --verify-only 轮询到超时 → 结论 1
    # ② 缺 --delete-branch → 结论 2（一个 gh / git 调用都不发）
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "0")
    stub.setenv("FAKE_LS_REMOTE_RC", "0")
    cases = [
        (args_for_verify(stub), 1),
        (args_for(stub, delete_branch=False), 2),
    ]
    for index, (argv, expected) in enumerate(cases):
        stub.setenv("FAKE_GIT_COUNT", str(tmp_path / f"git-closed-{index}.count"))
        script = "exec 1>&-; exec " + shlex.join(
            [sys.executable, "-m", "team_board.merge_guard", *argv]
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            cwd=str(stub.repo),
            env=os.environ.copy(),  # 每次重建：setenv 改的是 os.environ
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        captured = proc.stderr
        assert proc.returncode == expected, (argv, proc.returncode, captured)
        assert "Traceback" not in captured
        assert captured.count("\n") <= 1  # 至多一行 错误: …（rc1 档干脆没有）


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT", "SIGHUP"])
def test_signal_after_merge_keeps_evidence_rc2(
    stub: Stub, tmp_path: Path, signame: str
) -> None:
    """合并已经发生、正在轮询删除验证时被信号杀掉：证据必须留在 stdout、rc=2。

    复现路径（假 gh / 假 git，绝不真合并）：``gh pr merge`` 成功 → 探针卡住 →
    外部发信号。旧行为是 0 字节 stdout + rc=143/130，下一轮会读成「没合并、可以重跑」。
    """

    signums: dict[str, int | None] = {
        name: getattr(signal, name, None) for name in ("SIGTERM", "SIGINT", "SIGHUP")
    }
    signum = signums[signame]
    if signum is None:  # pragma: no cover - Windows
        pytest.skip(f"平台没有 {signame}")

    stub.setenv("FAKE_GIT_SLEEP", "10")  # 合并后的探针卡住（合并前的闸门不睡）
    stub.setenv("FAKE_LS_REMOTE_RC", "128")

    proc = _spawn(
        args_for(stub, "--timeout", "30", "--interval", "1"), cwd=stub.repo
    )
    try:
        # ls-remote 第 2 次 = 合并后的轮询探针已启动 → 「合并 | PASS」那行已经攒好
        # （不能按 git 总调用次数判：rebase 闸门的 fetch / rev-parse / merge-base 也在里头）
        _wait_until(lambda: len([c for c in stub.git_calls() if " ls-remote " in c]) >= 2)
        proc.send_signal(signum)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:  # pragma: no cover - 兜底，别留僵尸
            proc.kill()
            proc.communicate()

    assert proc.returncode == 2  # 不是 143 / 130 / 124 / 0
    assert f"合并 | PASS | {merge_call(stub)}" in out
    assert "分支存在 | PASS | " in out  # 合并前那一步也留着
    assert err.startswith(f"错误: 收到信号 {signame}（")
    assert err.count("\n") == 1  # 恰好一行
    assert "Traceback" not in out + err
    assert "合并是否已发生请人工核对" in err


def test_signal_before_merge_rc2_stdout_empty(stub: Stub) -> None:
    """还没走到合并就被杀：stdout 空、rc2、stderr 一行（没有证据要保，也不许落 rc1）。"""

    stub.setenv("FAKE_VIEW_SLEEP", "10")  # 预检的 gh pr view 卡住

    proc = _spawn(args_for(stub), cwd=stub.repo)
    try:
        _wait_until(lambda: any("gh pr view" in call for call in stub.gh_calls()))
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
            proc.communicate()

    assert proc.returncode == 2
    assert out == ""  # 合并没发生 → 没有证据可保
    assert err.startswith("错误: 收到信号 SIGTERM（")
    assert err.count("\n") == 1
    assert "gh pr merge" not in stub.gh_calls()  # 一个写操作都没发


def test_signals_restored_after_main_returns(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main()`` 返回后 handler 回到原样：不许把宿主进程的信号处理改掉。"""

    before = {signum: signal.getsignal(signum) for signum in mg._interrupt_signals()}

    rc, _out, _err = run(args_for(stub), capsys)

    assert rc == 0  # 默认 stub：分支已删 → 合并完成
    assert {signum: signal.getsignal(signum) for signum in before} == before


# =============== 认账四参数（--card / --by / --subject / --body-file，卡 t_bb6600d8）
#
# 缺口（pm 实跑）：``gh pr merge`` 不带 ``--subject`` / ``--body-file`` 时用 gh 默认文案，
# 合并提交**没有任何 trailer** → 经 merge_guard 合出来的提交必然过不了
# ``merge-audit --require``（缺 merged-by / merge-card / merge-pr）。本节四组用例分别锁住：
# ① argv 逐字转发；② 缺参数四连红（rc2 + stdout 空 + 零 gh 调用）；③ body 坏输入；
# ④ 认账闭环（假 gh 真造 merge commit + 删远端分支 → merge-audit --require rc0）。


def test_merge_argv_forwards_accounting_verbatim(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """① argv 逐字：三个语义词原位，``--subject`` / ``--body-file`` 追加在 sha 之后。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0
    assert err == ""
    assert stub.gh_calls() == [VIEW_CALL, *API_CALLS, merge_call(stub)]
    call = stub.gh_calls()[3]
    assert call.startswith(
        f"gh pr merge 9 -R {REPO_SLUG} --merge --delete-branch --match-head-commit {HEAD_OID} "
    )
    assert call.endswith(f"--subject {MERGE_SUBJECT} --body-file {stub.body_file}")
    assert f"合并 | PASS | {merge_call(stub)} | rc=0 | " in out


def _drop_flag(argv: list[str], flag: str) -> list[str]:
    """从 argv 里删掉 ``flag`` 与它后面那个值（用来造「缺某一项」的红样例）。"""

    kept: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token == flag:
            skip = True
            continue
        kept.append(token)
    return kept


@pytest.mark.parametrize("flag", ["--card", "--by", "--subject", "--body-file"])
def test_missing_accounting_flag_rc2_with_zero_gh_calls(
    stub: Stub, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    """② 红样例四连：缺任一项 → rc2、stdout **0 字节**、stderr 恰一行、假 gh 零调用。"""

    rc, out, err = run(_drop_flag(args_for(stub), flag), capsys)

    assert rc == 2
    assert out == ""  # 一个字节都不许有（0 字节 stdout）
    assert err.startswith(f"错误: 合并命令缺少 {flag}"), err
    assert err.count("\n") == 1  # stderr 严格一行
    # 零调用用调用日志断言，不靠肉眼：合并前的「分支存在」闸门也没跑
    assert stub.calls() == []
    assert not stub.log.exists() or stub.calls() == []


@pytest.mark.parametrize(
    ("label", "overrides", "hint"),
    [
        ("文件不存在", {"body_file": "missing"}, "不存在"),
        ("空文件", {"body": ""}, "空文件"),
        ("非 UTF-8", {"body_bytes": b"merged-by: pm/run200\n\xff\xfe\n"}, "UTF-8"),
        ("三字段缺一", {"body": f"merged-by: {MERGE_BY}\nmerge-card: {MERGE_CARD}\n"}, "缺字段"),
        (
            "merge-card 不一致",
            {"body": f"merged-by: {MERGE_BY}\nmerge-card: t_deadbeef\nmerge-pr: #9\n"},
            "字段与参数不一致",
        ),
        ("merge-pr 不一致", {"body": f"merged-by: {MERGE_BY}\nmerge-card: {MERGE_CARD}\nmerge-pr: #10\n"}, "#10"),
        ("纯空白 body", {"body": " \n"}, "缺字段"),
    ],
)
def test_bad_body_file_rc2_with_zero_gh_calls(
    stub: Stub,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    label: str,
    overrides: dict[str, object],
    hint: str,
) -> None:
    """③ body 坏输入：rc2 + 零 gh 调用（判定全在 ensure_branch_present 之前）。"""

    argv = args_for(stub)
    if "body_file" in overrides:
        # 「文件不存在」：换一个不存在的路径
        argv = _replace_flag_value(argv, "--body-file", str(tmp_path / "missing-body.txt"))
    if "body" in overrides:
        stub.set_body(str(overrides["body"]))
    if "body_bytes" in overrides:
        stub.body_file.write_bytes(overrides["body_bytes"])  # type: ignore[arg-type]

    rc, out, err = run(argv, capsys)

    assert rc == 2, label
    assert out == "", label
    assert err.startswith("错误: "), label
    assert err.count("\n") == 1, label
    assert hint in err, label
    assert stub.calls() == [], label


def _replace_flag_value(argv: list[str], flag: str, value: str) -> list[str]:
    """把 argv 里 ``flag`` 后面的值替换成 ``value``。"""

    out = list(argv)
    index = out.index(flag)
    out[index + 1] = value
    return out


# ---------------------- ②b. --body-file 的路径形态（P2-1：两套 cwd 解析同一个相对路径）
#
# 修之前：校验 ``read_body_file`` 按**进程 cwd** 解析，而 ``gh pr merge`` 的 cwd 是
# ``--repo``。同一个相对路径在两处指向不同文件：校验读 caller 的那份、gh 读 ``--repo``
# 下那份 —— 轻则 gh 报读不到、rc1 冒充「未达成」；重则 gh 静默合进另一份 body，合出
# 一条**没有任何 trailer** 的提交（R15-A 的红样例）。现在相对路径在 validate 阶段 rc2
# 拒掉，交给 gh 的路径与校验的路径恒为同一个绝对路径。


def test_relative_body_file_rc2_with_zero_calls(
    stub: Stub,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """cwd 里有合法 body、``--repo`` 指向别处且那里没有同名文件、``--body-file`` 给

    相对路径 → **rc2**（不是 rc1）、stdout 空、stderr 恰一行、零 gh 零 git 调用。
    """

    monkeypatch.chdir(tmp_path)  # body 就在 cwd 下（tmp_path/merge-body.txt）
    assert not (stub.repo / stub.body_file.name).exists()  # --repo 下没有同名文件
    argv = _replace_flag_value(args_for(stub), "--body-file", stub.body_file.name)

    rc, out, err = run(argv, capsys)

    assert rc == 2, err
    assert out == ""
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert "--body-file" in err and "绝对路径" in err
    assert stub.calls() == []


def test_absolute_body_file_unchanged_when_cwd_differs_from_repo(
    stub: Stub,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """反向保护：同一份文件给**绝对路径**、cwd 与 ``--repo`` 不同 → 行为不变（照旧合并 rc0）。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")  # 合并后探针报「没有这个 ref」
    monkeypatch.chdir(tmp_path)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0, err
    assert err == ""
    assert "合并 | PASS | " in out
    # 交给 gh 的就是校验时用的那个绝对路径原文
    assert merge_call(stub) in stub.gh_calls()


#: 探针卡号（合法形态的假号，不是真卡号）
PROBE_CARD = "t_deadbeef"
PROBE_BRANCH = f"wt/{PROBE_CARD}"
PROBE_PR = 20
#: 探针库写进 remote 配置的 URL（GitHub 形态；真实传输由 url.<裸库>.insteadOf 改写到本地）
PROBE_REMOTE_URL = "git@github.com:alsj213/team-board.git"

#: 假 gh：收到 ``pr merge`` 时**真的**按 ``--subject`` / ``--body-file`` 造一个 merge commit，
#: 再删远端分支（本地裸库当 origin）。--subject / --body-file 缺一个就退出 9。
MERGE_GH_STUB = '''#!{python}
"""假 gh（认账闭环用）：真的合出带 trailer 的合并提交，再删远端源分支。"""

import json
import os
import subprocess
import sys

GIT = os.environ["FAKE_REAL_GIT"]
WORK = os.environ["FAKE_WORK"]
BRANCH = os.environ["FAKE_PROBE_BRANCH"]
LOG = os.environ["FAKE_LOG"]
MARK = os.environ["FAKE_MERGED_MARK"]

argv = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write("gh " + " ".join(argv) + "\\n")


def git(*args):
    proc = subprocess.run([GIT, "-C", WORK, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(proc.returncode)
    return proc


if argv[:1] == ["api"]:
    endpoint = argv[1]
    sha = git("rev-parse", "origin/" + BRANCH).stdout.strip()
    if "/pulls/" in endpoint:
        print(json.dumps([{"body": "REVIEW-HEAD: " + sha + "\\nREVIEW-KIND: defect\\n"}]))
        raise SystemExit(0)
    if "/issues/" in endpoint:
        print(json.dumps([{"body": "REVIEW-HEAD: " + sha + "\\nREVIEW-KIND: regression\\n"}]))
        raise SystemExit(0)
    sys.stderr.write("fake gh: unexpected api endpoint " + endpoint + "\\n")
    raise SystemExit(9)

if argv[:2] == ["pr", "view"]:
    if os.path.exists(MARK):
        print(json.dumps({"state": "MERGED"}))
        raise SystemExit(0)
    sha = git("rev-parse", "origin/" + BRANCH).stdout.strip()
    print(json.dumps({"number": int(argv[2]), "state": "OPEN", "headRefName": BRANCH,
                      "baseRefName": "main", "headRefOid": sha}))
    raise SystemExit(0)

if argv[:2] == ["pr", "merge"]:
    if "--subject" not in argv or "--body-file" not in argv:
        sys.stderr.write("fake gh: pr merge 缺 --subject / --body-file\\n")
        raise SystemExit(9)
    subject = argv[argv.index("--subject") + 1]
    with open(argv[argv.index("--body-file") + 1], encoding="utf-8") as handle:
        body = handle.read()
    with open(MARK, "w", encoding="utf-8") as handle:
        handle.write("merged\\n")
    git("fetch", "-q", "origin")
    git("merge", "--no-ff", "--no-commit", "origin/" + BRANCH)
    message = os.path.join(WORK, ".fake-gh-message.txt")
    with open(message, "w", encoding="utf-8") as handle:
        handle.write(subject + "\\n\\n" + body)
    git("commit", "-q", "--no-verify", "-F", message)
    git("push", "-q", "origin", "--delete", BRANCH)
    print("fake gh: merged #" + argv[2])
    raise SystemExit(0)

sys.stderr.write("fake gh: unexpected args " + " ".join(argv) + "\\n")
raise SystemExit(9)
'''


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [REAL_GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc


def _probe_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """造一对「裸库当 origin + 工作库」：main 上一条基线提交，wt/t_deadbeef 上一条特性提交。"""

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run([REAL_GIT, "init", "-q", "-b", "main", str(work)], check=True)
    for key, value in (
        ("user.email", "probe@local"),
        ("user.name", "probe"),
        ("commit.gpgsign", "false"),
    ):
        _git("config", key, value, cwd=work)

    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", "base.txt", cwd=work)
    _git("commit", "-q", "-m", "base", cwd=work)
    # 配置里写 GitHub 形态的 URL（双问绑定闸门的仓库核对按配置回答「这是哪个仓库」），
    # 再用 url.<本地裸库>.insteadOf 把真实传输改写回本地裸库 —— 于是 fetch / ls-remote /
    # push 全打在本地，一次网络调用都没有。
    _git("remote", "add", "origin", PROBE_REMOTE_URL, cwd=work)
    _git("config", f"url.{origin}.insteadOf", PROBE_REMOTE_URL, cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)

    _git("checkout", "-q", "-b", PROBE_BRANCH, cwd=work)
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git("add", "feature.txt", cwd=work)
    _git("commit", "-q", "-m", f"feat({PROBE_CARD}): probe", cwd=work)
    _git("push", "-q", "origin", PROBE_BRANCH, cwd=work)
    _git("checkout", "-q", "main", cwd=work)

    baseline = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    return work, origin, baseline


def test_merge_guard_commit_passes_merge_audit_require(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """④ 认账闭环（裁决点名的那条）：经 merge_guard 合出的提交 ``merge-audit --require`` 判绿。

    假 gh **真的**按 ``--subject`` / ``--body-file`` 造一个 merge commit
    （``git merge --no-ff --no-commit`` + ``git commit -F``）再删远端分支；合并完成后跑
    ``merge-audit --repo <探针库> --baseline <合并前 HEAD> --card t_deadbeef --require``
    ⇒ rc0 且输出含 ``by=pm/run200``。
    """

    work, _origin, baseline = _probe_repo(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(MERGE_GH_STUB.replace("#!{python}", f"#!{sys.executable}"), encoding="utf-8")
    gh.chmod(0o755)

    log = tmp_path / "gh.log"
    mark = tmp_path / "merged.mark"
    body = tmp_path / "merge-body.txt"
    body.write_text(
        f"merged-by: {MERGE_BY}\nmerge-card: {PROBE_CARD}\nmerge-pr: #{PROBE_PR}\n",
        encoding="utf-8",
    )
    # 判据①（merged-by 的 profile == 该卡 tasks.assignee）要读看板库：造一份最小库，
    # 探针卡 assignee=repo，与 merged-by: repo/run200 的 profile 严格一致。
    # 同一份库也给 merge_guard 的认领闸门与证据状态机闸门用：探针卡正文认领 PR #PROBE_PR、
    # 四槽证据全绑探针分支当前的 head（= 假 gh 预检会报出来的那个 headRefOid）。
    board = tmp_path / "board.db"
    head = _git("rev-parse", f"origin/{PROBE_BRANCH}", cwd=work).stdout.strip()
    with sqlite3.connect(board) as conn:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, status TEXT, body TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks (id, assignee, status, body) VALUES (?, ?, ?, ?)",
            (PROBE_CARD, "repo", "running", f"PR-CLAIM: #{PROBE_PR}\n"),
        )
        conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, profile TEXT)")
        conn.execute(
            "CREATE TABLE task_events ("
            "id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, "
            "run_id INTEGER, created_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO task_runs (id, profile) VALUES (?, ?)",
            [(1, "pm"), (2, "qa"), (3, "paimeng")],
        )
        conn.executemany(
            "INSERT INTO task_events (task_id, kind, payload, run_id, created_at) "
            "VALUES (?, ?, ?, ?, 0)",
            [(PROBE_CARD, kind, payload, run_id) for kind, payload, run_id in default_evidence_rows(head)],
        )
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_REAL_GIT", REAL_GIT)
    monkeypatch.setenv("FAKE_WORK", str(work))
    monkeypatch.setenv("FAKE_PROBE_BRANCH", PROBE_BRANCH)
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_MERGED_MARK", str(mark))

    subject = f"Merge PR #{PROBE_PR}（{PROBE_CARD}）"
    rc, out, err = run(
        [
            "--pr",
            str(PROBE_PR),
            "--branch",
            PROBE_BRANCH,
            "--method",
            "merge",
            "--delete-branch",
            "--card",
            PROBE_CARD,
            "--by",
            MERGE_BY,
            "--subject",
            subject,
            "--body-file",
            str(body),
            "--repo",
            str(work),
            # 认领闸门读的库：探针库（探针卡认领 PR #PROBE_PR）
            "--db",
            str(board),
            "--timeout",
            "1",
            "--interval",
            "1",
        ],
        capsys,
    )

    assert rc == 0, (out, err)
    assert err == ""
    assert f"--subject {subject} --body-file {body}" in " ".join(
        line for line in log.read_text(encoding="utf-8").splitlines()
    )
    assert "结论：合并完成" in out

    # 真 git 看到的合并提交：2 个父提交 + 三行 trailer
    message = _git("log", "-1", "--format=%B", cwd=work).stdout
    assert f"merged-by: {MERGE_BY}" in message
    assert f"merge-card: {PROBE_CARD}" in message
    assert f"merge-pr: #{PROBE_PR}" in message

    env = {**os.environ, "PYTHONPATH": str(Path(mg.__file__).resolve().parents[1])}
    audit = subprocess.run(
        [
            sys.executable,
            "-m",
            "team_board.cli",
            "merge-audit",
            "--repo",
            str(work),
            "--baseline",
            baseline,
            "--card",
            PROBE_CARD,
            "--db",
            str(board),
            "--require",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert f"by={MERGE_BY}" in audit.stdout
    assert "结论：无问题" in audit.stdout


# ------------------------------- 16. 合并前 rebase 闸门：红样（base 不是 head 的祖先）
#
# 修之前：分支落后 main 也照样合 —— 只钉 head sha 不保证合进去的树包含 main 尖端。
# 现在预检之后、`gh pr merge` 之前判 `merge-base --is-ancestor base head`，不是祖先
# 就 rc2：stdout 0 字节（证据行还没开始攒）、stderr 恰一行、零 `gh pr merge`。

def test_rebase_gate_rejects_stale_branch_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.setenv("FAKE_MERGE_BASE_RC", "1")  # base 不是 head 的祖先（落后 / 分叉）

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2  # 与「源分支不存在」「PR 不是 OPEN」同档：合并不可能发生
    assert out == ""  # 一个字的证据都不该有
    assert err.startswith("错误: ")
    assert err.count("\n") == 1
    assert f"分支 {CARD_BRANCH} 落后 main" in err
    assert "git merge-base --is-ancestor rc=1" in err
    assert "合并前必须先 rebase 到 main" in err
    # P2-A（返工卡 t_0a16dc4d）：只说「先 rebase」不够 —— 拒绝文案必须带「谁来做 rebase」的
    # 短版，否则读者按最省事的做法（在合并卡里用 pm 身份 rebase）补完就被 commit-identity 判红；
    # 同时不许把「把 main 同步合并进卡分支」当替代方案（实测 merge-accountability 在 PR head 必红）
    assert "setup-bot-identity" in err
    assert "commit-identity" in err
    assert "merge-accountability" in err
    assert "git merge origin/main" not in err
    # gh 只调过那一次预检：一个 `gh pr merge` 都没发
    assert stub.gh_calls() == [VIEW_CALL, *API_CALLS]
    assert not any(call.startswith("gh pr merge") for call in stub.gh_calls())


def test_rebase_gate_fetch_failure_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``git fetch`` 抓不到远端提交 → rc2；后面两条命令一条都不跑。

    E4 起这次 fetch 由 :func:`mg.ensure_pr_refs` 提供、双问闸门先调：所以拒绝发生在
    **双问闸门**里（两条 ``gh api`` 都还没发），文案仍是同一句 rebase 口径的原话。
    """

    stub.setenv("FAKE_FETCH_RC", "128")
    stub.setenv("FAKE_FETCH_ERR", "fatal: could not read from remote repository")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert "取远端提交失败（git fetch rc=128）" in err
    assert "could not read from remote repository" in err
    assert "无法判定 head 是否已 rebase 到 main" in err
    # 取数失败就停：既没发 gh api（双问闸门在 fetch 之后就停），也没往下判祖先关系
    assert stub.gh_calls() == [VIEW_CALL]
    assert not [c for c in stub.git_calls() if " rev-parse " in c or " merge-base " in c]


def test_review_head_gate_head_pushed_after_preflight_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """预检到合并之间分支被推送（远端 head ≠ headRefOid）→ rc2「预检之后分支被推送」。

    E4 起这条交叉核对在**双问闸门**里（认领 → 双问 → rebase，双问先跑），文案沿用
    rebase 闸门那一份原话；判了不一致就不许再算指纹、也不许判祖先关系。
    """

    stub.setenv("FAKE_REV_PARSE_OUT", "0055aa66bb77cc88dd99ee00112233445566778899")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert "0055aa66bb77cc88dd99ee00112233445566778899" in err
    assert f"headRefOid {HEAD_OID}" in err
    assert "预检之后分支被推送" in err
    # 判了「不一致」就不许再往下判祖先关系，也不许拿新 ref 算指纹后放行
    assert not [c for c in stub.git_calls() if " merge-base " in c or " patch-id " in c]
    assert stub.gh_calls() == [VIEW_CALL]


def test_rebase_gate_undetermined_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``merge-base`` 退出码不是 0/1（128 = 对象缺失 / 通信错误）→ 判不了，同样拒绝。"""

    stub.setenv("FAKE_MERGE_BASE_RC", "128")
    stub.setenv("FAKE_MERGE_BASE_ERR", "fatal: bad object refs/merge-guard/9/base")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert "无法判定 main 是否为 head 的祖先（git merge-base rc=128）" in err
    assert "bad object refs/merge-guard/9/base" in err
    assert stub.gh_calls() == [VIEW_CALL, *API_CALLS]


@pytest.mark.parametrize(
    ("base", "hint"),
    [
        ("", "不能是空串"),
        ("ma in", "不能含空白字符"),
        ("refs/heads/main", "不能带 refs/ 前缀"),
    ],
    ids=["empty", "whitespace", "refs-prefix"],
)
def test_rebase_gate_rejects_malformed_base_ref_name(
    stub: Stub, capsys: pytest.CaptureFixture[str], base: str, hint: str
) -> None:
    """``gh pr view`` 报出畸形 ``baseRefName`` → rc2、零 git 调用（fetch 之前就拦）。"""

    stub.view(base=base)

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 2
    assert out == ""
    assert err.count("\n") == 1
    assert "baseRefName" in err  # 文案必须点名 baseRefName，不是 --branch
    assert hint in err
    assert "--branch" not in err
    # 闸门自身不发 git 调用：只有合并前那次存在性探针（它在 gh 预检之前）
    assert not [
        c
        for c in stub.git_calls()
        if " fetch " in c or " rev-parse " in c or " merge-base " in c
    ]
    assert len([c for c in stub.git_calls() if " ls-remote " in c]) == 1
    # 形态检查在 :func:`mg.ensure_pr_refs` 里、双问闸门先调：所以连 gh api 都没发
    assert stub.gh_calls() == [VIEW_CALL]


def test_rebase_gate_docs_name_the_rebaser_and_drop_zero_git_claim() -> None:
    """返工卡 ``t_0a16dc4d`` 的两条 P2：三处文档都要写「谁来做 rebase」+「同步合并提交必红」，
    且不许再声称畸形 ``baseRefName`` 时「一个 git 调用都不发」—— 闸门之前已经跑过一次
    ``ls-remote`` 存在性探针，那句是假声明（实测 git 调用条数 1）。

    判定权在文档原文，不在实现里：真正的 git 调用顺序由
    :func:`test_rebase_gate_rejects_malformed_base_ref_name` 用假脚本钉住，本用例只防文档回退。
    """

    root = Path(__file__).resolve().parents[2]
    places = {
        "README.md": (root / "README.md").read_text(encoding="utf-8"),
        "docs/merge-rebase-gate.md": (root / "docs" / "merge-rebase-gate.md").read_text(
            encoding="utf-8"
        ),
        "src/team_board/merge_guard.py 模块 docstring": mg.__doc__ or "",
    }

    for name, text in places.items():
        assert "setup-bot-identity" in text, name  # 谁来做：作者的 bot 身份 worktree
        assert "merge-accountability" in text, name  # 同步合并提交会在 PR head 上必红
        assert "一个 git 调用都不发" not in text, name  # 假声明不许留
        # 「把 main 同步合并进分支」曾被文档当成替代方案，实测 merge-accountability 必红 → 作废
        assert "或改用 git merge origin/main" not in text, name


def test_rebase_gate_git_subcommand_order_passing(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """通过路径的 git 子命令顺序（E4 起双问闸门先跑、共用一次 fetch）：

    存在性探针 → remote get-url（仓库核对）→ ①双问闸门的 fetch → rev-parse（交叉核对）
    → 内容指纹（merge-base --all / diff --numstat / diff -U0，sha256 内容行）→ ②rebase 闸门的
    fetch → rev-parse（交叉核对）→ merge-base --is-ancestor → rev-parse(base，只用于证据行）
    → 删除探针。
    """

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, _err = run(args_for(stub), capsys)

    assert rc == 0
    def git_sub(call: str) -> str:
        """取 git 子命令：跳过全局 ``-c key=value``（指纹那一路会带配置钉死）。"""
        parts = call.split()
        i = 3
        while i < len(parts):
            if parts[i] == "-c":
                i += 2
                continue
            if parts[i].startswith("-"):
                i += 1
                continue
            return parts[i]
        return ""

    subs = [git_sub(call) for call in stub.git_calls()]
    assert subs[:11] == [
        "ls-remote",
        "config",
        "fetch",       # 双问闸门：与 rebase 闸门共用的那一份取数
        "rev-parse",   # 双问闸门：远端 head 必须 == headRefOid
        "merge-base",  # 内容指纹：merge-base --all（真 git）
        "diff",        # 内容指纹：diff --numstat -U0（真 git）
        "diff",        # 内容指纹：diff -U0（sha256 内容行，无 patch-id 调用）
        "fetch",       # rebase 闸门：同一份取数再跑一次
        "rev-parse",   # rebase 闸门：远端 head 必须 == headRefOid
        "merge-base",  # rebase 闸门：merge-base --is-ancestor
        "rev-parse",   # rebase 闸门：读 base 的前 7 位拼证据行
    ]
    assert set(subs[11:]) == {"ls-remote"}  # 后面全是删除探针
    assert " --is-ancestor " in stub.git_calls()[9]  # 第 10 条才是 rebase 判定
    assert " --is-ancestor " not in stub.git_calls()[4]  # 第 5 条是指纹的 merge-base
    assert REMOTE_CALL.format(repo=stub.repo) in stub.git_calls()
    assert rebase_line(stub) in out.splitlines()
    assert review_line(stub) in out.splitlines()


def test_verify_only_never_emits_rebase_step(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--verify-only`` 一次 gh 都不调、也不宣称合并前状态：不许出现 ``rebase |``。"""

    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")  # verify-only 没有「合并前存在」闸门
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for_verify(stub), capsys)

    assert rc == 0, (out, err)
    assert "rebase |" not in out
    assert "head sha |" not in out
    assert stub.gh_calls() == []
    assert not [c for c in stub.git_calls() if " fetch " in c or " merge-base " in c]



# ============================================================================
# 16. 合并前的两道证据闸门（R17⑤ 认领唯一性 / R18④ 双问绑定 head sha）
#
# 顺序固定：预检（拿到 headRefOid）→ ① 认领 → ② 双问 → ③ rebase → gh pr merge。
# 五种拒绝态都要求：rc2、stdout **0 字节**、stderr 恰一行、**零 gh 写操作**；
# 文案必须一眼分清「无认领 / 重复认领 / 认领卡≠--card / 双问缺证 / 双问陈旧 / 双问缺类」。
# ============================================================================

#: 另一个合法卡号（用来造「重复认领」与「认领卡≠--card」）
OTHER_CARD = "t_aaaa0001"
#: 另一条 sha（用来造「旧 REVIEW-HEAD 标记」的 legacy 形态）
STALE_OID = "0f1e2d3c4b5a69788796a5b4c3d2e1f00a1b2c3d"
#: 另一份内容指纹（40 hex，合法形态但不是当前那份 → 用来造「双问陈旧」）
STALE_PID = "0f1e2d3c4b5a69788796a5b4c3d2e1f00a1b2c3d"


def merged(stub: Stub) -> bool:
    """假 gh 有没有收到过 ``pr merge``（= 有没有发出 gh 写操作）。"""

    return any(call.startswith("gh pr merge") for call in stub.gh_calls())


def assert_rejected(rc: int, out: str, err: str, *, text: str) -> None:
    """拒绝态的公共断言：rc2、stdout 0 字节、stderr 恰一行且包含 ``text``。"""

    assert rc == 2, (rc, out, err)
    assert out == ""  # 合并没发生 → 一个字节的证据都不该有
    assert err.startswith("错误: "), err
    assert err.count("\n") == 1, err  # stderr 严格一行
    assert text in err, err


# ---------------------------- 16a. 没有跳过开关（照抄卡面「无跳过开关」）
def test_no_skip_switches_for_gates(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """``--skip-claim`` / ``--skip-review-head`` 一律不存在：给了就 rc2、零外部调用。"""

    for flag in ("--skip-claim", "--skip-review-head", "--allow-unclaimed", "--skip-gates"):
        rc, out, err = run(args_for(stub, flag), capsys)
        assert rc == 2, flag
        assert out == "", flag
        assert err.startswith("错误: "), flag
    assert stub.calls() == []


# ---------------------------- 16b. 认领唯一性：三种拒绝态
def test_claim_gate_no_claim_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """0 张卡认领 → rc2「无未结卡认领该 PR」，且一个 gh 写操作都不发。"""

    stub.set_claims([])

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="无未结卡认领该 PR")
    assert "PR #9" in err
    # 认领闸门只读本地库：一次 gh api 都不发，更不会有 gh pr merge
    assert stub.api_calls() == []
    assert not merged(stub)
    assert stub.gh_calls() == [VIEW_CALL]  # 只跑了预检


def test_claim_gate_dupe_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """≥2 张卡认领同一 PR → rc2「重复认领」并把两张卡号都点出来。"""

    stub.set_claims(
        [
            (MERGE_CARD, "PR-CLAIM: #9\n"),
            (OTHER_CARD, "PR-CLAIM: #9\n"),
        ]
    )

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="重复认领")
    assert MERGE_CARD in err and OTHER_CARD in err
    assert stub.gh_calls() == [VIEW_CALL]
    assert not merged(stub)


def test_claim_gate_card_mismatch_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """恰好一张卡认领，但卡号 ≠ ``--card`` → rc2，文案点明两侧卡号。"""

    stub.set_claims([(OTHER_CARD, "PR-CLAIM: #9\n")])

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="≠ --card")
    assert OTHER_CARD in err and MERGE_CARD in err
    assert stub.gh_calls() == [VIEW_CALL]
    assert not merged(stub)


def test_claim_gate_ignores_done_cards_and_reference_lines(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """已结卡与 ``PR-REF:`` 行都不算认领：判定口径与 ``pr-claim-audit`` 同源。"""

    # 未结卡：认领 #9 + 引用 #8（引用行不参与唯一性判定）
    stub.set_claims([(MERGE_CARD, "PR-CLAIM: #9\nPR-REF: #8\n")])
    # 已结卡也认领 #9 —— 已结卡不参与判定，所以这里仍然只有一张未结卡认领
    conn = sqlite3.connect(str(stub.board_db))
    try:
        conn.execute(
            "INSERT INTO tasks (id, status, assignee, body) VALUES (?, ?, ?, ?)",
            ("t_done0001", "done", "engineer", "PR-CLAIM: #9\n"),
        )
        conn.commit()
    finally:
        conn.close()

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0, (out, err)
    assert claim_line(stub) in out.splitlines()


def test_claim_gate_db_unreadable_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """看板库读不到 → rc2 且 points 出原因：绝不当成「没有卡认领」。"""

    rc, out, err = run(
        args_for(stub, "--db", str(stub.board_db.with_name("nope.db"))), capsys
    )

    assert_rejected(rc, out, err, text="读看板库失败")
    assert stub.gh_calls() == [VIEW_CALL]
    assert not merged(stub)


# ---------------------------- 16c. 双问绑定：三种拒绝态（认领一律先通过）
def test_review_head_gate_missing_kind_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """当前内容指纹上只有 defect → rc2「双问缺类」并点出缺 regression。"""

    stub.set_review_bodies([patch_body(stub.pid, "defect")], [])

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="双问缺类")
    assert "regression" in err
    assert stub.pid[:12] in err  # 缺类结论行点的是内容指纹，不是 head sha
    assert stub.api_calls() == API_CALLS  # 两条只读 GET 都发了
    assert not merged(stub)


def test_review_head_gate_stale_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """标记绑在**别的内容**上（内容变了没重跑双问）→ rc2「双问陈旧」。"""

    stub.set_review_bodies(
        [patch_body(STALE_PID, "defect")], [patch_body(STALE_PID, "regression")]
    )

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="双问陈旧")
    assert HEAD_OID[:12] in err
    assert stub.pid[:12] in err  # 当前内容指纹也在文案里（便于对账）
    assert not merged(stub)


def test_review_head_gate_pure_rebase_passes(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """**纯 rebase 放行**：父提交换了、内容一字未改 → 双问证据按内容复用，仍 PASS。

    三步：① main 前进一次（新 base）；② 分支那份改动重放到新 base 上（= 真 rebase，
    提交 sha 变了）；③ 先断言指纹没变，再分别用 ``REVIEW-PATCH`` 与旧 ``REVIEW-HEAD``
    两种标记跑一遍合并路径 —— 都该走到 ``双问绑定 | PASS``。
    """

    pid_before = stub.pid
    old_head = stub.head_sha

    # ① main 前进一次：只在 main 上出现一个无关改动
    subprocess.run(
        [REAL_GIT, "-C", str(stub.repo), "checkout", "-q", "-B", "main-side", stub.base_sha],
        check=True,
    )
    new_base = _commit(stub.repo, {"main-only.txt": b"main\n"}, "main: unrelated change")
    # ② 分支改动重放到新 base 上（树差异不变，提交 sha 换了）
    rebased_head = _replay_on(stub.repo, new_base, {"feature.txt": b"feature\n"}, "feat: rebased")
    _set_ref(stub.repo, MG_BASE_REF, new_base)
    _set_ref(stub.repo, MG_HEAD_REF, rebased_head)
    stub.base_sha, stub.head_sha = new_base, rebased_head
    assert rebased_head != old_head  # 确实是 rebase 之后的新 sha
    assert stub.fingerprint() == pid_before  # 内容指纹一个字节都没变

    # ③a 新口径：REVIEW-PATCH 绑的就是这份内容
    stub.setenv("FAKE_LS_REMOTE_RC", "2")
    stub.set_review_bodies(
        [patch_body(pid_before, "defect")], [patch_body(pid_before, "regression")]
    )
    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0, (out, err)
    assert err == ""
    assert review_line(stub) in out.splitlines()
    assert f"内容指纹 {pid_before[:12]}" in out

    # ③b 旧口径：REVIEW-HEAD 绑的是 rebase 前的 sha —— 按内容复用（不是「双问陈旧」）
    stub.set_review_bodies(
        [mark_body(old_head, "defect")], [mark_body(old_head, "regression")]
    )
    stub.reset_ls_remote_count()  # 第二次跑完整合并：ls-remote 计次从 1 重新开始
    rc2, out2, err2 = run(args_for(stub), capsys)

    assert rc2 == 0, (out2, err2)
    assert err2 == ""
    assert review_line(stub) in out2.splitlines()


def test_review_head_gate_binary_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """分支含二进制文件 → rc2「双问不可复用」并点名路径（patch-id 对二进制内容盲）。"""

    stub.add_content({"assets/blob.bin": b"\x00\x01\x02\x03\xff"})

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="双问不可复用")
    assert "assets/blob.bin" in err  # 点名路径
    assert "双问陈旧" not in err and "双问缺证" not in err and "双问缺类" not in err
    assert stub.api_calls() == []  # 指纹不可复用：一条 gh api 都不发
    assert not merged(stub)


def test_review_head_gate_empty_diff_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """分支相对 base 无差异（空 diff）→ rc2「双问不可复用」，不许当 PASS。"""

    stub.point_head_at_base()

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="双问不可复用")
    assert "无差异" in err and "main" in err
    assert "双问陈旧" not in err and "双问缺证" not in err and "双问缺类" not in err
    assert stub.api_calls() == []
    assert not merged(stub)


def test_review_head_gate_fingerprint_failure_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """内容指纹算不出来（这里把 head ref 删掉 → merge-base 指不到对象）→ rc2「算不出…内容指纹」。

    绝不许被读成「双问缺证」（那是 rc1 语义 / 没做双问）。
    """

    subprocess.run(
        [REAL_GIT, "-C", str(stub.repo), "update-ref", "-d", MG_HEAD_REF], check=True
    )

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="算不出 PR #9 的内容指纹")
    assert "双问缺证" not in err and "双问陈旧" not in err
    assert stub.api_calls() == []
    assert not merged(stub)


def test_review_head_gate_no_mark_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """一条结构化标记都没有 → rc2「双问缺证」（不是「陈旧」「缺类」）。"""

    stub.set_review_bodies([], [])

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="双问缺证")
    assert "双问陈旧" not in err and "双问缺类" not in err
    assert not merged(stub)


def test_review_head_gate_gh_api_failure_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``gh api`` 非 0（取不到评审正文）→ rc2；不许读成「双问没做」。"""

    stub.setenv("FAKE_API_RC", "1")

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="取 PR #9 的评审正文失败")
    assert "拒绝合并" in err
    assert not merged(stub)


def test_gates_run_before_rebase_gate(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """顺序固定：认领在最前（拒了就不发 gh api），双问在 rebase 之前。"""

    stub.set_claims([])  # 认领就拒
    stub.setenv("FAKE_FETCH_RC", "128")  # rebase 闸门本来也会红

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="无未结卡认领该 PR")
    assert stub.api_calls() == []
    assert not [c for c in stub.git_calls() if " fetch " in c or " merge-base " in c]


# ---------------------------- 16d. rc0 正路：两道闸门放行后 argv 逐字不变
def test_gates_pass_keeps_merge_argv_verbatim(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """闸门放行后，``gh pr merge`` 的 argv 逐字等于 :func:`merge_call`。

    唯一允许的「新 flag」是仓库钉死 ``-R <slug>``（P2-2）—— 除它之外一个都不许多。
    """

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0, (out, err)
    assert err == ""
    merge_calls = [c for c in stub.gh_calls() if c.startswith("gh pr merge")]
    assert merge_calls == [
        f"gh pr merge 9 -R {REPO_SLUG} --merge --delete-branch --match-head-commit {HEAD_OID}"
        f" --subject {MERGE_SUBJECT} --body-file {stub.body_file}"
    ]
    # 三道闸门的证据行都在，顺序 = 认领 → 证据状态机 → 双问 → rebase
    lines = out.splitlines()
    assert lines[3] == claim_line(stub)
    assert lines[4] == evidence_line(stub)
    assert lines[5] == review_line(stub)
    assert lines[6] == rebase_line(stub)


def test_verify_only_never_runs_gates(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """``--verify-only`` 不跑这两道闸门：不读库（没有认领也照样 rc0）、不发 gh api。"""

    stub.set_claims([])  # 未结卡里没有一张认领 #9
    stub.set_review_bodies([], [])
    stub.setenv("FAKE_LS_REMOTE_FIRST_RC", "")
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for_verify(stub), capsys)

    assert rc == 0, (out, err)
    assert "认领唯一性 |" not in out
    assert "双问绑定 |" not in out
    assert stub.api_calls() == []
    assert not merged(stub)


# --------------------- 16e. 双问绑定闸门的仓库核对（--repo 的 remote 必须指向同一仓库）
#
# ``reviewhead`` 的 ``gh api`` 路径前缀写死 ``REPO = alsj213/team-board``，不看 ``--repo``。
# 跨仓时它会去读**另一个仓库**同号 PR 的 Review —— 判的是别人的证据。所以闸门先用
# ``git config --get remote.<name>.url`` 解析 ``owner/name``，与 ``REPO`` 不一致就拒绝。


def test_review_head_gate_other_repo_rc2(stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    """``--repo`` 的 remote 指向别的仓库 → rc2，且一条 ``gh api`` 都不发。"""

    stub.setenv("FAKE_REMOTE_URL", "git@github.com:someone/team-board.git")

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="≠ 双问判定仓库")
    assert "someone/team-board" in err and mg.reviewhead.REPO in err
    assert stub.api_calls() == []  # 没拿别人的仓评审判定
    assert not merged(stub)


def test_review_head_gate_non_github_remote_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """remote 不是 GitHub 形态（本地裸库 / 别的 host）→ rc2「判不了」，不是放行。"""

    stub.setenv("FAKE_REMOTE_URL", "/tmp/probe-origin.git")

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="不是 GitHub 形态")
    assert mg.reviewhead.REPO in err
    assert stub.api_calls() == []
    assert not merged(stub)


def test_review_head_gate_remote_config_unreadable_rc2(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``remote.<name>.url`` 读不到（rc≠0）→ rc2 并点出 ``--repo`` / remote 名。"""

    stub.setenv("FAKE_REMOTE_URL", "")
    stub.setenv("FAKE_REMOTE_RC", "1")

    rc, out, err = run(args_for(stub), capsys)

    assert_rejected(rc, out, err, text="读不到 --repo")
    assert "remote.origin.url" in err
    assert stub.api_calls() == []
    assert not merged(stub)


def test_review_head_gate_accepts_https_remote(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """``origin-https`` 那种 https 形态同样认（R10：两种通道都留着）。"""

    stub.setenv("FAKE_REMOTE_URL", "https://github.com/alsj213/team-board.git")
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0, (out, err)
    assert review_line(stub) in out.splitlines()


# ---------------- 16h. P2-1：remote URL 带凭据时，报错正文不许回显凭据
#
# PR #46 双问「缺陷视角」实测：临时 repo 把 origin 配成
# ``https://x-access-token:<TOKEN>@github.com/alsj213/team-board.git`` → rc2 的报错正文
# 把整条 URL（含 token）打进 stderr。stderr 会进卡片、CI 日志、终端 scrollback，等于
# 把凭据抄了一遍。判据：仍 rc2（这种形态本工具不认，见下面的注释）、stderr 恰一行、
# stdout 0 字节、正文不含 token 子串。

TOKEN = "ghp_SECRET123"
CREDENTIAL_REMOTE = f"https://x-access-token:{TOKEN}@github.com/alsj213/team-board.git"


def test_remote_url_with_credentials_never_echoes_token(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """A：带 userinfo 的 remote URL → 仍 rc2，但正文里一个 token 字符都不许出现。"""

    stub.setenv("FAKE_REMOTE_URL", CREDENTIAL_REMOTE)

    rc, out, err = run(args_for(stub), capsys)

    # 形态判定没放宽：带凭据的 remote URL 仍按「不是 GitHub 形态」拒绝（rc2）
    assert_rejected(rc, out, err, text="不是 GitHub 形态")
    assert TOKEN not in err, err  # 核心判据：不回显凭据
    assert "x-access-token" not in err, err
    assert "***" in err  # 脱敏后的形态还在（人能看出「这里本来有 userinfo」）
    assert mg.reviewhead.REPO in err
    assert stub.api_calls() == []  # 没拿任何评审当证据
    assert not merged(stub)


# ---------------- 16i. P2-2：闸门核对的仓库 == gh 实际操作的仓库
#
# PR #46 双问「缺陷视角」实测：非 git 目录里 ``GH_REPO=alsj213/team-board gh pr view 46
# --json state`` → rc0，证明 gh 的目标仓由 cwd / ``GH_REPO`` 决定。本工具的仓库核对读的是
# ``git config --get remote.<name>.url``，两者能各指一个仓库 → 「核对的是 A、合并的是 B」。
# 修法：合并模式每一条 gh 调用都显式钉 ``-R <slug>``（``-R`` 优先级高于 ``GH_REPO``）。


def test_merge_mode_pins_every_gh_call_to_gate_checked_repo(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """B：``GH_REPO`` 指别仓时，预检 / 合并仍钉 ``-R <核对过的仓>``（rc0）。"""

    stub.setenv("GH_REPO", "someone/else")
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0, (out, err)
    assert stub.gh_calls() == [VIEW_CALL, *API_CALLS, merge_call(stub)]
    # 逐条核对：没有一条 gh 调用靠 cwd / GH_REPO 选仓
    for call in stub.gh_calls():
        if call.startswith("gh api "):
            assert f"repos/{REPO_SLUG}/" in call, call  # api 路径自带仓库
        else:
            assert f" -R {REPO_SLUG} " in call, call  # view / merge 显式钉 -R


def test_claim_gate_db_failure_names_db_flag(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """库读不到时文案点出 ``--db`` 参数名（``merge-record`` 打印的命令不带 --db，走默认值）。"""

    missing = stub.board_db.with_name("nope.db")

    rc, out, err = run(args_for(stub, "--db", str(missing)), capsys)

    assert_rejected(rc, out, err, text="读看板库失败")
    assert f"--db {missing}" in err
    assert "缺省" in err and mg.DEFAULT_DB in err
    assert stub.gh_calls() == [VIEW_CALL]
    assert not merged(stub)


# --------------------- 16f. 证据行顺序：实跑 = 常量 = README / docs 样例
#
# PR #45 第二视角回归项 3/4：证据行顺序加了认领唯一性、双问绑定两道，README 与
# docs/merge-rebase-gate.md 的「步骤行顺序固定」声明和样例都没同步，而且没有测试
# 解析这段（把文档改回旧样板红不了）。这里把「实跑顺序」和「文档写的东西」绑在一起。

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 合并路径证据行的执行顺序（唯一来源 = merge_guard 的 STEP_* 常量）
STEP_ORDER = (
    mg.STEP_BRANCH_PRESENT,
    mg.STEP_PREFLIGHT,
    mg.STEP_HEAD_SHA,
    mg.STEP_CLAIM,
    mg.STEP_EVIDENCE,
    mg.STEP_REVIEW_HEAD,
    mg.STEP_REBASE,
    mg.STEP_MERGE,
    mg.STEP_DELETE,
)

#: 文档里「步骤行顺序固定：…」声明的出现位置（两份文档各一处）
DOC_FILES = ("README.md", "docs/merge-rebase-gate.md")


def evidence_labels(lines: list[str]) -> list[str]:
    """只留步骤行（``<标签> | …``），按出现顺序返回标签。"""

    labels = []
    for line in lines:
        head = line.split(" | ", 1)[0]
        if " | " in line and head in STEP_ORDER:
            labels.append(head)
    return labels


def declaration_labels(text: str) -> list[str]:
    """抓文档里「步骤行顺序固定：」那一行的反引号标签串。"""

    for line in text.splitlines():
        if "步骤行顺序固定" in line:
            return re.findall(r"`([^`]+)`", line)
    raise AssertionError("文档里找不到「步骤行顺序固定」声明行")


def sample_block_labels(text: str, marker: str) -> list[str]:
    """抓 ``marker`` 之后第一个围栏块里的步骤行标签。"""

    index = text.find(marker)
    assert index >= 0, f"文档里找不到取样标记：{marker}"
    rest = text[index:]
    start = rest.index("```")
    end = rest.index("```", start + 3)
    block = rest[start + 3 : end]
    return evidence_labels(block.splitlines())


def test_evidence_step_order_matches_constants(
    stub: Stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """走通的合并路径打 8 行步骤证据，顺序 = 常量顺序（实跑，不是照抄文档）。"""

    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc, out, err = run(args_for(stub), capsys)

    assert rc == 0, (out, err)
    assert evidence_labels(out.splitlines()) == list(STEP_ORDER)


@pytest.mark.parametrize("name", DOC_FILES)
def test_docs_declare_same_step_order(name: str) -> None:
    """两份文档的「步骤行顺序固定」声明必须与实跑的常量顺序逐字一致。"""

    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    assert declaration_labels(text) == list(STEP_ORDER)


@pytest.mark.parametrize(
    ("name", "marker"),
    [
        ("README.md", "跑出来的端到端样例（"),
        ("docs/merge-rebase-gate.md", "现行 9 行样例（2026-10-31"),
    ],
)
def test_docs_sample_block_matches_step_order(name: str, marker: str) -> None:
    """文档样例块的步骤行顺序必须与实跑一致（改回旧样板 → 本用例红）。"""

    text = (REPO_ROOT / name).read_text(encoding="utf-8")

    assert sample_block_labels(text, marker) == list(STEP_ORDER)


# --------------- 16g. merge-record 打印出来的合并命令必须真能走到认领闸门
#
# PR #45 第二视角回归项 7：把 ``DEFAULT_DB`` 改成不存在的路径后全量测试仍然 1247 passed
# —— 说明没有任何用例从「打印出来的命令」真正走到认领闸门（既有的
# ``test_printed_command_is_merge_guard_and_runnable`` 在更早的「源分支存在」闸门就
# 退出，认领闸门一次都没跑）。打印的命令**不带** ``--db``，走的是 ``DEFAULT_DB``，
# 所以这两条用例把 ``DEFAULT_DB`` 改成 fixture 库 / 不存在的路径来分别证明
# 「走到并放行」与「走到并拒绝」。


def printed_merge_argv(out: str) -> list[str]:
    """从 ``merge-record`` 的输出里取出打印命令，``shlex.split`` 成 merge_guard 的 argv。"""

    marker = "python3 -m team_board.merge_guard"
    for line in out.splitlines():
        if marker in line:
            return shlex.split(line.split(marker, 1)[1])
    raise AssertionError(f"输出里没有打印命令：{out!r}")


def merge_record_argv(stub: Stub, body_out: Path) -> list[str]:
    """``merge-record`` 的生成模式 argv（与真实调用同形，参数取自 fixture 常量）。"""

    return [
        "merge-record",
        "--pr",
        "9",
        "--branch",
        CARD_BRANCH,
        "--card",
        MERGE_CARD,
        "--by",
        MERGE_BY,
        "--subject",
        MERGE_SUBJECT,
        "--body-out",
        str(body_out),
        "--repo",
        str(stub.repo),
    ]


def test_printed_command_reaches_claim_gate_and_passes(
    stub: Stub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """照抄 ``merge-record`` 打印的命令 → 认领闸门放行 → 一路合并到 rc0。

    打印的命令不带 ``--db``；这里把它指向的 ``DEFAULT_DB`` 换成 fixture 库（恰好一张未结
    卡认领 PR #9 且卡号 = ``--card``），其余一切照原样（假 gh / 假 git，不联网）。
    """

    rc = board_cli.main(merge_record_argv(stub, tmp_path / "recv-body.txt"))
    printed = capsys.readouterr()
    assert rc == 0, (printed.out, printed.err)
    argv = printed_merge_argv(printed.out)
    assert "--db" not in argv  # 打印的命令本来就不带 --db（走默认值）

    monkeypatch.setattr(mg, "DEFAULT_DB", str(stub.board_db))
    stub.setenv("FAKE_LS_REMOTE_RC", "2")

    rc = mg.main(argv)
    out, err = capsys.readouterr()

    assert rc == 0, (out, err)
    lines = out.splitlines()
    # 认领闸门这一行真的出现了（不是被更早的闸门截住）
    assert lines[3] == claim_line(stub)
    assert evidence_labels(lines) == list(STEP_ORDER)


def test_printed_command_reaches_claim_gate_when_default_db_missing(
    stub: Stub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``DEFAULT_DB`` 指向不存在的库 → rc2、stdio 干净、文案点出 ``--db``。

    这是「打印的命令真的走到了认领闸门」的对照组：如果哪天闸门被挪到更早的位置，这条
    用例会因为错误文案变成「源分支存在」那一档而红。
    """

    rc = board_cli.main(merge_record_argv(stub, tmp_path / "recv-body.txt"))
    printed = capsys.readouterr()
    assert rc == 0, (printed.out, printed.err)
    argv = printed_merge_argv(printed.out)

    missing = tmp_path / "no-such-board.db"
    monkeypatch.setattr(mg, "DEFAULT_DB", str(missing))

    rc = mg.main(argv)
    out, err = capsys.readouterr()

    assert rc == 2, (out, err)
    assert out == ""  # 合并没发生 → 一个字的证据都不该有
    assert err.count("\n") == 1  # stderr 恰一行
    assert err.startswith("错误: 读看板库失败（--db ")
    assert str(missing) in err
    assert "不在 origin 远端" not in err  # 不是更早的「源分支存在」闸门
    assert not merged(stub)
