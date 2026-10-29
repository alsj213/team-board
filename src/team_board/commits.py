"""提交身份稽核（判据②③）：author 白名单 + committer 是本 bot。

只依赖标准库与 ``git`` 可执行文件，不引入任何新依赖；git 调用层在本模块自写
一份（``_git``），**不 import ``team_board.branches``** —— 两个子命令各自封装。

身份表 ``<repo>/.team-board/bots.tsv``
-------------------------------------
``profile<TAB>name<TAB>email`` 每行一个 bot，``#`` 开头的注释行与空行忽略。
它是 ``commit-audit`` / CI job ``commit-identity`` / ``scripts/setup-bot-identity.sh``
的**唯一身份源**。注意：GitHub 页面恒显示 ``alsj213``（单账号），这张表只解决
「提交元数据里能区分」，不解决页面显示。

判定口径
--------
区间 = ``origin/<main_branch>..<rev>``（``origin/<main_branch>`` 不存在则退回
``refs/heads/<main_branch>``），只看**非合并提交**（``git log --no-merges``）。

每个提交按 author / committer 的**邮箱 localpart**（@ 之前那段）判：

1. 判据② ``author 不在白名单``：``email_localpart(author_email)`` 必须落在
   ``{卡 assignee, engineer, qa}`` 里（assignee 从看板库 ``tasks.assignee`` 读，
   只读 ``mode=ro``）。
2. 判据③ ``committer 不是本 bot``：``email_localpart(committer_email)`` 必须在
   ``bots.tsv`` 的 profile 集合里；
   ``committer 与 author 不一致``：两者 localpart 必须相同。两条可同时出现。
3. ``身份表里没有该身份``：author 的 localpart 不在 ``bots.tsv`` 里 —— 这条提交的
   作者没有登记过的 bot 身份（因此判据③ 也永远过不了），单独点名，避免只留下一句
   ``committer 不是本 bot`` 让人误解成「作者是对的」。

为什么判定权在 ``commit-audit`` + CI 而不在钩子：``--no-verify``、message 里已有
``Co-authored-by``、hooks 被重装，三种都能让钩子失效；钩子（``.githooks/pre-commit``）
只做早警。

空区间 → ``commits=()``、``problems=()`` → rc0，输出一行 ``区间内无提交``。
"""

from __future__ import annotations

import os
import re
import sqlite3

# bandit B404 假阳性：只用于调用固定的 git 可执行文件（见 _git），
# 调用形式为 argv 列表、shell=False，不拼 shell 字符串
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

from .model import DEFAULT_DB

#: 卡号：``t_`` + 8 位小写十六进制
CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")

#: 判据② 白名单里除「该卡 assignee」之外恒允许的两个执行者
DEFAULT_AUTHOR_PROFILES = ("engineer", "qa")

#: git log 输出格式：记录分隔 0x1e，字段分隔 0x1f
#: （sha / subject / author name / author email / committer name / committer email）
_LOG_FORMAT = "%x1e%H%x1f%s%x1f%an%x1f%ae%x1f%cn%x1f%ce"
_LOG_FIELDS = 6

#: 身份表默认位置（相对被稽核仓库根）
BOTS_FILE = Path(".team-board") / "bots.tsv"

KIND_AUTHOR_NOT_ALLOWED = "author 不在白名单"
KIND_COMMITTER_NOT_BOT = "committer 不是本 bot"
KIND_COMMITTER_AUTHOR_MISMATCH = "committer 与 author 不一致"
KIND_IDENTITY_UNKNOWN = "身份表里没有该身份"


class CommitAuditError(Exception):
    """提交身份稽核的参数/环境错误（CLI 转 rc2）。"""


@dataclass(frozen=True)
class Bot:
    """身份表里的一行：``profile`` 唯一，``name`` / ``email`` 是提交元数据。"""

    profile: str
    name: str
    email: str


