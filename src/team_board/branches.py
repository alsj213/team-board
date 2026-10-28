"""分支稽核：卡分支上的提交归属（规则 A）与 checkout 偏移（规则 B）。

只依赖标准库与 ``git`` 可执行文件，不引入任何新依赖。

规则 A —— 卡分支上的提交归属
--------------------------------
卡分支 = 名字**精确**等于 ``wt/t_<8 位小写十六进制>`` 的本地分支
（``refs/heads/wt/…``）与远端分支（``refs/remotes/origin/wt/…``）。
其它分支（``main``、``pm/ci-check``、``wt/ci-setup``）一律跳过。

要检查的提交是 ``git log --no-merges <base>..<ref>``，``<base>`` 优先用本地
``refs/heads/<main_branch>``，不存在则用 ``refs/remotes/origin/<main_branch>``。

每个提交的**申报卡号集合** = 提交标题里的卡号 ∪ 该提交改动文件路径里的卡号。
**只看标题与路径，不看正文**（正文常引用别的卡做上下文）。据此判红两类：

- ``未声明卡号``：集合为空 —— 无法归属，不许进卡分支；
- ``跨卡提交``：集合里出现了 ≠ 本分支卡号的卡号 —— 输出里两张卡号都点名。

豁免只能按「分支卡号 + 对方卡号（或未声明）+ 提交 sha」逐条登记，命中时从
``findings`` 移到 ``suppressed`` 并在输出里逐条回显理由，绝不静默吞掉。
豁免对规则 B 不生效。

规则 B —— checkout 偏移
------------------------
- 主 checkout（``git rev-parse --git-common-dir`` 的父目录）必须停在
  ``refs/heads/<main_branch>``；
- 其它 worktree：detached HEAD 一律判红；分支为 ``wt/t_<X>`` 时目录名必须
  精确等于 ``t_<X>``；分支为 ``<main_branch>`` 通过；其它分支判红。

退出码（由 CLI 落）：0 无偏差 / 1 发现偏差 / 2 参数或环境错误（``AuditError``，
含 ``PATH`` 里没有 ``git``、豁免表不是 UTF-8、**git 输出不是 UTF-8**）。
"""

from __future__ import annotations

import os
import re

# bandit B404 假阳性：只用于调用固定的 git 可执行文件（见 _git），
# 调用形式为 argv 列表、shell=False，不拼 shell 字符串
import subprocess  # nosec B404
from dataclasses import dataclass, field
from pathlib import Path

#: 卡号：``t_`` + 8 位小写十六进制，前后都不许粘字母数字下划线
CARD_RE = re.compile(r"(?<![0-9a-zA-Z_])t_[0-9a-f]{8}(?![0-9a-f])")

#: 卡分支名：``wt/`` + 卡号，整体精确匹配
BRANCH_RE = re.compile(r"^wt/(t_[0-9a-f]{8})$")

#: allowlist 里的 sha 字段：7–40 位小写十六进制
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

KIND_NO_CARD = "未声明卡号"
KIND_FOREIGN = "跨卡提交"

_RS = "\x1e"
_US = "\x1f"


class AuditError(Exception):
    """参数 / 环境级错误，CLI 转 rc2（stderr ``错误: …``，stdout 空）。"""


@dataclass(frozen=True)
class Commit:
    """一个被稽核的提交。"""

    sha: str
    short: str
    subject: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    """一处提交归属偏差。"""

    kind: str
    branch_card: str
    foreign_card: str | None
    commit: Commit
    evidence: str


@dataclass(frozen=True)
class CheckoutDrift:
    """一处 checkout 偏移。"""

    path: str
    actual: str
    expected: str


@dataclass(frozen=True)
class AllowEntry:
    """一条按提交登记的豁免。"""

    branch_card: str
    foreign_card: str | None
    short_sha: str
    reason: str


@dataclass(frozen=True)
class Report:
    """一次稽核的完整结果。"""

    repo: str
    main_branch: str
    checked_branches: tuple[str, ...]
    findings: tuple[Finding, ...]
    drifts: tuple[CheckoutDrift, ...]
    suppressed: tuple[tuple[AllowEntry, Finding], ...]
    notes: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """没有任何提交归属偏差与 checkout 偏移。"""

        return not self.findings and not self.drifts


