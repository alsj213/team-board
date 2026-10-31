"""``cardtemplate`` 的判据单测：红 / warn 两侧 / 存量豁免 / 缺 TEMPLATE 行 / rewrite_hint。

口径逐条照卡 t_7c17f775 评论 #1：``scan`` 只判三件事（固定段落标题 → red、
字节 > 1200 → warn、无 ``TEMPLATE:`` 引用行 → warn），存量（``created_at <
FIXED_SECTION_EPOCH``）不判红。2048 字节硬上限仍归 ``bodylimit``，本文件不碰。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from team_board import bodylimit, cardtemplate

#: 模板引用行（``rewrite_hint`` 文案里必须出现的那一串）
REF = "TEMPLATE: card-template.md#v3"

#: 切点之后的卡（新卡，判红）
NEW_CARD = cardtemplate.FIXED_SECTION_EPOCH
CST = timezone(timedelta(hours=8))


def _body(*lines: str) -> str:
    return "\n".join(lines)


def _padded(nbytes: int) -> str:
    """造一条「带模板引用行、恰好 ``nbytes`` 字节」的正文（ASCII 填充，字节=字符）。"""
    head = REF + "\n"
    assert nbytes >= len(head.encode("utf-8"))
    return head + "a" * (nbytes - len(head.encode("utf-8")))


class TestConstants:
    """常量逐条钉死：改任何一个都要连 ``docs/card-template.md`` 与内核一起改。"""

    def test_byte_thresholds(self) -> None:
        assert (cardtemplate.TARGET_BYTES, cardtemplate.WARN_BYTES) == (800, 1200)

    def test_cutoff_is_2026_11_01_midnight_cst(self) -> None:
        assert cardtemplate.FIXED_SECTION_EPOCH == 1793462400
        assert datetime(2026, 11, 1, tzinfo=CST).timestamp() == cardtemplate.FIXED_SECTION_EPOCH

    def test_headings_exact_and_ordered(self) -> None:
        assert cardtemplate.FIXED_HEADINGS == (
            "完成契约",
            "验收三段式",
            "验收三段",
            "边界默认值",
            "R6 三条",
            "交付方式",
            "长内容去哪",
        )

    def test_fixed_heading_re_is_derived_from_headings(self) -> None:
        """正则必须由 ``FIXED_HEADINGS`` 推出（不许另写字面量正则）。

        字面量期望里 ``R6 三条`` 中间那个空格是 ``R6\\ 三条``：``re.escape`` 自
        3.7 起把空格算进 ``_special_chars_map``（本机 3.10/3.11/3.14 实测都是
        ``R6\\ 三条``，CI 的 3.12 同一张表），所以「照抄评论 #1 里那串不带反斜杠
        的字面量」与「由 ``FIXED_HEADINGS`` 推出」互相矛盾 —— 本卡取后者
        （差异要求第 1 条：推导是硬规，括号里的转写是注释）。``\\ `` 匹配的仍是
        普通空格，行为不受影响（见 ``test_each_heading_is_red`` 的 ``R6 三条`` 用例）。

        """
        derived = r"^##\s*(?:" + "|".join(
            map(re.escape, cardtemplate.FIXED_HEADINGS)
        ) + r")\s*$"
        assert cardtemplate.FIXED_HEADING_RE.pattern == derived
        # 同一件事的字面量期望：内核镜像测试比的就是这一串
        assert cardtemplate.FIXED_HEADING_RE.pattern == (
            r"^##\s*(?:完成契约|验收三段式|验收三段|边界默认值|R6\ 三条|交付方式|长内容去哪)\s*$"
        )
        assert cardtemplate.FIXED_HEADING_RE.flags & re.MULTILINE

    def test_template_ref_re_shape(self) -> None:
        assert cardtemplate.TEMPLATE_REF_RE.pattern == r"^TEMPLATE: card-template\.md#(v\d+)$"
        assert cardtemplate.TEMPLATE_REF_RE.flags & re.MULTILINE
        assert cardtemplate.TEMPLATE_REF_RE.search(REF) is not None

    def test_warn_markers_are_only_two_literals(self) -> None:
        assert cardtemplate.WARN_OVER == "over-1200"
        assert cardtemplate.WARN_NO_TEMPLATE == "no-template-ref"

    def test_template_ref_line_matches_template_constants(self) -> None:
        assert cardtemplate.template_ref_line() == REF
        assert f"TEMPLATE: {cardtemplate.TEMPLATE_FILE}#{cardtemplate.TEMPLATE_VERSION}" == REF


class TestRedFixedHeadings:
    """固定段落标题：命中即 red（新卡）。"""

    @pytest.mark.parametrize("heading", cardtemplate.FIXED_HEADINGS)
    def test_each_heading_is_red(self, heading: str) -> None:
        red, warn = cardtemplate.scan(_body(REF, "", f"## {heading}", "内容"), NEW_CARD)
        assert red == (heading,)
        assert warn == ()

    @pytest.mark.parametrize(
        "line",
        [
            "##完成契约",  # 无空格也命中（\s*）
            "##   完成契约   ",  # 前后多余空白仍命中
            "##\t验收三段",  # tab 也命中
        ],
    )
    def test_whitespace_variants(self, line: str) -> None:
        red, _ = cardtemplate.scan(_body(REF, line), NEW_CARD)
        assert red == (line.strip().lstrip("#").strip(),)

    def test_hits_keep_first_seen_order_and_dedupe(self) -> None:
        red, _ = cardtemplate.scan(
            _body(REF, "## 交付方式", "## 完成契约", "## 交付方式", "## 长内容去哪"), NEW_CARD
        )
        assert red == ("交付方式", "完成契约", "长内容去哪")

    @pytest.mark.parametrize(
        "line",
        [
            "### 完成契约",  # 三级标题不算
            "#### 长内容去哪",
            "完成契约",  # 无 ## 不算
            "前文 ## 完成契约 中段",  # 不是行首也不算
            "## 完成契约（旧）",  # 行尾有尾随文字不算
            "## 目标",  # 非固定段落
            "## 边界与不做",
        ],
    )
    def test_non_hits(self, line: str) -> None:
        red, _ = cardtemplate.scan(_body(REF, line), NEW_CARD)
        assert red == ()

    def test_fixed_hits_helper(self) -> None:
        assert cardtemplate.fixed_hits(None) == ()
        assert cardtemplate.fixed_hits("") == ()
        assert cardtemplate.fixed_hits("## R6 三条") == ("R6 三条",)


class TestWarnBytes:
    """字节 warn：严格大于 ``WARN_BYTES`` 才告警（1200 与 1201 两侧都钉住）。"""

    def test_exactly_1200_is_not_warned(self) -> None:
        body = _padded(1200)
        assert bodylimit.body_bytes(body) == 1200
        assert cardtemplate.scan(body, NEW_CARD) == ((), ())

    def test_1201_is_warned(self) -> None:
        body = _padded(1201)
        assert bodylimit.body_bytes(body) == 1201
        red, warn = cardtemplate.scan(body, NEW_CARD)
        assert (red, warn) == ((), (cardtemplate.WARN_OVER,))

    def test_target_800_is_not_a_warn_line(self) -> None:
        """``TARGET_BYTES`` 是目标不是警线：801 字节不告警。"""
        assert cardtemplate.scan(_padded(800), NEW_CARD) == ((), ())
        assert cardtemplate.scan(_padded(801), NEW_CARD) == ((), ())

    def test_2048_hard_limit_is_not_judged_here(self) -> None:
        """2048 硬上限归 ``bodylimit``：本模块越过它也只报 ``over-1200`` 一条 warn。"""
        body = _padded(2049)
        assert bodylimit.is_over(body) is True  # 上限口径仍在 bodylimit
        assert cardtemplate.scan(body, NEW_CARD) == ((), (cardtemplate.WARN_OVER,))

    def test_none_body_is_zero_bytes(self) -> None:
        _, warn = cardtemplate.scan(None, NEW_CARD)
        assert cardtemplate.WARN_OVER not in warn


class TestStockCutoff:
    """存量（``created_at < cutoff``）不判红，warn 照判。"""

    def test_just_before_cutoff_is_stock(self) -> None:
        body = _body(REF, "## 完成契约")
        assert cardtemplate.scan(body, cardtemplate.FIXED_SECTION_EPOCH - 1)[0] == ()

    def test_exactly_at_cutoff_is_judged(self) -> None:
        body = _body(REF, "## 完成契约")
        assert cardtemplate.scan(body, cardtemplate.FIXED_SECTION_EPOCH)[0] == ("完成契约",)

    def test_long_ago_card_is_stock(self) -> None:
        red, _ = cardtemplate.scan(_body("## 完成契约"), 1)
        assert red == ()

    def test_stock_still_gets_warns(self) -> None:
        body = _body("## 完成契约", "a" * 1300)
        red, warn = cardtemplate.scan(body, cardtemplate.FIXED_SECTION_EPOCH - 1)
        assert red == ()
        assert warn == (cardtemplate.WARN_OVER, cardtemplate.WARN_NO_TEMPLATE)

    def test_null_created_at_is_treated_as_stock(self) -> None:
        assert cardtemplate.is_stock(None) is True
        red, _ = cardtemplate.scan(_body(REF, "## 完成契约"), None)
        assert red == ()

    def test_explicit_cutoff_argument_wins(self) -> None:
        body = _body(REF, "## 完成契约")
        assert cardtemplate.scan(body, 100, cutoff=50)[0] == ("完成契约",)
        assert cardtemplate.scan(body, 49, cutoff=50)[0] == ()


class TestTemplateRef:
    """缺 ``TEMPLATE:`` 引用行 → warn ``no-template-ref``。"""

    def test_missing_ref_line(self) -> None:
        red, warn = cardtemplate.scan(_body("## 目标", "内容"), NEW_CARD)
        assert (red, warn) == ((), (cardtemplate.WARN_NO_TEMPLATE,))

    def test_ref_line_present(self) -> None:
        assert cardtemplate.scan(_body(REF, "## 目标"), NEW_CARD) == ((), ())

    def test_ref_in_middle_of_line_does_not_count(self) -> None:
        """必须**顶格整行**：句中提到不算。"""
        _, warn = cardtemplate.scan(_body("见 " + REF), NEW_CARD)
        assert warn == (cardtemplate.WARN_NO_TEMPLATE,)

    def test_ref_with_trailing_text_does_not_count(self) -> None:
        _, warn = cardtemplate.scan(_body(REF + " 见模板"), NEW_CARD)
        assert warn == (cardtemplate.WARN_NO_TEMPLATE,)

    def test_ref_with_trailing_space_does_not_count(self) -> None:
        _, warn = cardtemplate.scan(_body(REF + " "), NEW_CARD)
        assert warn == (cardtemplate.WARN_NO_TEMPLATE,)

    def test_null_and_empty_body_are_warned(self) -> None:
        assert cardtemplate.scan(None, NEW_CARD) == ((), (cardtemplate.WARN_NO_TEMPLATE,))
        assert cardtemplate.scan("", NEW_CARD) == ((), (cardtemplate.WARN_NO_TEMPLATE,))

    def test_has_template_ref_helper(self) -> None:
        assert cardtemplate.has_template_ref(REF) is True
        assert cardtemplate.has_template_ref(None) is False
        assert cardtemplate.has_template_ref("TEMPLATE: card-template.md#\n") is False


class TestWarnOrder:
    def test_over_then_no_template(self) -> None:
        """两条 warn 同时成立时顺序固定：``over-1200`` 在前。"""
        _, warn = cardtemplate.scan("a" * 1201, NEW_CARD)
        assert warn == ("over-1200", "no-template-ref")


class TestRewriteHint:
    def test_contains_template_ref_line(self) -> None:
        assert "TEMPLATE: card-template.md#v3" in cardtemplate.rewrite_hint("完成契约")

    @pytest.mark.parametrize("heading", cardtemplate.FIXED_HEADINGS)
    def test_hint_mentions_the_hit(self, heading: str) -> None:
        hint = cardtemplate.rewrite_hint(heading)
        assert heading in hint
        assert "TEMPLATE: card-template.md#v3" in hint

    def test_exact_wording(self) -> None:
        assert cardtemplate.rewrite_hint("完成契约") == (
            f"「完成契约」属模板固定段落：删掉它，正文首行改为 {REF}（固定段落由 dispatcher 注入）。"
        )
