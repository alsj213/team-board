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

#: 节点内的三行文字（display / 在跑数 / 首张 running 卡 id）
_NODE_SPANS = (
    'make("span", "name", node.display)',
    'make("span", "state", running > 0 ? "在跑 " + running : "空闲")',
    'make("span", "card", card)',
)


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
    """节点：running>0 才 active，否则 idle（CSS opacity .45）；内含 .name/.state/.card。"""
    html = _page()
    body = _render_body(html)

    assert 'make("div", running > 0 ? "topo-node active" : "topo-node idle")' in body
    for span in _NODE_SPANS:
        assert span in body
    assert "box.dataset.name = node.name" in body
    assert "box.style.left = spot.left" in body
    assert "box.style.top = spot.top" in body
    # 位置由 level 决定：同行同 top、同层相邻多一个 nodeW + gapX
    assert "top: TOPO_LAYOUT.padY + level * (TOPO_LAYOUT.nodeH + TOPO_LAYOUT.gapY)" in body
    assert "left: left0 + index * (TOPO_LAYOUT.nodeW + TOPO_LAYOUT.gapX)" in body

    style = _style(html)
    assert ".topo-node.active {" in style
    assert ".topo-node.idle { opacity: .45; }" in style


def test_edge_markup() -> None:
    """边：.topo-edge + data-cards + transform: rotate + 中点 ×N 标签 + 末端箭头。"""
    html = _page()
    body = _render_body(html)

    assert 'make("div", "topo-edge")' in body
    assert "line.dataset.cards = String(edge.cards)" in body
    assert '"transform: rotate(" + deg + "deg);"' in body
    assert 'make("span", "topo-edge-label", "×" + edge.cards)' in body
    assert "Math.atan2(dy, dx) * 180 / Math.PI" in body
    assert 'line.append(label, make("span", "topo-edge-head"))' in body
    # 边先入画布（垫在节点下面），节点后入
    assert "lines.concat(boxes)" in body

    style = _style(html)
    assert ".topo-edge {" in style
    assert "transform-origin: 0 0" in style


def test_running_list() -> None:
    """在跑卡清单：每项「display · 卡 id 标题」，按 (display, id) 升序排序。"""
    body = _render_body(_page())

    assert 'document.getElementById("topology-cards")' in body
    assert 'card.display + " · " + card.id + tail' in body
    assert 'const tail = card.title ? " " + card.title : ""' in body
    assert "pairs.sort(" in body
    assert "a.display < b.display ? -1 : 1" in body
    assert "a.id < b.id ? -1 : 1" in body
    # 排序在收集之后统一做（后端给的顺序不作数）
    assert body.index("pairs.sort(") > body.index("pairs.push(")
    # 空清单也必须落到一次清空（不是留着上一帧）
    assert "list.replaceChildren.apply(list, pairs.map(" in body


def test_empty_state() -> None:
    """空看板：亮占位行、清空图与清单、直接返回（不抛异常）。"""
    body = _render_body(_page())
    branch = body[body.index("if (nodes.length === 0) {"): body.index("empty.hidden = true;")]

    assert "empty.hidden = false" in branch
    assert "graph.replaceChildren()" in branch
    assert "list.replaceChildren()" in branch
    assert "return;" in branch
    assert "throw" not in body
    # 节点非空立刻把占位行收起来，不能两帧都留着
    assert "empty.hidden = true;" in body


def test_all_idle() -> None:
    """全空闲：图照画、所有节点落 idle、无报错行（class 只由 running>0 决定）。"""
    body = _render_body(_page())

    assert body.count('"topo-node active"') == 1
    assert body.count('"topo-node idle"') == 1
    assert 'make("div", running > 0 ? "topo-node active" : "topo-node idle")' in body
    assert "graph.replaceChildren.apply(graph, lines.concat(boxes))" in body
    assert "showBotsError" not in body
    # active 的条件只有 running，没有第二条按 status 涂亮的路径
    assert "node.status === \"working\"" not in body


def test_required_snippets() -> None:
    """整页含契约要求的片段：五段 DOM、布局常量、函数名、fetch 与两类节点。"""
    html = _page()

    for needle in (
        '<h2>团队拓扑</h2>',
        'id="topology-empty"',
        'id="topology-graph"',
        'id="topology-cards"',
        'role="img" aria-label="团队派发拓扑图"',
        "暂无派发记录",
        'fetch("/api/topology")',
        "function renderTopology",
        "TOPO_LAYOUT",
        '"topo-node active"',
        '"topo-node idle"',
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
