"""``verify-batch`` 的分组契约层：plan 校验 + head 解析 + patch-id 分组（E1）。

口径唯一源是需求卡 ``t_f4e71bd3`` 冻结的契约 v1
（``/home/liu/project/tb-contracts/t_f4e71bd3.md`` §1–§3、§8）。本模块只做**分组**：

- ``load_plan`` —— 读 ``plan.json`` 并按 §1 的规则校验，不合法一律 :class:`VerifyBatchError`
  （CLI 层转 rc2：stderr 恰一行 ``错误: …``、stdout 0 字节、零 traceback）；
- ``resolve_head`` / ``patch_id`` —— 只调 git，不算别的东西；
- ``batch_id_for`` / ``build_groups`` —— 纯逻辑（除 ``resolve_head`` / ``patch_id`` 两次 git 调用）。

**本模块不跑检查、不建 worktree、不写日志、不 print**：``run_groups`` / ``write_batch_json`` /
``render_summary`` / ``emit_commands`` 是 E2 及之后的卡的事，本卡（E1）只建分组契约层。

三条容易写错的契约点，写在这里免得下游再猜：

1. **比不出来的不批量**（§0 D5）：``patch_id`` 返回 ``None`` 的 unit 用 ``head:<sha>`` 做组键，
   绝不靠猜并组。注意单 unit 的组按 §3.3 一律 ``kind="solo"`` —— 「patch_id 为 None 且独占组」
   仍归 ``solo``，``kind="head"`` 只出现在**同 head 的 ≥2 个 unit**上（契约 §3.3 优先于卡面措辞）。
2. **组键与组内 unit 顺序都是确定的**（§0 D6）：组按 ``(kind, key)`` 升序，组内 unit 按
   ``(card, pr)`` 升序；``batch_id`` 只吃组内 head_sha 排序后的集合，与 plan 里的书写顺序无关。
3. **组级检查跑在组首 unit 的 head_sha 上**（§3.6）：同 patch ⇒ 同内容，首 unit 取排序后的第一个。
"""

from __future__ import annotations

import hashlib
import json
import os
import re

# bandit B404 假阳性：只用于调用固定的 git 可执行文件（见 _git / _git_raw），
# 调用形式为 argv 列表、shell=False，不拼 shell 字符串
import subprocess  # nosec B404
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CARD_RE",
    "DEFAULT_BASE",
    "DEFAULT_GROUP_CHECKS",
    "ECHO_LIMIT",
    "GIT_TIMEOUT_S",
    "KIND_HEAD",
    "KIND_PATCH",
    "KIND_SOLO",
    "MAX_PR",
    "Group",
    "Plan",
    "Unit",
    "VerifyBatchError",
    "batch_id_for",
    "build_groups",
    "load_plan",
    "patch_id",
    "resolve_head",
]

#: 卡号：``t_`` + 8 位小写十六进制（与 ``branches.CARD_RE`` 同一口径，本卡不改别的文件）
CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")

#: PR 号上限（与 ``prclaim.MAX_PR`` 同一个数字：1..999999999）
MAX_PR = 999999999

#: 单条 git 调用的超时秒数（超时 → ``VerifyBatchError``，不挂死整条命令）
GIT_TIMEOUT_S = 120.0

#: 错误消息里回显用户输入的字符上限：超长输入（如 10 万字符的假 head）不许原样喷进 stderr
ECHO_LIMIT = 200

#: ``base`` 缺省值（§1）
DEFAULT_BASE = "origin/main"

#: 组的三种形态（§2 ``Group.kind`` 的封闭取值）
KIND_PATCH = "patch"
KIND_HEAD = "head"
KIND_SOLO = "solo"

#: ``group_checks`` 缺省值（§1）：与卡面示例同三条，但解释器用 ``sys.executable``
DEFAULT_GROUP_CHECKS: tuple[tuple[str, ...], ...] = (
    (sys.executable, "-m", "pytest", "-q", "--tb=line"),
    (sys.executable, "-m", "ruff", "check", "."),
    (sys.executable, "-m", "mypy"),
)


