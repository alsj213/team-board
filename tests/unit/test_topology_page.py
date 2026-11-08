"""「Bot 状态」页拓扑图（E3）的页面级断言。

只读 ``web.index_html()`` 的渲染结果，外加真起一个临时端口的服务取 ``GET /``：
不进 payload 层、不改数据库口径，页面结构变了就在这里红。

图与线是页面在浏览器里用 DOM API 现画的，Python 侧拿不到渲染结果，所以这里断言
的是**页面源码里的结构片段**（与 ``test_bots_tabs.py`` 同一套路）：先按函数 /
样式块切成小片再断言，避免整页 ``in`` 带来的假绿。
"""

from __future__ import annotations

import contextlib
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from team_board import web
from tests.fixtures.make_board import FIXTURE_NOW, build_board

#: 拓扑段的容器；页面里嵌套在 #view-bots 之内（所以取 view-bots 的闭合标签要 rindex）
_TOPOLOGY_OPEN = '<section id="topology">'
_BOTS_OPEN = '<section id="view-bots" hidden>'

_TOPOLOGY_BODY = ("function renderTopology(data)", "async function refreshTopology()")
_REFRESH_BODY = ("async function refreshTopology()", "async function refresh()")


def _page() -> str:
    return web.index_html()


def _slice(html: str, start: str, end: str) -> str:
    begin = html.index(start)
    return html[begin : html.index(end, begin)]


def _render_body(html: str) -> str:
    return _slice(html, *_TOPOLOGY_BODY)


def _refresh_body(html: str) -> str:
    return _slice(html, *_REFRESH_BODY)


def _style(html: str) -> str:
    return _slice(html, "<style>", "</style>")


def test_markup_position() -> None:
    """拓扑段在 #view-bots 内、#bots-table 之后（嵌套 section：闭合标签取最后一个）。"""
    html = _page()
    begin = html.index(_BOTS_OPEN)
    close = html.rindex("</section>", begin, html.index("<script>")) + len("</section>")
    view_bots = html[begin:close]

    assert view_bots.startswith(_BOTS_OPEN)
    assert _TOPOLOGY_OPEN in view_bots
    assert view_bots.index(_TOPOLOGY_OPEN) > view_bots.index('<table id="bots-table">')
    assert view_bots.index("</table>") < view_bots.index(_TOPOLOGY_OPEN)


def test_node_markup() -> None:
    """节点：active=true 才亮，否则 idle（CSS opacity .45）；节点内只剩 .name。"""
    html = _page()
    body = _render_body(html)

    assert 'make("div", active ? "topo-node active" : "topo-node idle")' in body
    assert 'make("span", "name", node.display)' in body
    # 团队关系图只画人：节点里没有在跑数、也没有卡 id
    assert 'make("span", "state"' not in body
    assert 'make("span", "card"' not in body
    assert "box.dataset.name = node.name" in body
    assert "box.style.left = spot.left" in body
    assert "box.style.top = spot.top" in body
    # 位置由 level 决定：同行同 top、同层相邻多一个 nodeW + gapX
    assert "top: TOPO_LAYOUT.padY + level * (TOPO_LAYOUT.nodeH + TOPO_LAYOUT.gapY)" in body
    assert "left: left0 + index * (TOPO_LAYOUT.nodeW + TOPO_LAYOUT.gapX)" in body

    style = _style(html)
    assert ".topo-node.active {" in style
    assert ".topo-node.idle { opacity: .45; }" in style
    assert ".topo-node .state" not in style
    assert ".topo-node .card {" not in style


def test_edge_markup() -> None:
    """边：.topo-edge + transform: rotate + 末端箭头；关系图不标卡数。"""
    html = _page()
    body = _render_body(html)

    assert 'make("div", "topo-edge")' in body
    assert '"transform: rotate(" + deg + "deg);"' in body
    assert "Math.atan2(dy, dx) * 180 / Math.PI" in body
    assert 'line.append(make("span", "topo-edge-head"))' in body
    # 边上不标计数：没有中点标签、没有 data-cards、页面里没有 ×
    assert 'make("span", "topo-edge-label"' not in body
    assert "dataset.cards" not in body
    assert "×" not in html
    # 边先入画布（垫在节点下面），节点后入
    assert "lines.concat(boxes)" in body

    style = _style(html)
    assert ".topo-edge {" in style
    assert "transform-origin: 0 0" in style
    assert ".topo-edge-label" not in style


