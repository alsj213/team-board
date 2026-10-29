"""bodylimit.py 的单元测试：字节口径 / 分桶边界 / 汇总 / 整块渲染。

这里的期望文案就是 ``body-audit`` 的格式契约（卡 t_d3625ec7 评论 #1 第 2 节）：
以后要改输出，先改这里的期望值，别在渲染层改一处、测试里跟着改一处。
"""

from __future__ import annotations

import pytest

from team_board.bodylimit import (
    BODY_LIMIT_BYTES,
    BUCKET_LABELS,
    SCOPE_ALL,
    SCOPE_PENDING,
    body_bytes,
    bucket_of,
    is_over,
    render,
    summarize,
    violation_message,
)

LIMIT = 2048


class TestBodyBytes:
    def test_none_is_zero(self) -> None:
        assert body_bytes(None) == 0

    def test_empty_string_is_zero(self) -> None:
        assert body_bytes("") == 0

    def test_single_ascii_char_is_one_byte(self) -> None:
        assert body_bytes("a") == 1

    def test_chinese_uses_utf8_bytes_not_chars(self) -> None:
        # "中文" 2 个字符 = 6 字节：按字符数算会把它当成没超限
        assert body_bytes("中文") == 6

    def test_exactly_at_limit_is_not_over(self) -> None:
        assert body_bytes("a" * LIMIT) == LIMIT
        assert body_bytes("a" * LIMIT) == BODY_LIMIT_BYTES
        assert is_over("a" * LIMIT) is False

    def test_one_byte_over_limit_is_over(self) -> None:
        assert is_over("a" * (LIMIT + 1)) is True

    def test_none_is_not_over(self) -> None:
        assert is_over(None) is False


class TestViolationMessage:
    def test_exact_text(self) -> None:
        assert violation_message(3000) == (
            "body 超过硬上限：3000 字节（UTF-8），上限 2048 字节；"
            "长背景/证据请放附件或卡评论，卡正文只写差异部分。"
        )

    def test_limit_comes_from_the_constant(self) -> None:
        assert f"上限 {BODY_LIMIT_BYTES} 字节" in violation_message(1)


class TestBucketOf:
    @pytest.mark.parametrize(
        ("nbytes", "label"),
        [
            (0, "0"),
            (1, "1-512"),
            (512, "1-512"),
            (513, "513-1024"),
            (1024, "513-1024"),
            (1025, "1025-2048"),
            (2048, "1025-2048"),
            (2049, "2049-4096"),
            (4096, "2049-4096"),
            (4097, "4097-"),
            (10**9, "4097-"),
            (None, "NULL"),
        ],
    )
    def test_right_endpoint_is_inclusive(self, nbytes: int | None, label: str) -> None:
        assert bucket_of(nbytes) == label

    def test_every_bucket_is_reachable_and_label_list_is_stable(self) -> None:
        assert BUCKET_LABELS == (
            "0",
            "1-512",
            "513-1024",
            "1025-2048",
            "2049-4096",
            "4097-",
            "NULL",
        )


class TestSummarize:
    def test_sorts_bytes_desc_then_id_asc_with_null_last(self) -> None:
        summary = summarize(
            [
                ("t_d", "todo", "pm", 100),
                ("t_c", "todo", "pm", None),
                ("t_b", "todo", "pm", 100),
                ("t_a", "todo", "pm", 5000),
            ]
        )
        assert [row.id for row in summary.rows] == ["t_a", "t_b", "t_d", "t_c"]

    def test_over_list_follows_detail_order(self) -> None:
        summary = summarize(
            [
                ("t_a", "todo", "pm", 4097),
                ("t_b", "todo", "pm", 2049),
                ("t_c", "todo", "pm", 2048),
            ]
        )
        assert summary.over == ("t_a", "t_b")
        assert summary.counts == {"total": 3, "over": 2, "null": 0}

    def test_null_is_counted_but_never_over(self) -> None:
        summary = summarize([("t_a", "todo", "pm", None)])
        assert summary.buckets["NULL"] == 1
        assert summary.over == ()
        assert summary.counts == {"total": 1, "over": 0, "null": 1}

    def test_empty_board_has_all_buckets_zero(self) -> None:
        summary = summarize([])
        assert summary.rows == ()
        assert summary.buckets == dict.fromkeys(BUCKET_LABELS, 0)
        assert summary.counts == {"total": 0, "over": 0, "null": 0}

    def test_buckets_cover_every_row(self) -> None:
        summary = summarize(
            [
                ("t_a", "todo", "pm", 0),
                ("t_b", "todo", "pm", 512),
                ("t_c", "todo", "pm", 1024),
                ("t_d", "todo", "pm", 2048),
                ("t_e", "todo", "pm", 4096),
                ("t_f", "todo", "pm", 4097),
                ("t_g", "todo", "pm", None),
            ]
        )
        assert summary.buckets == {
            "0": 1,
            "1-512": 1,
            "513-1024": 1,
            "1025-2048": 1,
            "2049-4096": 1,
            "4097-": 1,
            "NULL": 1,
        }
        assert sum(summary.buckets.values()) == summary.counts["total"] == 7


