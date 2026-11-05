"""``bots.py`` 的单元测试：名册来源、哨兵、状态判定、计数、排序、行形状、纯函数。

用例名即验收判据的 ``-k`` 过滤器（``empty_board`` / ``null_and_blank`` /
``sentinel_nobody`` / ``archived`` / ``status`` / ``running_count`` / ``order`` /
``row_keys`` / ``pure``），改名会直接让判据跑空（pytest -k 无匹配 → rc 5）。

卡一律用 :func:`_task` 现造，不读库、不冻结时钟 —— 本层是纯函数。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import cast

from team_board import bots
from team_board.model import Task


def _task(name: str, assignee: str | None, status: str) -> Task:
    """造一张卡；``assignee`` 允许 None（真实库的 assignee 列可为 NULL）。"""
    return Task(
        id=f"t_{name}",
        title=name,
        assignee=cast(str, assignee),
        status=status,
        created_at=1_000,
        started_at=None,
        completed_at=None,
    )


def _names(payload: dict[str, object]) -> list[str]:
    rows = payload["bots"]
    assert isinstance(rows, list)
    return [row["name"] for row in rows]


# --------------------------------------------------------------------------- #
# 1) 空板
# --------------------------------------------------------------------------- #


def test_empty_board_yields_empty_bots_list() -> None:
    """空输入 → ``{"bots": []}``（键只有 bots，值是空 list）。"""
    payload = bots.bots_payload([])

    assert payload == {"bots": []}
    assert list(payload) == ["bots"]


# --------------------------------------------------------------------------- #
# 2) NULL / 空串 / 纯空白
# --------------------------------------------------------------------------- #


def test_null_and_blank_assignees_skipped() -> None:
    """NULL / ``""`` / ``"  "`` 都不入名册（strip 后为空即跳过）。"""
    tasks = [
        _task("null_assignee", None, "running"),
        _task("empty_assignee", "", "running"),
        _task("blank_assignee", "  ", "todo"),
        _task("tab_assignee", "\t \n", "archived"),
    ]

    assert bots.bots_payload(tasks) == {"bots": []}


# --------------------------------------------------------------------------- #
# 3) 哨兵 nobody 精确等值
# --------------------------------------------------------------------------- #


def test_sentinel_nobody_exact_match() -> None:
    """``nobody`` 跳过；``Nobody`` 大小写不同 → 算 bot（精确等值比较）。"""
    tasks = [
        _task("nobody_running", "nobody", "running"),
        _task("nobody_archived", "nobody", "archived"),
        _task("capitalized", "Nobody", "running"),
    ]

    payload = bots.bots_payload(tasks)

    assert _names(payload) == ["Nobody"]
    assert payload["bots"] == [{"name": "Nobody", "status": "working", "running": 1}]


def test_module_constants_match_contract() -> None:
    """常量按契约写死：哨兵集合与两个状态字面量。"""
    assert frozenset({"nobody"}) == bots.NON_BOT_ASSIGNEES
    assert bots.STATUS_WORKING == "working"
    assert bots.STATUS_IDLE == "idle"


# --------------------------------------------------------------------------- #
# 4) 只剩 archived 卡
# --------------------------------------------------------------------------- #


def test_archived_only_assignee_still_listed_idle() -> None:
    """只有 archived 卡的 assignee 仍入册，且没有 running 卡 → idle / 0。"""
    tasks = [
        _task("old_card", "paimeng", "archived"),
        _task("older_card", "paimeng", "archived"),
        _task("live_card", "qa", "review"),
    ]

    payload = bots.bots_payload(tasks)

    assert _names(payload) == ["paimeng", "qa"]
    assert payload["bots"] == [
        {"name": "paimeng", "status": "idle", "running": 0},
        {"name": "qa", "status": "idle", "running": 0},
    ]


# --------------------------------------------------------------------------- #
# 5) 状态判定
# --------------------------------------------------------------------------- #


def test_status_working_when_any_card_running() -> None:
    """running + blocked + review 混合 → working（只看有没有 running 卡）。"""
    tasks = [
        _task("mixed_running", "engineer", "running"),
        _task("mixed_blocked", "engineer", "blocked"),
        _task("mixed_review", "engineer", "review"),
        _task("only_review", "qa", "review"),
        _task("only_done", "pm", "done"),
        _task("only_triage", "hatch", "triage"),
    ]

    payload = bots.bots_payload(tasks)
    by_name = {row["name"]: row for row in payload["bots"]}

    assert by_name["engineer"] == {"name": "engineer", "status": "working", "running": 1}
    for idle_name in ("qa", "pm", "hatch"):
        assert by_name[idle_name]["status"] == "idle"
        assert by_name[idle_name]["running"] == 0

    # running 在前，三个 idle 按 name 码位升序
    assert _names(payload) == ["engineer", "hatch", "pm", "qa"]


# --------------------------------------------------------------------------- #
# 6) running 计数
# --------------------------------------------------------------------------- #


def test_running_count_two_running_cards() -> None:
    """同一 bot 2 张 running → ``running=2``；idle 的行恒 0（不是 None）。"""
    tasks = [
        _task("run_one", "engineer", "running"),
        _task("run_two", "engineer", "running"),
        _task("idle_one", "qa", "done"),
    ]

    payload = bots.bots_payload(tasks)
    by_name = {row["name"]: row for row in payload["bots"]}

    assert by_name["engineer"]["running"] == 2
    assert isinstance(by_name["engineer"]["running"], int)
    assert by_name["qa"]["running"] == 0
    assert by_name["qa"]["status"] == "idle"


# --------------------------------------------------------------------------- #
# 7) 排序
# --------------------------------------------------------------------------- #


def test_order_working_first_then_name_codepoint() -> None:
    """``engineer`` / ``pm`` working，``qa`` idle → ``engineer, pm, qa``。

    输入顺序刻意打乱：排序只看 ``(0 if working else 1, name)``，与输入无关。
    """
    tasks = [
        _task("qa_card", "qa", "ready"),
        _task("pm_card", "pm", "running"),
        _task("engineer_card", "engineer", "running"),
    ]

    assert _names(bots.bots_payload(tasks)) == ["engineer", "pm", "qa"]


def test_order_is_stable_across_input_permutations() -> None:
    """同一批卡换输入顺序，名册顺序逐字节相同（纯函数的直接推论）。"""
    cards = [
        _task("zeta", "zeta", "running"),
        _task("alpha", "alpha", "todo"),
        _task("beta", "beta", "running"),
    ]

    forward = bots.bots_payload(list(cards))
    backward = bots.bots_payload(list(reversed(cards)))

    assert forward == backward
    assert _names(forward) == ["beta", "zeta", "alpha"]


# --------------------------------------------------------------------------- #
# 8) 行形状
# --------------------------------------------------------------------------- #


def test_row_keys_are_name_status_running() -> None:
    """每行键顺序恰为 ``name, status, running``，类型 ``str, str, int``。"""
    payload = bots.bots_payload([_task("shape", "engineer", "running")])
    row = payload["bots"][0]

    assert list(row) == ["name", "status", "running"]
    assert list(payload) == ["bots"]
    assert (row["name"], row["status"], row["running"]) == ("engineer", "working", 1)
    assert isinstance(row["name"], str)
    assert isinstance(row["status"], str)
    assert isinstance(row["running"], int)


# --------------------------------------------------------------------------- #
# 9) 纯函数
# --------------------------------------------------------------------------- #


def test_pure_function_no_clock_no_io() -> None:
    """签名只有 ``tasks``（没有 now），源码不引用时钟 / 文件 / 数据库。

    判据用 AST（不 grep 原文）：注释与 docstring 里写「不读时钟」不该被误判，
    而 ``import time`` / ``time.time()`` / ``open(...)`` 这类真实引用必须抓得到；
    末尾再补一条字面子串断言，保证这四个 token 在整个模块里都不出现。
    """
    signature = inspect.signature(bots.bots_payload)
    assert list(signature.parameters) == ["tasks"]
    assert "now" not in signature.parameters

    source_path = Path(bots.__file__).resolve()
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported & {"time", "datetime", "sqlite3", "os", "pathlib"} == set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert node.value.id not in {"time", "datetime", "sqlite3"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open"

    for token in ("time.", "datetime", "open(", "sqlite3"):
        assert token not in source
