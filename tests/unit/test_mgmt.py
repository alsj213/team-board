"""``mgmt`` 单测：manifest、文件漂移、安装/回抄、计划清单判定。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from team_board import mgmt

GOOD_CRONTAB = """\
*/2 * * * * /home/liu/.hermes/profiles/ops/scripts/guard_run.sh web_watchdog 2 /home/liu/.hermes/profiles/ops/scripts/tb_serve_watchdog.sh
*/5 * * * * /home/liu/.hermes/profiles/ops/scripts/guard_run.sh unstick 5 /usr/bin/python3 /home/liu/.hermes/profiles/ops/scripts/kanban_unstick.py --apply
17 6 * * * /home/liu/other/cron-job.sh
"""


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    home = tmp_path / "home"
    (root / "ops" / "scripts" / "ops").mkdir(parents=True)
    (root / "ops" / "systemd").mkdir()
    script = root / "ops" / "scripts" / "ops" / "a.sh"
    script.write_text("echo a\n", encoding="utf-8")
    script.chmod(0o755)
    unit = root / "ops" / "systemd" / "team-board-x.timer"
    unit.write_text("[Timer]\n", encoding="utf-8")
    manifest = {
        "files": [
            {
                "source": "ops/scripts/ops/a.sh",
                "target": "~/.hermes/profiles/ops/scripts/a.sh",
                "mode": "0755",
            },
            {
                "source": "ops/systemd/team-board-x.timer",
                "target": "~/.config/systemd/user/team-board-x.timer",
                "mode": "0644",
            },
        ]
    }
    (root / "ops" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return root, home


def make_schedules(root: Path) -> None:
    payload = {
        "crontab": [
            {
                "guard": "unstick",
                "period": 5,
                "script": "kanban_unstick.py",
                "args": ["--apply"],
            }
        ],
        "systemd_timers": [{"name": "team-board-x", "enabled": True}],
        "hermes_jobs": {
            "ops": [
                {
                    "name": "看板自愈",
                    "schedule": "every 2m",
                    "script": "selfheal_guard.sh",
                    "enabled": True,
                }
            ]
        },
    }
    (root / "ops" / "schedules.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def make_home(home: Path) -> None:
    (home / ".hermes" / "profiles" / "ops" / "cron").mkdir(parents=True)
    (home / ".hermes" / "profiles" / "ops" / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "看板自愈",
                        "schedule": {"display": "every 2m"},
                        "script": "selfheal_guard.sh",
                        "enabled": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (home / ".config" / "systemd" / "user" / "timers.target.wants").mkdir(parents=True)
    timer = home / ".config" / "systemd" / "user" / "team-board-x.timer"
    timer.write_text("[Timer]\n", encoding="utf-8")
    (home / ".config" / "systemd" / "user" / "timers.target.wants" / "team-board-x.timer").symlink_to(
        timer
    )


def test_repo_artifacts_are_loadable(tmp_path: Path) -> None:
    """CI 里直接用真实仓库的 manifest/schedules，防止提交坏 JSON 或漏登记。"""

    root = Path(__file__).resolve().parents[2]
    files = mgmt.load_manifest(root, tmp_path)
    assert any(item.source_label == "ops/playbook.md" for item in files)
    schedules = mgmt.load_schedules(root)
    assert schedules["crontab"]
    assert schedules["systemd_timers"]
    assert schedules["hermes_jobs"]


class TestManifest:
    def test_loads_and_expands_home(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        assert [item.source_label for item in files] == [
            "ops/scripts/ops/a.sh",
            "ops/systemd/team-board-x.timer",
        ]
        assert files[0].target == home / ".hermes/profiles/ops/scripts/a.sh"
        assert files[0].mode == 0o755

    def test_unlisted_script_is_rejected(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        (root / "ops" / "scripts" / "ops" / "b.sh").write_text("echo b\n", encoding="utf-8")
        with pytest.raises(mgmt.MgmtError, match="未登记"):
            mgmt.load_manifest(root, home)

    def test_duplicate_target_is_rejected(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        payload = json.loads((root / "ops" / "manifest.json").read_text(encoding="utf-8"))
        payload["files"][1]["target"] = payload["files"][0]["target"]
        (root / "ops" / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(mgmt.MgmtError, match="target 重复"):
            mgmt.load_manifest(root, home)


class TestFiles:
    def test_check_passes_after_install(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        assert [item.code for item in mgmt.check_files(files)] == [
            "target-missing",
            "target-missing",
        ]
        mgmt.install(files, home)
        assert mgmt.check_files(files) == ()

    def test_reports_missing_drift_and_mode(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        drifts = mgmt.check_files(files)
        assert {item.code for item in drifts} == {"target-missing"}

        mgmt.install(files, home)
        target = home / ".hermes/profiles/ops/scripts/a.sh"
        target.write_text("echo changed\n", encoding="utf-8")
        target.chmod(0o755)
        assert [item.code for item in mgmt.check_files(files)] == ["content-drift"]

        target.write_text("echo a\n", encoding="utf-8")
        target.chmod(0o644)
        assert [item.code for item in mgmt.check_files(files)] == ["mode-drift"]

    def test_install_refuses_to_overwrite_without_force(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        mgmt.install(files, home)
        target = home / ".hermes/profiles/ops/scripts/a.sh"
        target.write_text("echo live\n", encoding="utf-8")
        with pytest.raises(mgmt.MgmtError, match="--force"):
            mgmt.install(files, home)

    def test_install_force_backs_up_before_overwrite(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        target = home / ".hermes/profiles/ops/scripts/a.sh"
        target.parent.mkdir(parents=True)
        target.write_text("echo live\n", encoding="utf-8")
        mgmt.install(files, home, force=True)
        assert target.read_text(encoding="utf-8") == "echo a\n"
        backups = list((home / ".cache" / "team-board" / "mgmt-backup").rglob("a.sh"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "echo live\n"

    def test_install_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        actions = mgmt.install(files, home, dry_run=True)
        assert [action for action, _ in actions] == ["would-write", "would-write"]
        assert not (home / ".hermes/profiles/ops/scripts/a.sh").exists()

    def test_unmanaged_live_script_is_reported(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        mgmt.install(files, home)
        extra = home / ".hermes/profiles/ops/scripts/leftover.py"
        extra.write_text("print('old')\n", encoding="utf-8")
        (home / ".hermes/profiles/ops/scripts/a.sh.bak").write_text(
            "echo backup\n", encoding="utf-8"
        )
        drifts = mgmt.unmanaged_scripts(files, home)
        assert [item.code for item in drifts] == ["script-unmanaged"]
        assert "leftover.py" in drifts[0].subject

    def test_capture_writes_live_back_to_repo(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        mgmt.install(files, home)
        target = home / ".hermes/profiles/ops/scripts/a.sh"
        target.write_text("echo live\n", encoding="utf-8")
        actions = mgmt.capture(files, dry_run=True)
        assert ("would-update", "ops/scripts/ops/a.sh") in actions
        assert (root / "ops/scripts/ops/a.sh").read_text(encoding="utf-8") == "echo a\n"

        mgmt.capture(files)
        assert (root / "ops/scripts/ops/a.sh").read_text(encoding="utf-8") == "echo live\n"
        assert mgmt.check_files(files) == ()


class TestCrontab:
    def test_parses_python_and_bash_forms(self) -> None:
        entries = mgmt.parse_crontab(GOOD_CRONTAB)
        assert [(item.guard, item.period, item.script, item.args) for item in entries] == [
            ("web_watchdog", 2, "tb_serve_watchdog.sh", ()),
            ("unstick", 5, "kanban_unstick.py", ("--apply",)),
        ]

    def test_ignores_non_guard_lines(self) -> None:
        assert mgmt.parse_crontab("17 6 * * * /home/liu/other.sh\n") == ()

    def test_reports_missing_extra_and_drift(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        make_schedules(root)
        make_home(home)

        clean = mgmt.check(
            root, home, check_files_flag=False, crontab_text="\n".join(
                GOOD_CRONTAB.splitlines()[1:]
            )
        )
        assert clean.errors == ()
        assert clean.schedule_drifts == ()

        drifted = mgmt.check(
            root,
            home,
            check_files_flag=False,
            crontab_text=GOOD_CRONTAB.replace(
                "unstick 5 /usr/bin/python3", "unstick 7 /usr/bin/python3"
            ).replace("web_watchdog", "unknown_watchdog"),
        )
        codes = {item.code for item in drifted.schedule_drifts}
        assert {"crontab-drift", "crontab-extra"} <= codes


class TestSchedules:
    def test_hermes_job_drift_and_extra(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        make_schedules(root)
        make_home(home)
        path = home / ".hermes" / "profiles" / "ops" / "cron" / "jobs.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["jobs"][0]["cooldown"] = "same"
        payload["jobs"][0]["enabled"] = False
        payload["jobs"].append({"name": "野任务", "schedule": {"display": "every 1m"}})
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        report = mgmt.check(root, home, check_files_flag=False, crontab_text=GOOD_CRONTAB)
        codes = {item.code for item in report.schedule_drifts}
        assert {"hermes-job-drift", "hermes-job-extra"} <= codes

    def test_disabled_timer_is_reported(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        make_schedules(root)
        make_home(home)
        (home / ".config/systemd/user/timers.target.wants/team-board-x.timer").unlink()

        report = mgmt.check(
            root, home, check_files_flag=False, crontab_text=GOOD_CRONTAB.splitlines()[1]
        )
        assert [item.code for item in report.schedule_drifts] == ["timer-disabled"]

    def test_extra_timer_is_reported(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        make_schedules(root)
        make_home(home)
        (home / ".config/systemd/user/team-board-y.timer").write_text(
            "[Timer]\n", encoding="utf-8"
        )

        report = mgmt.check(
            root, home, check_files_flag=False, crontab_text=GOOD_CRONTAB.splitlines()[1]
        )
        assert [item.code for item in report.schedule_drifts] == ["timer-extra"]


class TestReport:
    def test_json_carries_codes_and_ok(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        mgmt.install(files, home)
        report = mgmt.check(
            root, home, check_schedules_flag=False, check_files_flag=True
        )
        payload = json.loads(mgmt.render_json(report))
        assert payload["ok"] is True
        assert payload["files"]["checked"] == 2

    def test_render_puts_conclusion_last(self, tmp_path: Path) -> None:
        root, home = make_repo(tmp_path)
        files = mgmt.load_manifest(root, home)
        mgmt.install(files, home)
        report = mgmt.check(root, home, check_schedules_flag=False)
        assert mgmt.render(report).splitlines()[-1] == "结论: 通过"
