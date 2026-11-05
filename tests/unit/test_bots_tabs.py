"""Bot 状态标签页的页面级断言（E2）。

只读 ``web.index_html()`` 的渲染结果，外加真起一个临时端口的服务取 ``GET /``：
不进 payload 层、不改数据库口径，页面结构变了就在这里红。
"""

from __future__ import annotations

import contextlib
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from team_board import web
from tests.fixtures.make_board import FIXTURE_NOW, build_board

#: 新起的页面里，卡片视图的容器到 bot 容器之间的切片起点
_CARDS_OPEN = '<div id="view-cards">'
_BOTS_OPEN = '<section id="view-bots" hidden>'


@contextlib.contextmanager
def _running_server(db_path: Path) -> Iterator[str]:
    """起一个临时端口的只读服务（写法同 ``tests/integration/test_web_server.py``）。"""
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


def test_tabs_markup_complete() -> None:
    """标签页结构逐字齐：两个 tab、两个容器、bot 容器默认 hidden。"""
    html = web.index_html()

    assert '<nav class="tabs" role="tablist">' in html
    assert html.count('role="tab"') == 2
    assert (
        'id="tab-cards" data-view="cards" aria-selected="true" aria-controls="view-cards"'
        in html
    )
    assert (
        'id="tab-bots" data-view="bots" aria-selected="false" aria-controls="view-bots"'
        in html
    )
    assert _CARDS_OPEN in html
    assert _BOTS_OPEN in html
    assert 'id="bots-error" role="alert"' in html
    assert 'id="bots-table"' in html
    assert 'id="bots-body"' in html
    # hidden 必须靠 CSS 强制：将来给容器加 display 也不会顶掉 UA 的 hidden
    assert "[hidden] { display: none !important; }" in html
    # 默认视图是卡片：卡片容器在 bot 容器之前
    assert html.index(_CARDS_OPEN) < html.index(_BOTS_OPEN)
    # 自动刷新开关仍在 header 里，没被挪进任何标签页
    header = html[html.index("<header>") : html.index("</header>")]
    assert 'id="autorefresh"' in header


def test_cards_view_intact() -> None:
    """卡片视图逐字原样：统计行、错误行、四列、自动刷新开关。"""
    html = web.index_html()
    cards = html[html.index(_CARDS_OPEN) : html.index(_BOTS_OPEN)]

    assert '<div id="summary"></div>' in cards
    assert '<div id="error" role="alert"></div>' in cards
    assert '<main id="main">' in cards
    for name in ("running", "queued", "blocked", "done"):
        assert f'<section class="col" data-col="{name}"><h2>{name}</h2>' in cards
    assert (
        '<label><input type="checkbox" id="autorefresh" checked> 自动刷新</label>' in html
    )
    # 卡片视图的刷新路径没被 bot 抢走：refresh() 仍只 fetch summary + board
    refresh_body = html[html.index("async function refresh()") : html.index("function start()")]
    assert 'fetch("/api/summary")' in refresh_body
    assert 'fetch("/api/board")' in refresh_body
    assert 'fetch("/api/bots")' not in refresh_body


def test_no_dollar_no_external_no_innerhtml() -> None:
    """模板硬约束：无 ``$`` 残留、零外部引用、不拼 innerHTML、切视图不重建。"""
    html = web.index_html()

    # _PAGE_TEMPLATE 是 string.Template：渲染完不许再有 $（含模板字符串写法）
    assert "$" not in html
    assert "${" not in html
    for needle in ("http://", "https://", "<script src=", '<link rel="stylesheet"'):
        assert needle not in html
    assert "innerHTML" not in html
    assert "reverse()" not in html
    # 页码仍是模块级单例，刷新路径不复位
    assert html.count("const pageIndex = {") == 1
    assert "pageIndex[name] = 0" not in html

    show_view = html[html.index("function showView") : html.index("function make(")]
    assert "replaceChildren" not in show_view
    # 切视图只改 hidden / aria-selected：不发请求、不重启定时器
    assert "fetch(" not in show_view
    assert "setInterval" not in show_view
    assert "clearInterval" not in show_view

    # bot 状态另开一条刷新路径，失败只写 #bots-error 并保留上一帧
    bots_body = html[
        html.index("async function refreshBots()") : html.index("async function refresh()")
    ]
    assert 'fetch("/api/bots")' in bots_body
    assert 'showBotsError("Bot 状态刷新失败（HTTP "' in bots_body
    assert "，保留上一帧" in bots_body
    render_bots = html[
        html.index("function renderBots(") : html.index("async function refreshBots()")
    ]
    assert 'row.status === "working" ? "工作中" : "空闲"' in render_bots
    assert 'document.getElementById("bots-body")' in render_bots
    assert "replaceChildren" in render_bots


def test_index_bots_live_server(tmp_path: Path) -> None:
    """真起服务：``GET /`` 200，页面里带 tab-bots、Bot 状态与 /api/bots 的 fetch。"""
    path = tmp_path / "b.db"
    build_board(path, now=FIXTURE_NOW)

    with _running_server(path) as base, urllib.request.urlopen(base + "/", timeout=5) as resp:
        status = resp.status
        body = resp.read().decode("utf-8")

    assert status == 200
    assert 'id="tab-bots"' in body
    assert "Bot 状态" in body
    assert 'fetch("/api/bots")' in body