class VerifyBatchError(Exception):
    """``verify-batch`` 的参数/环境级错误（CLI 层转 rc2 一行 ``错误: …``）。"""


# --------------------------------------------------------------------------- 数据类型


@dataclass(frozen=True)
class Unit:
    """plan 里的一条待验证单元（§1 的一个 ``units[]`` 元素，已校验）。"""

    card: str
    pr: int
    head: str
    checks: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class Plan:
    """一份已校验的 plan（``base`` / ``group_checks`` 已代入缺省值）。"""

    base: str
    group_checks: tuple[tuple[str, ...], ...]
    units: tuple[Unit, ...] = field(default=())


@dataclass(frozen=True)
class Group:
    """一个分组：``key`` 是组键（``patch:<pid>`` 或 ``head:<sha>``），``head_sha`` 是组级检查的落点。"""

    batch_id: str
    kind: str
    key: str
    patch_id: str | None
    head_sha: str
    units: tuple[Unit, ...]


# --------------------------------------------------------------------------- git 调用
#
# 与 ``branches.py`` / ``merges.py`` 是同一种写法、同一份理由（那两处各有一份，注释里
# 也说明了「两个模块各自一份」的取舍）：本卡只建新文件、不改旧模块，所以这里第三份。

#: 会覆盖 ``git -C <repo>`` 定位的环境变量：hook / bisect / rebase --exec 里继承来的
#: ``GIT_DIR`` 会让 ``-C`` 失效，静默去读另一个仓库并返回假结果
_GIT_LOCATION_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
)


def _git_env() -> dict[str, str]:
    """``os.environ`` 的一份副本，剔掉 :data:`_GIT_LOCATION_ENV` 里的定位变量。

    ``subprocess.run(env=...)`` 传 ``None`` 等于继承父进程全部环境（含 ``GIT_DIR``），
    传 ``{}`` 又等于清空（``PATH`` 没了）；所以这里显式构造「剔除后的副本」。
    """

    return {
        key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_ENV
    }