#: 一张小板的期望整块输出（格式契约的金标准）
EXPECTED_PENDING = """\
/tmp/board.db
口径：body 为 UTF-8 字节数；NULL body 记 NULL；上限 2048 字节
范围：未结卡（status ∉ {done, archived}）
分桶：
  0          1
  1-512      0
  513-1024   0
  1025-2048  1
  2049-4096  1
  4097-      1
  NULL       1
合计：5 张；超限：2 张；NULL：1 张
明细（字节数降序：卡号 / 状态 / 负责人 / 字节 / 判定）：
  t_e  review    pm        4097  OVER
  t_a  running   engineer  2049  OVER
  t_b  todo      pm        2048  ok
  t_d  archived  pm           0  ok
  t_c  done      hatch     NULL  NULL
超限清单（2 张）：t_e t_a"""

ROWS: list[tuple[str, str, str, int | None]] = [
    ("t_a", "running", "engineer", 2049),
    ("t_b", "todo", "pm", 2048),
    ("t_c", "done", "hatch", None),
    ("t_d", "archived", "pm", 0),
    ("t_e", "review", "pm", 4097),
]


class TestRender:
    def test_whole_block_pending_scope(self) -> None:
        summary = summarize(ROWS)
        assert render(summary, db_path="/tmp/board.db", scope=SCOPE_PENDING) == (
            EXPECTED_PENDING
        )

    def test_all_scope_changes_only_the_scope_line(self) -> None:
        summary = summarize(ROWS)
        text = render(summary, db_path="/tmp/board.db", scope=SCOPE_ALL)
        assert "范围：未归档卡（status ≠ archived）" in text
        assert text.replace("未归档卡（status ≠ archived）", "未结卡（status ∉ {done, archived}）") == (
            EXPECTED_PENDING
        )

    def test_no_trailing_newline(self) -> None:
        text = render(summarize(ROWS), db_path="/tmp/board.db", scope=SCOPE_PENDING)
        assert not text.endswith("\n")

    def test_zero_over_board_uses_dash_for_empty_list(self) -> None:
        summary = summarize([("t_a", "todo", "pm", 2048)])
        text = render(summary, db_path="/tmp/board.db", scope=SCOPE_PENDING)
        assert "合计：1 张；超限：0 张；NULL：0 张" in text
        assert text.endswith("超限清单（0 张）：—")
        assert "  t_a  todo  pm  2048  ok" in text

    def test_detail_null_row_prints_null_in_bytes_and_verdict(self) -> None:
        summary = summarize([("t_a", "todo", "pm", None)])
        text = render(summary, db_path="/tmp/board.db", scope=SCOPE_PENDING)
        assert "  t_a  todo  pm  NULL  NULL" in text

    def test_db_path_is_absolutized(self) -> None:
        summary = summarize([])
        text = render(summary, db_path="rel/board.db", scope=SCOPE_PENDING)
        assert text.splitlines()[0].startswith("/")
        assert text.splitlines()[0].endswith("rel/board.db")

    def test_empty_board_prints_headers_only(self) -> None:
        text = render(summarize([]), db_path="/tmp/board.db", scope=SCOPE_PENDING)
        lines = text.splitlines()
        assert "合计：0 张；超限：0 张；NULL：0 张" in lines
        index = lines.index("明细（字节数降序：卡号 / 状态 / 负责人 / 字节 / 判定）：")
        # 明细段只有标题行，紧接着就是超限清单
        assert lines[index + 1] == "超限清单（0 张）：—"