@dataclass(frozen=True)
class CommitInfo:
    """区间内的一条提交（git 给什么就是什么，不改写）。"""

    sha: str
    short: str
    subject: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str


@dataclass(frozen=True)
class CommitProblem:
    """一处问题：``kind`` 取本模块的四个 KIND_* 常量之一。"""

    kind: str
    sha: str
    detail: str


@dataclass(frozen=True)
class CommitReport:
    """一轮稽核的结果。``ok`` 只由 ``problems`` 决定，空区间也算 ok。"""

    repo: Path
    card: str
    main_branch: str
    rev: str
    commits: tuple[CommitInfo, ...]
    problems: tuple[CommitProblem, ...]
    assignee: str

    @property
    def ok(self) -> bool:
        return not self.problems


def email_localpart(email: str) -> str:
    """取邮箱 ``@`` 之前那段（``engineer@team-board.local`` → ``engineer``）。

    不是合法邮箱（没有 ``@``、空串）时返回去掉首尾空白后的原值 —— 稽核要的是
    「这个字符串是不是某个 bot 的 localpart」，不是邮箱语法校验；拿不到 localpart
    就自然匹配不上任何身份，由调用方报问题。
    """

    text = email.strip()
    head, sep, _ = text.rpartition("@")
    return head.strip() if sep else text


def load_bots(path: str | Path) -> tuple[Bot, ...]:
    """读身份表。

    缺文件 / 不是 UTF-8 / 某行不是三列制表符分隔 / profile 或邮箱为空 /
    profile 重复 → ``CommitAuditError``（**不静默跳过**：身份表是判据③ 的唯一源，
    读残了就等于豁免所有提交）。
    """

    raw_path = os.fspath(path)
    if not raw_path.strip():
        raise CommitAuditError("身份表路径为空，无法读取 bot 身份")
    file_path = Path(raw_path)
    if not file_path.exists():
        raise CommitAuditError(f"身份表不存在：{raw_path}（格式为 profile<TAB>name<TAB>email）")
    if not file_path.is_file():
        raise CommitAuditError(f"身份表不是普通文件：{raw_path}")
    try:
        text = file_path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CommitAuditError(
            f"身份表不是 UTF-8 文本，无法解析：{raw_path}（第 {exc.start} 字节起解码失败）"
        ) from exc
    except OSError as exc:
        raise CommitAuditError(f"身份表读不了：{raw_path}: {exc}") from exc

    bots: list[Bot] = []
    seen: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise CommitAuditError(
                f"身份表第 {lineno} 行格式非法：需要 3 列制表符分隔"
                f"（profile<TAB>name<TAB>email），实际 {len(fields)} 列：{line!r}"
            )
        profile, name, email = (field.strip() for field in fields)
        if not profile or not name or not email:
            raise CommitAuditError(f"身份表第 {lineno} 行有空字段：{line!r}")
        if profile in seen:
            raise CommitAuditError(
                f"身份表第 {lineno} 行 profile 重复：{profile!r}"
                f"（第 {seen[profile]} 行已定义）"
            )
        seen[profile] = lineno
        bots.append(Bot(profile=profile, name=name, email=email))
    if not bots:
        raise CommitAuditError(f"身份表没有任何有效行（只有注释/空行）：{raw_path}")
    return tuple(bots)


