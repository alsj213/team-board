"""``GET /api/topology`` 的集成测试：真起 HTTP server，走 ``urllib``。

覆盖 E2 签署版里路由侧的八组判据：键序锁死、只调 ``load_topo_cards``、
13 位毫秒、缺 ``created_by`` 列、前缀不误判与方法、响应头不变、
``/api/bots`` 逐字节 golden、空库。

数据库一律本文件现建：``tests/fixtures/make_board.py`` 的 7 列库**没有**
``created_by``，拓扑在它上面必然 500（那正是 E2-4 要断言的一条）。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from team_board import web
from tests.fixtures.make_board import FIXTURE_NOW, build_board

#: 每次响应都必须带上的头
CACHE_CONTROL = "no-store"

#: 13 位毫秒：``datetime.fromtimestamp`` 会抛 ``ValueError``（year out of range）
THIRTEEN_DIGIT_MS = 1793160000000

#: 缺 ``created_by`` 列的库（7 列，``make_board`` 口径）上拓扑的对外响应
SOURCE_UNAVAILABLE = '{"error": "source_unavailable: 看板数据源不可用，细节见服务端日志"}'

#: ``make_board`` 库上 ``/api/bots`` 的 golden 响应体（118 字节）
BOTS_GOLDEN = (
    '{"bots": [{"name": "engineer", "status": "working", "running": 1}, '
    '{"name": "pm", "status": "working", "running": 1}]}'
)

_SCHEMA_FULL = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    assignee TEXT,
    status TEXT,
    created_at INTEGER,
    started_at INTEGER,
    completed_at INTEGER,
    created_by TEXT
);
"""

#: 只有拓扑要用的六列：缺 started_at / completed_at，``load_tasks`` 必失败
_SCHEMA_TOPO_ONLY = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    assignee TEXT,
    status TEXT,
    created_at INTEGER,
    created_by TEXT
);
"""


def _write_tasks(
    path: Path,
    rows: list[dict[str, object]],
    schema: str,
    *,
    drop: tuple[str, ...] = (),
) -> Path:
    """建一个只有 ``tasks`` 表的库，按 ``rows`` 逐行写入（列名即字典键）。

    ``drop`` 让同一套 ``_row`` 也能喂给缺列的表（如六列口径没有时间列）。
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(schema)
        for row in rows:
            kept = {key: value for key, value in row.items() if key not in drop}
            columns = ", ".join(kept)
            marks = ", ".join("?" for _ in kept)
            conn.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({marks})",
                tuple(kept.values()),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _row(
    card_id: str,
    *,
    title: str = "",
    assignee: str | None = None,
    status: str = "ready",
    created_at: int | None = FIXTURE_NOW - 60,
    started_at: int | None = None,
    completed_at: int | None = None,
    created_by: str | None = None,
) -> dict[str, object]:
    return {
        "id": card_id,
        "title": title,
        "assignee": assignee,
        "status": status,
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "created_by": created_by,
    }


@pytest.fixture()
def topo_board(tmp_path: Path) -> Path:
    """正常拓扑库：user → pm → engineer，pm 有 running 卡（三条边两条节点层）。"""
    return _write_tasks(
        tmp_path / "topo.db",
        [
            _row("t_pm", title="派给 pm", assignee="pm", status="running",
                 started_at=FIXTURE_NOW - 50, created_by="user"),
            _row("t_eng", title="派给 engineer", assignee="engineer", status="done",
                 created_at=FIXTURE_NOW - 40, started_at=FIXTURE_NOW - 30,
                 completed_at=FIXTURE_NOW - 20, created_by="pm"),
        ],
        _SCHEMA_FULL,
    )


@contextlib.contextmanager
def _running_server(db_path: str | Path) -> Iterator[str]:
    """起一个临时端口的只读 server，退出 with 时 shutdown + server_close + join。"""
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


