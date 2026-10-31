"""bodylimit.py 的单元测试：字节口径 / 分桶边界 / 汇总 / 整块渲染。

这里的期望文案就是 ``body-audit`` 的格式契约（卡 t_d3625ec7 评论 #1 第 2 节）：
以后要改输出，先改这里的期望值，别在渲染层改一处、测试里跟着改一处。

卡 t_339b3d36 起 ``summarize`` 多吃两列（``created_at`` / ``body`` 原文），输出末尾
多三段（固定段落红清单 / warn 清单 / 存量跳过），所以 ``ROWS`` 与 ``EXPECTED_PENDING``
按新契约重写；前六段（路径 / 口径 / 范围 / 分桶 / 合计 / 明细 / 超限清单）一行未动。
"""

from __future__ import annotations

import pytest

from team_board import cardtemplate
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

#: 恰在切点上的卡 = 新卡（``created_at < cutoff`` 才算存量）
NEW_CARD = cardtemplate.FIXED_SECTION_EPOCH


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
                ("t_d", "todo", "pm", NEW_CARD, "a" * 100),
                ("t_c", "todo", "pm", NEW_CARD, None),
                ("t_b", "todo", "pm", NEW_CARD, "a" * 100),
                ("t_a", "todo", "pm", NEW_CARD, "a" * 5000),
            ]
        )
        assert [row.id for row in summary.rows] == ["t_a", "t_b", "t_d", "t_c"]

    def test_over_list_follows_detail_order(self) -> None:
        summary = summarize(
            [
                ("t_a", "todo", "pm", NEW_CARD, "a" * 4097),
                ("t_b", "todo", "pm", NEW_CARD, "a" * 2049),
                ("t_c", "todo", "pm", NEW_CARD, "a" * 2048),
            ]
        )
        assert summary.over == ("t_a", "t_b")
        assert summary.counts == {
            "total": 3,
            "over": 2,
            "null": 0,
            "red": 0,
            "warn": 3,
            "legacy": 0,
        }

    def test_null_is_counted_but_never_over(self) -> None:
        summary = summarize([("t_a", "todo", "pm", NEW_CARD, None)])
        assert summary.buckets["NULL"] == 1
        assert summary.over == ()
        assert summary.counts == {
            "total": 1,
            "over": 0,
            "null": 1,
            "red": 0,
            "warn": 1,
            "legacy": 0,
        }

    def test_empty_board_has_all_buckets_zero(self) -> None:
        summary = summarize([])
        assert summary.rows == ()
        assert summary.buckets == dict.fromkeys(BUCKET_LABELS, 0)
        assert summary.counts == {
            "total": 0,
            "over": 0,
            "null": 0,
            "red": 0,
            "warn": 0,
            "legacy": 0,
        }
        assert summary.red == ()
        assert summary.warn == ()
        assert summary.legacy == ()

    def test_buckets_cover_every_row(self) -> None:
        summary = summarize(
            [
                ("t_a", "todo", "pm", NEW_CARD, ""),
                ("t_b", "todo", "pm", NEW_CARD, "a" * 512),
                ("t_c", "todo", "pm", NEW_CARD, "a" * 1024),
                ("t_d", "todo", "pm", NEW_CARD, "a" * 2048),
                ("t_e", "todo", "pm", NEW_CARD, "a" * 4096),
                ("t_f", "todo", "pm", NEW_CARD, "a" * 4097),
                ("t_g", "todo", "pm", NEW_CARD, None),
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


class TestFixedSectionAndWarn:
    """卡 t_339b3d36：固定段落红 / 1200B warn / 存量切点三个计数。"""

    def test_fixed_heading_on_new_card_is_red(self) -> None:
        summary = summarize([("t_red", "todo", "pm", NEW_CARD, "## 完成契约\n正文")])
        assert summary.counts["red"] == 1
        assert summary.red[0].id == "t_red"
        assert summary.red[0].hits == ("完成契约",)

    def test_multiple_headings_are_each_reported_once(self) -> None:
        body = "## 完成契约\n## 边界默认值\n## 完成契约\n"
        summary = summarize([("t_red", "todo", "pm", NEW_CARD, body)])
        assert summary.red[0].hits == ("完成契约", "边界默认值")

    def test_stock_card_before_cutoff_is_not_red_but_is_warned(self) -> None:
        summary = summarize([("t_old", "todo", "pm", NEW_CARD - 1, "## 完成契约")])
        assert summary.counts["red"] == 0
        assert summary.red == ()
        assert summary.legacy == ("t_old",)
        assert summary.warn[0].marks == (cardtemplate.WARN_NO_TEMPLATE,)

    def test_cutoff_zero_makes_every_card_new(self) -> None:
        summary = summarize([("t_old", "todo", "pm", 1, "## 完成契约")], cutoff=0)
        assert summary.counts["red"] == 1
        assert summary.counts["legacy"] == 0

    def test_null_created_at_is_treated_as_stock(self) -> None:
        summary = summarize([("t_null", "todo", "pm", None, "## 完成契约")])
        assert summary.counts["red"] == 0
        assert summary.legacy == ("t_null",)

    def test_warn_bytes_line_is_strict(self) -> None:
        at_line = summarize(
            [("t_a", "todo", "pm", NEW_CARD, "a" * cardtemplate.WARN_BYTES)]
        )
        # 等于 1200 字节不算 over-1200，但缺 TEMPLATE 行照样 warn
        assert at_line.warn[0].marks == (cardtemplate.WARN_NO_TEMPLATE,)
        over_line = summarize(
            [("t_a", "todo", "pm", NEW_CARD, "a" * (cardtemplate.WARN_BYTES + 1))]
        )
        assert over_line.warn[0].marks == (
            cardtemplate.WARN_OVER,
            cardtemplate.WARN_NO_TEMPLATE,
        )
        assert over_line.warn[0].nbytes == cardtemplate.WARN_BYTES + 1

    def test_template_ref_line_silences_both_warns(self) -> None:
        summary = summarize(
            [("t_a", "todo", "pm", NEW_CARD, "TEMPLATE: card-template.md#v3\n短正文")]
        )
        assert summary.counts["warn"] == 0
        assert summary.warn == ()


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
超限清单（2 张）：t_e t_a
固定段落红清单（0 张）：—
warn 清单（>1200B 或 缺 TEMPLATE 行，5 张）：
  t_e  4097 字节  over-1200 no-template-ref
  t_a  2049 字节  over-1200 no-template-ref
  t_b  2048 字节  over-1200 no-template-ref
  t_d  0 字节  no-template-ref
  t_c  0 字节  no-template-ref
存量跳过（早于切点，不判红）：0 张"""

ROWS: list[tuple[str, str, str, int | None, str | None]] = [
    ("t_a", "running", "engineer", NEW_CARD, "a" * 2049),
    ("t_b", "todo", "pm", NEW_CARD, "a" * 2048),
    ("t_c", "done", "hatch", NEW_CARD, None),
    ("t_d", "archived", "pm", NEW_CARD, ""),
    ("t_e", "review", "pm", NEW_CARD, "a" * 4097),
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
        summary = summarize([("t_a", "todo", "pm", NEW_CARD, "a" * 2048)])
        text = render(summary, db_path="/tmp/board.db", scope=SCOPE_PENDING)
        assert "合计：1 张；超限：0 张；NULL：0 张" in text
        assert "超限清单（0 张）：—" in text
        assert "固定段落红清单（0 张）：—" in text
        assert "  t_a  todo  pm  2048  ok" in text

    def test_detail_null_row_prints_null_in_bytes_and_verdict(self) -> None:
        summary = summarize([("t_a", "todo", "pm", NEW_CARD, None)])
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
        # 明细段只有标题行，紧接着就是超限清单（新增三段都排在它之后）
        assert lines[index + 1] == "超限清单（0 张）：—"

    def test_red_hint_and_legacy_count_follow_the_over_list(self) -> None:
        summary = summarize(
            [
                ("t_red", "todo", "pm", NEW_CARD, "## 完成契约"),
                ("t_old", "todo", "pm", NEW_CARD - 1, "## 完成契约"),
            ]
        )
        text = render(summary, db_path="/tmp/board.db", scope=SCOPE_PENDING)
        lines = text.splitlines()
        assert "固定段落红清单（1 张）：" in lines
        red_block = lines[lines.index("固定段落红清单（1 张）：") + 1]
        assert red_block.startswith("  t_red  ")
        # 红清单逐卡附 rewrite_hint：必须给出模板引用行的准确写法
        assert cardtemplate.template_ref_line() in red_block
        # 存量卡不进红清单
        assert "t_old" not in red_block
        assert lines[-1] == "存量跳过（早于切点，不判红）：1 张"