def _git(repo: str | os.PathLike[str], *args: str) -> subprocess.CompletedProcess[str]:
    """在 ``repo`` 里跑 git（文本模式），不抛 ``CalledProcessError``，rc 交给调用方判。

    ``PATH`` 里没有 git / 输出不是 UTF-8 一律 ``VerifyBatchError``（环境级错误，
    CLI 转 rc2），不许冒 traceback 给用户。
    """

    try:
        # bandit B603/B607 假阳性：可执行文件名写死为 "git"（走 PATH 以跨平台一致）；
        # 参数以 argv 列表传入、shell=False（默认），仓库路径 / rev 只作为独立 argv 元素
        return subprocess.run(  # nosec B603 B607
            ["git", "-C", os.fspath(repo), *args],
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            env=_git_env(),
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerifyBatchError(
            f"git 调用超时（>{GIT_TIMEOUT_S:g}s）：git {' '.join(args)}"
        ) from exc
    except FileNotFoundError as exc:
        raise VerifyBatchError("找不到 git 可执行文件（PATH 里没有 git），无法分组") from exc
    except UnicodeDecodeError as exc:
        raise VerifyBatchError(
            f"git 输出不是 UTF-8，无法解析（第 {exc.start} 字节起解码失败）"
        ) from exc
    except OSError as exc:
        raise VerifyBatchError(f"无法执行 git: {exc}") from exc


def _git_raw(
    repo: str | os.PathLike[str], *args: str, stdin_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """在 ``repo`` 里跑 git（**字节模式**），只给 ``patch_id`` 用。

    diff 与 patch-id 的输入输出都按字节走：``git diff --binary`` 里既有 base64 形态的
    二进制块、也可能出现非 UTF-8 的文件正文，文本模式下 ``errors="strict"`` 会直接炸。
    """

    try:
        # bandit B603/B607 假阳性：同 _git，argv 列表 + shell=False
        return subprocess.run(  # nosec B603 B607
            ["git", "-C", os.fspath(repo), *args],
            capture_output=True,
            check=False,
            env=_git_env(),
            input=stdin_bytes,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerifyBatchError(
            f"git 调用超时（>{GIT_TIMEOUT_S:g}s）：git {' '.join(args)}"
        ) from exc
    except FileNotFoundError as exc:
        raise VerifyBatchError("找不到 git 可执行文件（PATH 里没有 git），无法算 patch-id") from exc
    except OSError as exc:
        raise VerifyBatchError(f"无法执行 git: {exc}") from exc


# --------------------------------------------------------------------------- plan 校验


def _err(where: str, detail: str) -> VerifyBatchError:
    return VerifyBatchError(f"{where}: {detail}")


def _echo(value: object, limit: int = ECHO_LIMIT) -> str:
    """把用户输入渲染进错误消息：``repr`` 形态，超过 ``limit`` 字符就截断并标出总长。

    为什么要有：错误消息会原样进 stderr（CLI 一行 ``错误: …``），一个 10 万字符的假
    head 会把终端刷屏、把日志撑爆。截断**只影响回显**，校验本身仍按完整输入做。
    """

    text = repr(value)
    if len(text) <= limit:
        return text
    note = f"…（共 {len(text)} 字符，已截断）"
    return f"{text[: max(0, limit - len(note))]}{note}"


def _as_card(value: object, where: str) -> str:
    """卡号必须逐字 ``t_[0-9a-f]{8}``（大小写敏感、不许带空白）。"""

    if not isinstance(value, str):
        raise _err(where, f"card 必须是字符串，实际是 {type(value).__name__}")
    if not CARD_RE.match(value):
        raise _err(where, f"card 形态非法（须为 t_[0-9a-f]{{8}}）：{_echo(value)}")
    return value


def _as_pr(value: object, where: str) -> int:
    """PR 号必须是 1..999999999 的整数；字符串形态 ``"53"``、``bool``、越界全拒。"""

    if isinstance(value, bool):
        raise _err(where, f"pr 必须是整数，不接受布尔值：{value!r}")
    if not isinstance(value, int):
        raise _err(
            where, f"pr 必须是整数（不接受 {type(value).__name__} 形态）：{value!r}"
        )
    if value < 1 or value > MAX_PR:
        raise _err(where, f"pr 必须在 1..{MAX_PR} 之间：{value}")
    return value


def _as_head(value: object, where: str) -> str:
    """``head`` 是 git rev（分支名或 sha），非空、不含 NUL、不含空白。"""

    if not isinstance(value, str):
        raise _err(where, f"head 必须是字符串，实际是 {type(value).__name__}")
    if not value.strip():
        raise _err(where, "head 不许为空串")
    if "\x00" in value:
        raise _err(where, "head 不许含 NUL")
    if any(ch.isspace() for ch in value):
        raise _err(where, f"head 不许含空白（不是单个 git rev）：{_echo(value)}")
    return value


def _as_base(value: object, where: str) -> str:
    """``base`` 是 git rev（缺省 ``origin/main``），与 :func:`_as_head` 同级校验。

    额外一条：**不许以 ``-`` 开头**。``git merge-base <base> <head>`` 不隔离选项，
    ``base="--help"`` 会被当选项解析（打印帮助、rc0、无祖先输出），``patch_id`` 于是
    返回 ``None``、所有 unit 静默降级成 ``solo``/``head`` 组 —— 配置写错却 rc0 放行。
    这类输入必须在 plan 校验层就报错，不许走到 git 里当「比不出来」。
    """

    if not isinstance(value, str):
        raise _err(where, f"必须是字符串，实际是 {type(value).__name__}")
    if not value.strip():
        raise _err(where, "不许为空串")
    if "\x00" in value:
        raise _err(where, "不许含 NUL")
    if any(ch.isspace() for ch in value):
        raise _err(where, f"不许含空白（不是单个 git rev）：{_echo(value)}")
    if value.startswith("-"):
        raise _err(where, f"不许以 '-' 开头（会被 git 当选项解析）：{_echo(value)}")
    return value



def _as_argv(value: object, where: str) -> tuple[str, ...]:
    """一条命令 = 非空字符串数组（不经 shell，所以**不支持**字符串形态）。"""

    if isinstance(value, str):
        raise _err(where, f"不接受字符串形态的命令（须为 argv 数组）：{_echo(value)}")
    if not isinstance(value, list):
        raise _err(where, f"命令必须是 argv 数组，实际是 {type(value).__name__}")
    if not value:
        raise _err(where, "命令不许为空数组")
    argv: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise _err(f"{where}[{index}]", f"argv 每项必须是字符串，实际是 {type(item).__name__}")
        if not item:
            raise _err(f"{where}[{index}]", "argv 每项不许为空串")
        if "\x00" in item:
            raise _err(f"{where}[{index}]", "argv 每项不许含 NUL")
        argv.append(item)
    return tuple(argv)


def _as_argv_list(value: object, where: str) -> tuple[tuple[str, ...], ...]:
    """一串命令（``group_checks`` / ``checks``）：数组，可空，每项按 :func:`_as_argv` 校验。"""

    if not isinstance(value, list):
        raise _err(where, f"必须是 argv 数组的数组，实际是 {type(value).__name__}")
    return tuple(_as_argv(item, f"{where}[{index}]") for index, item in enumerate(value))


def _parse_unit(raw: object, index: int) -> Unit:
    where = f"units[{index}]"
    if not isinstance(raw, dict):
        raise _err(where, f"必须是 JSON 对象，实际是 {type(raw).__name__}")
    for key in ("card", "pr", "head"):
        if key not in raw:
            raise _err(where, f"缺必填键 {key}")
    checks_raw = raw.get("checks", [])
    return Unit(
        card=_as_card(raw["card"], f"{where}.card"),
        pr=_as_pr(raw["pr"], f"{where}.pr"),
        head=_as_head(raw["head"], f"{where}.head"),
        checks=_as_argv_list(checks_raw, f"{where}.checks"),
    )


def _parse_plan(raw: object) -> Plan:
    if not isinstance(raw, dict):
        raise _err("plan", f"顶层必须是 JSON 对象，实际是 {type(raw).__name__}")

    base_raw = raw.get("base", DEFAULT_BASE)
    base = _as_base(base_raw, "base")

    checks_raw = raw.get("group_checks", [list(argv) for argv in DEFAULT_GROUP_CHECKS])
    group_checks = _as_argv_list(checks_raw, "group_checks")

    units_raw = raw.get("units")
    if units_raw is None:
        raise _err("units", "缺必填键（必须是非空数组）")
    if not isinstance(units_raw, list):
        raise _err("units", f"必须是非空数组，实际是 {type(units_raw).__name__}")
    if not units_raw:
        raise _err("units", "不许为空数组（没有待验证单元）")

    units = tuple(_parse_unit(item, index) for index, item in enumerate(units_raw))
    return Plan(base=base, group_checks=group_checks, units=units)


def load_plan(path: str | os.PathLike[str]) -> Plan:
    """读 ``plan.json`` 并校验（§1）；任何不合法都 ``VerifyBatchError``。"""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        # UnicodeDecodeError 不是 OSError（它是 UnicodeError/ValueError 子类），
        # 漏掉它会让非 UTF-8 的 plan 变成 traceback + rc1，打穿「不合法一律 rc2」契约。
        raise VerifyBatchError(f"读不到 plan 文件 {_echo(os.fspath(path))}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerifyBatchError(
            f"plan 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}"
        ) from exc
    return _parse_plan(raw)


# --------------------------------------------------------------------------- git 侧


def resolve_head(repo: str | os.PathLike[str], rev: str) -> str:
    """``git rev-parse <rev>^{commit}`` 取 40 位 head sha；失败 → ``VerifyBatchError``。

    失败是**整条命令**失败（§3.1：不静默跳过），不是「这个 unit 单独降级」。
    """

    if not isinstance(rev, str) or not rev.strip():
        raise VerifyBatchError("head 不许为空串")
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {proc.returncode}"
        raise VerifyBatchError(f"head {_echo(rev)} 解析失败（不是本仓库里的提交）: {tail}")
    return sha


def patch_id(repo: str | os.PathLike[str], base: str, head_sha: str) -> str | None:
    """``merge-base`` → ``diff --binary`` → ``patch-id --stable`` 的首要字段。

    任何一步失败（merge-base 找不到共同祖先、diff 为空、git 不可用）都返回 ``None`` ——
    属于「比不出来」，由调用方把它退化成 ``head:<sha>`` 组键（§0 D5），**不抛异常**。
    """

    merge_base = _git(repo, "merge-base", base, head_sha)
    if merge_base.returncode != 0:
        return None
    ancestor = merge_base.stdout.strip()
    if not ancestor:
        return None

    diff = _git_raw(
        repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", ancestor, head_sha
    )
    if diff.returncode != 0 or not diff.stdout.strip():
        return None

    proc = _git_raw(repo, "patch-id", "--stable", stdin_bytes=diff.stdout)
    if proc.returncode != 0:
        return None
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if fields:
            return fields[0]
    return None


# --------------------------------------------------------------------------- 分组


def batch_id_for(head_shas: Sequence[str]) -> str:
    """``"b-" + sha256("\\n".join(sorted(head_shas)))[:8]``（§2；与书写顺序无关）。"""

    digest = hashlib.sha256("\n".join(sorted(head_shas)).encode("utf-8")).hexdigest()
    return "b-" + digest[:8]


def _unit_order(unit: Unit) -> tuple[str, int]:
    return (unit.card, unit.pr)


def build_groups(repo: str | os.PathLike[str], plan: Plan) -> tuple[Group, ...]:
    """按 §3 把 plan 的 unit 分组，组与组内顺序都是确定的（§0 D6）。

    顺序：

    1. 每个 unit ``resolve_head``（失败 → ``VerifyBatchError``，整条命令失败）；
    2. ``patch_id`` 有值 → 组键 ``patch:<pid>``；无值 → ``head:<sha>``；
    3. 组内 unit 按 ``(card, pr)`` 升序；组首 unit 的 head_sha 就是组级检查的落点（§3.6）；
    4. 组内 unit 数 <2 → ``kind="solo"``，否则 ``patch``（按 patch 聚）/ ``head``（同 head 聚）；
    5. 组按 ``(kind, key)`` 升序返回。
    """

    resolved: list[tuple[Unit, str, str | None]] = []
    # base 在这里再校验一次：``Plan`` 是公开 dataclass，调用方可以绕开 ``load_plan``
    # 手搓一个（pm 的复审探针就是这么构造的）。不校验就会把 ``--help`` / NUL 送进 git。
    _as_base(plan.base, "base")
    for unit in plan.units:
        head_sha = resolve_head(repo, unit.head)
        resolved.append((unit, head_sha, patch_id(repo, plan.base, head_sha)))

    buckets: dict[str, list[tuple[Unit, str, str | None]]] = {}
    for item in resolved:
        pid = item[2]
        key = f"patch:{pid}" if pid else f"head:{item[1]}"
        buckets.setdefault(key, []).append(item)

    groups: list[Group] = []
    for key, items in buckets.items():
        ordered = sorted(items, key=lambda item: _unit_order(item[0]))
        units = tuple(item[0] for item in ordered)
        if len(units) < 2:
            kind = KIND_SOLO
        elif key.startswith("patch:"):
            kind = KIND_PATCH
        else:
            kind = KIND_HEAD
        groups.append(
            Group(
                batch_id=batch_id_for([item[1] for item in ordered]),
                kind=kind,
                key=key,
                patch_id=ordered[0][2],
                head_sha=ordered[0][1],
                units=units,
            )
        )

    groups.sort(key=lambda group: (group.kind, group.key))
    return tuple(groups)