def _http(
    base: str, path: str, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    """发一个请求；4xx/5xx 也照常返回 (status, headers, body)。"""
    data = None if method == "GET" else b""
    request = urllib.request.Request(base + path, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


# --------------------------------------------------------------------------- #
# E2-1 键序锁死
# --------------------------------------------------------------------------- #

def test_schema_keys_and_order(topo_board: Path) -> None:
    """200 + JSON 头 + 顶层/node/edge 键序逐字锁死。"""
    with _running_server(topo_board) as base:
        status, headers, body = _http(base, "/api/topology")

    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    payload = json.loads(body, object_pairs_hook=dict)
    assert list(payload) == ["nodes", "edges"]
    assert payload["nodes"] and payload["edges"]
    for node in payload["nodes"]:
        assert list(node) == ["name", "display", "level", "active"]
    for edge in payload["edges"]:
        assert list(edge) == ["from", "to"]


# --------------------------------------------------------------------------- #
# E2-2 只调 load_topo_cards
# --------------------------------------------------------------------------- #

def test_only_topo_columns(tmp_path: Path) -> None:
    """只有六列（无 started_at / completed_at）的库：拓扑 200，/api/bots 500。"""
    path = _write_tasks(
        tmp_path / "six.db",
        [_row("t1", assignee="pm", created_by="user")],
        _SCHEMA_TOPO_ONLY,
        drop=("started_at", "completed_at"),
    )
    with _running_server(path) as base:
        status, _, body = _http(base, "/api/topology")
        bots_status, _, bots_body = _http(base, "/api/bots")

    assert status == 200
    assert json.loads(body)["edges"] == [{"from": "user", "to": "pm"}]
    assert bots_status == 500
    assert json.loads(bots_body)["error"].startswith("source_unavailable: ")


# --------------------------------------------------------------------------- #
# E2-3 13 位毫秒
# --------------------------------------------------------------------------- #

def test_ms_timestamps(tmp_path: Path) -> None:
    """毫秒时间戳：拓扑 200（不校验时间），/api/board 与 /api/bots 仍 500 data_invalid。"""
    path = _write_tasks(
        tmp_path / "ms.db",
        [
            _row(
                "t_ms",
                assignee="engineer",
                status="running",
                created_at=THIRTEEN_DIGIT_MS,
                started_at=THIRTEEN_DIGIT_MS,
                created_by="pm",
            )
        ],
        _SCHEMA_FULL,
    )
    with _running_server(path) as base:
        status, _, body = _http(base, "/api/topology")
        board_status, _, board_body = _http(base, "/api/board")
        bots_status, _, bots_body = _http(base, "/api/bots")

    assert status == 200
    names = [node["name"] for node in json.loads(body)["nodes"]]
    assert names == ["pm", "engineer"] or sorted(names) == ["engineer", "pm"]
    assert {"pm", "engineer"} <= set(names)
    assert board_status == 500
    assert bots_status == 500
    assert board_body.decode("utf-8").startswith('{"error": "data_invalid: ')
    assert bots_body.decode("utf-8").startswith('{"error": "data_invalid: ')


# --------------------------------------------------------------------------- #
# E2-4 缺 created_by 列
# --------------------------------------------------------------------------- #

def test_missing_created_by(tmp_path: Path) -> None:
    """7 列库（无 created_by）：拓扑 500 脱敏体逐字节；同库 /api/bots 200。"""
    path = tmp_path / "shop.db"
    build_board(path, now=FIXTURE_NOW)
    with _running_server(path) as base:
        status, headers, body = _http(base, "/api/topology")
        bots_status, _, _ = _http(base, "/api/bots")

    assert status == 500
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    text = body.decode("utf-8")
    assert text == SOURCE_UNAVAILABLE
    # 不含绝对路径 / 表名 / SQLite 原文
    assert str(path) not in text
    assert "created_by" not in text
    assert "SQL" not in text
    assert bots_status == 200


# --------------------------------------------------------------------------- #
# E2-5 前缀不误判与方法
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method", ["POST", "OPTIONS", "TRACE"])
def test_prefix_and_methods_with_body(topo_board: Path, method: str) -> None:
    """POST/OPTIONS/TRACE → 405 + 体；前缀与子路径 → 404；查询串不影响。"""
    with _running_server(topo_board) as base:
        for path in ("/api/topolog", "/api/topology/x"):
            status, _, body = _http(base, path)
            assert status == 404
            assert json.loads(body) == {"error": "not found"}

        status, _, body = _http(base, "/api/topology", method=method)
        assert status == 405
        assert json.loads(body) == {"error": "method not allowed"}

        plain = _http(base, "/api/topology")
        queried = _http(base, "/api/topology?x=1")
        assert plain[0] == queried[0] == 200
        assert plain[2] == queried[2]


def test_prefix_and_methods_head_no_body(topo_board: Path) -> None:
    """HEAD → 405 且空体、Content-Length 0。"""
    with _running_server(topo_board) as base:
        status, headers, body = _http(base, "/api/topology", method="HEAD")

    assert status == 405
    assert body == b""
    assert headers["Content-Length"] == "0"


# --------------------------------------------------------------------------- #
# E2-6 头不变
# --------------------------------------------------------------------------- #

def test_no_store_on_all_four_apis(topo_board: Path) -> None:
    """四个接口都带 Cache-Control: no-store，Content-Length 与体长一致。"""
    with _running_server(topo_board) as base:
        for path in ("/api/topology", "/api/board", "/api/summary", "/api/bots"):
            status, headers, body = _http(base, path)
            assert status == 200, path
            assert headers["Cache-Control"] == CACHE_CONTROL, path
            assert int(headers["Content-Length"]) == len(body), path


# --------------------------------------------------------------------------- #
# E2-7 /api/bots 逐字节 golden
# --------------------------------------------------------------------------- #

def test_bots_byte_identical(tmp_path: Path) -> None:
    """``make_board`` 库上 /api/bots 响应体恰为 golden（118 字节）。"""
    path = tmp_path / "bots.db"
    build_board(path, now=FIXTURE_NOW)
    with _running_server(path) as base:
        status, _, body = _http(base, "/api/bots")

    assert status == 200
    assert body == BOTS_GOLDEN.encode("utf-8")
    assert len(body) == 118


# --------------------------------------------------------------------------- #
# E2-8 空库
# --------------------------------------------------------------------------- #

def test_empty_board(tmp_path: Path) -> None:
    """tasks 表为空 → 200 且体恰为 ``{"nodes": [], "edges": []}``。"""
    path = _write_tasks(tmp_path / "empty.db", [], _SCHEMA_FULL)
    with _running_server(path) as base:
        status, _, body = _http(base, "/api/topology")

    assert status == 200
    assert body == b'{"nodes": [], "edges": []}'
