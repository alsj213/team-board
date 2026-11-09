"""管理面漂移检查：仓库 ``ops/`` 是唯一源，安装件在 ``~/.hermes`` 与 systemd 目录。

三件事：

1. :func:`check` —— 比对源与安装件的字节/权限，再比对 crontab、systemd timer、
   hermes cron 任务清单；只读。
2. :func:`install` —— 从仓库写安装件；目标已存在且内容不同时拒绝覆盖，除非
   ``force=True``（覆盖前先备份到 ``~/.cache/team-board/mgmt-backup``）。
3. :func:`capture` —— 反向把安装件写回仓库，供运维在线上改完规则后落盘。

``check`` 的 rc 由 CLI 层给：0 一致 / 1 有漂移 / 2 环境错误。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404 - 只跑固定的 `crontab -l`，argv 直传
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "CheckReport",
    "CrontabEntry",
    "Drift",
    "ManagedFile",
    "MgmtError",
    "capture",
    "check",
    "check_files",
    "install",
    "load_hooks",
    "load_manifest",
    "load_schedules",
    "parse_crontab",
    "render",
    "render_json",
    "repo_root",
    "unmanaged_scripts",
]

MANIFEST_PATH = "ops/manifest.json"
SCHEDULES_PATH = "ops/schedules.json"
HOOKS_PATH = "ops/hooks.json"
SCRIPTS_DIR = "ops/scripts"
SYSTEMD_DIR = "ops/systemd"


class MgmtError(Exception):
    """manifest 坏了、源缺失或目标冲突；CLI 一律 rc2。"""


@dataclass(frozen=True)
class ManagedFile:
    """一条安装映射。``source`` 相对仓库根，``target`` 已展开到绝对路径。"""

    source: Path
    target: Path
    mode: int
    source_label: str
    target_label: str


@dataclass(frozen=True)
class Drift:
    """一条漂移；``code`` 稳定，``subject`` 是人能定位的对象。"""

    code: str
    subject: str
    detail: str = ""


@dataclass(frozen=True)
class CrontabEntry:
    """看护件条目：``guard_run.sh <guard> <period> <interpreter> <script> [args]``。"""

    guard: str
    period: int
    script: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class CheckReport:
    """一次完整检查的结果。"""

    root: Path
    home: Path
    checked_files: int
    file_drifts: tuple[Drift, ...]
    schedule_drifts: tuple[Drift, ...]
    errors: tuple[str, ...]
    schedules_checked: bool

    @property
    def ok(self) -> bool:
        return not (self.file_drifts or self.schedule_drifts or self.errors)


def repo_root() -> Path:
    """默认仓库根：``TEAM_BOARD_ROOT`` 优先，否则按包位置反推（``<root>/src/...``）。"""

    env = os.environ.get("TEAM_BOARD_ROOT")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2]


def _parse_mode(value: object, source: str) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 8)
        except ValueError as exc:
            raise MgmtError(f"{source}: mode 不是八进制串: {value!r}") from exc
    raise MgmtError(f"{source}: mode 必须是八进制串或整数")


def load_manifest(root: Path, home: Path) -> tuple[ManagedFile, ...]:
    """读 ``ops/manifest.json``，并强制 scripts/systemd 下的每个文件都登记在册。

    漏登记的文件不会被安装，也不会被检查 —— 与「唯一源」相反，所以直接拒绝。
    """

    path = root / MANIFEST_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MgmtError(f"读不到 {path}: {exc}") from exc
    except ValueError as exc:
        raise MgmtError(f"{path} 不是合法 JSON: {exc}") from exc
    rows = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise MgmtError(f"{path}: files 必须是非空数组")

    files: list[ManagedFile] = []
    seen_sources: set[Path] = set()
    seen_targets: set[Path] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise MgmtError(f"{path}: files[{index}] 必须是对象")
        raw_source = row.get("source")
        raw_target = row.get("target")
        if not isinstance(raw_source, str) or not raw_source:
            raise MgmtError(f"{path}: files[{index}].source 必须是非空字符串")
        if not isinstance(raw_target, str) or not raw_target.startswith("~/"):
            raise MgmtError(f"{path}: files[{index}].target 必须以 ~/ 开头")
        source = Path(raw_source)
        if source.is_absolute() or ".." in source.parts:
            raise MgmtError(f"{path}: files[{index}].source 不许绝对路径或 ..: {raw_source}")
        target = home / raw_target[2:]
        if source in seen_sources:
            raise MgmtError(f"{path}: source 重复: {raw_source}")
        if target in seen_targets:
            raise MgmtError(f"{path}: target 重复: {raw_target}")
        seen_sources.add(source)
        seen_targets.add(target)
        files.append(
            ManagedFile(
                source=root / source,
                target=target,
                mode=_parse_mode(row.get("mode", "0644"), f"{path}: files[{index}]"),
                source_label=raw_source,
                target_label=raw_target,
            )
        )

    for directory in (SCRIPTS_DIR, SYSTEMD_DIR):
        base = root / directory
        if not base.is_dir():
            raise MgmtError(f"缺目录 {base}")
        for candidate in sorted(base.rglob("*")):
            if not candidate.is_file() or "__pycache__" in candidate.parts:
                continue
            relative = candidate.relative_to(root)
            if relative not in seen_sources:
                raise MgmtError(f"{path}: {relative} 未登记（先加 manifest 再提交）")

    return tuple(files)


def load_schedules(root: Path) -> dict:
    """读 ``ops/schedules.json``；结构错误一律 :class:`MgmtError`。"""

    path = root / SCHEDULES_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MgmtError(f"读不到 {path}: {exc}") from exc
    except ValueError as exc:
        raise MgmtError(f"{path} 不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MgmtError(f"{path}: 顶层必须是对象")
    for key in ("crontab", "systemd_timers", "hermes_jobs"):
        if key not in payload:
            raise MgmtError(f"{path}: 缺 {key}")
    if not isinstance(payload["crontab"], list):
        raise MgmtError(f"{path}: crontab 必须是数组")
    if not isinstance(payload["systemd_timers"], list):
        raise MgmtError(f"{path}: systemd_timers 必须是数组")
    if not isinstance(payload["hermes_jobs"], dict):
        raise MgmtError(f"{path}: hermes_jobs 必须是对象")
    return payload


def load_hooks(root: Path) -> list[dict]:
    """读 ``ops/hooks.json``：角色硬边界 hook 的注册期望。"""

    path = root / HOOKS_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MgmtError(f"读不到 {path}: {exc}") from exc
    except ValueError as exc:
        raise MgmtError(f"{path} 不是合法 JSON: {exc}") from exc
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, list):
        raise MgmtError(f"{path}: hooks 必须是数组")
    for index, row in enumerate(hooks):
        if not isinstance(row, dict) or not isinstance(row.get("profile"), str):
            raise MgmtError(f"{path}: hooks[{index}] 缺 profile")
        for key in ("event", "script"):
            if not isinstance(row.get(key), str):
                raise MgmtError(f"{path}: hooks[{index}] 缺 {key}")
    return hooks


def check_hooks(hooks: list[dict], home: Path) -> tuple[Drift, ...]:
    """``config.yaml`` 是运行时配置，不整份版本化；只查硬边界 hook 还注册着。

    文本级匹配足够发现「hook 被删/被改成 fail_closed: false」，不做 YAML 解析，
    避免为一个字段引入依赖。
    """

    drifts: list[Drift] = []
    for row in hooks:
        profile = row["profile"]
        path = home / ".hermes" / "profiles" / profile / "config.yaml"
        subject = f"{profile}/{row['event']}:{row['script']}"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            drifts.append(Drift("hook-config-missing", subject, f"读不到 {path}"))
            continue
        if row["script"] not in text:
            drifts.append(Drift("hook-missing", subject, "config.yaml 里没有这个脚本"))
            continue
        if row.get("fail_closed") and not re.search(r"fail_closed:\s*true", text):
            drifts.append(Drift("hook-drift", subject, "fail_closed 不是 true"))
        matcher = row.get("matcher")
        if matcher and f'matcher: {matcher}' not in text and f'matcher: "{matcher}"' not in text:
            drifts.append(Drift("hook-drift", subject, f"matcher 不是 {matcher}"))
    return tuple(drifts)


def _same_content(left: Path, right: Path) -> bool:
    try:
        return left.read_bytes() == right.read_bytes()
    except OSError as exc:
        raise MgmtError(f"读文件失败: {exc}") from exc


def check_files(files: tuple[ManagedFile, ...]) -> tuple[Drift, ...]:
    """比对源与安装件；只报差异，不写任何东西。"""

    drifts: list[Drift] = []
    for item in files:
        if not item.source.is_file():
            drifts.append(
                Drift("source-missing", item.source_label, f"源文件不存在: {item.source}")
            )
            continue
        if not item.target.is_file():
            drifts.append(
                Drift("target-missing", item.target_label, f"安装件不存在: {item.target}")
            )
            continue
        if not _same_content(item.source, item.target):
            drifts.append(
                Drift("content-drift", item.target_label, f"与源不同: {item.source_label}")
            )
            continue
        actual_mode = stat.S_IMODE(item.target.stat().st_mode)
        if actual_mode != item.mode:
            drifts.append(
                Drift(
                    "mode-drift",
                    item.target_label,
                    f"权限是 {actual_mode:04o}，期望 {item.mode:04o}",
                )
            )
    return tuple(drifts)


#: 纳入管理面的 profile；安装目录里多出脚本也要报，防止旧副本继续被 cron 调用。
MANAGED_PROFILES = ("ops", "paimeng")


def unmanaged_scripts(
    files: tuple[ManagedFile, ...], home: Path
) -> tuple[Drift, ...]:
    """安装目录里未纳入 manifest 的脚本；``.bak`` 与 ``__pycache__`` 跳过。"""

    targets = {item.target for item in files}
    drifts: list[Drift] = []
    for profile in MANAGED_PROFILES:
        directory = home / ".hermes" / "profiles" / profile / "scripts"
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or ".bak" in path.name:
                continue
            if path in targets:
                continue
            drifts.append(
                Drift(
                    "script-unmanaged",
                    f"~/.hermes/profiles/{profile}/scripts/{path.relative_to(directory)}",
                    "安装目录里多出来的脚本（未纳入 ops/）",
                )
            )
    return tuple(drifts)


def _write_atomic(target: Path, data: bytes, mode: int) -> None:
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(data)
        temp.chmod(mode)
        os.replace(temp, target)
    except OSError as exc:
        raise MgmtError(f"写 {target} 失败: {exc}") from exc
    finally:
        if temp.exists():
            temp.unlink()


def _backup(target: Path, home: Path, stamp: str) -> Path:
    try:
        relative = target.relative_to(home)
    except ValueError:
        relative = Path(target.name)
    destination = home / ".cache" / "team-board" / "mgmt-backup" / stamp / relative
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)
    except OSError as exc:
        raise MgmtError(f"备份 {target} 失败: {exc}") from exc
    return destination


def install(
    files: tuple[ManagedFile, ...],
    home: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[tuple[str, str], ...]:
    """把源写到安装件；返回 ``(动作, 安装件)``，动作 ∈ written / same / would-write。"""

    for item in files:
        if not item.source.is_file():
            raise MgmtError(f"源文件不存在: {item.source}")

    conflicts = [
        item
        for item in files
        if item.target.is_file() and not _same_content(item.source, item.target)
    ]
    if conflicts and not force:
        labels = "、".join(item.target_label for item in conflicts[:5])
        more = f" 等 {len(conflicts)} 件" if len(conflicts) > 5 else ""
        raise MgmtError(f"目标已存在且内容不同，拒绝覆盖（先 capture 或加 --force）：{labels}{more}")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    actions: list[tuple[str, str]] = []
    for item in files:
        same = item.target.is_file() and _same_content(item.source, item.target)
        if same and stat.S_IMODE(item.target.stat().st_mode) == item.mode:
            actions.append(("same", item.target_label))
            continue
        if dry_run:
            actions.append(("would-write", item.target_label))
            continue
        if item.target.is_file() and not same:
            _backup(item.target, home, stamp)
        _write_atomic(item.target, item.source.read_bytes(), item.mode)
        actions.append(("written", item.target_label))
    return tuple(actions)


def capture(
    files: tuple[ManagedFile, ...], *, dry_run: bool = False
) -> tuple[tuple[str, str], ...]:
    """把安装件写回仓库；返回 ``(动作, 源)``，动作 ∈ updated / same / would-update。"""

    actions: list[tuple[str, str]] = []
    for item in files:
        if not item.target.is_file():
            raise MgmtError(f"安装件不存在，无法 capture: {item.target}")
        if item.source.is_file() and _same_content(item.source, item.target):
            actions.append(("same", item.source_label))
            continue
        if dry_run:
            actions.append(("would-update", item.source_label))
            continue
        _write_atomic(item.source, item.target.read_bytes(), item.mode)
        actions.append(("updated", item.source_label))
    return tuple(actions)


def parse_crontab(text: str) -> tuple[CrontabEntry, ...]:
    """只抽 ``guard_run.sh`` 行；其它 cron 行归别的团队，不纳入管理。"""

    entries: list[CrontabEntry] = []
    for line in text.splitlines():
        if "guard_run.sh" not in line or line.lstrip().startswith("#"):
            continue
        match = _GUARD_RE.search(line)
        if match is None:
            raise MgmtError(f"crontab 行解析不出 guard/period: {line!r}")
        guard, period, tail = match.groups()
        tokens = tail.split()
        if not tokens:
            raise MgmtError(f"crontab 行没有命令: {line!r}")
        first = Path(tokens[0]).name
        if first.startswith("python") or first in {"bash", "sh"}:
            if len(tokens) < 2:
                raise MgmtError(f"crontab 行没有脚本: {line!r}")
            script, args = Path(tokens[1]).name, tuple(tokens[2:])
        else:
            script, args = Path(tokens[0]).name, tuple(tokens[1:])
        entries.append(
            CrontabEntry(guard=guard, period=int(period), script=script, args=args)
        )
    return tuple(entries)


def _crontab_drifts(expected: list[dict], text: str) -> tuple[Drift, ...]:
    actual: dict[str, CrontabEntry] = {}
    drifts: list[Drift] = []
    for entry in parse_crontab(text):
        if entry.guard in actual:
            drifts.append(Drift("crontab-duplicate", entry.guard, "同一看护件出现多行"))
        actual[entry.guard] = entry
    wanted = {row["guard"]: row for row in expected}
    for guard, row in wanted.items():
        seen = actual.get(guard)
        if seen is None:
            drifts.append(Drift("crontab-missing", guard, "crontab 里没有这条看护件"))
            continue
        if seen.period != int(row["period"]):
            drifts.append(
                Drift("crontab-drift", guard, f"周期 {seen.period}，期望 {row['period']}")
            )
        if seen.script != row["script"]:
            drifts.append(
                Drift("crontab-drift", guard, f"脚本 {seen.script}，期望 {row['script']}")
            )
        if list(seen.args) != list(row.get("args", [])):
            drifts.append(
                Drift("crontab-drift", guard, f"参数 {list(seen.args)}，期望 {row.get('args', [])}")
            )
    for guard in sorted(set(actual) - set(wanted)):
        drifts.append(Drift("crontab-extra", guard, "crontab 里有、清单里没有"))
    return tuple(drifts)


def _hermes_drifts(home: Path, expected: dict) -> tuple[tuple[Drift, ...], tuple[str, ...]]:
    drifts: list[Drift] = []
    errors: list[str] = []
    for profile, rows in expected.items():
        path = home / ".hermes" / "profiles" / profile / "cron" / "jobs.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            jobs = payload["jobs"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"读不到 {path}: {exc}")
            continue
        actual: dict[str, dict] = {}
        for job in jobs:
            if isinstance(job, dict) and isinstance(job.get("name"), str):
                actual[job["name"]] = job
        wanted = {row["name"]: row for row in rows}
        for name, row in wanted.items():
            job = actual.get(name)
            if job is None:
                drifts.append(Drift("hermes-job-missing", f"{profile}/{name}", "jobs.json 里没有"))
                continue
            schedule = (job.get("schedule") or {}).get("display")
            if schedule != row["schedule"]:
                drifts.append(
                    Drift(
                        "hermes-job-drift",
                        f"{profile}/{name}",
                        f"schedule {schedule!r}，期望 {row['schedule']!r}",
                    )
                )
            if job.get("script") != row["script"]:
                drifts.append(
                    Drift(
                        "hermes-job-drift",
                        f"{profile}/{name}",
                        f"script {job.get('script')!r}，期望 {row['script']!r}",
                    )
                )
            if bool(job.get("enabled")) != bool(row["enabled"]):
                drifts.append(
                    Drift(
                        "hermes-job-drift",
                        f"{profile}/{name}",
                        f"enabled {bool(job.get('enabled'))}，期望 {bool(row['enabled'])}",
                    )
                )
        for name in sorted(set(actual) - set(wanted)):
            drifts.append(Drift("hermes-job-extra", f"{profile}/{name}", "清单里没有"))
    return tuple(drifts), tuple(errors)


def _systemd_drifts(home: Path, expected: list[dict]) -> tuple[Drift, ...]:
    unit_dir = home / ".config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    drifts: list[Drift] = []
    for row in expected:
        name = row["name"]
        if not (unit_dir / f"{name}.timer").is_file():
            drifts.append(Drift("timer-missing", name, "timer 文件不存在"))
            continue
        if row.get("enabled", True) and not (wants_dir / f"{name}.timer").exists():
            drifts.append(Drift("timer-disabled", name, "没有 enable（timers.target.wants 缺链接）"))
    known = {row["name"] for row in expected}
    for path in sorted(unit_dir.glob("team-board-*.timer")):
        if path.stem not in known:
            drifts.append(Drift("timer-extra", path.stem, "清单里没有"))
    return tuple(drifts)


def check(
    root: Path,
    home: Path,
    *,
    check_files_flag: bool = True,
    check_schedules_flag: bool = True,
    crontab_text: str | None = None,
) -> CheckReport:
    """跑一次完整检查。``crontab_text`` 只给测试/离线用；None 时读真实 crontab。"""

    file_drifts: tuple[Drift, ...] = ()
    checked = 0
    if check_files_flag:
        files = load_manifest(root, home)
        checked = len(files)
        file_drifts = check_files(files) + unmanaged_scripts(files, home)

    schedule_drifts: list[Drift] = []
    errors: list[str] = []
    if check_schedules_flag:
        schedules = load_schedules(root)
        if crontab_text is None:
            try:
                result = subprocess.run(  # nosec B603 B607 - 固定命令, 无 shell
                    ["crontab", "-l"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                if result.returncode != 0:
                    raise MgmtError(f"crontab -l rc={result.returncode}: {result.stderr.strip()}")
                crontab_text = result.stdout
            except (OSError, MgmtError) as exc:
                errors.append(f"读不到 crontab: {exc}")
                crontab_text = ""
        schedule_drifts.extend(_crontab_drifts(schedules["crontab"], crontab_text))
        hermes_drifts, hermes_errors = _hermes_drifts(home, schedules["hermes_jobs"])
        schedule_drifts.extend(hermes_drifts)
        errors.extend(hermes_errors)
        schedule_drifts.extend(_systemd_drifts(home, schedules["systemd_timers"]))
        schedule_drifts.extend(check_hooks(load_hooks(root), home))

    return CheckReport(
        root=root,
        home=home,
        checked_files=checked,
        file_drifts=file_drifts,
        schedule_drifts=tuple(schedule_drifts),
        errors=tuple(errors),
        schedules_checked=check_schedules_flag,
    )


def render(report: CheckReport) -> str:
    """人读报告；结论行最后。"""

    lines = [f"管理面检查 root={report.root} home={report.home}"]
    if report.checked_files or report.file_drifts:
        lines.append(
            f"文件: {report.checked_files} 件，漂移 {len(report.file_drifts)}"
        )
        lines.extend(
            f"  [{item.code}] {item.subject}：{item.detail}" for item in report.file_drifts
        )
    if report.schedules_checked:
        lines.append(f"计划: 漂移 {len(report.schedule_drifts)}")
        lines.extend(
            f"  [{item.code}] {item.subject}：{item.detail}"
            for item in report.schedule_drifts
        )
    if report.errors:
        lines.append(f"环境错误 {len(report.errors)} 条：")
        lines.extend(f"  {item}" for item in report.errors)
    if report.ok:
        lines.append("结论: 通过")
    elif report.errors and not (report.file_drifts or report.schedule_drifts):
        lines.append("结论: 环境错误（判不了）")
    else:
        lines.append(
            f"结论: 有漂移（{len(report.file_drifts) + len(report.schedule_drifts)} 条）"
        )
    return "\n".join(lines)


def render_json(report: CheckReport) -> str:
    return json.dumps(
        {
            "root": str(report.root),
            "home": str(report.home),
            "ok": report.ok,
            "files": {
                "checked": report.checked_files,
                "drifts": [
                    {"code": item.code, "subject": item.subject, "detail": item.detail}
                    for item in report.file_drifts
                ],
            },
            "schedules_checked": report.schedules_checked,
            "schedules": {
                "drifts": [
                    {"code": item.code, "subject": item.subject, "detail": item.detail}
                    for item in report.schedule_drifts
                ],
            },
            "errors": list(report.errors),
        },
        ensure_ascii=False,
    )


_GUARD_RE = re.compile(r"guard_run\.sh\s+(\S+)\s+(\d+)\s+(.+)$")
