"""``GET /api/bots`` 的集成测试：真起 HTTP server，走 ``urllib``。

覆盖验收判据的四组：payload 口径（fixture 下 engineer / pm 都是 working·1）、
只读边界（POST 405 / HEAD 405 空体 / 查询串忽略且逐字节相同）、前缀不误判
（``/api/bot`` 仍是 404）、13 位毫秒 → 500 ``data_invalid``。

fixture 口径（``now=FIXTURE_NOW``）：
    engineer  running_stale（running）等 4 张 → running=1 / working
    pm        running_fresh（running）等 6 张 → running=1 / working
    fixture 里只有这两个 assignee（没有 NULL / ``nobody`` 行，那两条口径由单测覆盖）
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


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    path = tmp_path / "b.db"
    build_board(path, now=FIXTURE_NOW)
    return path


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
# 1) payload 口径
# --------------------------------------------------------------------------- #


def test_payload_lists_engineer_and_pm_working(board: Path) -> None:
    """``GET /api/bots`` 200；fixture 下 engineer=working/1、pm=working/1。"""
    with _running_server(board) as base:
        status, headers, body = _http(base, "/api/bots")

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Content-Type"].endswith("charset=utf-8")
    assert headers["Cache-Control"] == CACHE_CONTROL
    assert int(headers["Content-Length"]) == len(body)

    payload = json.loads(body.decode("utf-8"))
    assert list(payload) == ["bots"]
    assert payload["bots"] == [
        {"name": "engineer", "status": "working", "running": 1},
        {"name": "pm", "status": "working", "running": 1},
    ]


# --------------------------------------------------------------------------- #
# 2) 只读边界
# --------------------------------------------------------------------------- #


def test_readonly_post_head_and_query_string(board: Path) -> None:
    """POST → 405 JSON；HEAD → 405 空体；``?x=1`` 与无查询串逐字节相同。"""
    with _running_server(board) as base:
        plain = _http(base, "/api/bots")
        with_query = _http(base, "/api/bots?x=1")
        post_status, post_headers, post_body = _http(base, "/api/bots", method="POST")
        head_status, head_headers, head_body = _http(base, "/api/bots", method="HEAD")

    assert plain[0] == with_query[0] == 200
    assert plain[2] == with_query[2]

    assert post_status == 405
    assert post_headers["Content-Type"].startswith("application/json")
    assert json.loads(post_body.decode("utf-8")) == {"error": "method not allowed"}

    assert head_status == 405
    assert head_body == b""
    assert head_headers["Content-Length"] == "0"


# --------------------------------------------------------------------------- #
# 3) 前缀不误判
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/api/bot", "/api/bots/extra", "/api/botsx"])
def test_prefix_bot_path_404(board: Path, path: str) -> None:
    """``/api/bot`` 这类前缀相近的路径仍是 404 ``not found``，不误命中。"""
    with _running_server(board) as base:
        status, headers, body = _http(base, path)

    assert status == 404
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body.decode("utf-8")) == {"error": "not found"}


# --------------------------------------------------------------------------- #
# 4) 13 位毫秒
# --------------------------------------------------------------------------- #


def _dirty_created_at(path: Path, task_id: str, value: int) -> None:
    """把一张卡的 ``created_at`` 改成 13 位毫秒（``started_at`` 保持 NULL）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (value, task_id))
        conn.commit()
    finally:
        conn.close()


def test_data_invalid_on_13_digit_ms(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """脏时间戳 → 500 ``data_invalid``，响应体不泄内部值，日志只一行。"""
    _dirty_created_at(board, "t_null_start", THIRTEEN_DIGIT_MS)

    with _running_server(board) as base:
        capsys.readouterr()
        status, headers, body = _http(base, "/api/bots")
        captured = capsys.readouterr()

    assert status == 500
    assert headers["Content-Type"].startswith("application/json")
    error = json.loads(body.decode("utf-8"))
    assert set(error) == {"error"}
    assert error["error"].startswith("data_invalid: ")
    assert str(THIRTEEN_DIGIT_MS) not in error["error"]
    assert "t_null_start" not in error["error"]

    log_lines = [
        line for line in captured.err.splitlines() if "[team-board] 500 data_invalid" in line
    ]
    assert len(log_lines) == 1
    assert "GET /api/bots" in log_lines[0]
    assert "t_null_start" in log_lines[0]
    assert "Traceback (most recent call last)" not in captured.err
