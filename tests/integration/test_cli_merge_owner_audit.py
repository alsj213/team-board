"""``cli merge-owner-audit`` 的集成测试：假 gh 挂 PATH 上，tmp DB 造正文/评论/认领。

假 gh 只认本子命令要的那一条 ``gh pr view <n> --repo <repo> --json state,mergedAt``：

- 状态表 ``$FAKE_STATES``：每行 ``<pr> <STATE>``，查不到就按「`gh pr view` 解不出该号」
  真实行为回 rc=1 + 一段 GraphQL 报错（这正好是契约 v2 增量 3 里「查不到 → WARN →
  降级」的现实路径）；
- ``$FAKE_RC`` / ``$FAKE_ERR``：整体失败（rc=1 + 长 stderr，验「stderr 原文不进报告」）；
- ``$FAKE_OUT``：原样吐一段输出（验「非 JSON → 降级」）；
- ``$FAKE_SLEEP``：``exec sleep`` 住（验「超时 → 降级」，用 exec 保证杀的是直接子进程，
  不留下握着管道的孙进程）；
- 每次调用记进 ``$FAKE_LOG``，用来验「坏参数时 gh 一次都不发」。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from team_board import cli

CARD = "t_11112222"
CLAIMER = "t_33334444"
MISSING = "t_99998888"
REPO = "alsj213/team-board"

GH_STUB = """#!/bin/sh
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" != "pr" ] || [ "$2" != "view" ]; then
  printf 'fake gh: unexpected args: %s\\n' "$*" >&2
  exit 9
fi
pr="$3"
if [ -n "${FAKE_SLEEP:-}" ]; then exec sleep "$FAKE_SLEEP"; fi
if [ -n "${FAKE_RC:-}" ] && [ "$FAKE_RC" != "0" ]; then
  if [ -n "${FAKE_ERR:-}" ]; then printf '%s\\n' "$FAKE_ERR" >&2; fi
  exit "$FAKE_RC"
fi
if [ -n "${FAKE_OUT:-}" ]; then printf '%s\\n' "$FAKE_OUT"; exit 0; fi
state=$(grep -m1 "^$pr " "$FAKE_STATES" 2>/dev/null | cut -d' ' -f2-)
if [ -z "$state" ]; then
  printf 'GraphQL: Could not resolve to a PullRequest with the number of %s.\\n' "$pr" >&2
  exit 1