def _git(repo: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在 ``repo`` 里跑 git，不抛 CalledProcessError，交给调用方判 rc。

    两类环境级错误一律 ``AuditError``（CLI 转 rc2），不许冒 traceback 给用户：

    - ``PATH`` 里没有 ``git``（``FileNotFoundError``）或其它 ``OSError``
      （权限、进程数上限等）；
    - git 输出**不是 UTF-8**：``i18n.logOutputEncoding=GBK``（中文 Windows
      常见）下 ``git log`` 会把提交标题转码输出，``core.quotePath=false``
      下 ``--name-only`` 会吐原始字节，``subprocess`` 的严格解码于是抛
      ``UnicodeDecodeError``（``ValueError`` 系，不是 ``OSError``，会直接逃出）。
      rc1 是「发现偏差」的专用码，崩溃必须与它分开 —— 这里的解码失败意味着
      整轮稽核没法进行，属环境错误。

    解码固定用 UTF-8（不随 ``locale`` 漂移），因此同样的仓库在任何机器上
    得到同样的 rc。
    """

    try:
        # bandit B603/B607 假阳性：可执行文件名写死为 "git"（走 PATH 以保证
        # 跨平台一致）；参数以 argv 列表传入、shell=False（默认），用户给的
        # 仓库路径/分支名只作为独立 argv 元素，不经 shell 解释，无注入面
        return subprocess.run(  # nosec B603 B607
            ["git", "-C", os.fspath(repo), *args],
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
    except FileNotFoundError as exc:
        raise AuditError(
            "找不到 git 可执行文件（PATH 里没有 git），无法稽核"
        ) from exc
    except UnicodeDecodeError as exc:
        raise AuditError(
            f"git 输出不是 UTF-8，无法解析（第 {exc.start} 字节起解码失败；"
            "常见原因：仓库配置 i18n.logOutputEncoding=GBK 或 "
            "core.quotePath=false 且提交信息/文件路径含非 UTF-8 字节；"
            "可执行 git config --unset i18n.logOutputEncoding 后重试）"
        ) from exc
    except OSError as exc:
        raise AuditError(f"无法执行 git: {exc}") from exc


def _must_git(repo: str | Path, *args: str, what: str) -> str:
    """跑 git，失败即 ``AuditError``（环境级错误）。"""

    proc = _git(repo, *args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {proc.returncode}"
        raise AuditError(f"{what}: {tail}")
    return proc.stdout


def _ref_exists(repo: str | Path, ref: str) -> bool:
    """``ref`` 是否存在（不打印输出）。"""

    proc = _git(repo, "rev-parse", "--verify", "--quiet", ref)
    return proc.returncode == 0


def load_allowlist(path: str | Path | None) -> tuple[AllowEntry, ...]:
    """读豁免表；文件不存在视为空表（不报错）。

    行格式（空白分隔）::

        <分支卡号> <对方卡号或 -> <提交sha7..40> <理由，必填>

    ``#`` 开头为注释，空行忽略。少于 3 个字段、缺理由、sha 不是 7–40 位
    十六进制、卡号格式不对 → ``AuditError``。
    """

    if path is None:
        return ()
    file_path = Path(path)
    if not file_path.exists():
        return ()

    entries: list[AllowEntry] = []
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # 不是 UTF-8 文本（比如误指到二进制文件）：参数级错误 → rc2
        raise AuditError(
            f"豁免表 {file_path} 不是 UTF-8 文本，无法解析"
            f"（第 {exc.start} 字节起解码失败）"
        ) from exc
    except OSError as exc:  # 权限、路径是目录等
        raise AuditError(f"无法读取豁免表 {file_path}: {exc}") from exc

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3:
            raise AuditError(
                f"豁免表 {file_path} 第 {lineno} 行字段不足（至少要 "
                f"<分支卡号> <对方卡号或-> <sha>）: {line!r}"
            )
        if len(fields) < 4:
            raise AuditError(
                f"豁免表 {file_path} 第 {lineno} 行缺理由（理由必填）: {line!r}"
            )
        branch_card, foreign_raw, sha = fields[0], fields[1], fields[2]
        reason = " ".join(fields[3:]).strip()

        if not BRANCH_RE.match(f"wt/{branch_card}"):
            raise AuditError(
                f"豁免表 {file_path} 第 {lineno} 行分支卡号不合法: {branch_card!r}"
            )
        if foreign_raw == "-":
            foreign_card: str | None = None
        elif BRANCH_RE.match(f"wt/{foreign_raw}"):
            foreign_card = foreign_raw
        else:
            raise AuditError(
                f"豁免表 {file_path} 第 {lineno} 行对方卡号不合法"
                f"（应为卡号或 -）: {foreign_raw!r}"
            )
        if not SHA_RE.match(sha):
            raise AuditError(
                f"豁免表 {file_path} 第 {lineno} 行提交 sha 不是 7–40 位"
                f"小写十六进制: {sha!r}"
            )
        if not reason:
            raise AuditError(f"豁免表 {file_path} 第 {lineno} 行缺理由")

        entries.append(
            AllowEntry(
                branch_card=branch_card,
                foreign_card=foreign_card,
                short_sha=sha,
                reason=reason,
            )
        )
    return tuple(entries)


def _parse_commits(raw: str) -> tuple[Commit, ...]:
    """解析 ``git log --format=%x1e%H%x1f%s --name-only`` 的输出。"""

    commits: list[Commit] = []
    for chunk in raw.split(_RS):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        header = lines[0]
        if _US not in header:
            continue
        sha, subject = header.split(_US, 1)
        sha = sha.strip()
        paths = tuple(
            line.strip() for line in lines[1:] if line.strip()
        )
        commits.append(
            Commit(
                sha=sha,
                short=sha[:7],
                subject=subject,
                paths=paths,
            )
        )
    return tuple(commits)


def _cards_in(text: str) -> set[str]:
    """文本里出现的卡号集合。"""

    return set(CARD_RE.findall(text))


def _make_findings(
    branch_card: str, commits: tuple[Commit, ...]
) -> list[Finding]:
    """按规则 A 产出偏差（未豁免前）。"""

    findings: list[Finding] = []
    for commit in commits:
        from_subject = _cards_in(commit.subject)
        claimed = set(from_subject)
        path_hit: dict[str, str] = {}
        for path in commit.paths:
            for card in _cards_in(path):
                claimed.add(card)
                path_hit.setdefault(card, path)

        if not claimed:
            findings.append(
                Finding(
                    kind=KIND_NO_CARD,
                    branch_card=branch_card,
                    foreign_card=None,
                    commit=commit,
                    evidence="提交标题",
                )
            )
            continue

        for foreign in sorted(claimed - {branch_card}):
            evidence = (
                "提交标题"
                if foreign in from_subject
                else f"文件路径 {path_hit[foreign]}"
            )
            findings.append(
                Finding(
                    kind=KIND_FOREIGN,
                    branch_card=branch_card,
                    foreign_card=foreign,
                    commit=commit,
                    evidence=evidence,
                )
            )
    return findings


def _allow_hit(entry: AllowEntry, finding: Finding) -> bool:
    """豁免是否精确命中这一条偏差（卡号 + 对方卡号 + 提交 sha 前缀）。"""

    return (
        entry.branch_card == finding.branch_card
        and entry.foreign_card == finding.foreign_card
        and finding.commit.sha.startswith(entry.short_sha)
    )


def _checkout_drifts(
    repo: str | Path, main_branch: str
) -> list[CheckoutDrift]:
    """规则 B：解析 worktree 列表，产出 checkout 偏移。"""

    raw = _must_git(
        repo, "worktree", "list", "--porcelain", what="读取 worktree 列表失败"
    )
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        worktrees.append(current)

    common_dir = _must_git(
        repo,
        "rev-parse",
        "--git-common-dir",
        what="定位主 checkout 失败",
    ).strip()
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = Path(repo).resolve() / common_path
    main_checkout = common_path.resolve().parent

    expected_main_ref = f"refs/heads/{main_branch}"
    drifts: list[CheckoutDrift] = []
    for entry in worktrees:
        path_text = entry.get("worktree")
        if not path_text:
            continue
        path = Path(path_text).resolve()
        sha = entry.get("HEAD", "")
        branch_ref = entry.get("branch")
        detached = "detached" in entry or branch_ref is None

        if detached:
            actual = f"detached@{sha[:7]}"
            basename = path.name
            expected = (
                f"wt/{basename}" if BRANCH_RE.match(f"wt/{basename}") else main_branch
            )
        else:
            if branch_ref is None:
                # 不变量：非 detached 的 worktree 必有 branch 字段。写成显式
                # raise 而不是 assert —— assert 在 -O 下会被剥掉（bandit B101）
                raise AuditError(
                    "内部不变量被破坏：worktree 既不是 detached 也没有 branch 字段"
                )
            actual = branch_ref.removeprefix("refs/heads/")
            card_match = BRANCH_RE.match(actual)
            basename = path.name
            if path == main_checkout:
                expected = main_branch
            elif card_match:
                expected = f"wt/{card_match.group(1)}"
            else:
                expected = main_branch

        if path == main_checkout:
            if not detached and branch_ref == expected_main_ref:
                continue
            drifts.append(
                CheckoutDrift(path=str(path), actual=actual, expected=main_branch)
            )
            continue

        if detached:
            drifts.append(
                CheckoutDrift(path=str(path), actual=actual, expected=expected)
            )
            continue

        if branch_ref is None:
            # 同上：非 detached 分支走到这里必有 branch 字段（bandit B101）
            raise AuditError(
                "内部不变量被破坏：非 detached 的 worktree 缺少 branch 字段"
            )
        branch_name = branch_ref.removeprefix("refs/heads/")
        if branch_name == main_branch:
            continue
        card_match = BRANCH_RE.match(branch_name)
        if card_match:
            if path.name == card_match.group(1):
                continue
            drifts.append(
                CheckoutDrift(
                    path=str(path),
                    actual=branch_name,
                    expected=f"wt/{path.name}",
                )
            )
            continue
        drifts.append(
            CheckoutDrift(path=str(path), actual=branch_name, expected=main_branch)
        )
    return drifts


def audit(
    repo: str | Path = ".",
    *,
    main_branch: str = "main",
    only_branch: str | None = None,
    allow_file: str | Path | None = None,
) -> Report:
    """稽核 ``repo``：规则 A（提交归属）+ 规则 B（checkout 偏移）。

    参数 / 环境问题抛 ``AuditError``（CLI 转 rc2）。
    """

    if not str(repo).strip():
        raise AuditError("仓库路径为空")
    repo_path = Path(repo)
    if not repo_path.exists():
        raise AuditError(f"仓库路径不存在: {repo_path}")
    if not repo_path.is_dir():
        raise AuditError(f"仓库路径不是目录: {repo_path}")

    proc = _git(repo_path, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise AuditError(f"不是 git 工作树: {repo_path}")
    top = Path(proc.stdout.strip()).resolve()

    base_ref = None
    for candidate in (
        f"refs/heads/{main_branch}",
        f"refs/remotes/origin/{main_branch}",
    ):
        if _ref_exists(top, candidate):
            base_ref = candidate
            break
    if base_ref is None:
        raise AuditError(
            f"主分支不存在（本地与 origin 都没有）: {main_branch}"
        )

    notes: list[str] = []
    refs: list[tuple[str, str]] = []  # (card, ref)
    if only_branch is not None:
        requested = only_branch.strip()
        if not requested:
            raise AuditError("--branch 不能为空")
        card_match = BRANCH_RE.match(
            requested if requested.startswith("wt/") else f"wt/{requested}"
        )
        card = card_match.group(1) if card_match else None
        if requested.startswith("wt/"):
            candidates = [
                f"refs/heads/{requested}",
                f"refs/remotes/origin/{requested}",
            ]
        else:
            candidates = [
                f"refs/heads/{requested}",
                f"refs/remotes/origin/{requested}",
                f"refs/heads/wt/{requested}",
                f"refs/remotes/origin/wt/{requested}",
            ]
        existing = [
            candidate for candidate in candidates if _ref_exists(top, candidate)
        ]
        if not existing:
            raise AuditError(f"分支不存在（本地与 origin 都没有）: {requested}")
        if card is None:
            notes.append(f"{requested} 不是卡分支，跳过提交归属检查")
        else:
            card_refs = [ref for ref in existing if "/wt/" in ref]
            if not card_refs:
                # 裸卡号只解析到「不带 wt/ 前缀」的同名分支：它根本不是卡分支，
                # 若就这样放过去，`--branch t_x` 会稽核零条 ref 并报「无偏差」，
                # 把「PR 头分支漏写 wt/」这种最该抓的偏差放行成假绿。
                # 卡面只许诺两种写法都指向卡分支，没许诺指向不存在的卡分支时判绿。
                raise AuditError(
                    f"分支不存在（本地与 origin 都没有）: wt/{card}"
                    f"（{requested} 只匹配到 "
                    f"{'、'.join(ref.removeprefix('refs/') for ref in existing)}，"
                    f"卡分支必须是 wt/{card}）"
                )
            refs.extend((card, ref) for ref in card_refs)
    else:
        listing = _must_git(
            top,
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads/wt",
            "refs/remotes/origin/wt",
            what="枚举卡分支失败",
        )
        for ref in listing.splitlines():
            ref = ref.strip()
            if not ref:
                continue
            stripped = re.match(
                r"^refs/(?:heads/|remotes/[^/]+/)(.+)$", ref
            )
            if stripped is None:
                continue
            base_name = stripped.group(1)
            match = BRANCH_RE.match(base_name)
            if not match:
                continue
            refs.append((match.group(1), ref))

    if allow_file is None:
        allow_path: Path | None = top / ".team-board" / "branch-audit-allow.txt"
    else:
        allow_path = Path(allow_file)
    allowlist = load_allowlist(allow_path)

    findings: list[Finding] = []
    suppressed: list[tuple[AllowEntry, Finding]] = []
    seen: set[tuple[str, str, str | None]] = set()
    checked: list[str] = []

    for card, ref in refs:
        if card not in checked:
            checked.append(card)
        raw = _must_git(
            top,
            "log",
            "--no-merges",
            f"--format={_RS}%H{_US}%s",
            "--name-only",
            f"{base_ref}..{ref}",
            what=f"读取提交失败（{ref}）",
        )
        for finding in _make_findings(card, _parse_commits(raw)):
            key = (finding.branch_card, finding.commit.sha, finding.foreign_card)
            if key in seen:
                continue
            seen.add(key)
            entry = next(
                (e for e in allowlist if _allow_hit(e, finding)), None
            )
            if entry is not None:
                suppressed.append((entry, finding))
            else:
                findings.append(finding)

    drifts = _checkout_drifts(top, main_branch)

    return Report(
        repo=str(top),
        main_branch=main_branch,
        checked_branches=tuple(sorted(checked)),
        findings=tuple(findings),
        drifts=tuple(drifts),
        suppressed=tuple(suppressed),
        notes=tuple(notes),
    )


def render_report(report: Report) -> str:
    """把 ``Report`` 渲染成人类可读文本（结尾一行「结论：…」，不含退出码行）。"""

    lines: list[str] = [
        "分支稽核（卡分支提交归属 + checkout 偏移）",
        f"仓库: {report.repo}",
        f"主分支: {report.main_branch}",
        "检查的卡分支: "
        + ("、".join(report.checked_branches) if report.checked_branches else "（无）"),
    ]
    for note in report.notes:
        lines.append(f"提示: {note}")

    lines.append(f"提交归属偏差（{len(report.findings)} 处）:")
    if not report.findings:
        lines.append("  （无）")
    for index, finding in enumerate(report.findings, start=1):
        if finding.kind == KIND_NO_CARD:
            lines.append(
                f'  {index}. 卡 {finding.branch_card} 的分支上，提交 '
                f'{finding.commit.short} "{finding.commit.subject}" '
                f"未声明卡号（无法归属）；证据：{finding.evidence}"
            )
        else:
            lines.append(
                f'  {index}. 卡 {finding.branch_card} 的提交 '
                f'{finding.commit.short} "{finding.commit.subject}" '
                f"申报的是卡 {finding.foreign_card}（跨卡提交）；"
                f"证据：{finding.evidence}"
            )

    lines.append(f"checkout 偏移（{len(report.drifts)} 处）:")
    if not report.drifts:
        lines.append("  （无）")
    for index, drift in enumerate(report.drifts, start=1):
        lines.append(
            f"  {index}. {drift.path} 当前在 {drift.actual}"
            f"（应为 {drift.expected}）"
        )

    lines.append(f"豁免（{len(report.suppressed)} 条）:")
    if not report.suppressed:
        lines.append("  （无）")
    for index, (entry, finding) in enumerate(report.suppressed, start=1):
        foreign = entry.foreign_card or "未声明卡号"
        lines.append(
            f"  {index}. 豁免 {entry.branch_card} 分支上的提交 "
            f"{finding.commit.short}（对方卡 {foreign}）：{entry.reason}"
        )

    total = len(report.findings) + len(report.drifts)
    if total == 0:
        lines.append("结论：无偏差")
    else:
        lines.append(
            f"结论：发现 {total} 处偏差"
            f"（提交归属 {len(report.findings)} 处 / "
            f"checkout {len(report.drifts)} 处）"
        )
    return "\n".join(lines)