def _git(repo: str | os.PathLike[str], *args: str) -> subprocess.CompletedProcess[str]:
    """在 ``repo`` 里跑 git，不抛 ``CalledProcessError``，退出码交给调用方判。

    两类环境级错误一律 ``CommitAuditError``（CLI 转 rc2），不许冒 traceback：

    - ``PATH`` 里没有 ``git``（``FileNotFoundError``）或其它 ``OSError``；
    - git 输出**不是 UTF-8**：``i18n.logOutputEncoding=GBK`` 下 ``git log`` 会把
      提交标题转码输出，``subprocess`` 的严格解码于是抛 ``UnicodeDecodeError``
      （``ValueError`` 系，不是 ``OSError``，会直接逃出去）。rc1 是「发现问题」的
      专用码，崩溃必须与它分开 —— 解码失败说明整轮稽核没法进行，属环境错误。

    解码固定 UTF-8（不随 ``locale`` 漂移），同一仓库在任何机器上得到同样的 rc。
    """

    try:
        # bandit B603/B607 假阳性：可执行文件名写死为 "git"（走 PATH 以跨平台一致）；
        # 参数以 argv 列表传入、shell=False（默认），仓库路径/rev 只作为独立 argv
        # 元素，不经 shell 解释，无注入面
        return subprocess.run(  # nosec B603 B607
            ["git", "-C", os.fspath(repo), *args],
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommitAuditError("找不到 git 可执行文件（PATH 里没有 git），无法做提交身份稽核") from exc
    except UnicodeDecodeError as exc:
        raise CommitAuditError(
            f"git 输出不是 UTF-8，无法解析（第 {exc.start} 字节起解码失败；"
            "常见原因：仓库配置 i18n.logOutputEncoding=GBK 或 "
            "core.quotePath=false 且提交信息含非 UTF-8 字节；"
            "可执行 git config --unset i18n.logOutputEncoding 后重试）"
        ) from exc
    except OSError as exc:
        raise CommitAuditError(f"无法执行 git: {exc}") from exc


def _git_ok(repo: str | os.PathLike[str], *args: str, what: str) -> str:
    """跑 git，非 0 退出即 ``CommitAuditError``（环境级错误）。"""

    proc = _git(repo, *args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {proc.returncode}"
        raise CommitAuditError(f"{what}: {tail}")
    return proc.stdout


def _resolve_repo(repo: str | Path) -> Path:
    """校验仓库路径并解析成工作树根（空串 / 不存在 / 不是目录 / 不是工作树 → 报错）。"""

    if not isinstance(repo, (str, Path)):
        raise CommitAuditError(f"仓库路径非法：{repo!r}（应为 str 或 Path）")
    raw = os.fspath(repo)
    if not raw.strip():
        raise CommitAuditError("仓库路径为空，无法稽核")
    path = Path(raw)
    if not path.exists():
        raise CommitAuditError(f"仓库路径不存在：{raw}")
    if not path.is_dir():
        raise CommitAuditError(f"仓库路径不是目录：{raw}")
    toplevel = _git_ok(path, "rev-parse", "--show-toplevel", what=f"不是 git 工作树：{raw}")
    return Path(toplevel.strip())


def _resolve_commit(repo: str | Path, value: str, what: str) -> str:
    """把 ``value`` 解析成 40 位提交 sha；解析不了（含空串）即 ``CommitAuditError``。"""

    if not isinstance(value, str):
        raise CommitAuditError(f"{what}非法：{value!r}（应为 str）")
    if not value.strip():
        raise CommitAuditError(f"{what}为空，无法解析为提交")
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}")
    if proc.returncode != 0:
        raise CommitAuditError(f"{what}无法解析为提交：{value!r}")
    return proc.stdout.strip()


def _resolve_base(repo: str | Path, main_branch: str) -> tuple[str, str]:
    """取区间起点：优先 ``origin/<main_branch>``，不存在退回 ``refs/heads/<main_branch>``。

    返回 ``(sha, 人类可读 ref 名)``。两者都不存在 → ``CommitAuditError``（不静默
    把整段历史当区间）。
    """

    if not isinstance(main_branch, str):
        raise CommitAuditError(f"主分支名非法：{main_branch!r}（应为 str）")
    if not main_branch.strip():
        raise CommitAuditError("主分支名为空，无法确定区间起点")
    candidates = (
        (f"refs/remotes/origin/{main_branch}", f"origin/{main_branch}"),
        (f"refs/heads/{main_branch}", f"refs/heads/{main_branch}"),
    )
    for ref, label in candidates:
        proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip(), label
    raise CommitAuditError(
        f"找不到主分支 {main_branch!r}：origin/{main_branch} 与 refs/heads/{main_branch} 都不存在"
    )


def _parse_log(text: str) -> tuple[CommitInfo, ...]:
    """把 ``_LOG_FORMAT`` 的输出解析成 ``CommitInfo``（字段数不对即环境错误）。"""

    commits: list[CommitInfo] = []
    for chunk in text.split("\x1e"):
        payload = chunk.strip("\n")
        if not payload:
            continue
        fields = payload.split("\x1f")
        if len(fields) != _LOG_FIELDS:
            raise CommitAuditError(
                f"git log 输出格式不符（期望 {_LOG_FIELDS} 个 \x1f 分隔字段，"
                f"实际 {len(fields)} 个）：{payload!r}"
            )
        sha, subject, author_name, author_email, committer_name, committer_email = fields
        commits.append(
            CommitInfo(
                sha=sha,
                short=sha[:7],
                subject=subject,
                author_name=author_name,
                author_email=author_email,
                committer_name=committer_name,
                committer_email=committer_email,
            )
        )
    return tuple(commits)


def card_assignee(db_path: str | Path, card: str) -> str:
    """从看板库**只读**取该卡 assignee（``mode=ro``，不改库）。

    库不存在 / 不是 SQLite / 没有 ``tasks`` 表 / 卡不存在 / assignee 为空
    → ``CommitAuditError``。判据② 的白名单依赖它，缺了就等于静默放宽，所以不放过。
    """

    raw_path = os.fspath(db_path)
    if not raw_path.strip():
        raise CommitAuditError("看板库路径为空，无法做判据②（author 白名单需要该卡 assignee）")
    path = Path(raw_path)
    if not path.exists():
        raise CommitAuditError(
            "看板库不可读，无法做判据②（author 白名单需要该卡 assignee）："
            f"数据源不存在: {raw_path}"
        )
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError) as exc:
        # sqlite3.Error 之外还会抛 ValueError（路径转不成 URI）。两种都属「库不可用」，
        # 一律 rc2 —— 判据② 缺 assignee 就等于静默放宽白名单。
        raise CommitAuditError(
            f"看板库不可读，无法做判据②（author 白名单需要该卡 assignee）：{exc}"
        ) from exc
    try:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (card,)).fetchone()
    except sqlite3.Error as exc:
        raise CommitAuditError(
            f"看板库 {raw_path} 读不了 tasks.assignee，无法做判据②：{exc}"
        ) from exc
    finally:
        conn.close()
    if row is None:
        raise CommitAuditError(
            f"看板库 {raw_path} 里没有卡 {card}（判据② 需要该卡 assignee，不静默跳过）"
        )
    assignee = row[0]
    if not isinstance(assignee, str) or not assignee.strip():
        raise CommitAuditError(f"卡 {card} 的 assignee 为空，无法做判据②（author 白名单）")
    return assignee.strip()


