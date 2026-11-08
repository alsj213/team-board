"""团队拓扑聚合：把 ``created_by → assignee`` 的派发关系算成节点/边 payload。

看板网页的「团队拓扑」只消费这一层：谁在图里、谁向谁派过卡、谁在哪一层。
数据来源是 ``tasks`` 表出现过的全部名字（**含 archived 卡**），口径与
``bots.py`` 名册同源，但图里同时保留 ``created_by`` 一侧——只开卡不接卡的人
（例如只拆卡的 ``user``）也必须在图上有一个位置。

这是**关系模型，不是历史统计**：边只回答“谁把卡派给谁”，payload 不带卡数、
不带运行卡清单，也不带卡号。运行态只留一个 ``active: bool``（当前是否有
running 卡），用于节点是否亮起；计数归独立的统计接口。

口径（逐条都是判据）：

1. 节点集 = 全部卡的 ``assignee`` ∪ ``created_by``；``NULL`` / 空串 / 纯空白跳过
   （``strip() == ""``），``bots.NON_BOT_ASSIGNEES`` 里的哨兵跳过——**精确等值**，
   ``Nobody`` 与 ``nobody`` 不是一回事，前者仍成节点。两侧同口径。
2. 边集 = (``created_by`` → ``assignee``) 去重；任一侧被第 1 条跳过则不成边；
   自环（``created_by == assignee``）不进边集。**边不带 cards 计数。**
3. ``active``：该名下有 ``status == "running"`` 的卡 → True，否则 False；不输出
   running 卡数、卡号或标题。
4. ``level``：已知名取 :data:`ROLE_LEVEL`；未知名 = 1 + max(已定级且有边指向它的
   来源)，无入边者先按 0 级参与计算（链根，之后下游逐跳 +1）；按名字码位升序逐轮
   relax、最多 ``len(nodes)`` 轮，仍定不下来 → 0（有环也不死循环）。
5. ``display``：已知名取 :data:`ROLE_DISPLAY`；未知名原样输出 ``name``。
6. 排序：``nodes`` 按 ``(level, name)`` 升序、``edges`` 按 ``(from, to)`` 升序。
7. 空输入 → ``{"nodes": [], "edges": []}``。
8. 同一份卡任意顺序输入 → 逐字节同一 payload（排序与定级都在聚合之后统一做）。
9. 纯函数：不读时钟 / 环境变量 / 文件 / 数据库，签名里也没有时间参数——同一份
   ``TopoCard`` 列表永远得到同一个 payload，测试不必冻结时钟。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import bots

#: 角色层级（越小越高）：纵坐标常量，只在这里定义一份
ROLE_LEVEL: dict[str, int] = {
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

#: 角色显示名；未知名不查表，原样输出 name
ROLE_DISPLAY: dict[str, str] = {
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

#: 判定「在跑」的状态字面量（与 ``bots.bots_payload`` 同口径；单独写常量是为了
#: 不在拓扑层引入 status 流转顺序的依赖）
_RUNNING_STATUS = "running"


@dataclass(frozen=True)
class TopoCard:
    """拓扑图需要的一张卡：只有身份、标题与派发关系，没有时间字段。"""

    id: str
    title: str
    assignee: str
    status: str
    created_by: str


def _node_name(value: str | None) -> str:
    """``assignee`` / ``created_by`` 归一：跳过 NULL、空白与哨兵，返回空串表跳过。"""
    name = "" if value is None else str(value)
    if name.strip() == "":
        return ""
    if name in bots.NON_BOT_ASSIGNEES:
        return ""
    return name


def _resolve_levels(names: list[str], sources: dict[str, set[str]]) -> dict[str, int]:
    """定级：已知名查表，未知名按入边逐轮 relax（最多 ``len(names)`` 轮）。

    ``sources[child]`` 是所有向 ``child`` 派过卡的上游名。未知名里**没有入边**的那些
    是链根，先把它们记成 0 级参与计算，下游才能逐跳 +1；其余未知名只有在存在
    「已定级」的上游时才拿到 ``1 + max(上游 level)``。一轮下来没有任何进展就停，
    因此成环也不会死循环，定不下来的一律落到 0。
    """
    levels: dict[str, int] = {name: ROLE_LEVEL[name] for name in names if name in ROLE_LEVEL}
    pending = [name for name in names if name not in levels]

    # 链根：无入边的未知名先按 0 级参与计算（否则下游永远等不到已定级来源）
    for name in pending:
        if not sources.get(name):
            levels[name] = 0

    for _ in range(len(names)):
        progressed = False
        for name in pending:  # names 已按码位升序，轮内顺序确定
            if name in levels:
                continue
            known = [levels[src] for src in sorted(sources.get(name, ())) if src in levels]
            if known:
                levels[name] = 1 + max(known)
                progressed = True
        if not progressed:
            break

    for name in pending:
        levels.setdefault(name, 0)
    return levels


def topology_payload(cards: list[TopoCard]) -> dict[str, Any]:
    """把 ``cards`` 聚合成 ``{"nodes": [...], "edges": [...]}``（规则见模块 docstring）。

    - 一次遍历登记节点、活跃状态与去重后的边；
    - 排序与定级在聚合之后统一做，所以 payload 与 ``cards`` 的输入顺序无关；
    - 名字原样输出（不 strip、不折叠大小写），payload 里看到的就是库里存的那个；
    - 不输出任何计数/卡号/卡标题——这是团队关系，不是历史报表。
    """
    active: set[str] = set()
    edges: set[tuple[str, str]] = set()
    sources: dict[str, set[str]] = {}
    names: set[str] = set()

    for card in cards:
        assignee = _node_name(card.assignee)
        creator = _node_name(card.created_by)
        if assignee:
            names.add(assignee)
        if creator:
            names.add(creator)

        if creator and assignee and creator != assignee:
            edges.add((creator, assignee))
            sources.setdefault(assignee, set()).add(creator)

        if assignee and card.status == _RUNNING_STATUS:
            active.add(assignee)

    ordered_names = sorted(names)
    levels = _resolve_levels(ordered_names, sources)

    nodes: list[dict[str, Any]] = [
        {
            "name": name,
            "display": ROLE_DISPLAY.get(name, name),
            "level": levels[name],
            "active": name in active,
        }
        for name in ordered_names
    ]
    nodes.sort(key=lambda node: (node["level"], node["name"]))

    return {
        "nodes": nodes,
        "edges": [{"from": src, "to": dst} for src, dst in sorted(edges)],
    }
