"""gatelog.py 的单元测试：名字形态 / 失败行挑选 / 计数解析 / 摘要渲染 / 真写盘。

这里的期望文案就是 ``gate`` 子命令摘要的格式契约（卡 t_c9a1c5a6 评论 #1 第 1 节）：
以后要改摘要格式，先改这里的期望值。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from team_board.gatelog import (
    FAIL_LINE_LIMIT,
    LOG_DIR_DEFAULT,
    OUTPUT_LIMIT_BYTES,
    UNPARSED,
    GateLogError,
    counts_line,
    failure_lines,
    log_filename,
    render_summary,
    write_log,
)


class TestConstants:
    def test_output_limit_is_8192(self) -> None:
        assert OUTPUT_LIMIT_BYTES == 8192

    def test_fail_line_limit_is_20(self) -> None:
        assert FAIL_LINE_LIMIT == 20

    def test_log_dir_default_is_evidence(self) -> None:
        assert LOG_DIR_DEFAULT == "evidence"


class TestLogFilename:
    def test_plain(self) -> None:
        assert log_filename("pytest", "t_023f2827") == "pytest-t_023f2827.log"

    def test_gate_allows_dot_dash_underscore(self) -> None:
        assert log_filename("gate.v2_a-b", "t_023f2827") == "gate.v2_a-b-t_023f2827.log"

    @pytest.mark.parametrize("gate", ["", " ", "gate name", "evidence/x", "gate/x", "/gate"])
    def test_bad_gate(self, gate: str) -> None:
        with pytest.raises(GateLogError) as excinfo:
            log_filename(gate, "t_023f2827")
        assert str(excinfo.value) == f"gate 形态非法: {gate}"

    @pytest.mark.parametrize(
        "card",
        [
            "",                       # 空串
            "t_023F2827",             # 大写十六进制
            "t_XYZ",                  # 非十六进制
            "t_023f28271",            # 9 位
            "t_1234567890123",        # 13 位数字
            "t_023f282",              # 7 位
            "023f2827",               # 缺 t_ 前缀
            "evidence/x",             # 含 "/"
            "t_023f 2827",            # 含空格
        ],
    )
    def test_bad_card(self, card: str) -> None:
        with pytest.raises(GateLogError) as excinfo:
            log_filename("pytest", card)
        assert str(excinfo.value) == f"卡号形态非法: {card}"

    def test_gate_checked_before_card(self) -> None:
        with pytest.raises(GateLogError) as excinfo:
            log_filename("", "bad")
        assert str(excinfo.value) == "gate 形态非法: "


class TestFailureLines:
    def test_collects_marked_lines_and_dedupes(self) -> None:
        text = "\n".join(
            [
                "collected 4 items",
                "FAILED tests/unit/a.py::test_x - AssertionError",
                "tests/unit/a.py::test_y",
                "",
                "ERROR tests/unit/b.py",
                "FAILED tests/unit/a.py::test_x - AssertionError",
                "Traceback (most recent call last):",
                "E   assert 1 == 2",
                "错误: 找不到 fixture",
                "12 passed, 2 failed",
            ]
        )
        assert failure_lines(text) == (
            "FAILED tests/unit/a.py::test_x - AssertionError",
            "ERROR tests/unit/b.py",
            "Traceback (most recent call last):",
            "E   assert 1 == 2",
            "错误: 找不到 fixture",
        )

    def test_lowercase_error_colon_is_a_hit(self) -> None:
        text = "build ok\nfoo.c:3: error: unknown type name 'x'\n"
        assert failure_lines(text) == ("foo.c:3: error: unknown type name 'x'",)

    def test_falls_back_to_last_lines_when_no_hit(self) -> None:
        text = "\n".join(f"line-{i}" for i in range(1, 26))
        picked = failure_lines(text)
        assert len(picked) == FAIL_LINE_LIMIT
        assert picked[0] == "line-6"
        assert picked[-1] == "line-25"

    def test_caps_first_round_at_limit(self) -> None:
        text = "\n".join(f"FAILED tests/unit/test_x.py::test_{i}" for i in range(25))
        picked = failure_lines(text)
        assert len(picked) == FAIL_LINE_LIMIT
        assert picked[0] == "FAILED tests/unit/test_x.py::test_0"
        assert picked[-1] == "FAILED tests/unit/test_x.py::test_19"

    def test_zero_limit_is_empty(self) -> None:
        assert failure_lines("FAILED a\nFAILED b", limit=0) == ()

    def test_negative_limit_is_empty(self) -> None:
        assert failure_lines("FAILED a", limit=-3) == ()

    def test_custom_limit(self) -> None:
        assert failure_lines("FAILED a\nFAILED b\nFAILED c", limit=2) == ("FAILED a", "FAILED b")

    def test_empty_text_is_empty(self) -> None:
        assert failure_lines("") == ()

    def test_blank_lines_are_dropped_and_trailing_space_stripped(self) -> None:
        text = "FAILED a   \n\n   \nFAILED b\t\n"
        assert failure_lines(text) == ("FAILED a", "FAILED b")


class TestCountsLine:
    def test_pytest_quiet_style(self) -> None:
        assert counts_line("12 passed, 2 failed in 0.31s") == (
            "passed=12 failed=2 error=0 skipped=0"
        )

    def test_errors_token_merges_into_error(self) -> None:
        assert counts_line("1 error\ntest_b.py: 2 errors in teardown") == (
            "passed=0 failed=0 error=3 skipped=0"
        )

    def test_accumulates_same_kind(self) -> None:
        assert counts_line("1 passed\n2 passed\n3 skipped") == (
            "passed=3 failed=0 error=0 skipped=3"
        )

    def test_nothing_parsed(self) -> None:
        assert counts_line("no tests ran in 0.01s") == UNPARSED

    def test_empty_text(self) -> None:
        assert counts_line("") == UNPARSED

    def test_missing_items_are_zero_not_omitted(self) -> None:
        assert counts_line("4 skipped") == "passed=0 failed=0 error=0 skipped=4"

    def test_word_boundary_blocks_substring(self) -> None:
        # "unpassed" / "bypassed" 不是计数项
        assert counts_line("10 unpassed bypassed") == UNPARSED


class TestRenderSummary:
    def test_exact_snapshot(self) -> None:
        text = "\n".join(
            [
                "tests/unit/x.py .F",
                "FAILED tests/unit/x.py::test_y - assert 1 == 2",
                "FAILED tests/unit/x.py::test_z - assert 3 == 4",
                "12 passed, 2 failed in 0.31s",
            ]
        )
        got = render_summary(
            gate="pytest",
            card="t_023f2827",
            exit_code=1,
            text=text,
            log_path="/abs/path/evidence/pytest-t_023f2827.log",
        )
        assert got == "\n".join(
            [
                "gate: pytest",
                "card: t_023f2827",
                "exit: 1",
                "counts: passed=12 failed=2 error=0 skipped=0",
                "失败行（2/2，≤20）:",
                "  FAILED tests/unit/x.py::test_y - assert 1 == 2",
                "  FAILED tests/unit/x.py::test_z - assert 3 == 4",
                "log: /abs/path/evidence/pytest-t_023f2827.log",
            ]
        )
        assert not got.endswith("\n")

    def test_empty_text_has_zero_failures(self) -> None:
        got = render_summary(
            gate="ruff",
            card="t_023f2827",
            exit_code=0,
            text="",
            log_path="/abs/path/evidence/ruff-t_023f2827.log",
        )
        assert got == "\n".join(
            [
                "gate: ruff",
                "card: t_023f2827",
                "exit: 0",
                "counts: 未解析",
                "失败行（0/0，≤20）:",
                "log: /abs/path/evidence/ruff-t_023f2827.log",
            ]
        )

    def test_fallback_tail_is_listed_under_tail_header(self) -> None:
        # 裁决 C：第一轮零命中时尾巴行进「末尾行」节，失败节挂 0/0 且下面不挂行
        got = render_summary(
            gate="ruff",
            card="t_023f2827",
            exit_code=0,
            text="All checks passed!",
            log_path="/tmp/x.log",
        )
        assert got == "\n".join(
            [
                "gate: ruff",
                "card: t_023f2827",
                "exit: 0",
                "counts: 未解析",
                "失败行（0/0，≤20）:",
                "末尾行（1/1，≤20）:",
                "  All checks passed!",
                "log: /tmp/x.log",
            ]
        )

    def test_fallback_truncation_bracket_counts_all_nonempty_lines(self) -> None:
        text = "\n".join(f"line-{i}" for i in range(1, 101))
        got = render_summary(
            gate="ruff",
            card="t_023f2827",
            exit_code=0,
            text=text,
            log_path="/tmp/x.log",
        )
        lines = got.split("\n")
        assert lines[4] == "失败行（0/0，≤20）:"
        assert lines[5] == "末尾行（20/100，≤20）:"
        # tail window：列最后 20 条，原序，最后一条是第 100 行
        assert len(lines) == 5 + 1 + 20 + 1
        assert lines[6] == "  line-81"
        assert lines[25] == "  line-100"
        assert lines[-1] == "log: /tmp/x.log"

    def test_marked_case_has_no_tail_block(self) -> None:
        got = render_summary(
            gate="pytest",
            card="t_023f2827",
            exit_code=1,
            text="FAILED tests/unit/x.py::test_y\nE   assert 1 == 2",
            log_path="/tmp/x.log",
        )
        assert "末尾行" not in got

    def test_blank_text_has_no_tail_block(self) -> None:
        got = render_summary(
            gate="ruff",
            card="t_023f2827",
            exit_code=0,
            text="   \n\n",
            log_path="/tmp/x.log",
        )
        assert got == "\n".join(
            [
                "gate: ruff",
                "card: t_023f2827",
                "exit: 0",
                "counts: 未解析",
                "失败行（0/0，≤20）:",
                "log: /tmp/x.log",
            ]
        )

    def test_truncation_is_visible_in_the_bracket(self) -> None:
        text = "\n".join(f"FAILED tests/unit/test_x.py::test_{i}" for i in range(25))
        got = render_summary(
            gate="pytest",
            card="t_023f2827",
            exit_code=1,
            text=text,
            log_path="/tmp/x.log",
        )
        lines = got.split("\n")
        assert lines[4] == "失败行（20/25，≤20）:"
        # 5 行头 + 20 条失败行 + 1 行 log
        assert len(lines) == 26
        assert lines[5].startswith("  FAILED tests/unit/test_x.py::test_0")
        assert lines[-1] == "log: /tmp/x.log"


class TestWriteLog:
    def test_writes_file_and_returns_abspath(self, tmp_path: Path) -> None:
        log_dir = str(tmp_path / "evidence")
        got = write_log("完整输出\n第二行\n", gate="pytest", card="t_023f2827", log_dir=log_dir)
        assert got == str(Path(log_dir, "pytest-t_023f2827.log").resolve())
        assert Path(got).read_bytes() == "完整输出\n第二行\n".encode()

    def test_creates_missing_directory(self, tmp_path: Path) -> None:
        log_dir = str(tmp_path / "a" / "b" / "evidence")
        got = write_log("x", gate="ruff", card="t_023f2827", log_dir=log_dir)
        assert Path(got).is_file()

    def test_overwrites_previous_run(self, tmp_path: Path) -> None:
        log_dir = str(tmp_path / "evidence")
        write_log("old\nold\n", gate="pytest", card="t_023f2827", log_dir=log_dir)
        got = write_log("new\n", gate="pytest", card="t_023f2827", log_dir=log_dir)
        assert Path(got).read_bytes() == b"new\n"

    def test_bad_card_rejected_before_any_write(self, tmp_path: Path) -> None:
        log_dir = str(tmp_path / "evidence")
        with pytest.raises(GateLogError):
            write_log("x", gate="pytest", card="t_XYZ", log_dir=log_dir)
        assert not Path(log_dir).exists()

    def test_oserror_wrapped(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("file")
        with pytest.raises(GateLogError) as excinfo:
            write_log("x", gate="pytest", card="t_023f2827", log_dir=str(blocker))
        assert str(excinfo.value).startswith("写日志失败: ")