def test_no_card_list() -> None:
    """页面不列运行卡：无清单容器、无 running_cards、无卡 id 拼接、无 +N 计数。"""
    html = _page()
    body = _render_body(html)

    # 清单容器与它的 CSS / 失败路径注释一律不在（整页范围）
    assert "topology-cards" not in html
    # 节点与边都不读 running_cards
    assert "running_cards" not in html
    # 不拼「卡 id + 标题」与「+N」
    assert "cards[0].id" not in body
    assert "(running - 1)" not in body
    assert "card.display" not in body
    # 清单的收集 / 排序 / 落地三处都不在渲染函数里
    assert "pairs.push(" not in body
    assert "pairs.sort(" not in body
    assert "replaceChildren.apply(list" not in body


def test_empty_state() -> None:
    """空看板：亮占位行、清空画布、直接返回（不抛异常）。"""
    body = _render_body(_page())
    branch = body[body.index("if (nodes.length === 0) {"): body.index("empty.hidden = true;")]

    assert "empty.hidden = false" in branch
    assert "graph.replaceChildren()" in branch
    assert "return;" in branch
    assert "throw" not in body
    # 节点非空立刻把占位行收起来，不能两帧都留着
    assert "empty.hidden = true;" in body


def test_all_idle() -> None:
    """全空闲：图照画、所有节点落 idle、无报错行（class 只由 active 决定）。"""
    body = _render_body(_page())

    assert body.count('"topo-node active"') == 1
    assert body.count('"topo-node idle"') == 1
    assert 'make("div", active ? "topo-node active" : "topo-node idle")' in body
    assert "graph.replaceChildren.apply(graph, lines.concat(boxes))" in body
    assert "showBotsError" not in body
    # 节点只消费关系 payload 的 active，不再读 running 计数或 card status
    assert "node.running" not in body
    assert "node.status ===" not in body


def test_required_snippets() -> None:
    """整页含契约要求的片段：拓扑段的 DOM、布局常量、函数名、fetch 与两类节点。"""
    html = _page()

    for needle in (
        '<h2>团队拓扑</h2>',
        'id="topology-empty"',
        'id="topology-graph"',
        'role="img" aria-label="团队派发拓扑图"',
        "暂无派发记录",
        'fetch("/api/topology")',
        "function renderTopology",
        "TOPO_LAYOUT",
        '"topo-node active"',
        '"topo-node idle"',
        "const active = node.active === true;",
        "const TOPO_LAYOUT = { nodeW: 148, nodeH: 52, gapX: 28, gapY: 86, padX: 16, padY: 12 };",
    ):
        assert needle in html, needle

    # 布局常量只有一份（页面里定义一次，Python 侧不再复制第二份数字）
    assert html.count("const TOPO_LAYOUT =") == 1
    # start() 与定时器各调一次，不多不少
    assert html.count("refreshTopology();") == 2
    assert "async function refreshTopology()" in html


def test_refresh_keeps_last_frame() -> None:
    """失败路径：复用 showBotsError 且文案点名「拓扑」，不清图、不碰 #bots-body。"""
    body = _refresh_body(_page())

    assert 'fetch("/api/topology")' in body
    assert 'showBotsError("拓扑刷新失败（HTTP "' in body
    assert 'showBotsError("拓扑刷新失败（" + err + "），保留上一帧")' in body
    assert "，保留上一帧" in body
    assert "renderTopology(data)" in body
    # 失败分支只 return：既不 replaceChildren、也不碰 bots 表格
    assert 'getElementById("bots-body")' not in body
    assert "replaceChildren" not in body


@contextlib.contextmanager
def _running_server(db_path: Path) -> Iterator[str]:
    """起一个临时端口的只读服务（写法同 ``test_bots_tabs.py``）。"""
    server = web.make_server(db_path, port=0, clock=lambda: FIXTURE_NOW)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        assert isinstance(host, str)
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_live(tmp_path: Path) -> None:
    """真起服务：``GET /`` 200，页面里带拓扑段与 ``/api/topology`` 的 fetch。"""
    path = tmp_path / "b.db"
    build_board(path, now=FIXTURE_NOW)

    with _running_server(path) as base, urllib.request.urlopen(base + "/", timeout=5) as resp:
        status = resp.status
        body = resp.read().decode("utf-8")

    assert status == 200
    assert 'id="topology-graph"' in body
    assert 'fetch("/api/topology")' in body
    assert '<h2>团队拓扑</h2>' in body
