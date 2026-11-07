"""``topology.py`` 的单元测试：字段形状、角色表、节点/边口径、定级、排序、纯函数。

用例名即验收判据的 ``-k`` 过滤器（``topo_card_fields`` / ``role_tables`` /
``node_union`` / ``nobody`` / ``blank_assignee`` / ``self_cards`` /
``running_count`` / ``running_cards`` / ``level_cycle`` / ``unknown_bot`` /
``ordering`` / ``empty_payload`` / ``deterministic``），改名会直接让判据跑空
（pytest -k 无匹配 → rc 5）。

卡一律用 :func:`_card` 现造，不读库、不冻结时钟 —— 本层是纯函数。
"""

from __future__ import annotations

import dataclasses
import json
import random
from typing import cast

import pytest

from team_board import bots
from team_board.fmt import ELLIPSIS, display_width
from team_board.model import TITLE_MAX
from team_board.topology import ROLE_DISPLAY, ROLE_LEVEL, TopoCard, topology_payload

_UNSET: object = object()


def _card(
    card_id: str,
    *,
    assignee: str | None = "",
    created_by: str | None = "",
    status: str = "todo",
    title: object = _UNSET,
) -> TopoCard:
    """造一张卡；``assignee`` / ``created_by`` / ``title`` 允许 None（真实库可为 NULL）。

    ``title`` 不传 → 用 card_id；显式传 None → 卡上就是 NULL，用来验 ``title or ""``。
    """
    return TopoCard(
        id=card_id,
        title=card_id if title is _UNSET else cast(str, title),
        assignee=cast(str, assignee),
        status=status,
        created_by=cast(str, created_by),
    )


def _nodes(payload: dict[str, object]) -> list[dict[str, object]]:
    nodes = payload["nodes"]
    assert isinstance(nodes, list)
    return nodes