fi
printf '{"state":"%s","mergedAt":null}\\n' "$state"
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def make_board(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """``rows`` = ``[(卡号, status, body), …]``；两张表按契约给最小列集。"""

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, status TEXT, assignee TEXT, body TEXT
            );
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT,
                created_at INTEGER
            );
            """
        )
        for card_id, status, body in rows:
            conn.execute(
                "INSERT INTO tasks (id, status, assignee, body) VALUES (?, ?, ?, ?)",
                (card_id, status, "engineer", body),
            )
        conn.commit()
    finally:
        conn.close()


def add_comment(path: Path, card: str, body: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at)"
            " VALUES (?, ?, ?, ?)",
            (card, "pm", body, 1793410000),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir()
    _write_exe(bindir / "gh", GH_STUB)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    log = tmp_path / "calls.log"
    states = tmp_path / "states.txt"
    states.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_STATES", str(states))
    return {
        "bindir": bindir,
        "log": log,
        "states": states,
        "setenv": monkeypatch.setenv,
        "monkeypatch": monkeypatch,
        "tmp": tmp_path,
    }


def states(stub: dict[str, Any], mapping: dict[int, str]) -> None:
    stub["states"].write_text(
        "".join(f"{pr} {state}\n" for pr, state in mapping.items()), encoding="utf-8"
    )


def calls(stub: dict[str, Any]) -> list[str]:
    log: Path = stub["log"]
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(["merge-owner-audit", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestRc0:
    def test_no_pr_at_all(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "## 目标\n这张卡里没有任何 PR 号\n")])
        log_dir = tmp_path / "logs"

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(log_dir))

        assert code == 0
        assert err == ""
        assert out.splitlines()[0] == f"卡 {CARD} [running]"
        assert "抽取到 0 个 PR 号" in out.splitlines()[-1]
        # 没有 PR 号 → 一次 gh 都不发、也不写降级日志
        assert calls(stub) == []
        assert not log_dir.exists()

    def test_merged_pr(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {50: "MERGED"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "## 交付\nPR #50 已合并\n")])

        code, out, err = run(capsys, CARD, "--db", str(db))

        assert code == 0
        assert err == ""
        assert out.splitlines() == [
            f"卡 {CARD} [running]",
            "PR #50 MERGED",
            "结论: 1 个 PR 全部有归属 → 可关",
        ]
        assert calls(stub) == [
            f"gh pr view 50 --repo {REPO} --json state,mergedAt"
        ]

    def test_open_pr_claimed_by_another_pending_card(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {53: "OPEN"})
        db = tmp_path / "b.db"
        make_board(
            db,
            [
                (CARD, "running", "本卡正文引用 PR #53\n"),
                (CLAIMER, "todo", "PR-CLAIM: #53\n收口卡\n"),
                ("t_77778888", "done", "PR-CLAIM: #53\n已结的卡不算\n"),
            ],
        )

        code, out, err = run(capsys, CARD, "--db", str(db))

        assert code == 0
        assert err == ""
        assert out.splitlines() == [
            f"卡 {CARD} [running]",
            f"PR #53 CLAIMED {CLAIMER}",
            "结论: 1 个 PR 全部有归属 → 可关",
        ]


class TestRc1:
    def test_open_pr_without_claim(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {53: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])

        code, out, err = run(capsys, CARD, "--db", str(db))

        assert code == 1
        assert err == ""
        assert out.splitlines() == [
            f"卡 {CARD} [running]",
            "PR #53 UNOWNED 来源: 正文",
            "结论: 1 个 PR / 通过 0 / 无归属 1 → "
            "先建收口卡（首行 PR-CLAIM: #53）再关本卡",
        ]

    def test_self_claim_does_not_count(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {53: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "PR-CLAIM: #53\nPR #53\n")])

        code, out, _ = run(capsys, CARD, "--db", str(db))

        assert code == 1
        assert "PR #53 UNOWNED" in out


class TestDegrade:
    def test_gh_rc_is_warn_and_rc0(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        long_err = "GraphQL: Could not resolve to a PullRequest " + "x" * 400
        stub["setenv"]("FAKE_RC", "1")
        stub["setenv"]("FAKE_ERR", long_err)
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])
        log_dir = tmp_path / "nested" / "logs"

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(log_dir))

        assert code == 0
        assert out.splitlines()[-1] == (
            "结论: gh 不可用（gh rc=1）→ 降级放行（日志已写），晨报须列"
        )
        assert err == "warn: gh 不可用（gh rc=1）→ 降级放行，晨报须列\n"

        log = log_dir / f"merge-owner-audit-{CARD}.log"
        assert log.exists()
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert lines[0].endswith(" warn pr=53 gh rc=1")
        # 行首是 UTC ISO8601（秒精度）
        assert lines[0][:4].isdigit() and lines[0][10] == "T" and lines[0][19] == "Z"
        # gh 的 stderr 原文既不进报告也不进日志
        assert "GraphQL" not in out
        assert "GraphQL" not in err
        assert "GraphQL" not in log.read_text(encoding="utf-8")

    def test_gh_not_in_path(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        stub["setenv"]("PATH", str(empty))
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])
        log_dir = tmp_path / "logs"

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(log_dir))

        assert code == 0
        assert err == "warn: gh 不可用（gh 不在 PATH）→ 降级放行，晨报须列\n"
        assert "gh 不在 PATH" in out.splitlines()[-1]
        assert (log_dir / f"merge-owner-audit-{CARD}.log").read_text(
            encoding="utf-8"
        ).endswith(" warn pr=53 gh 不在 PATH\n")

    def test_gh_output_not_json(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub["setenv"]("FAKE_OUT", "not json at all")
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])

        code, _, err = run(
            capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs")
        )

        assert code == 0
        assert err == "warn: gh 不可用（gh 输出非 JSON）→ 降级放行，晨报须列\n"

    def test_gh_timeout(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub["setenv"]("FAKE_SLEEP", "30")
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])

        code, _, err = run(
            capsys,
            CARD,
            "--db",
            str(db),
            "--timeout",
            "1",
            "--log-dir",
            str(tmp_path / "logs"),
        )

        assert code == 0
        assert err == "warn: gh 不可用（gh 超时 1s）→ 降级放行，晨报须列\n"

    def test_unowned_wins_over_warn(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 50 MERGED / 51 OPEN 无人认领（UNOWNED）/ 53 查不到（rc=1 → WARN）
        states(stub, {50: "MERGED", 51: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "PR #50 已合\nPR #51 待合并\nPR #53 待合并\n")])
        log_dir = tmp_path / "logs"

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(log_dir))

        assert code == 1
        # 降级文案只在「无 UNOWNED」时出现；rc1 时只留日志
        assert err == ""
        assert out.splitlines()[-1] == (
            "结论: 3 个 PR / 通过 2 / 无归属 1 → "
            "先建收口卡（首行 PR-CLAIM: #51）再关本卡"
        )
        assert (log_dir / f"merge-owner-audit-{CARD}.log").read_text(
            encoding="utf-8"
        ).endswith(" warn pr=53 gh rc=1\n")
        assert "PR #53 WARN gh rc=1" in out


    def test_ascii_stdout_rc2_has_single_stderr_line(
        self,
        stub: dict[str, Any],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P2-b：rc2 时 stderr **恰一行** ``错误: …``，``warn:`` 不许抢跑。"""
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        stub["setenv"]("PATH", str(empty))  # 有 WARN → 修复前会先打一行 warn:
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])

        class _AsciiStdout:
            encoding = "ascii"

            def write(self, text: str) -> int:
                raise UnicodeEncodeError("ascii", text, 0, 1, "ordinal not in range")

            def flush(self) -> None:
                return None

        monkeypatch.setattr(sys, "stdout", _AsciiStdout())

        code, out, err = run(
            capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs")
        )

        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: 当前 stdout 编码 ")
        assert "warn:" not in err

    def test_log_write_failure_keeps_rc_and_says_so(
        self,
        stub: dict[str, Any],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """日志写不进去是**环境**问题：只 stderr 一行 warn、rc 不变、不许说「（日志已写）」。

        P2（2026-10-31 返工）：旧口径把它判成 rc2，等于「日志目录不可写」这个纯环境
        故障改写了稽核结论 —— 一个本来 rc0 的降级被翻成 rc2，卡就关不掉了。
        """
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        stub["setenv"]("PATH", str(empty))
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", "/dev/null/x")

        assert code == 0
        # 结论行照旧是「降级放行」，但不许出现「（日志已写）」这句假话
        assert out.splitlines()[-1] == (
            "结论: gh 不可用（gh 不在 PATH）→ 降级放行（日志写入失败，见 stderr），晨报须列"
        )
        assert "（日志已写）" not in out
        # stderr 恰一行 warn，且带真实 rc
        assert err.count("\n") == 1
        assert err.startswith("warn: 日志写入失败: ")
        assert "rc 仍 0" in err
        assert "错误: " not in err


class TestRc2Params:
    @pytest.mark.parametrize("card", ["t_XYZ", "t_123", "", "t_1111222G", "t_111122223"])
    def test_bad_card(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str], card: str
    ) -> None:
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "PR #53\n")])

        code, out, err = run(capsys, card, "--db", str(db))

        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: 卡号非法：")
        assert "Traceback" not in err
        assert calls(stub) == []

    @pytest.mark.parametrize("timeout", ["0", "-1", "601", "", "20s", "1_0", "9999999999999"])
    def test_bad_timeout(
        self,
        stub: dict[str, Any],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        timeout: str,
    ) -> None:
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "PR #53\n")])

        code, out, err = run(capsys, CARD, f"--timeout={timeout}", "--db", str(db))

        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: 超时")
        assert "Traceback" not in err
        assert calls(stub) == []

    @pytest.mark.parametrize("repo", ["", "a/b/c", "alsj213", "alsj213/team board", "/team-board"])
    def test_bad_repo(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str], repo: str
    ) -> None:
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "PR #53\n")])

        code, out, err = run(capsys, CARD, f"--repo={repo}", "--db", str(db))

        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: 仓库名非法：")
        assert calls(stub) == []

    def test_missing_positional_is_argparse_rc2(self, stub: dict[str, Any]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["merge-owner-audit"])
        assert excinfo.value.code == 2


