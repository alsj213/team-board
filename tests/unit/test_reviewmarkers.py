"""``reviewmarkers`` 单测：required check 取数、定点重跑、失败与超时。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from team_board import reviewmarkers

PR = 108
HEAD = "a" * 40
SLUG = "owner/repo"
LINK = "https://github.com/owner/repo/actions/runs/123/job/456"


class FakeProc:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class FakeRunner:
    """按 gh 子命令回放：view / checks（按序列）/ rerun。"""

    def __init__(
        self,
        *,
        states: Sequence[str] = ("SUCCESS",),
        link: str = LINK,
        pr_state: str = "OPEN",
        view_rc: int = 0,
        checks_json: str | None = None,
        rerun_rc: int = 0,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._states = list(states)
        self._link = link
        self._pr_state = pr_state
        self._view_rc = view_rc
        self._checks_json = checks_json
        self._rerun_rc = rerun_rc

    def __call__(self, argv: Sequence[str]) -> Any:
        self.calls.append(tuple(argv))
        if argv[2] == "view":
            payload = {
                "headRefOid": HEAD,
                "headRefName": "wt/t_1234abcd",
                "state": self._pr_state,
            }
            return FakeProc(self._view_rc, json.dumps(payload))
        if argv[2] == "checks":
            if self._checks_json is not None:
                return FakeProc(0, self._checks_json)
            state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
            rows = [
                {"name": reviewmarkers.CHECK_NAME, "state": state, "link": self._link},
            ]
            rc = 0 if state == "SUCCESS" else 1
            return FakeProc(rc, json.dumps(rows))
        if argv[1] == "run":
            return FakeProc(self._rerun_rc, "", "boom" if self._rerun_rc else "")
        raise AssertionError(f"unexpected argv: {argv}")

    @property
    def rerun_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[1] == "run"]


def run(**kwargs: Any) -> reviewmarkers.RefreshResult:
    runner = kwargs.pop("runner", None) or FakeRunner()
    return reviewmarkers.refresh(
        PR,
        slug=SLUG,
        wait=kwargs.pop("wait", 10),
        poll=1,
        runner=runner,
        sleep=lambda _: None,
        **kwargs,
    )


class TestParse:
    def test_parse_pr(self) -> None:
        info = reviewmarkers.parse_pr(
            json.dumps({"headRefOid": HEAD, "headRefName": "wt/t_1234abcd", "state": "OPEN"}),
            pr=PR,
        )
        assert info.head_oid == HEAD

    @pytest.mark.parametrize("bad", ["not json", "[]", '{"headRefOid": "abc"}'])
    def test_parse_pr_rejects(self, bad: str) -> None:
        with pytest.raises(reviewmarkers.ReviewMarkersError):
            reviewmarkers.parse_pr(bad, pr=PR)

    def test_parse_checks_empty(self) -> None:
        assert reviewmarkers.parse_checks("") == ()

    def test_parse_checks_bad_json(self) -> None:
        with pytest.raises(reviewmarkers.ReviewMarkersError):
            reviewmarkers.parse_checks("nope")

    def test_parse_checks_ignores_missing_link(self) -> None:
        checks = reviewmarkers.parse_checks(
            json.dumps([{"name": "x", "state": "SUCCESS"}])
        )
        assert checks[0].link == ""


class TestRefresh:
    def test_already_green_does_not_rerun(self) -> None:
        runner = FakeRunner(states=("SUCCESS",))
        result = run(runner=runner)
        assert result.reran is False
        assert runner.rerun_calls == []

    def test_failed_job_is_rerun_then_green(self) -> None:
        runner = FakeRunner(states=("FAILURE", "IN_PROGRESS", "SUCCESS"))
        result = run(runner=runner)
        assert result.reran is True
        assert result.run_id == 123
        assert result.job_id == 456
        assert runner.rerun_calls == [
            ("gh", "run", "rerun", "123", "--job", "456", "-R", SLUG),
        ]

    def test_rerun_then_failed_raises(self) -> None:
        runner = FakeRunner(states=("FAILURE", "IN_PROGRESS", "FAILURE"))
        with pytest.raises(reviewmarkers.ReviewMarkersError, match="重跑后仍"):
            run(runner=runner)

    def test_api_lag_failure_is_retried_within_grace(self) -> None:
        runner = FakeRunner(states=("FAILURE", "FAILURE", "SUCCESS"))
        result = run(runner=runner)
        assert result.reran is True

    def test_missing_check_raises_on_timeout(self) -> None:
        runner = FakeRunner(checks_json="[]")
        with pytest.raises(reviewmarkers.ReviewMarkersError, match="找不到 required check"):
            run(runner=runner, wait=0)

    def test_link_without_job_raises(self) -> None:
        runner = FakeRunner(states=("FAILURE",), link="https://example.com/job")
        with pytest.raises(reviewmarkers.ReviewMarkersError, match="没有 run/job id"):
            run(runner=runner)

    def test_pr_not_open_raises(self) -> None:
        with pytest.raises(reviewmarkers.ReviewMarkersError, match="不是 OPEN"):
            run(runner=FakeRunner(pr_state="MERGED"))

    def test_unknown_state_raises(self) -> None:
        with pytest.raises(reviewmarkers.ReviewMarkersError, match="不认识"):
            run(runner=FakeRunner(states=("SOMETHING_NEW",)))

    def test_rerun_rc_nonzero_raises(self) -> None:
        runner = FakeRunner(states=("FAILURE",), rerun_rc=1)
        with pytest.raises(reviewmarkers.ReviewMarkersError, match="gh run rerun"):
            run(runner=runner)


class TestMain:
    def test_cli_green(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            reviewmarkers, "refresh", lambda *a, **k: reviewmarkers.RefreshResult(PR, HEAD, "SUCCESS")
        )
        assert reviewmarkers.main(["--pr", str(PR)]) == 0
        assert "review-markers: PASS" in capsys.readouterr().out

    def test_cli_failure(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any, **k: Any) -> Any:
            raise reviewmarkers.ReviewMarkersError("no marker")

        monkeypatch.setattr(reviewmarkers, "refresh", boom)
        assert reviewmarkers.main(["--pr", str(PR)]) == 1
        assert "review-markers: FAIL" in capsys.readouterr().err
