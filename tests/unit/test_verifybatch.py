"""``verifybatch`` 的纯函数单测：plan 校验（§1）、``batch_id_for`` 确定性、dataclass 契约。

这里不跑 git（``resolve_head`` / ``patch_id`` / ``build_groups`` 的真 git 行为在
``tests/integration/test_verifybatch_grouping.py``）；本文件的四组用例对应本卡（E1）
验收标准第 2 / 3 / 4 条里**不需要仓库**的那部分：

- :class:`TestLoadPlan` —— 合法 plan 的缺省值与 argv 归一；
- :class:`TestLoadPlanRejects` —— 越界/形态非法的**每一条**都抛 ``VerifyBatchError``
  （pr=0 / 负数 / 13 位毫秒 / 天文数字 / 空串 / ``"53"`` / ``True`` / card 形态 /
  units 空 / 字符串形态的 checks …），一条一个 id，打回时能精确指到哪条漏了；
- :class:`TestBatchId` —— 与书写顺序无关、sha 集合变一个字符就变、形态恒 ``b-[0-9a-f]{8}``；
- :class:`TestDataclasses` —— 契约 §2 的字段名与 frozen 语义（下游 E2–E4 靠字段名取数）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from team_board import verifybatch as vb
from team_board.verifybatch import Plan, Unit, VerifyBatchError

CARD_A = "t_aaaaaaaa"
CARD_B = "t_bbbbbbbb"


def write_plan(tmp_path: Path, payload: object) -> Path:
    """把任意 JSON 值写进 tmp 的 ``plan.json``（不合法值也照写，用例就是要它非法）。"""

    path = tmp_path / "plan.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def one_unit(**overrides: object) -> dict[str, object]:
    unit: dict[str, object] = {"card": CARD_A, "pr": 53, "head": "af45471"}
    unit.update(overrides)
    return unit


def plan_with(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"units": [one_unit()]}
    payload.update(overrides)
    return payload


class TestLoadPlan:
    def test_defaults(self, tmp_path: Path) -> None:
        """``base`` 缺省 ``origin/main``；``group_checks`` 缺省三条且解释器是 ``sys.executable``。"""
        plan = vb.load_plan(write_plan(tmp_path, plan_with()))
        assert isinstance(plan, Plan)
        assert plan.base == vb.DEFAULT_BASE == "origin/main"
        assert plan.group_checks == (
            (sys.executable, "-m", "pytest", "-q", "--tb=line"),
            (sys.executable, "-m", "ruff", "check", "."),
            (sys.executable, "-m", "mypy"),
        )
        assert "python" not in plan.group_checks[0]  # 不许写死解释器
        assert plan.units == (Unit(card=CARD_A, pr=53, head="af45471", checks=()),)

    def test_checks_default_empty_and_argv_tupled(self, tmp_path: Path) -> None:
        """``checks`` 可缺省（=空）；给了就归一成 ``tuple[tuple[str, ...], ...]``。"""
        payload = plan_with(
            group_checks=[["ruff", "check", "."]],
            units=[
                one_unit(checks=[["pytest", "-q", "tests/x.py"], ["mypy", "src"]]),
                one_unit(card=CARD_B, pr=57),
            ],
        )
        plan = vb.load_plan(write_plan(tmp_path, payload))
        assert plan.group_checks == (("ruff", "check", "."),)
        assert plan.units[0].checks == (
            ("pytest", "-q", "tests/x.py"),
            ("mypy", "src"),
        )
        assert plan.units[1].checks == ()

    def test_base_override(self, tmp_path: Path) -> None:
        plan = vb.load_plan(write_plan(tmp_path, plan_with(base="b870c6f")))
        assert plan.base == "b870c6f"


class TestLoadPlanRejects:
    """§1 的越界与形态非法：一条一个用例，一律 ``VerifyBatchError``。"""

    @pytest.mark.parametrize(
        "bad_pr",
        [
            pytest.param(0, id="pr=0"),
            pytest.param(-1, id="pr=-1"),
            pytest.param(-999, id="pr=-999"),
            pytest.param(1793160000000, id="pr=13位毫秒"),
            pytest.param(10**30, id="pr=天文数字"),
            pytest.param(1000000000, id="pr=10位超上限"),
            pytest.param("53", id="pr=字符串53"),
            pytest.param("", id="pr=空串"),
            pytest.param("0", id="pr=字符串0"),
            pytest.param(True, id="pr=布尔True"),
            pytest.param(53.0, id="pr=浮点"),
            pytest.param(None, id="pr=null"),
        ],
    )
    def test_bad_pr(self, tmp_path: Path, bad_pr: object) -> None:
        path = write_plan(tmp_path, plan_with(units=[one_unit(pr=bad_pr)]))
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        assert "units[0].pr" in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_card",
        [
            pytest.param("", id="空串"),
            pytest.param("t_AAAAAAAA", id="大写"),
            pytest.param("t_1234567", id="7位"),
            pytest.param("t_123456789", id="9位"),
            pytest.param("b1a3b00f", id="缺前缀"),
            pytest.param("wt/t_aaaaaaaa", id="分支名"),
            pytest.param("t_zzzzzzzz", id="非十六进制"),
            pytest.param(53, id="整数"),
            pytest.param(None, id="null"),
        ],
    )
    def test_bad_card(self, tmp_path: Path, bad_card: object) -> None:
        path = write_plan(tmp_path, plan_with(units=[one_unit(card=bad_card)]))
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        assert "card" in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_head",
        [
            pytest.param("", id="空串"),
            pytest.param("   ", id="全空白"),
            pytest.param("af45 471", id="含空白"),
            pytest.param("af\x00471", id="含NUL"),
            pytest.param(53, id="整数"),
            pytest.param(None, id="null"),
        ],
    )
    def test_bad_head(self, tmp_path: Path, bad_head: object) -> None:
        path = write_plan(tmp_path, plan_with(units=[one_unit(head=bad_head)]))
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        assert "head" in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_checks",
        [
            pytest.param(["pytest", "-q"], id="字符串形态的命令"),
            pytest.param([[]], id="空 argv"),
            pytest.param([[""]], id="空 argv 项"),
            pytest.param([["pytest", ""]], id="含空串项"),
            pytest.param([["pytest", 3]], id="含非字符串项"),
            pytest.param([["pytest", "a\x00b"]], id="含 NUL"),
            pytest.param({"cmd": "pytest"}, id="dict"),
            pytest.param("pytest -q", id="字符串"),
            pytest.param([None], id="null 项"),
        ],
    )
    def test_bad_unit_checks(self, tmp_path: Path, bad_checks: object) -> None:
        path = write_plan(tmp_path, plan_with(units=[one_unit(checks=bad_checks)]))
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        assert "units[0].checks" in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_units",
        [
            pytest.param([], id="空数组"),
            pytest.param({}, id="对象"),
            pytest.param("units", id="字符串"),
            pytest.param(None, id="null"),
        ],
    )
    def test_bad_units(self, tmp_path: Path, bad_units: object) -> None:
        path = write_plan(tmp_path, plan_with(units=bad_units))
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        assert "units" in str(excinfo.value)

    def test_missing_units(self, tmp_path: Path) -> None:
        path = write_plan(tmp_path, {"base": "origin/main"})
        with pytest.raises(VerifyBatchError):
            vb.load_plan(path)

    def test_unit_not_object(self, tmp_path: Path) -> None:
        path = write_plan(tmp_path, plan_with(units=["t_aaaaaaaa"]))
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        assert "units[0]" in str(excinfo.value)

    @pytest.mark.parametrize("missing", ["card", "pr", "head"])
    def test_unit_missing_key(self, tmp_path: Path, missing: str) -> None:
        unit = one_unit()
        del unit[missing]
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(write_plan(tmp_path, plan_with(units=[unit])))
        assert missing in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_base",
        [
            pytest.param("", id="空串"),
            pytest.param(53, id="整数"),
            pytest.param(None, id="null"),
            # 以下四条来自 PR #64 复审（base 与 head 同级校验 + 不许以 '-' 开头）
            pytest.param("origin/main\x00--all", id="含NUL"),
            pytest.param("--help", id="以-开头"),
            pytest.param("-h", id="单破折号选项"),
            pytest.param("a b", id="含空白"),
            pytest.param("   ", id="全空白"),
        ],
    )
    def test_bad_base(self, tmp_path: Path, bad_base: object) -> None:
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(write_plan(tmp_path, plan_with(base=bad_base)))
        assert "base" in str(excinfo.value)

    @pytest.mark.parametrize("bad_base", ["origin/main\x00--all", "--help", "a b"])
    def test_bad_base_fails_in_build_groups_too(self, bad_base: str) -> None:
        """手搓 ``Plan`` 绕开 ``load_plan`` 也要拦：``build_groups`` 自己再验一次 base。

        pm 的复审探针就是直接构造 ``Plan`` 的 —— 只在校验层拦会把缺陷留给下游。
        """

        with pytest.raises(VerifyBatchError) as excinfo:
            vb.build_groups(".", Plan(base=bad_base, group_checks=(), units=()))
        assert "base" in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_group_checks",
        [
            pytest.param("pytest -q", id="字符串"),
            pytest.param([[]], id="空 argv"),
            pytest.param([["pytest", 3]], id="含非字符串项"),
        ],
    )
    def test_bad_group_checks(self, tmp_path: Path, bad_group_checks: object) -> None:
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(write_plan(tmp_path, plan_with(group_checks=bad_group_checks)))
        assert "group_checks" in str(excinfo.value)

    @pytest.mark.parametrize(
        "raw_text",
        [
            pytest.param("", id="空文件"),
            pytest.param("{", id="坏 JSON"),
            pytest.param("[1, 2]", id="顶层数组"),
            pytest.param('"plan"', id="顶层字符串"),
        ],
    )
    def test_bad_json(self, tmp_path: Path, raw_text: str) -> None:
        path = tmp_path / "plan.json"
        path.write_text(raw_text, encoding="utf-8")
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        assert str(excinfo.value)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(tmp_path / "nope.json")
        assert "读不到 plan 文件" in str(excinfo.value)

    @pytest.mark.parametrize(
        "raw_bytes",
        [
            pytest.param(
                b'{"units":[{"card":"t_aaaaaaaa","pr":53,"head":"a\xff"}]}',
                id="非法UTF-8字节",
            ),
            pytest.param(
                '{"units":[{"card":"t_aaaaaaaa","pr":53,"head":"é"}]}'.encode("gbk"),
                id="GBK编码",
            ),
        ],
    )
    def test_non_utf8_plan(self, tmp_path: Path, raw_bytes: bytes) -> None:
        """非 UTF-8 的 plan 必须也是 VerifyBatchError（不能漏成 UnicodeDecodeError）。

        UnicodeDecodeError 是 UnicodeError/ValueError 子类、不是 OSError，只 catch
        OSError 会让这条输入变成 traceback + rc1，打穿「一律 rc2」契约。
        """
        path = tmp_path / "plan.json"
        path.write_bytes(raw_bytes)
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        assert "读不到 plan 文件" in str(excinfo.value)
        assert "UnicodeDecodeError" in type(excinfo.value.__cause__).__name__


class TestBatchId:
    def test_shape_and_length(self) -> None:
        value = vb.batch_id_for(["a" * 40])
        assert re.fullmatch(r"b-[0-9a-f]{8}", value)

    def test_order_independent(self) -> None:
        """同一组 head_sha，plan 里写反也一样（§0 D6）。"""
        a, b = "af45471" + "0" * 33, "034ae4c" + "0" * 33
        assert vb.batch_id_for([a, b]) == vb.batch_id_for([b, a])

    def test_differs_when_member_differs(self) -> None:
        a, b = "a" * 40, "b" * 40
        assert vb.batch_id_for([a]) != vb.batch_id_for([a, b])
        assert vb.batch_id_for([a]) != vb.batch_id_for([b])

    def test_known_vector(self) -> None:
        """钉死算法：``"b-" + sha256("\\n".join(sorted))[:8]``，换个实现立刻红。"""
        import hashlib

        shas = ["034ae4c" + "0" * 33, "af45471" + "0" * 33]
        expected = "b-" + hashlib.sha256("\n".join(sorted(shas)).encode()).hexdigest()[:8]
        assert vb.batch_id_for(shas) == expected


class TestGitCallHardening:
    """PR #64 复审的两条加固：超长输入的回显截断、git 调用超时 → ``VerifyBatchError``。"""

    def test_long_rev_message_is_truncated(self, tmp_path: Path) -> None:
        """``resolve_head(repo, "a" * 100000)`` 的错误消息不许原样回显 10 万字符。"""

        with pytest.raises(VerifyBatchError) as excinfo:
            vb.resolve_head(tmp_path, "a" * 100000)
        message = str(excinfo.value)
        assert len(message) <= vb.ECHO_LIMIT + 100, len(message)
        assert "已截断" in message

    def test_short_rev_message_kept_verbatim(self, tmp_path: Path) -> None:
        """短输入照旧逐字回显（截断不许误伤正常输入的可读性）。"""

        with pytest.raises(VerifyBatchError) as excinfo:
            vb.resolve_head(tmp_path, "deadbeef")
        assert "'deadbeef'" in str(excinfo.value)

    def test_git_timeout_becomes_verify_batch_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``subprocess.run(timeout=...)`` 触发时必须是 ``VerifyBatchError``，不许是 TimeoutExpired。"""

        monkeypatch.setattr(vb, "GIT_TIMEOUT_S", 0.000001)
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.resolve_head(tmp_path, "HEAD")
        assert "超时" in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_pr",
        [
            pytest.param("8" * 300, id="pr=300位字符串"),
            pytest.param(int("9" * 300), id="pr=300位整数"),
        ],
    )
    def test_long_pr_message_is_truncated(self, tmp_path: Path, bad_pr: object) -> None:
        """``pr`` 的三条回显分支（布尔 / 非 int / 越界）也必须走 ``_echo``。

        之前这里直接写 ``{value!r}`` / ``{value}``，300 位的 pr 会把一行错误消息撑到 300+
        字符；qa 复审残留就出在这条路径上（同长度输入在 head/base/card 上已截断）。
        """

        path = write_plan(tmp_path, plan_with(units=[one_unit(pr=bad_pr)]))
        with pytest.raises(VerifyBatchError) as excinfo:
            vb.load_plan(path)
        message = str(excinfo.value)
        assert "units[0].pr" in message
        assert len(message) <= vb.ECHO_LIMIT + 100, len(message)
        assert "已截断" in message

    def test_echo_survives_int_str_digit_limit(self) -> None:
        """位数超 ``int_max_str_digits``（4300）的 int 连 ``repr`` 都抛 ValueError。

        这种值进不了 JSON plan，但 ``_as_pr`` 是公开可调的校验层；回显路径不许把
        格式化异常漏成非 ``VerifyBatchError``。
        """

        text = vb._echo(10**5000)
        assert "位数过多" in text
        assert len(text) <= vb.ECHO_LIMIT


class TestDataclasses:
    def test_fields_match_contract(self) -> None:
        """§2 的字段名与顺序（下游 E2–E4 按名字取数，改名就是契约破坏）。"""
        assert [f.name for f in Unit.__dataclass_fields__.values()] == [
            "card",
            "pr",
            "head",
            "checks",
        ]
        assert [f.name for f in Plan.__dataclass_fields__.values()] == [
            "base",
            "group_checks",
            "units",
        ]
        assert [f.name for f in vb.Group.__dataclass_fields__.values()] == [
            "batch_id",
            "kind",
            "key",
            "patch_id",
            "head_sha",
            "units",
        ]

    def test_frozen(self) -> None:
        unit = Unit(card=CARD_A, pr=53, head="af45471")
        with pytest.raises(Exception):  # noqa: B017 - frozen dataclass 的 FrozenInstanceError 细节不入契约
            unit.pr = 54  # type: ignore[misc]

    def test_kind_constants(self) -> None:
        """``kind`` 的封闭取值只有三个，且与契约字面量一致。"""
        assert (vb.KIND_PATCH, vb.KIND_HEAD, vb.KIND_SOLO) == ("patch", "head", "solo")