class TestRc2Board:
    def test_card_not_found(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "PR #53\n")])

        code, out, err = run(capsys, MISSING, "--db", str(db))

        assert code == 2
        assert out == ""
        assert err == f"错误: 卡不存在: {MISSING}\n"
        assert calls(stub) == []

    def test_db_missing(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, CARD, "--db", str(tmp_path / "nope.db"))

        assert code == 2
        assert out == ""
        assert err.startswith("错误: 数据源不存在: ")
        assert "Traceback" not in err

    def test_db_not_sqlite(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broken = tmp_path / "broken.db"
        broken.write_text("这不是 SQLite\n", encoding="utf-8")

        code, out, err = run(capsys, CARD, "--db", str(broken))

        assert code == 2
        assert out == ""
        assert err.startswith("错误: 不是合法的 SQLite 库 ")
        assert "Traceback" not in err


class TestExemptEscapeHatch:
    """P2-a 后的豁免面：只认**本卡正文**的顶格一行；评论里的、围栏里的都不算。"""

    def test_comment_exempt_does_not_count(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {50: "MERGED", 12: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "## 交付\nPR #50 已合并\n")])
        add_comment(
            db,
            CARD,
            "示例：\n```\nPR-MERGE-EXEMPT: #12\n```\n上面那行里的 PR #12 是别的卡的\n",
        )

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 1
        assert err == ""
        assert "PR #12 UNOWNED 来源: 评论" in out
        assert "PR #12 EXEMPT" not in out
        assert out.splitlines()[-1] == (
            "结论: 2 个 PR / 通过 1 / 无归属 1 → 先建收口卡（首行 PR-CLAIM: #12）再关本卡"
        )

    def test_body_exempt_line_counts(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {50: "MERGED", 12: "OPEN"})
        db = tmp_path / "b.db"
        make_board(
            db,
            [(CARD, "running", "## 交付\nPR #50 已合并\nPR #12 是别的卡\nPR-MERGE-EXEMPT: #12\n")],
        )

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 0
        assert err == ""
        assert out.splitlines() == [
            f"卡 {CARD} [running]",
            "PR #12 EXEMPT",
            "PR #50 MERGED",
            "结论: 2 个 PR 全部有归属 → 可关",
        ]

    def test_fenced_body_exempt_does_not_count(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {50: "MERGED", 12: "OPEN"})
        db = tmp_path / "b.db"
        make_board(
            db,
            [
                (
                    CARD,
                    "running",
                    "## 交付\nPR #50 已合并\nPR #12 是别的卡\n```\nPR-MERGE-EXEMPT: #12\n```\n",
                )
            ],
        )

        code, out, _ = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 1
        assert "PR #12 UNOWNED" in out
        assert "PR #12 EXEMPT" not in out

    def test_indented_exempt_line_does_not_count(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {12: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "示例 PR #12\n  PR-MERGE-EXEMPT: #12\n")])

        code, out, _ = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 1
        assert "PR #12 UNOWNED" in out
        assert "PR #12 EXEMPT" not in out