def _by_name(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {cast(str, node["name"]): node for node in _nodes(payload)}


def _names(payload: dict[str, object]) -> list[str]:
    return [cast(str, node["name"]) for node in _nodes(payload)]


def _edges(payload: dict[str, object]) -> list[dict[str, object]]:
    edges = payload["edges"]
    assert isinstance(edges, list)
    return edges


# --------------------------------------------------------------------------- #
# E1-1 数据类形状
# --------------------------------------------------------------------------- #


def test_topo_card_fields_order_and_frozen() -> None:
    """字段名与顺序恰为 ``id,title,assignee,status,created_by``，且是 frozen。"""
    assert [field.name for field in dataclasses.fields(TopoCard)] == [
        "id",
        "title",
        "assignee",
        "status",
        "created_by",
    ]
    params = TopoCard.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen is True

    card = TopoCard(id="t_a", title="a", assignee="pm", status="running", created_by="user")
    with pytest.raises(dataclasses.FrozenInstanceError):
        card.assignee = "qa"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# E1-2 角色表逐字
# --------------------------------------------------------------------------- #


def test_role_tables_match_contract() -> None:
    """``ROLE_LEVEL`` / ``ROLE_DISPLAY`` 两张表按契约写死。"""
    assert ROLE_LEVEL == {
        "user": 0,
        "auto-decomposer": 1,
        "chief": 1,
        "pm": 2,
        "engineer": 3,
        "qa": 3,
        "repo": 3,
        "ops": 3,
        "hatch": 3,
        "paimeng": 3,
    }
    assert ROLE_DISPLAY == {
        "user": "老板",
        "chief": "幕僚长",
        "pm": "PM",
        "engineer": "执行者",
        "qa": "QA",
        "repo": "收口",
        "ops": "运维",
        "hatch": "hatch",
        "paimeng": "paimeng",
        "auto-decomposer": "自动拆卡",
    }


# --------------------------------------------------------------------------- #
# E1-4 节点集 = assignee ∪ created_by
# --------------------------------------------------------------------------- #


def test_node_union_assignee_and_created_by() -> None:
    """只由 created_by 出现的名字与只由 assignee 出现的名字都要成节点（含 archived）。"""
    cards = [
        _card("t_1", assignee="pm", created_by="user"),
        _card("t_2", assignee="", created_by="chief", status="triage"),
        _card("t_3", assignee="paimeng", created_by="", status="archived"),
    ]

    payload = topology_payload(cards)

    assert set(_names(payload)) == {"user", "pm", "chief", "paimeng"}
    # 只由 created_by 出现的 user / chief 也在图里
    assert {"user", "chief"} <= set(_names(payload))
    # 只由 assignee 出现的 paimeng 也在图里
    assert "paimeng" in _names(payload)


# --------------------------------------------------------------------------- #
# E1-5 nobody 双侧跳过
# --------------------------------------------------------------------------- #


def test_nobody_skipped_on_both_sides_exact_match() -> None:
    """``nobody`` 两侧都不进节点、不成边；``Nobody`` 大小写不同仍成节点。"""
    cards = [
        _card("t_1", assignee="nobody", created_by="pm"),
        _card("t_2", assignee="pm", created_by="nobody"),
        _card("t_3", assignee="Nobody", created_by="pm", status="running"),
    ]

    payload = topology_payload(cards)

    assert "nobody" not in _names(payload)
    assert "Nobody" in _names(payload)
    assert set(_names(payload)) == {"pm", "Nobody"}
    assert _edges(payload) == [{"from": "pm", "to": "Nobody", "cards": 1}]


# --------------------------------------------------------------------------- #
# E1-6 NULL / 空串 / 纯空白
# --------------------------------------------------------------------------- #


def test_blank_assignee_makes_no_edge_but_creator_still_a_node() -> None:
    """``NULL`` / ``""`` / ``"   "`` 的 assignee 不成边；其 created_by 仍贡献节点。"""
    cards = [
        _card("t_null", assignee=None, created_by="pm"),
        _card("t_empty", assignee="", created_by="pm"),
        _card("t_blank", assignee="   ", created_by="pm", status="running"),
    ]

    payload = topology_payload(cards)

    assert _edges(payload) == []
    assert _names(payload) == ["pm"]
    assert _by_name(payload)["pm"]["self_cards"] == 0
    # assignee 被跳过的卡也不该给任何人记 running
    assert _by_name(payload)["pm"]["running"] == 0
    assert _by_name(payload)["pm"]["status"] == bots.STATUS_IDLE


# --------------------------------------------------------------------------- #
# E1-7 自环
# --------------------------------------------------------------------------- #


def test_self_cards_counted_but_never_an_edge() -> None:
    """pm→pm×3 → 不进 edges，``self_cards == 3``；无自环的节点是 0。"""
    cards = [
        _card("t_1", assignee="pm", created_by="pm", status="running"),
        _card("t_2", assignee="pm", created_by="pm"),
        _card("t_3", assignee="pm", created_by="pm"),
        _card("t_4", assignee="qa", created_by=""),
    ]

    payload = topology_payload(cards)

    assert _edges(payload) == []
    assert all(edge["from"] != edge["to"] for edge in _edges(payload))
    by_name = _by_name(payload)
    assert by_name["pm"]["self_cards"] == 3
    assert by_name["qa"]["self_cards"] == 0


# --------------------------------------------------------------------------- #
# E1-8 status / running
# --------------------------------------------------------------------------- #


def test_running_count_and_status_follow_bots_payload() -> None:
    """有 running 卡 → ``working`` 且 ``running`` = 卡数；没有 → ``idle`` / 0。"""
    cards = [
        _card("t_run_1", assignee="engineer", status="running"),
        _card("t_run_2", assignee="engineer", status="running"),
        _card("t_blocked", assignee="engineer", status="blocked"),
        _card("t_review", assignee="qa", status="review"),
    ]

    by_name = _by_name(topology_payload(cards))

    assert by_name["engineer"]["status"] == bots.STATUS_WORKING
    assert by_name["engineer"]["status"] == "working"
    assert by_name["engineer"]["running"] == 2
    assert isinstance(by_name["engineer"]["running"], int)
    assert by_name["qa"]["status"] == bots.STATUS_IDLE
    assert by_name["qa"]["status"] == "idle"
    assert by_name["qa"]["running"] == 0


# --------------------------------------------------------------------------- #
# E1-9 running_cards 排序与截断
# --------------------------------------------------------------------------- #


def test_running_cards_sorted_by_id_and_title_truncated() -> None:
    """按 ``id`` 码位升序；超长标题以 ``ELLIPSIS`` 结尾且宽度 ≤ ``TITLE_MAX``。"""
    long_title = "中文长标题截断验证" * 6
    cards = [
        _card("t_zz", assignee="engineer", status="running", title="后一张"),
        _card("t_aa", assignee="engineer", status="running", title=long_title),
        _card("t_mm", assignee="engineer", status="running", title=None),
        _card("t_idle", assignee="engineer", status="done", title="不在清单里"),
    ]

    engineer = _by_name(topology_payload(cards))["engineer"]
    running_cards = engineer["running_cards"]
    assert isinstance(running_cards, list)

    assert [row["id"] for row in running_cards] == ["t_aa", "t_mm", "t_zz"]
    assert list(running_cards[0]) == ["id", "title"]

    truncated = cast(str, running_cards[0]["title"])
    assert truncated.endswith(ELLIPSIS)
    assert display_width(truncated) <= TITLE_MAX
    # title 为 NULL 的卡：``title or ""`` → 空串
    assert running_cards[1]["title"] == ""
    # 未在跑的卡不进清单
    assert "t_idle" not in [row["id"] for row in running_cards]


# --------------------------------------------------------------------------- #
# E1-10 环
# --------------------------------------------------------------------------- #


def test_level_cycle_terminates_and_uses_role_level() -> None:
    """pm→chief 与 chief→pm 并存：两节点 level 来自 ``ROLE_LEVEL``，不死循环、可重复。"""
    cards = [
        _card("t_1", assignee="chief", created_by="pm"),
        _card("t_2", assignee="pm", created_by="chief"),
    ]

    first = topology_payload(cards)
    second = topology_payload(cards)

    by_name = _by_name(first)
    assert by_name["pm"]["level"] == ROLE_LEVEL["pm"] == 2
    assert by_name["chief"]["level"] == ROLE_LEVEL["chief"] == 1
    assert all("level" in node for node in _nodes(first))
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# E1-11 未知名
# --------------------------------------------------------------------------- #


def test_unknown_bot_becomes_node_with_level_one_above_source() -> None:
    """``researcher``（只由 pm 开卡）→ 成节点、display 原样、level = pm.level + 1。"""
    cards = [
        _card("t_1", assignee="researcher", created_by="pm", status="running"),
        _card("t_2", assignee="solo", created_by=""),
    ]

    by_name = _by_name(topology_payload(cards))

    assert by_name["researcher"]["display"] == "researcher"
    assert by_name["researcher"]["level"] == ROLE_LEVEL["pm"] + 1 == 3
    # 无入边的未知名 → level 0
    assert by_name["solo"]["level"] == 0
    assert by_name["solo"]["display"] == "solo"


# --------------------------------------------------------------------------- #
# E1-12 排序
# --------------------------------------------------------------------------- #


def test_ordering_nodes_by_level_then_name_and_edges_by_endpoints() -> None:
    """``nodes`` 按 ``(level, name)``、``edges`` 按 ``(from, to)`` 升序；打乱输入不变。"""
    cards = [
        _card("t_1", assignee="qa", created_by="pm"),
        _card("t_2", assignee="engineer", created_by="pm"),
        _card("t_3", assignee="chief", created_by="user"),
        _card("t_4", assignee="pm", created_by="chief"),
        _card("t_5", assignee="engineer", created_by="qa"),
    ]

    payload = topology_payload(cards)
    keys = [(node["level"], node["name"]) for node in _nodes(payload)]
    assert keys == sorted(keys)

    edge_keys = [(edge["from"], edge["to"]) for edge in _edges(payload)]
    assert edge_keys == sorted(edge_keys)
    assert edge_keys == [
        ("chief", "pm"),
        ("pm", "engineer"),
        ("pm", "qa"),
        ("qa", "engineer"),
        ("user", "chief"),
    ]

    shuffled = list(cards)
    random.Random(11).shuffle(shuffled)
    assert _names(topology_payload(shuffled)) == _names(payload)
    assert _edges(topology_payload(shuffled)) == _edges(payload)


# --------------------------------------------------------------------------- #
# E1-13 空输入 / 只有节点
# --------------------------------------------------------------------------- #


def test_empty_payload_and_nodes_only_have_level_zero() -> None:
    """空输入 → 两个空 list；只有节点没有边 → ``edges == []`` 且全体 level 为 0。"""
    assert topology_payload([]) == {"nodes": [], "edges": []}

    nodes_only = [
        _card("t_1", assignee="alpha", created_by=""),
        _card("t_2", assignee="beta", created_by=""),
    ]
    payload = topology_payload(nodes_only)

    assert list(payload) == ["nodes", "edges"]
    assert _edges(payload) == []
    assert [node["level"] for node in _nodes(payload)] == [0, 0]
    assert _names(payload) == ["alpha", "beta"]


# --------------------------------------------------------------------------- #
# E1-14 顺序无关、逐字节确定
# --------------------------------------------------------------------------- #


def test_deterministic_across_input_permutations() -> None:
    """同一批卡正序 / 逆序 / 随机序 → ``json.dumps`` 三次完全相同。"""
    cards = [
        _card("t_a", assignee="pm", created_by="user", status="running"),
        _card("t_b", assignee="engineer", created_by="pm", status="running", title="很长的标题" * 8),
        _card("t_c", assignee="engineer", created_by="pm", status="done"),
        _card("t_d", assignee="pm", created_by="pm"),
        _card("t_e", assignee="researcher", created_by="pm", status="todo"),
        _card("t_f", assignee="", created_by="chief"),
        _card("t_g", assignee="nobody", created_by="pm"),
    ]

    shuffled = list(cards)
    random.Random(3).shuffle(shuffled)

    forward = json.dumps(topology_payload(list(cards)), ensure_ascii=False)
    backward = json.dumps(topology_payload(list(reversed(cards))), ensure_ascii=False)
    mixed = json.dumps(topology_payload(shuffled), ensure_ascii=False)

    assert forward == backward == mixed


# --------------------------------------------------------------------------- #
# 纯函数（不进 -k 判据，守签名与 import 面）
# --------------------------------------------------------------------------- #


def test_pure_function_signature_and_import_surface() -> None:
    """签名只有 ``cards``（没有时间参数）；模块不 import 时钟 / 库 / 文件相关模块。"""
    import ast
    import inspect
    from pathlib import Path

    import team_board.topology as module

    signature = inspect.signature(topology_payload)
    assert list(signature.parameters) == ["cards"]
    assert "dict" in str(signature.return_annotation)

    tree = ast.parse(Path(module.__file__).resolve().read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported & {"time", "datetime", "sqlite3", "os", "pathlib", "subprocess"} == set()