def _allowed_authors(assignee: str) -> tuple[str, ...]:
    """判据② 允许的 author localpart 集合（顺序稳定、去重）。"""

    ordered: list[str] = [assignee]
    for profile in DEFAULT_AUTHOR_PROFILES:
        if profile not in ordered:
            ordered.append(profile)
    return tuple(ordered)


def _check_commit(
    commit: CommitInfo,
    *,
    assignee: str,
    profiles: frozenset[str],
) -> tuple[CommitProblem, ...]:
    """单个提交的判据②③（纯函数，不碰 git / 文件）。"""

    allowed = _allowed_authors(assignee)
    author = email_localpart(commit.author_email)
    committer = email_localpart(commit.committer_email)
    problems: list[CommitProblem] = []

    if author not in allowed:
        problems.append(
            CommitProblem(
                kind=KIND_AUTHOR_NOT_ALLOWED,
                sha=commit.sha,
                detail=(
                    f"author={commit.author_name} <{commit.author_email}>，"
                    f"允许={{{', '.join(allowed)}}}"
                ),
            )
        )
    if author not in profiles:
        problems.append(
            CommitProblem(
                kind=KIND_IDENTITY_UNKNOWN,
                sha=commit.sha,
                detail=(
                    f"author={commit.author_name} <{commit.author_email}>，"
                    f"bots.tsv 里没有 profile={author or '-'}"
                ),
            )
        )
    if committer not in profiles:
        problems.append(
            CommitProblem(
                kind=KIND_COMMITTER_NOT_BOT,
                sha=commit.sha,
                detail=(
                    f"committer={commit.committer_name} <{commit.committer_email}>，"
                    f"bots.tsv 里没有 profile={committer or '-'}"
                ),
            )
        )
    if committer != author:
        problems.append(
            CommitProblem(
                kind=KIND_COMMITTER_AUTHOR_MISMATCH,
                sha=commit.sha,
                detail=f"author={author or '-'}，committer={committer or '-'}",
            )
        )
    return tuple(problems)