class TestFenceUnbalanced:
    """围栏不配对（行首 ``` 奇数）：豁免未生效必须在结论行点名，不许无声 rc1。"""

    def test_odd_fence_names_the_cause(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {12: "OPEN"})
        db = tmp_path / "b.db"
        # 只有开围栏、没有闭围栏 → 后面的豁免行被当成围栏内容吞掉
        make_board(db, [(CARD, "running", "PR #12 是别的卡\n```\nPR-MERGE-EXEMPT: #12\n")])

        code, out, _ = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 1
        assert "PR #12 UNOWNED" in out
        assert "PR #12 EXEMPT" not in out
        conclusion = out.splitlines()[-1]
        assert conclusion.startswith("结论: ")
        assert "围栏不配对，豁免未生效" in conclusion

    def test_even_fence_has_no_note(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {12: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "PR #12 是别的卡\n```\n示例\n```\n")])

        code, out, _ = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 1
        assert "围栏不配对" not in out


class TestUnownedSource:
    """UNOWNED 行注明来源（正文 / 评论）；评论来源提示补豁免行才能自解。"""

    def test_comment_only_pr_hints_the_exempt_line(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {77: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "## 目标\n本卡正文没有 PR 号\n")])
        add_comment(db, CARD, "顺手提一句：PR #77 还开着\n")

        code, out, _ = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 1
        line = next(row for row in out.splitlines() if row.startswith("PR #77 "))
        assert line.startswith("PR #77 UNOWNED 来源: 评论")
        assert "需在本卡正文写 PR-MERGE-EXEMPT: #77" in line

    def test_body_pr_has_no_hint(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {77: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #77\n")])

        code, out, _ = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 1
        line = next(row for row in out.splitlines() if row.startswith("PR #77 "))
        assert line == "PR #77 UNOWNED 来源: 正文"
        assert "PR-MERGE-EXEMPT" not in out

    def test_both_sources_are_named(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {77: "OPEN"})
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #77\n")])
        add_comment(db, CARD, "评论里也提 PR #77\n")

        code, out, _ = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 1
        line = next(row for row in out.splitlines() if row.startswith("PR #77 "))
        assert line.startswith("PR #77 UNOWNED 来源: 正文+评论")
        # 评论来源照样给提示：补了正文豁免行这条才算解
        assert "需在本卡正文写 PR-MERGE-EXEMPT: #77" in line


class TestExemptSkipsGh:
    """已判 EXEMPT 的 PR 不再发 ``gh pr view``（P3）：判定优先级 1 已经定了。"""

    def test_exempt_pr_is_not_asked(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        states(stub, {50: "MERGED", 12: "OPEN"})
        db = tmp_path / "b.db"
        make_board(
            db,
            [(CARD, "running", "PR #50 已合并\nPR #12 是别的卡\nPR-MERGE-EXEMPT: #12\n")],
        )

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 0
        assert err == ""
        assert "PR #12 EXEMPT" in out
        assert calls(stub) == [
            "gh pr view 50 --repo alsj213/team-board --json state,mergedAt"
        ]


class TestGhNotExecutable:
    """``gh`` 存在但起不来（EACCES / ENOEXEC）不许报成「不在 PATH」（P3）。"""

    @staticmethod
    def _stub_with(tmp_path: Path, mode: int, body: str) -> Path:
        bindir = tmp_path / "broken-bin"
        bindir.mkdir()
        target = bindir / "gh"
        target.write_text(body, encoding="utf-8")
        target.chmod(mode)
        return bindir

    def test_permission_denied_is_not_path(
        self,
        stub: dict[str, Any],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bindir = self._stub_with(tmp_path, 0o644, "#!/bin/sh\necho hi\n")
        stub["setenv"]("PATH", str(bindir))
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 0
        assert "PR #53 WARN gh 不可执行（EACCES）" in out
        assert "不在 PATH" not in out
        assert "不在 PATH" not in err

    def test_enoexec_is_not_path(
        self,
        stub: dict[str, Any],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bindir = self._stub_with(tmp_path, 0o755, "echo hi\n")
        stub["setenv"]("PATH", str(bindir))
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])

        code, out, err = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 0
        assert "PR #53 WARN gh 不可执行（ENOEXEC）" in out
        assert "不在 PATH" not in out
        assert "不在 PATH" not in err

    def test_missing_gh_is_still_path(
        self, stub: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        stub["setenv"]("PATH", str(empty))
        db = tmp_path / "b.db"
        make_board(db, [(CARD, "running", "本卡正文引用 PR #53\n")])

        code, out, _ = run(capsys, CARD, "--db", str(db), "--log-dir", str(tmp_path / "logs"))

        assert code == 0
        assert "PR #53 WARN gh 不在 PATH" in out
