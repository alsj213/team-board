"""outputaudit 纯函数的单测：排序、超限边界（8192 放行 / 8193 超）、渲染逐字。"""

from __future__ import annotations

from team_board import bodylimit, gatelog, outputaudit

LIMIT = gatelog.OUTPUT_LIMIT_BYTES


def rows_over_and_under() -> list[tuple[str, str, str, int]]:
    """三条超限 + 两条限内（含恰好等于上限的那条）。"""
    return [
        ("t_b", "comment", "9", 16336),
        ("t_a", "result", "-", 8193),
        ("t_a", "comment", "7", 9000),
        ("t_a", "comment", "10", 100),
        ("t_b", "result", "-", LIMIT),
    ]


class TestSummarize:
    def test_sorted_by_bytes_desc_then_triplet(self) -> None:
        summary = outputaudit.summarize(rows_over_and_under())
        got = [(r.task_id, r.source, r.row_id, r.nbytes) for r in summary.rows]
        assert got == [
            ("t_b", "comment", "9", 16336),
            ("t_a", "comment", "7", 9000),
            ("t_a", "result", "-", 8193),
            ("t_b", "result", "-", LIMIT),
            ("t_a", "comment", "10", 100),
        ]

    def test_tie_break_is_triplet_order(self) -> None:
        summary = outputaudit.summarize(
            [
                ("t_b", "result", "-", 5),
                ("t_b", "comment", "3", 5),
                ("t_a", "result", "-", 5),
                ("t_a", "comment", "2", 5),
            ]
        )
        got = [(r.task_id, r.source, r.row_id) for r in summary.rows]
        assert got == [
            ("t_a", "comment", "2"),
            ("t_a", "result", "-"),
            ("t_b", "comment", "3"),
            ("t_b", "result", "-"),
        ]

    def test_boundary_equal_to_limit_is_not_over(self) -> None:
        summary = outputaudit.summarize([("t_a", "comment", "1", LIMIT)])
        assert summary.over == ()
        assert summary.counts == {"total": 1, "over": 0}

    def test_boundary_limit_plus_one_is_over(self) -> None:
        summary = outputaudit.summarize([("t_a", "comment", "1", LIMIT + 1)])
        assert [r.row_id for r in summary.over] == ["1"]
        assert summary.counts == {"total": 1, "over": 1}

    def test_over_is_subsequence_of_rows(self) -> None:
        summary = outputaudit.summarize(rows_over_and_under())
        assert summary.over == summary.rows[:3]
        assert summary.counts == {"total": 5, "over": 3}

    def test_empty_rows(self) -> None:
        summary = outputaudit.summarize([])
        assert (summary.rows, summary.over) == ((), ())
        assert summary.counts == {"total": 0, "over": 0}

    def test_custom_limit_is_honoured(self) -> None:
        summary = outputaudit.summarize([("t_a", "comment", "1", 6)], limit=5)
        assert summary.counts["over"] == 1


class TestRender:
    def test_with_over_rows_is_verbatim(self) -> None:
        summary = outputaudit.summarize(rows_over_and_under())
        text = outputaudit.render(
            summary, db_path="/tmp/x.db", scope=bodylimit.SCOPE_ALL
        )
        assert text == "\n".join(
            [
                "/tmp/x.db",
                "口径：单条评论 / result 的 UTF-8 字节数；上限 8192 字节",
                "范围：未归档卡（status ≠ archived）",
                "合计：5 条；超限：3 条",
                "超限清单（3 条）：",
                "  t_b  comment#9  16336 字节",
                "  t_a  comment#7  9000 字节",
                "  t_a  result#-  8193 字节",
            ]
        )

    def test_zero_over_is_single_last_line(self) -> None:
        summary = outputaudit.summarize([("t_a", "comment", "1", LIMIT)])
        text = outputaudit.render(
            summary, db_path="/tmp/x.db", scope=bodylimit.SCOPE_PENDING
        )
        assert text == "\n".join(
            [
                "/tmp/x.db",
                "口径：单条评论 / result 的 UTF-8 字节数；上限 8192 字节",
                "范围：未结卡（status ∉ {done, archived}）",
                "合计：1 条；超限：0 条",
                "超限清单（0 条）：—",
            ]
        )

    def test_limit_number_comes_from_argument(self) -> None:
        summary = outputaudit.summarize([("t_a", "comment", "1", 6)], limit=5)
        text = outputaudit.render(
            summary, db_path="/tmp/x.db", scope=bodylimit.SCOPE_PENDING, limit=5
        )
        assert text.splitlines()[1] == "口径：单条评论 / result 的 UTF-8 字节数；上限 5 字节"
        assert text.splitlines()[-1] == "  t_a  comment#1  6 字节"

    def test_no_trailing_newline_and_no_trailing_spaces(self) -> None:
        summary = outputaudit.summarize(rows_over_and_under())
        text = outputaudit.render(
            summary, db_path="/tmp/x.db", scope=bodylimit.SCOPE_PENDING
        )
        assert not text.endswith("\n")
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_db_path_is_absolutised(self) -> None:
        summary = outputaudit.summarize([])
        text = outputaudit.render(summary, db_path="x.db", scope=bodylimit.SCOPE_PENDING)
        assert text.splitlines()[0].startswith("/")
        assert text.splitlines()[0].endswith("x.db")

    def test_detail_lines_only_for_over_rows(self) -> None:
        summary = outputaudit.summarize(rows_over_and_under())
        text = outputaudit.render(
            summary, db_path="/tmp/x.db", scope=bodylimit.SCOPE_PENDING
        )
        assert "comment#10" not in text
        detail = [line for line in text.splitlines() if line.startswith("  t_")]
        assert len(detail) == 3
