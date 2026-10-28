"""fmt.py 的单元测试：显示宽度 / 按宽度截断 / 等宽表格。

这些期望值是 E3–E5 输出格式的地基：以后要改格式，先改这里的期望值。
"""

from __future__ import annotations

import pytest

from team_board.fmt import display_width, format_table, truncate


class TestDisplayWidth:
    def test_ascii_counts_one_column_per_char(self):
        assert display_width("abc") == 3

    def test_chinese_counts_two_columns_per_char(self):
        assert display_width("中文") == 4

    def test_horizontal_ellipsis_counts_one_column(self):
        assert display_width("…") == 1

    def test_mixed_ascii_and_chinese(self):
        assert display_width("a中文") == 5

    def test_empty_string_is_zero(self):
        assert display_width("") == 0

    def test_fullwidth_punctuation_counts_two_columns(self):
        assert display_width("（）") == 4


class TestTruncate:
    def test_exact_fit_is_returned_unchanged(self):
        assert truncate("abcd", 4) == "abcd"

    def test_chinese_exact_fit_is_returned_unchanged(self):
        assert truncate("中文", 4) == "中文"

    def test_over_by_one_ends_with_ellipsis_and_fills_width(self):
        out = truncate("abcdef", 5)
        assert out == "abcd…"
        assert display_width(out) == 5

    def test_chinese_over_by_one(self):
        out = truncate("中文中文中文", 5)
        assert out == "中文…"
        assert display_width(out) == 5

    def test_wide_char_is_never_split(self):
        # width-1 = 3 为奇数：留下的 1 列空位放不下一个全角字符，结果是 3 列而不是 4 列
        out = truncate("中文中文", 4)
        assert out == "中…"
        assert display_width(out) == 3
        assert "中文中文".startswith(out[:-1])  # 前缀是原文前缀，没劈开全角字符

    def test_long_chinese_title_at_title_max(self):
        title = "中文长标题截断验证" * 6
        assert display_width(title) >= 60
        out = truncate(title, 40)
        assert out.endswith("…")
        assert display_width(out) == 39  # 19 个汉字 38 列 + "…"
        assert title.startswith(out[:-1])

    def test_width_zero_and_negative_return_empty(self):
        assert truncate("中文", 0) == ""
        assert truncate("中文", -3) == ""

    def test_width_one_leaves_only_ellipsis(self):
        assert truncate("中", 1) == "…"
        assert display_width(truncate("中", 1)) == 1

    @pytest.mark.parametrize("width", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
    def test_result_width_stays_within_one_column_of_target(self, width):
        text = "a中b文c（）d中"
        assert display_width(text) > width  # 保证确实发生了截断
        out = truncate(text, width)
        assert out.endswith("…")
        assert display_width(out) <= width
        assert display_width(out) >= width - 1
        assert text.startswith(out[:-1])


# 与 E3 的 list 表同形的 5 列表（含中文表头 + 右对齐的耗时列）
LIST_HEADERS = ["卡号", "负责人", "状态", "耗时(分)", "标题"]
LIST_ROWS = [
    ["t_0001", "engineer", "running", "75", "骨架接起来"],
    ["t_0002", "pm", "blocked", "120", "等授权"],
]

EXPECTED_LIST_TABLE = (
    "卡号    负责人    状态     耗时(分)  标题\n"
    "------  --------  -------  --------  ----------\n"
    "t_0001  engineer  running        75  骨架接起来\n"
    "t_0002  pm        blocked       120  等授权"
)


class TestFormatTable:
    def test_full_table_is_pinned_char_by_char(self):
        out = format_table(LIST_HEADERS, LIST_ROWS, right_align=(3,))
        assert out == EXPECTED_LIST_TABLE
        assert not out.endswith("\n")

    def test_each_line_width_matches_the_laid_out_columns(self):
        lines = format_table(LIST_HEADERS, LIST_ROWS, right_align=(3,)).split("\n")
        # 列宽 6/8/7/8/10 + 4 个 2 空隔
        full = 6 + 8 + 7 + 8 + 10 + 2 * 4
        assert [display_width(line) for line in lines] == [full - 6, full, full, full - 4]

    def test_no_line_ends_with_spaces(self):
        lines = format_table(LIST_HEADERS, LIST_ROWS, right_align=(3,)).split("\n")
        for line in lines:
            assert line == line.rstrip(), f"行尾有空格: {line!r}"

    def test_separator_dashes_match_column_widths(self):
        lines = format_table(LIST_HEADERS, LIST_ROWS, right_align=(3,)).split("\n")
        assert len(lines) == 2 + len(LIST_ROWS)
        assert lines[1].split("  ") == [
            "-" * 6,
            "-" * 8,
            "-" * 7,
            "-" * 8,
            "-" * 10,
        ]

    def test_all_lines_same_width_when_last_column_fills(self):
        out = format_table(["卡号", "标题"], [["a", "中文"], ["bb", "词语"]])
        assert out == "卡号  标题\n----  ----\na     中文\nbb    词语"
        assert {display_width(line) for line in out.split("\n")} == {10}

    def test_short_last_column_trims_padding_only(self):
        out = format_table(["名称", "标题"], [["甲", "短"]])
        lines = out.split("\n")
        assert lines[0] == "名称  标题"
        assert lines[1] == "----  ----"
        assert lines[2] == "甲    短"
        assert display_width(lines[2]) == 8  # 短一列宽 2，右侧补的空格被裁掉
        assert lines[2] == lines[2].rstrip()

    def test_right_align_pushes_cells_right(self):
        out = format_table(["名", "n"], [["甲", "1"], ["乙", "22"]], right_align=(1,))
        lines = out.split("\n")
        assert lines[0] == "名   n"
        assert lines[2] == "甲   1"
        assert lines[3] == "乙  22"
        assert display_width(lines[2]) == display_width(lines[3])

    def test_left_align_is_the_default(self):
        out = format_table(["名", "n"], [["甲", "1"], ["乙", "22"]])
        lines = out.split("\n")
        assert lines[0] == "名  n"
        assert lines[2] == "甲  1"
        assert lines[3] == "乙  22"
        assert display_width(lines[2]) != display_width(lines[3])

    def test_single_row_and_single_column(self):
        assert format_table(["a"], [["b"]]) == "a\n-\nb"

    def test_empty_rows_uses_header_width_only(self):
        assert format_table(["卡号", "标题"], []) == "卡号  标题\n----  ----"

    def test_no_headers_returns_empty_string(self):
        assert format_table([], []) == ""

    def test_row_shorter_than_headers_is_padded(self):
        assert format_table(["a", "b"], [["x"]]) == "a  b\n-  -\nx"
