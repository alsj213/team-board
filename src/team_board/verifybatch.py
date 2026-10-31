"""``verify-batch`` 的契约层与执行层：分组（E1）+ 每组一次全套跑检查（E2）。

口径唯一源是需求卡 ``t_f4e71bd3`` 冻结的契约 v1
（``/home/liu/project/tb-contracts/t_f4e71bd3.md`` §1–§5、§8）。

- E1（分组契约层）：``load_plan`` —— 读 ``plan.json`` 并按 §1 的规则校验，不合法一律
  :class:`VerifyBatchError`（CLI 层转 rc2：stderr 恰一行 ``错误: …``、stdout 0 字节、零 traceback）；
  ``resolve_head`` / ``patch_id`` —— 只调 git；``batch_id_for`` / ``build_groups`` —— 分组。
- E2（执行层，§4）：``run_groups`` 每组一个临时 worktree、组级检查跑一次、unit 自检逐条跑，
  日志落 ``<log_dir>/batch-<batch_id>.log``；``write_batch_json`` 每组一个账本（§5）；
  ``render_summary`` 只回摘要、**不打印**任何原始输出（本模块也确实不 print —— 打印归 E3 的 CLI）。

没接 CLI 的边界（E3 的活）：本模块不做 argparse、不决定 rc0/1/2、不打 ``--dry-run`` 的压缩率行。

三条容易写错的契约点，写在这里免得下游再猜：

1. **同内容 = 同 tree**（pm 2026-10-31 决定，返工卡 ``t_600d3378``）：组键是 ``<head>^{tree}``，
   不是 ``git patch-id``。patch-id 会**剥空白**（``X = 2`` 与 `` X = 2`` 同 patch-id、tree 不同），
   拿它当组键会把一个语法坏掉的 unit 并进正常 unit 的组，而组级检查只跑在组首 unit 的
   ``head_sha`` 上 —— 坏 unit 白拿 PASS。unit 共用同一 ``base``，所以「同 tree」⇔「改动逐字相同」；
   ``patch_id`` 仍然算，但**只进日志/展示**（``Group.patch_id``），不参与分桶。tree 解析失败属于
   环境级错误（整条命令失败），没有「比不出来 → 单独一个桶」这条退化路径。
   注意单 unit 的组按 §3.3 一律 ``kind="solo"``；``kind="head"`` 只出现在**同 head 的 ≥2 个
   unit** 上，``kind="patch"`` 是同 tree 但不同 head 的 ≥2 个 unit（契约 §3.3 优先于卡面措辞）。
2. **组键与组内 unit 顺序都是确定的**（§0 D6）：组按 ``(kind, key)`` 升序，组内 unit 按
   ``(card, pr)`` 升序；``batch_id`` 只吃组内 head_sha 排序后的集合，与 plan 里的书写顺序无关。
3. **组级检查跑在组首 unit 的 head_sha 上**（§3.6）：同 tree ⇒ 同内容，首 unit 取排序后的第一个。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

# bandit B404 假阳性：只用于调用固定的 git 可执行文件（见 _git / _git_raw）与 plan 里的检查命令，
# 调用形式一律 argv 列表、shell=False，不拼 shell 字符串
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from team_board import gatelog

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
    "SHORT_SHA_LEN",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "Group",
    "GroupRun",
    "Plan",
    "Unit",
    "UnitResult",
    "VerifyBatchError",
    "batch_id_for",
    "build_groups",
    "load_plan",
    "patch_id",
    "render_summary",
    "resolve_head",
    "run_groups",
    "write_batch_json",
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

#: 逐 unit 的两种判定（§0 D3；只有这两种，没有「未知」）
VERDICT_PASS = "PASS"  # nosec B105 —— 判定文案，不是口令
VERDICT_FAIL = "FAIL"  # nosec B105 —— 同上，成对出现

#: 摘要里的短 sha 宽度（§4 示例 ``head=af45471`` 是 7 位；固定宽度才与 ``git`` 配置无关、可复现）
SHORT_SHA_LEN = 7

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


@dataclass(frozen=True)
class UnitResult:
    """一个 unit 的判定（§0 D3）：组级失败打全组，unit 自检失败只打自己。

    ``rc`` 是**导致这个 verdict 的**那次检查的退出码：PASS 恒 0；``failed_check`` 以 ``group:``
    开头时是组级检查的 rc，以 ``unit:`` 开头时是该 unit 自己那条检查的 rc。
    """

    card: str
    pr: int
    head_sha: str
    batch_id: str
    verdict: str
    rc: int
    failed_check: str | None


@dataclass(frozen=True)
class GroupRun:
    """一个组的执行结果。

    前四个字段是契约 §2 逐字规定的；后三个是 E2 执行层不得已的扩展（都带缺省值，
    用契约的四参数构造仍然成立）：

    - ``worktree``：``--keep-worktrees`` 时保留的临时 worktree 绝对路径，删掉时为 ``None``，
      否则 §4「保留并打进摘要」无处取路径；
    - ``failed_lines`` / ``failed_total``：日志里按 :mod:`team_board.gatelog` 口径挑出的失败行
      （已截断到 ≤20 行）与命中总数，供 :func:`render_summary` 做**纯函数**渲染 ——
      否则渲染器得回读日志文件，摘要就依赖磁盘状态了。
    """

    group: Group
    group_rc: int
    log_path: str
    results: tuple[UnitResult, ...]
    worktree: str | None = None
    failed_lines: tuple[str, ...] = ()
    failed_total: int = 0


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

#: 会**注入 git 配置**的环境变量（无文件也能改配置）：``GIT_CONFIG_COUNT`` 加
#: ``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>`` 成对使用，``GIT_CONFIG_PARAMETERS``
#: 是同一机制的内部展开形态。宿主里一个 ``color.ui=always`` 就能让 ``git diff`` 吐着色输出、
#: 让 ``patch-id`` 无输出 —— 见 :func:`_git_env`。
_GIT_CONFIG_ENV = frozenset({"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"})
_GIT_CONFIG_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

#: ``git diff`` 前必须钉死的全局配置（``-c`` 必须写在子命令**之前**）：
#: 输出不许着色（``patch-id`` 解析不了 ANSI 转义），改名检测必须开着
#: （``diff.renames`` 只有 true/false 两种，关了同一份改动会算成 delete+add 两个补丁）。
_DIFF_CONFIG = ("-c", "color.ui=false", "-c", "diff.renames=true")

#: ``git diff`` 的固定开关：``--no-color`` 兜住 ``color.ui=always`` 的残留、
#: ``--no-ext-diff`` 挡 ``GIT_EXTERNAL_DIFF`` / ``diff.external`` 换掉 diff 实现、
#: ``--no-textconv`` 挡 ``.gitattributes`` 里的 textconv 驱动程序改写内容形态。
_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--binary")


def _git_env() -> dict[str, str]:
    """``os.environ`` 的一份副本，剔掉 :data:`_GIT_LOCATION_ENV` / :data:`_GIT_CONFIG_ENV`。

    ``subprocess.run(env=...)`` 传 ``None`` 等于继承父进程全部环境（含 ``GIT_DIR``），
    传 ``{}`` 又等于清空（``PATH`` 没了）；所以这里显式构造「剔除后的副本」。

    第二类剔除是**配置注入**（pm 复审 P2）：``GIT_CONFIG_COUNT=1`` + ``GIT_CONFIG_KEY_0`` /
    ``GIT_CONFIG_VALUE_0`` 能在不落地任何文件的前提下把 ``color.ui=always`` 塞进 git，
    着色后的 diff 喂给 ``patch-id`` 直接**没有输出**，于是所有 unit 静默降级成 solo/head 组、
    整条命令 rc0 放行 —— 配置写错却看不出来。``GIT_CONFIG_PARAMETERS`` 是同一机制的内部形态，
    一并剔除；``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` 属于「读哪个配置文件」，不清洗
    （用例靠 ``GIT_CONFIG_GLOBAL=os.devnull`` 屏蔽宿主全局配置，清了就把那层保护也拆掉）。
    """

    return {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_LOCATION_ENV
        and key not in _GIT_CONFIG_ENV
        and not key.startswith(_GIT_CONFIG_ENV_PREFIXES)
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

    try:
        text = repr(value)
    except ValueError:  # pragma: no cover - 只在 int 位数超 sys.int_info 上限（3.11+）时
        # 超长 int 的 ``repr`` 自己会抛 ValueError；回显路径不许把格式化错误漏出去。
        text = f"<{type(value).__name__}，位数过多无法回显>"
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
        raise _err(where, f"pr 必须是整数，不接受布尔值：{_echo(value)}")
    if not isinstance(value, int):
        raise _err(
            where, f"pr 必须是整数（不接受 {type(value).__name__} 形态）：{_echo(value)}"
        )
    if value < 1 or value > MAX_PR:
        raise _err(where, f"pr 必须在 1..{MAX_PR} 之间：{_echo(value)}")
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


def _resolve_commit(
    repo: str | os.PathLike[str], rev: str, what: str
) -> str:
    """``git rev-parse <rev>^{commit}`` 取 40 位 sha；解析不出来 → ``VerifyBatchError`` 点名 ``what``。

    fail-closed 的唯一落点：``what`` 是 ``head`` / ``base``，错误消息里必须带出来，
    否则 base 写错时用户只能看到「某个 unit 比不出来」，看不出是配置的问题。
    """

    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {proc.returncode}"
        raise VerifyBatchError(
            f"{what} {_echo(rev)} 解析失败（不是本仓库里的提交）: {tail}"
        )
    return sha


def resolve_head(repo: str | os.PathLike[str], rev: str) -> str:
    """``git rev-parse <rev>^{commit}`` 取 40 位 head sha；失败 → ``VerifyBatchError``。

    失败是**整条命令**失败（§3.1：不静默跳过），不是「这个 unit 单独降级」。
    """

    if not isinstance(rev, str) or not rev.strip():
        raise VerifyBatchError("head 不许为空串")
    return _resolve_commit(repo, rev, "head")


def _head_tree(repo: str | os.PathLike[str], head_sha: str) -> str:
    """``git rev-parse <head_sha>^{tree}`` 取 tree sha —— **组键的唯一来源**。

    同 tree = 工作区内容逐字相同（这才是「同内容」，``patch-id`` 会剥空白）。
    head 已经 ``resolve_head`` 过了，这里再失败就是环境级的怪状态，按环境错误抛。
    """

    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{head_sha}^{{tree}}")
    tree = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", tree):
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"退出码 {proc.returncode}"
        raise VerifyBatchError(
            f"head {_echo(head_sha)} 的 tree 解析失败（组键取不到，不许猜）: {tail}"
        )
    return tree


def patch_id(repo: str | os.PathLike[str], base: str, head_sha: str) -> str | None:
    """``merge-base`` → ``diff --binary`` → ``patch-id --stable`` 的首要字段。

    **返回值只用于展示/日志**：分组键是 ``<head>^{tree}``（见 :func:`build_groups`），
    调用方不许拿它判「同内容」——它剥空白，``X = 2`` 与 `` X = 2`` 会算出同一个值。

    两类结果要分清：

    - ``base`` 自己解析不出来（写错 / 没 fetch）→ ``VerifyBatchError``，**点名 base**，
      整条命令失败。旧行为是返回 ``None``，于是全体 unit 静默降级成 solo/head 组、rc0
      放行 —— 配置写错却看起来「跑通了」（pm 复审 P2）。
    - base 能解析但比不出来（``merge-base`` 找不到共同祖先、diff 为空）→ ``None``，
      属于「这条改动算不出补丁」，由调用方按 D5 处理，不抛异常。
    """

    base_sha = _resolve_commit(repo, base, "base")
    merge_base = _git(repo, "merge-base", base_sha, head_sha)
    if merge_base.returncode != 0:
        return None
    ancestor = merge_base.stdout.strip()
    if not ancestor:
        return None

    diff = _git_raw(repo, *_DIFF_CONFIG, "diff", *_DIFF_FLAGS, ancestor, head_sha)
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

    组键是 ``tree:<head 的 tree sha>``（pm 在返工卡 ``t_600d3378`` 定的判据）：
    plan 里所有 unit 共用同一个 ``base``，所以「同 tree」⇔「改动逐字相同」；
    ``patch-id`` 会剥空白，同 patch-id 但 tree 不同的两个 head（``X = 2`` / `` X = 2``）
    必须分成两组，否则组级检查只跑在组首 unit 的 head 上，语法坏的那个白拿 PASS。
    ``Group.patch_id`` 仍然填（组首 unit 的 patch-id），**只作展示/日志**。

    顺序：

    1. ``base`` 先解析一次（失败 → ``VerifyBatchError`` 点名 base，整条命令失败）；
    2. 每个 unit ``resolve_head`` + 取 tree + 算 patch-id（head 失败 → 整条命令失败）；
    3. 按 ``tree:<tree>`` 分桶；组内 unit 按 ``(card, pr)`` 升序，组首 unit 的 head_sha
       就是组级检查的落点（§3.6）；
    4. 组内 unit 数 <2 → ``kind="solo"``；≥2 且 head_sha 全同 → ``kind="head"``；
       ≥2 且 head 不全同（同内容、不同提交）→ ``kind="patch"``；
    5. 组按 ``(kind, key)`` 升序返回。
    """

    # base 在这里再校验一次：``Plan`` 是公开 dataclass，调用方可以绕开 ``load_plan``
    # 手搓一个（pm 的复审探针就是这么构造的）。不校验就会把 ``--help`` / NUL 送进 git。
    _as_base(plan.base, "base")
    # 解析不出来就整条命令失败：旧行为是「静默全体 solo + rc0」，配置写错却看起来跑通了。
    _resolve_commit(repo, plan.base, "base")

    resolved: list[tuple[Unit, str, str, str | None]] = []
    for unit in plan.units:
        head_sha = resolve_head(repo, unit.head)
        tree = _head_tree(repo, head_sha)
        resolved.append((unit, head_sha, tree, patch_id(repo, plan.base, head_sha)))

    buckets: dict[str, list[tuple[Unit, str, str, str | None]]] = {}
    for item in resolved:
        buckets.setdefault(f"tree:{item[2]}", []).append(item)

    groups: list[Group] = []
    for key, items in buckets.items():
        ordered = sorted(items, key=lambda item: _unit_order(item[0]))
        units = tuple(item[0] for item in ordered)
        if len(units) < 2:
            kind = KIND_SOLO
        elif len({item[1] for item in ordered}) == 1:
            kind = KIND_HEAD
        else:
            kind = KIND_PATCH
        groups.append(
            Group(
                batch_id=batch_id_for([item[1] for item in ordered]),
                kind=kind,
                key=key,
                patch_id=ordered[0][3],
                head_sha=ordered[0][1],
                units=units,
            )
        )

    groups.sort(key=lambda group: (group.kind, group.key))
    return tuple(groups)


# --------------------------------------------------------------------------- 执行层（§4、§5）
#
# 判定规则（§0 D3，一个字都不许改）：
#   组级检查任一 rc≠0 → 该组**每个** unit 都 FAIL，failed_check = ``group:<argv>``；
#   unit 自检任一 rc≠0 → **只有该 unit** FAIL，failed_check = ``unit:<argv>``，同组其它 unit 不受影响；
#   组内一个 unit 失败不影响别的 unit。所以「一个 PR 红、其余绿」靠 unit 自检，不靠组级检查。


#: 账本 / 日志文件名里的 batch_id 形态（``b-`` + 8 位小写十六进制，见 :func:`batch_id_for`）
_BATCH_ID_RE = re.compile(r"^b-[0-9a-f]{8}$")


def _check_batch_id(batch_id: object) -> str:
    """``batch_id`` 要拼进文件名，落盘前先卡形态（同时拦住 ``../`` 之类的路径穿越）。"""

    if not isinstance(batch_id, str) or not _BATCH_ID_RE.match(batch_id):
        raise VerifyBatchError(f'batch_id 形态非法（须为 b-[0-9a-f]{{8}}）："{batch_id}"')
    return batch_id


def _short(sha: str) -> str:
    """摘要里的短 sha（固定 7 位，与 ``git`` 的 abbrev 配置无关 → 可复现）。"""

    return sha[:SHORT_SHA_LEN]


def _log_path(log_dir: str | os.PathLike[str], batch_id: str) -> str:
    return os.path.abspath(os.path.join(os.fspath(log_dir), f"batch-{_check_batch_id(batch_id)}.log"))


def _batch_json_path(path_dir: str | os.PathLike[str], batch_id: str) -> str:
    """账本路径；:func:`write_batch_json` 与 :func:`render_summary` 共用这一处命名。"""

    return os.path.abspath(
        os.path.join(os.fspath(path_dir), f"batch-{_check_batch_id(batch_id)}.json")
    )


def _ensure_dir(path: str | os.PathLike[str]) -> None:
    try:
        os.makedirs(os.fspath(path), exist_ok=True)
    except OSError as exc:
        raise VerifyBatchError(f"建不了目录 {os.fspath(path)}: {exc}") from exc


def _write_text(path: str, text: str) -> None:
    """写文本文件（UTF-8、``\\n`` 换行、末尾保证一个换行）；失败 → ``VerifyBatchError``（rc2）。"""

    payload = text if text.endswith("\n") else text + "\n"
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    except OSError as exc:
        raise VerifyBatchError(f"写不进文件 {path}: {exc}") from exc


def _worktree_add(repo: str | os.PathLike[str], worktree: Path, head_sha: str) -> None:
    """``git worktree add --detach <worktree> <head_sha>``；失败 → ``VerifyBatchError``（rc2）。"""

    proc = _git(repo, "worktree", "add", "--detach", os.fspath(worktree), head_sha)
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else f"退出码 {proc.returncode}"
        raise VerifyBatchError(f"worktree 建不起来（{worktree}）: {tail}")


def _worktree_remove(repo: str | os.PathLike[str], worktree: Path) -> None:
    """``git worktree remove --force <worktree>``；失败 → ``VerifyBatchError``。"""

    proc = _git(repo, "worktree", "remove", "--force", os.fspath(worktree))
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else f"退出码 {proc.returncode}"
        raise VerifyBatchError(f"worktree 删不掉（{worktree}）: {tail}")


def _run_check(
    argv: Sequence[str], worktree: Path, pieces: list[str], *, label: str
) -> int:
    """在 ``worktree`` 里跑一条检查（argv 列表、``shell=False``），原始输出追加进 ``pieces``。

    返回子进程 rc。可执行文件不存在 → 127、其它 ``OSError`` → 126，都只记进日志，
    不抛异常（一条坏命令不该把整批打成 rc2 —— 它是「这条检查失败」，判定归 D3）。
    """

    pieces.append(f"--- {label}: {' '.join(argv)}  (cwd={worktree}) ---")
    try:
        # bandit B603/B607 假阳性：argv 来自 plan（非空字符串数组、不经 shell），
        # 可执行文件名由 plan 给全（通常是 sys.executable 或工具名，走 PATH 与逐卡验证一致）
        proc = subprocess.run(  # nosec B603 B607
            list(argv),
            cwd=os.fspath(worktree),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=_git_env(),
        )
    except FileNotFoundError:
        pieces.append(f"<无法执行：找不到可执行文件 {argv[0]}>")
        pieces.append(f"--- {label}: rc=127 ---")
        return 127
    except OSError as exc:
        pieces.append(f"<无法执行: {exc}>")
        pieces.append(f"--- {label}: rc=126 ---")
        return 126

    raw = (proc.stdout or "") + (proc.stderr or "")
    if raw:
        pieces.append(raw.rstrip("\n"))
    pieces.append(f"--- {label}: rc={proc.returncode} ---")
    return proc.returncode


def _digest(text: str) -> tuple[tuple[str, ...], int]:
    """失败行摘要：``(≤20 条已列行, 第一轮命中总数)``。

    直接复用 :mod:`team_board.gatelog` 的选行实现（本仓失败行口径的唯一来源）——
    跨模块取私有名这件事是刻意的：另写一份就等于两个口径。

    只认第一轮（含 ``FAILED`` / ``ERROR`` / ``error:`` / ``错误:`` / ``Traceback`` / 行首 ``E   `` 的行）；
    gatelog 里「第一轮零命中就退回末尾 N 行」的兜底在这里**不用** —— 那是把原始输出搬上 stdout
    （§4 要求 stdout 只回摘要），零命中就老老实实 ``0/0``，细节看日志。
    """

    shown, from_first_round, total = gatelog._select(text, gatelog.FAIL_LINE_LIMIT)
    if not from_first_round:
        return (), 0
    return tuple(shown), total


def run_groups(
    repo: str | os.PathLike[str],
    groups: Sequence[Group],
    *,
    log_dir: str | os.PathLike[str] = gatelog.LOG_DIR_DEFAULT,
    group_checks: Sequence[Sequence[str]] = (),
    keep_worktrees: bool = False,
) -> tuple[GroupRun, ...]:
    """逐组执行（§4）：每组一个临时 worktree → 组级检查跑一次 → unit 自检逐条跑。

    参数（契约 §2 的 ``run_groups(repo, groups, *, log_dir)`` 之外，那两个关键字参数是
    **必须**的补充，否则 E3 无法接线）：

    - ``group_checks``：plan 的 ``group_checks``（§4「组级检查 = plan 的 group_checks」）。
      契约的 ``Group`` 不携带检查命令，只能由调用方传进来；缺省 = 不跑组级检查。
    - ``keep_worktrees``：``--keep-worktrees`` 时不删临时 worktree，路径进 :class:`GroupRun`
      并由 :func:`render_summary` 打进摘要。

    行为细节：

    - 组级检查**全部跑完**（任一失败也继续跑后面的），``group_rc`` 记第一条非 0 的 rc ——
      为了诊断，不是为了「整批绿」；
    - unit 自检在该 unit **所属组的** worktree 里跑（§4）；组级已失败时 unit 自检仍跑（日志留证），
      但判定按 D3 一律 FAIL、``failed_check`` 报组级那条；
    - 全量输出按写入顺序进 ``<log_dir>/batch-<batch_id>.log``；返回 :class:`GroupRun` 只带摘要，
      调用方拿它渲染 stdout（原始输出不上 stdout）。

    环境级失败一律 :class:`VerifyBatchError`（CLI 层 rc2）：建不了日志目录、worktree 建不起来 /
    删不掉、日志写不进、``batch_id`` 形态非法。
    """

    checks = tuple(tuple(argv) for argv in group_checks)
    _ensure_dir(log_dir)
    tmp_root = Path(tempfile.mkdtemp(prefix="verify-batch-"))
    runs: list[GroupRun] = []
    try:
        for group in groups:
            _check_batch_id(group.batch_id)
            worktree = tmp_root / f"vb-{group.batch_id}"
            _worktree_add(repo, worktree, group.head_sha)

            pieces = [
                f"===== batch {group.batch_id} kind={group.kind} key={group.key} "
                f"head={group.head_sha} units={len(group.units)} ====="
            ]
            group_rc = 0
            group_failed: tuple[str, ...] | None = None
            results: list[UnitResult] = []
            removal_error: VerifyBatchError | None = None
            try:
                for argv in checks:
                    rc = _run_check(argv, worktree, pieces, label="group")
                    if rc != 0 and group_failed is None:
                        group_rc, group_failed = rc, argv

                for unit in group.units:
                    head_sha = resolve_head(repo, unit.head)
                    unit_rc = 0
                    unit_failed: tuple[str, ...] | None = None
                    for argv in unit.checks:
                        rc = _run_check(argv, worktree, pieces, label=f"unit {unit.card}")
                        if rc != 0 and unit_failed is None:
                            unit_rc, unit_failed = rc, argv

                    if group_failed is not None:
                        verdict, failed_check, final_rc = (
                            VERDICT_FAIL,
                            f"group:{' '.join(group_failed)}",
                            group_rc,
                        )
                    elif unit_failed is not None:
                        verdict, failed_check, final_rc = (
                            VERDICT_FAIL,
                            f"unit:{' '.join(unit_failed)}",
                            unit_rc,
                        )
                    else:
                        verdict, failed_check, final_rc = VERDICT_PASS, None, 0

                    results.append(
                        UnitResult(
                            card=unit.card,
                            pr=unit.pr,
                            head_sha=head_sha,
                            batch_id=group.batch_id,
                            verdict=verdict,
                            rc=final_rc,
                            failed_check=failed_check,
                        )
                    )
            finally:
                if not keep_worktrees:
                    try:
                        _worktree_remove(repo, worktree)
                    except VerifyBatchError as exc:
                        # 清理失败不许盖掉真正的失败原因：先记下，正常跑完再抛
                        removal_error = exc
            if removal_error is not None:
                raise removal_error

            log_path = _log_path(log_dir, group.batch_id)
            log_text = "\n".join(pieces)
            _write_text(log_path, log_text)
            failed_lines, failed_total = _digest(log_text)

            runs.append(
                GroupRun(
                    group=group,
                    group_rc=group_rc,
                    log_path=log_path,
                    results=tuple(results),
                    worktree=os.fspath(worktree) if keep_worktrees else None,
                    failed_lines=failed_lines,
                    failed_total=failed_total,
                )
            )
    finally:
        # 临时根目录只在「不保留」时整个删掉；保留时连目录一起留给 --keep-worktrees 的排查用
        if not keep_worktrees:
            shutil.rmtree(tmp_root, ignore_errors=True)

    return tuple(runs)


def write_batch_json(
    path_dir: str | os.PathLike[str],
    runs: Sequence[GroupRun],
    *,
    base: str = DEFAULT_BASE,
) -> tuple[str, ...]:
    """每组一个账本 ``<path_dir>/batch-<batch_id>.json``（§5），返回写的文件绝对路径。

    ``base`` 是 plan 的 base（§5 的 ``"base"`` 字段）：``GroupRun`` 不携带它，同 :func:`run_groups`
    的 ``group_checks`` 一样由调用方补齐，缺省 ``origin/main``。

    序列化口径：``sort_keys=True``、``ensure_ascii=False``、缩进 2、末尾换行；
    ``created_at`` 取写盘那一刻的 epoch 秒 —— 除它之外，两次跑逐字相同（§0 D6）。
    """

    _ensure_dir(path_dir)
    paths: list[str] = []
    for run in runs:
        path = _batch_json_path(path_dir, run.group.batch_id)
        payload = {
            "batch_id": run.group.batch_id,
            "kind": run.group.kind,
            "key": run.group.key,
            "base": base,
            "head_sha": run.group.head_sha,
            "created_at": int(time.time()),
            "group_rc": run.group_rc,
            "units": [
                {
                    "card": result.card,
                    "pr": result.pr,
                    "head_sha": result.head_sha,
                    "verdict": result.verdict,
                    "rc": result.rc,
                    "failed_check": result.failed_check,
                    "log": run.log_path,
                }
                for result in run.results
            ],
        }
        _write_text(path, json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2))
        paths.append(path)
    return tuple(paths)


def render_summary(
    runs: Sequence[GroupRun], *, batch_dir: str | os.PathLike[str] = gatelog.LOG_DIR_DEFAULT
) -> str:
    """渲染 stdout 摘要（§4，不带尾随换行）：**只有摘要行，没有一行原始输出**。

    结构：每组一段（``batch:`` / ``  group: exit: … 失败行（n/m，≤20）:`` / 若干 ``  unit:`` 行 /
    solo 组的 ``degraded:`` 行 / 保留 worktree 时的 ``worktree:`` 行），然后一行 ``counts:``，
    最后每个组一对 ``log:`` / ``batch-json:``。

    ``batch_dir`` 必须与 :func:`write_batch_json` 的 ``path_dir`` 一致 —— 账本路径由
    :func:`_batch_json_path` 同一处算出（契约不把账本路径放进 :class:`GroupRun`）。
    """

    lines: list[str] = []
    groups_count = units_count = pass_count = fail_count = 0

    for run in runs:
        group = run.group
        groups_count += 1
        lines.append(
            f"batch: {group.batch_id}  kind={group.kind}  head={_short(group.head_sha)}  "
            f"units={len(group.units)}"
        )
        lines.append(
            f"  group: exit:{run.group_rc}  "
            f"失败行（{len(run.failed_lines)}/{run.failed_total}，≤{gatelog.FAIL_LINE_LIMIT}）:"
        )
        lines.extend(f"    {line}" for line in run.failed_lines)
        for result in run.results:
            units_count += 1
            if result.verdict == VERDICT_PASS:
                pass_count += 1
            else:
                fail_count += 1
            lines.append(
                f"  unit: {result.card} pr={result.pr} verdict={result.verdict}  "
                f"failed_check={result.failed_check or '-'}"
            )
        # §0 D4 / pm 2026-10-31 口径：degraded 只在 solo（= 组内 unit 数 <2）时打，
        # 不按「patch_id 为 None」判
        if group.kind == KIND_SOLO and len(group.units) < 2:
            lines.append(f"degraded: {group.batch_id} 单 unit 走逐卡流程")
        if run.worktree:
            lines.append(f"worktree: {run.worktree}")

    lines.append(
        f"counts: groups={groups_count} units={units_count} pass={pass_count} fail={fail_count}"
    )
    for run in runs:
        lines.append(f"log: {run.log_path}")
        lines.append(f"batch-json: {_batch_json_path(batch_dir, run.group.batch_id)}")
    return "\n".join(lines)