def audit_commits(
    repo: str | Path = ".",
    *,
    card: str,
    rev: str = "HEAD",
    main_branch: str = "main",
    db_path: str | Path | None = None,
    bots_path: str | Path | None = None,
) -> CommitReport:
    """稽核 ``origin/<main_branch>..<rev>`` 区间内每个非合并提交的 author / committer。

    参数/环境级错误一律抛 :class:`CommitAuditError`（CLI 转 rc2），检查顺序：
    卡号形态 → 仓库 → 身份表 → 看板库 assignee → 区间终点 → 区间起点。
    """

    if not isinstance(card, str) or CARD_RE.fullmatch(card) is None:
        raise CommitAuditError(f"卡号非法：{card!r}（应形如 t_1234abcd）")

    root = _resolve_repo(repo)
    bots = load_bots(Path(bots_path) if bots_path is not None else root / BOTS_FILE)
    profiles = frozenset(bot.profile for bot in bots)
    assignee = card_assignee(Path(db_path) if db_path is not None else Path(DEFAULT_DB), card)

    rev_sha = _resolve_commit(root, rev, "区间终点")
    base_sha, _base_label = _resolve_base(root, main_branch)

    text = _git_ok(
        root,
        "log",
        "--no-merges",
        f"--format={_LOG_FORMAT}",
        f"{base_sha}..{rev_sha}",
        what="无法读取区间内的提交",
    )
    commits = _parse_log(text)

    problems: list[CommitProblem] = []
    for commit in commits:
        problems.extend(_check_commit(commit, assignee=assignee, profiles=profiles))

    return CommitReport(
        repo=root,
        card=card,
        main_branch=main_branch,
        rev=rev,
        commits=commits,
        problems=tuple(problems),
        assignee=assignee,
    )


def render_report(report: CommitReport) -> str:
    """渲染人类可读报告（结尾不打印退出码，CLI 用返回值落 rc）。"""

    if not report.commits:
        return "区间内无提交"

    lines = [
        "提交身份稽核（author 白名单 + committer 是本 bot）",
        f"仓库: {report.repo}",
        f"卡: {report.card} assignee={report.assignee}",
        f"区间: origin/{report.main_branch}..{report.rev}",
        f"提交（{len(report.commits)} 条）:",
    ]
    for commit in report.commits:
        lines.append(
            f"  {commit.short} author={email_localpart(commit.author_email)}"
            f" committer={email_localpart(commit.committer_email)}"
            f' "{commit.subject}"'
        )
    lines.append(f"问题（{len(report.problems)} 处）:")
    for index, problem in enumerate(report.problems, start=1):
        sha = problem.sha[:7] if problem.sha else "-"
        lines.append(f"  {index}. {sha} {problem.kind}：{problem.detail}")
    if report.ok:
        lines.append("结论：无问题")
    else:
        lines.append(f"结论：发现 {len(report.problems)} 个问题")
    return "\n".join(lines)
