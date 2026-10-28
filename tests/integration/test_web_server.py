"""真起 HTTP server 的集成测试：路由、只读、脏库、CLI 参数边界。

全部用 ``web.make_server(path, port=0)`` + 后台线程跑 ``serve_forever``，
请求走 ``urllib.request``，不碰真实网络对端。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from team_board import cli, web
from tests.fixtures.make_board import FIXTURE_NOW, build_board

#: 每次响应都必须带上的头
CACHE_CONTROL = "no-store"

#: 13 位毫秒（2026-10-28 前后某个时刻的毫秒值）—— ``datetime.fromtimestamp``
#: 会抛 ``ValueError: year ... is out of range``，不是 ``db.BoardError``
THIRTEEN_DIGIT_MS = 1793160000000


@pytest.fixture()
def board(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "b.db"
    ids = build_board(path, now=FIXTURE_NOW)
    return path, ids


@contextlib.contextmanager
def _running_server(db_path: str | Path, *, clock: int = FIXTURE_NOW) -> Iterator[str]:
    """起一个临时端口的 server，退出 with 时 shutdown + server_close + join。

    ``clock`` 就是 ``web.make_server`` 的时钟注入点，用来把 13 位毫秒这种
    payload 层脏值塞进服务进程。
    """
    server = web.make_server(db_path, port=0, clock=lambda: clock)
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


def _expected_day() -> str:
    return datetime.datetime.fromtimestamp(FIXTURE_NOW).date().isoformat()


# --------------------------------------------------------------------------- #
# 1) /api/board
# --------------------------------------------------------------------------- #


def test_api_board_200_shape_and_grouping(
    board: tuple[Path, dict[str, str]],
) -> None:
    path, _ = board
    with _running_server(path) as base:
        status, headers, body = _http(base, "/api/board")

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Content-Type"].endswith("charset=utf-8")
    assert headers["Cache-Control"] == CACHE_CONTROL
    assert int(headers["Content-Length"]) == len(body)

    payload = json.loads(body.decode("utf-8"))
    assert list(payload["columns"]) == ["running", "queued", "blocked", "done"]

    columns = payload["columns"]
    for name in ("running", "queued", "blocked", "done"):
        assert isinstance(columns[name], list)
        for card in columns[name]:
            assert set(card) == {
                "id",
                "assignee",
                "status",
                "duration_minutes",
                "title",
            }
            assert isinstance(card["duration_minutes"], int)

    assert "t_running_stale" in [card["id"] for card in columns["running"]]
    assert "t_null_start" in [card["id"] for card in columns["queued"]]

    all_ids = {card["id"] for cards in columns.values() for card in cards}
    assert "t_archived_old" not in all_ids
    # 75 分钟降序在前；archived/todo/review 一张都不出现
    assert [card["id"] for card in columns["running"]]
    assert next(card["id"] for card in columns["running"]) == "t_running_stale"
    assert all_ids == {
        "t_running_stale",
        "t_running_fresh",
        "t_null_start",
        "t_blocked_one",
        "t_done_today",
        "t_done_yesterday",
    }


def test_api_board_ignores_query_string(
    board: tuple[Path, dict[str, str]],
) -> None:
    path, _ = board
    with _running_server(path) as base:
        plain = _http(base, "/api/board")
        with_query = _http(base, "/api/board?x=1")

    assert plain[0] == with_query[0] == 200
    assert plain[2] == with_query[2]


# --------------------------------------------------------------------------- #
# 2) /api/summary
# --------------------------------------------------------------------------- #


def test_api_summary_200_counts_and_anomalies(
    board: tuple[Path, dict[str, str]],
) -> None:
    """fixture 口径：完成 1 / 在跑 2 / 阻塞 1 / 异常 2。

    异常两条（实跑核对，不是推断）：``t_running_stale`` 75 > 60、
    ``t_review_stale`` 45 > 30，按分钟降序 → 第一条是 running_stale。
    """
    path, _ = board
    with _running_server(path) as base:
        status, headers, body = _http(base, "/api/summary")

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == CACHE_CONTROL

    payload = json.loads(body.decode("utf-8"))
    assert payload["completed"] == 1
    assert payload["running"] == 2
    assert payload["blocked"] == 1
    assert payload["anomaly"] == 2
    assert payload["date"] == _expected_day()
    assert payload["snapshot"] == datetime.datetime.fromtimestamp(
        FIXTURE_NOW
    ).strftime("%Y-%m-%d %H:%M")

    assert len(payload["anomalies"]) == 2
    assert payload["anomalies"][0]["id"] == "t_running_stale"
    assert payload["anomalies"][0]["minutes"] == 75
    assert payload["anomalies"][0]["threshold"] == 60
    assert {a["id"] for a in payload["anomalies"]} == {
        "t_running_stale",
        "t_review_stale",
    }


# --------------------------------------------------------------------------- #
# 3) /
# --------------------------------------------------------------------------- #


def test_index_html_200(board: tuple[Path, dict[str, str]]) -> None:
    path, _ = board
    with _running_server(path) as base:
        status, headers, body = _http(base, "/")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == CACHE_CONTROL
    text = body.decode("utf-8")
    for needle in ("running", "queued", "blocked", "done", "5000"):
        assert needle in text


# --------------------------------------------------------------------------- #
# 4) 404
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/nope", "/api/board/extra", "/api"])
def test_unknown_path_404(board: tuple[Path, dict[str, str]], path: str) -> None:
    db_path, _ = board
    with _running_server(db_path) as base:
        status, headers, body = _http(base, path)

    assert status == 404
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body.decode("utf-8")) == {"error": "not found"}


# --------------------------------------------------------------------------- #
# 5) 非 GET → 405
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_non_get_405(
    board: tuple[Path, dict[str, str]], method: str
) -> None:
    path, _ = board
    with _running_server(path) as base:
        status, headers, body = _http(base, "/api/board", method=method)

    assert status == 405
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == CACHE_CONTROL
    assert json.loads(body.decode("utf-8")) == {"error": "method not allowed"}


# --------------------------------------------------------------------------- #
# 6) 只读证明：sha256 不变
# --------------------------------------------------------------------------- #


def test_sha256_unchanged_after_requests(
    board: tuple[Path, dict[str, str]],
) -> None:
    """请求 /api/board + /api/summary 前后库文件 sha256 相同。

    后值是在 ``with`` 退出、即 server 已 shutdown + server_close + 线程 join
    之后取的字节摘要。
    """
    path, _ = board
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with _running_server(path) as base:
        assert _http(base, "/api/board")[0] == 200
        assert _http(base, "/api/summary")[0] == 200
        assert _http(base, "/")[0] == 200

    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after


# --------------------------------------------------------------------------- #
# 7) 脏库 → 500，且不崩
# --------------------------------------------------------------------------- #


def test_broken_db_500_and_server_survives(
    board: tuple[Path, dict[str, str]],
) -> None:
    path, _ = board
    path.unlink()
    path.write_bytes(b"definitely not a sqlite database")

    with _running_server(path) as base:
        status, headers, body = _http(base, "/api/board")
        assert status == 500
        assert headers["Content-Type"].startswith("application/json")
        assert headers["Cache-Control"] == CACHE_CONTROL
        error = json.loads(body.decode("utf-8"))
        assert set(error) == {"error"}
        # 500 响应体只给稳定类别 + 一句话：不带路径、表名、SQLite 原文
        assert error["error"].startswith("source_unavailable: ")
        assert str(path) not in error["error"]
        assert "SQLite" not in error["error"]
        assert "no such table" not in error["error"]

        # 库坏了不影响静态页：进程/线程没崩
        html_status, _, html_body = _http(base, "/")
        assert html_status == 200
        assert b"team-board" in html_body


# --------------------------------------------------------------------------- #
# 8) --port / --bind 边界 → rc2
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--port", "0"],
        ["serve", "--port", "-1"],
        ["serve", "--port", "65536"],
        ["serve", "--port", "abc"],
        ["serve", "--port", ""],
        ["serve", "--bind", "0.0.0.0"],
        ["serve", "--bind", ""],
        ["serve", "--bind", "192.168.1.10"],
    ],
)
def test_serve_invalid_args_exit_two(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_serve_bind_ipv6_exit_two_with_clear_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """IPv6 回环地址是假选项：rc2 + 一句说得清的错误（不是启动后 gaierror）。

    白名单背后的 ``ThreadingHTTPServer`` 恒为 ``AF_INET``，所以 IPv6 字面量必须
    在解析层就被拒，而不是放行后 100% 起不来。
    """
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve", "--bind", "::1"])
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert captured.out == ""
    assert "只允许本机地址" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("port", ["1", "8787", "65535"])
def test_parse_port_accepts_edges(port: str) -> None:
    assert cli._parse_port(port) == int(port)


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost"])
def test_parse_bind_accepts_local_addresses(bind: str) -> None:
    assert cli._parse_bind(bind) == bind


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost"])
def test_make_server_really_binds_whitelisted_address(
    board: tuple[Path, dict[str, str]], bind: str
) -> None:
    """白名单里的每个值都**真 bind 一次**并接受一次 GET。

    只断言 ``_parse_bind`` 的返回值等于入参是解析层自证，证明不了能起服务
    （IPv6 那个假选项就是这么漏出去的）；这里必须让内核真的 bind + 真的应答。
    """
    path, _ = board
    server = web.make_server(path, bind=bind, port=0, clock=lambda: FIXTURE_NOW)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        assert isinstance(host, str)
        status, _, body = _http(f"http://{host}:{port}", "/api/board")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert list(json.loads(body.decode("utf-8"))["columns"]) == [
        "running",
        "queued",
        "blocked",
        "done",
    ]


# --------------------------------------------------------------------------- #
# 9) serve --db 不存在 → rc1，不阻塞、无 traceback
# --------------------------------------------------------------------------- #


def test_serve_missing_db_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "definitely-missing.db"
    rc = cli.main(["serve", "--db", str(missing)])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("错误: ")
    assert "数据源不存在" in captured.err
    assert "Traceback" not in captured.err


def test_web_make_server_accepts_port_zero(
    board: tuple[Path, dict[str, str]],
) -> None:
    """port=0 由内核挑端口，server_address[1] 是真实端口。"""
    path, _ = board
    server = web.make_server(path, port=0)
    try:
        assert server.server_address[1] > 0
        assert server.board_db_path == str(path)
        assert callable(server.board_clock)
        assert server.daemon_threads is True
    finally:
        server.server_close()


# --------------------------------------------------------------------------- #
# 10) payload 层非 BoardError 异常 → 500 + JSON、无 traceback、不崩线程
# --------------------------------------------------------------------------- #


def _dirty_completed_at(path: Path, value: int) -> None:
    """把 done_today 的 completed_at 改成脏值（模拟 CLI 也能写出的 13 位毫秒）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (value, "t_done_today"),
        )
        conn.commit()
    finally:
        conn.close()


def test_summary_clock_13_digit_ms_500_json_server_survives(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """注入 13 位毫秒 clock：/api/summary → 500 + JSON（不是断连），/api/board 仍 200。

    这条走的是 ``datetime.fromtimestamp(now)``（web.py 里 ``.date()`` 那一步），
    抛的是 ``ValueError``，``isinstance(BoardError)`` 为 False —— 正是兜底层
    要接住的那类。
    """
    path, _ = board
    with _running_server(path, clock=THIRTEEN_DIGIT_MS) as base:
        capsys.readouterr()  # 丢掉启动阶段噪声，只看请求期间
        status, headers, body = _http(base, "/api/summary")
        board_status, _, board_body = _http(base, "/api/board")
        captured = capsys.readouterr()

    assert status == 500
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Content-Type"].endswith("charset=utf-8")
    assert headers["Cache-Control"] == CACHE_CONTROL
    assert int(headers["Content-Length"]) == len(body)
    error = json.loads(body.decode("utf-8"))
    assert set(error) == {"error"}
    assert error["error"]  # 空串不行：空则回 type(exc).__name__

    # 同一进程的 /api/board 不受影响（线程没崩、连接没坏）
    assert board_status == 200
    columns = json.loads(board_body.decode("utf-8"))["columns"]
    assert "t_running_stale" in [card["id"] for card in columns["running"]]

    assert "Traceback (most recent call last)" not in captured.err


def test_summary_dirty_completed_at_500_json(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """库里 done 卡的 ``completed_at`` 是 13 位毫秒 → /api/summary 500 + JSON。

    这条走的是 ``commands.summary_stats`` 里对 ``task.completed_at`` 的
    ``fromtimestamp``，与上面那条（``now`` 越界）不是同一个调用点，两条合起来
    才算「兜底范围覆盖整段 payload 构建」。
    """
    path, _ = board
    _dirty_completed_at(path, THIRTEEN_DIGIT_MS)

    with _running_server(path) as base:
        capsys.readouterr()
        status, headers, body = _http(base, "/api/summary")
        captured = capsys.readouterr()

    assert status == 500
    assert headers["Content-Type"].startswith("application/json")
    error = json.loads(body.decode("utf-8"))
    assert set(error) == {"error"}
    assert error["error"]
    assert "Traceback (most recent call last)" not in captured.err


# --------------------------------------------------------------------------- #
# 11) 错误路径收口：BLOB 行 → 500（不是断连）、写出失败静默、HEAD/OPTIONS/TRACE
# --------------------------------------------------------------------------- #


def _raw_bytes(base: str, method: str, path: str) -> bytes:
    """裸 socket 收发一条请求，返回服务端写回的**全部字节**（未解析）。

    「一条连接上有几条状态行」只能看这段原始字节数 ``b"HTTP/1."`` 出现次数 ——
    解析成 ``(状态行, 头, 体)`` 之后第二条状态行会混进 body，数不出来。
    """
    host, port = base.split("://", 1)[1].rsplit(":", 1)
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")

    chunks: list[bytes] = []
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(request)
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


def _raw(
    base: str, method: str, path: str
) -> tuple[bytes, dict[str, str], bytes]:
    """裸 socket 发一条 HTTP/1.1 请求。

    返回 ``(状态行, 头, 体)``；服务端一个字节都没回就关掉连接时状态行是 ``b""``
    —— 这正是「断连（不是 500）」的可观测形态，``urllib`` 会把异常包装掉。
    """
    head, _, body = _raw_bytes(base, method, path).partition(b"\r\n\r\n")
    lines = head.split(b"\r\n") if head else []
    if not lines:
        return b"", {}, b""

    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.decode("iso-8859-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers, body


def _blob_title(path: Path, task_id: str, payload: bytes) -> None:
    """把某卡的 ``title`` 改成 BLOB —— 正常库写不出来，但迁移/外部工具能写。

    ``db.load_tasks`` 原样读出 ``bytes``，``json.dumps`` 会抛
    ``TypeError: Object of type bytes is not JSON serializable``。
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE tasks SET title = ? WHERE id = ?",
            (sqlite3.Binary(payload), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_blob_title_500_json_not_disconnect(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """BLOB 行 → 客户端拿到 500 + 可解析 JSON（不是 RemoteDisconnected）。

    pm 实测的旧行为：序列化在 ``try`` 之外，``json.dumps`` 抛 TypeError 穿出
    ``do_GET``，socketserver 打 traceback，客户端一个字节都收不到。
    """
    path, _ = board
    _blob_title(path, "t_running_stale", b"\x00\x01not-utf8")

    with _running_server(path) as base:
        capsys.readouterr()
        status, headers, body = _http(base, "/api/board")
        again_status, _, again_body = _http(base, "/api/board")
        html_status, _, html_body = _http(base, "/")
        captured = capsys.readouterr()

    assert status == 500
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Content-Type"].endswith("charset=utf-8")
    assert headers["Cache-Control"] == CACHE_CONTROL
    assert int(headers["Content-Length"]) == len(body)
    error = json.loads(body.decode("utf-8"))
    assert set(error) == {"error"}
    # 序列化细节（Object of type bytes is not JSON serializable）不进响应体
    assert error["error"].startswith("internal_error: ")
    assert "JSON serializable" not in error["error"]
    assert str(path) not in error["error"]

    # 线程没崩：同一个库再请求一次仍然是 500 + JSON，静态页仍 200
    assert again_status == 500
    assert json.loads(again_body.decode("utf-8"))["error"].startswith(
        "internal_error: "
    )
    assert html_status == 200
    assert b"team-board" in html_body

    assert "Traceback (most recent call last)" not in captured.err
    # 细节落在 stderr 的一行日志里（含方法 + 路径 + 异常名 + 原文）
    log_lines = [line for line in captured.err.splitlines() if "[team-board] 500" in line]
    assert log_lines
    assert all("GET /api/board" in line for line in log_lines)
    assert any("TypeError" in line and "JSON serializable" in line for line in log_lines)



def test_broken_pipe_is_silent_and_server_survives(
    board: tuple[Path, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写出失败（模拟客户端已断开）→ 不打 traceback、服务仍在，后续 GET 200。

    只让第一次 ``_send_json`` 抛 ``BrokenPipeError``，之后走原实现；断言客户端
    收到**零字节**（不是 200 也不是 500 —— 对端已断，500 也写不出去），且同一
    进程的下一次请求照常 200。
    """
    path, _ = board
    original = web.BoardRequestHandler._send_json
    fired = {"count": 0}

    def flaky(self: web.BoardRequestHandler, status: int, obj: object) -> None:
        if fired["count"] == 0:
            fired["count"] += 1
            raise BrokenPipeError("client went away")
        original(self, status, obj)

    monkeypatch.setattr(web.BoardRequestHandler, "_send_json", flaky)

    with _running_server(path) as base:
        capsys.readouterr()
        status_line, _, raw_body = _raw(base, "GET", "/api/board")
        status, headers, body = _http(base, "/api/board")
        captured = capsys.readouterr()

    assert status_line == b""  # 断连形态：一个字节都没写出去
    assert raw_body == b""
    assert fired["count"] == 1

    # 服务进程/线程存活，后续请求正常
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert list(json.loads(body.decode("utf-8"))["columns"]) == [
        "running",
        "queued",
        "blocked",
        "done",
    ]
    assert "Traceback (most recent call last)" not in captured.err
    assert "BrokenPipeError" not in captured.err


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE"])
def test_head_options_trace_405(
    board: tuple[Path, dict[str, str]], method: str
) -> None:
    """README:102-103「非 GET 一律 405 + {"error": ...}」：HEAD 只给头。"""
    path, _ = board
    with _running_server(path) as base:
        status_line, headers, body = _raw(base, method, "/api/board")

    # BaseHTTPRequestHandler 默认 protocol_version 是 HTTP/1.0，只认状态码
    assert status_line.split(b" ")[1] == b"405"
    assert headers["cache-control"] == CACHE_CONTROL
    if method == "HEAD":
        # HTTP 语义：HEAD 不写响应体
        assert body == b""
        assert headers["content-length"] == "0"
    else:
        assert headers["content-type"].startswith("application/json")
        assert json.loads(body.decode("utf-8")) == {"error": "method not allowed"}


def test_index_html_guards_response_ok_and_renders_error_line() -> None:
    """前端结构断言（不跑浏览器）：只渲染 2xx，失败显示带状态码的错误行。"""
    html = web.index_html()

    # 1) 错误行元素存在，且样式里是「默认隐藏、加类才显示」
    assert 'id="error"' in html
    assert "#error.visible { display: block; }" in html

    # 2) 两个接口都必须查 response.ok，且渲染发生在检查之后
    ok_check = html.index("if (!responses[0].ok || !responses[1].ok)")
    assert "!response.ok" in html  # 逐个响应检查（grep 口径的 response.ok）
    assert ok_check < html.index("return !response.ok;")
    assert ok_check < html.index("const failed = responses.filter")
    assert ok_check < html.index("renderSummary(summary)")
    assert ok_check < html.index("renderBoard(board)")

    # 3) 失败分支：文案带 HTTP 状态码 + return（不往下渲染）+ 用 textContent
    assert html.index('"刷新失败（HTTP " + failed.status + "）"') > ok_check
    assert html.index("return;", ok_check) < html.index("renderSummary(summary)")
    assert "box.textContent = text;" in html
    assert "innerHTML" not in html
    assert "clearError();" in html  # 成功一次后错误行消失

    # 4) 5s 周期与自动刷新开关原样保留
    assert "const REFRESH_MS = 5000;" in html
    assert web.REFRESH_MS == 5000
    assert 'id="autorefresh" checked' in html


# --------------------------------------------------------------------------- #
# 12) 缺 task_events 表：两个接口都 200（summary 不再依赖无关表）
# --------------------------------------------------------------------------- #


def _drop_task_events(path: Path) -> None:
    """造一张「缺 task_events」的库：只读工具本身永远写不出来，迁移能。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DROP TABLE task_events")
        conn.commit()
    finally:
        conn.close()


def test_missing_task_events_keeps_both_apis_200(
    board: tuple[Path, dict[str, str]],
) -> None:
    """``/api/summary`` 与 ``/api/board`` 在缺 ``task_events`` 时行为一致（都 200）。

    旧行为：``/api/summary`` → 500 ``读取 task_events 失败: no such table: task_events``，
    同页面上「一半有数、一半报错」。
    """
    path, _ = board
    _drop_task_events(path)

    with _running_server(path) as base:
        board_status, _, board_body = _http(base, "/api/board")
        summary_status, headers, summary_body = _http(base, "/api/summary")

    assert board_status == 200
    assert summary_status == 200
    assert headers["Content-Type"].startswith("application/json")
    columns = json.loads(board_body.decode("utf-8"))["columns"]
    assert "t_running_stale" in [card["id"] for card in columns["running"]]
    summary = json.loads(summary_body.decode("utf-8"))
    assert list(summary) == [
        "snapshot",
        "date",
        "completed",
        "running",
        "blocked",
        "anomaly",
        "anomalies",
    ]
    assert (summary["completed"], summary["running"], summary["blocked"]) == (1, 2, 1)
    assert summary["anomaly"] == 2


def test_missing_task_events_cli_summary_still_needs_it(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """README 写的「命令行 `summary` / `member` 仍会读 task_events」有测试锁住。"""
    path, _ = board
    _drop_task_events(path)

    rc_summary = cli.main(["summary", "--db", str(path), "--now", str(FIXTURE_NOW)])
    err_summary = capsys.readouterr().err
    assert rc_summary == 1
    assert err_summary.startswith("错误: ")
    assert "读取 task_events 失败" in err_summary

    rc_member = cli.main(
        ["member", "engineer", "--db", str(path), "--now", str(FIXTURE_NOW)]
    )
    err_member = capsys.readouterr().err
    assert rc_member == 1
    assert "读取 task_events 失败" in err_member

    # list 不读 task_events，照常 rc0
    assert cli.main(["list", "--db", str(path), "--now", str(FIXTURE_NOW)]) == 0
    captured = capsys.readouterr()
    assert captured.out
    assert captured.err == ""


# --------------------------------------------------------------------------- #
# 13) 起点时间戳是 13 位毫秒：两个接口都 500 data_invalid（不再静默 0 分钟）
# --------------------------------------------------------------------------- #


def _dirty_created_at(path: Path, task_id: str, value: int) -> None:
    """把一张卡的 ``created_at`` 改成 13 位毫秒（``started_at`` 保持 NULL）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?", (value, task_id)
        )
        conn.commit()
    finally:
        conn.close()


def test_dirty_created_at_ms_500_data_invalid_on_both_endpoints(
    board: tuple[Path, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """``created_at`` 是 13 位毫秒 → 500 ``data_invalid``，且响应体不泄内部值。

    旧行为：``max(0, …)`` 静默算成 0 分钟、接口 200 —— 数据错了也看不出来。
    """
    path, _ = board
    _dirty_created_at(path, "t_null_start", THIRTEEN_DIGIT_MS)

    with _running_server(path) as base:
        capsys.readouterr()
        board_status, _, board_body = _http(base, "/api/board")
        summary_status, _, summary_body = _http(base, "/api/summary")
        captured = capsys.readouterr()

    for status, body in ((board_status, board_body), (summary_status, summary_body)):
        assert status == 500
        error = json.loads(body.decode("utf-8"))
        assert set(error) == {"error"}
        assert error["error"].startswith("data_invalid: ")
        assert str(THIRTEEN_DIGIT_MS) not in error["error"]
        assert "t_null_start" not in error["error"]

    log_lines = [
        line for line in captured.err.splitlines() if "[team-board] 500 data_invalid" in line
    ]
    assert len(log_lines) == 2
    assert "GET /api/board" in log_lines[0] or "GET /api/summary" in log_lines[0]
    assert all("t_null_start" in line for line in log_lines)
    assert "Traceback (most recent call last)" not in captured.err


# --------------------------------------------------------------------------- #
# 14) --port 严格十进制：int() 原本会静默接受的写法全部 rc2
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [" 8787", "+8787", "1_000", "١٢٣"])
def test_parse_port_rejects_int_style_strings(value: str) -> None:
    """``int()`` 会接受这四种写法（空白 / 正号 / 下划线 / 非 ASCII 数字）。"""
    assert int(value) > 0  # 旧解析器就是靠 int() 放行的
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        cli._parse_port(value)
    assert "ASCII 十进制整数" in str(excinfo.value)


@pytest.mark.parametrize(
    "value", [" 8787", "+8787", "1_000", "١٢٣", "8787\n", "0x10", "8 7", ""]
)
def test_serve_port_non_plain_decimal_exit_two(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve", "--port", value])
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert captured.out == ""
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------- #
# 15) README 与 --help 对齐：五个子命令、serve 无 --now
# --------------------------------------------------------------------------- #


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions
        for option in action.option_strings
    }


def test_readme_and_help_agree_on_five_subcommands() -> None:
    """README「五个子命令」与实际 parser 一致；serve 只认 ``--db/--port/--bind``。"""
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    parser = cli.build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert sorted(subparsers.choices) == [
        "export",
        "list",
        "member",
        "serve",
        "summary",
    ]
    assert "五个子命令" in readme
    assert "两个参数四个子命令" not in readme

    for name in ("list", "summary", "member", "export"):
        assert {"--db", "--now"} <= _option_strings(subparsers.choices[name])
    serve_options = _option_strings(subparsers.choices["serve"])
    assert {"--db", "--port", "--bind"} <= serve_options
    assert "--now" not in serve_options

    # serve 没有 --now：真跑一次拿到 rc2，而不是只比 help 文本
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve", "--now", str(FIXTURE_NOW)])
    assert excinfo.value.code == 2


def test_help_text_lists_all_five_subcommands() -> None:
    """``team-board --help`` 的 {list,summary,member,export,serve} 与 README 同一组。"""
    help_text = cli.build_parser().format_help()
    for name in ("list", "summary", "member", "export", "serve"):
        assert f"    {name} " in help_text or f"    {name}\n" in help_text
    assert "{list,summary,member,export,serve}" in help_text


# --------------------------------------------------------------------------- #
# 16) 断连收口补齐：非 GET 写出失败静默；200 头已发后失败不写第二条状态行
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE", "POST"])
def test_non_get_write_failure_is_silent_and_server_survives(
    board: tuple[Path, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """非 GET 方法在写 405 响应时对端已断 → 静默、无 traceback、服务存活。

    缺陷出处（PR #10 的第二视角 Review，已实测复现）：断连护栏只装在
    ``do_GET``，HEAD/OPTIONS/TRACE/POST 在 ``send_response``/``end_headers``
    写状态行时抛 ``ConnectionResetError``，异常穿出 ``do_*`` 落进
    ``socketserver.handle_error``，打整段 traceback（栈底 ``sendall``）。
    """
    path, _ = board
    original = web.BoardRequestHandler.flush_headers
    armed = {"on": True}

    def flaky_flush_headers(self: web.BoardRequestHandler) -> None:
        if armed["on"]:
            raise ConnectionResetError(104, "Connection reset by peer")
        original(self)

    monkeypatch.setattr(web.BoardRequestHandler, "flush_headers", flaky_flush_headers)

    with _running_server(path) as base:
        capsys.readouterr()  # 丢掉启动阶段噪声
        replies = [_raw_bytes(base, method, "/api/board") for _ in range(4)]
        armed["on"] = False
        status, headers, body = _http(base, "/api/board")
        captured = capsys.readouterr()

    # 断连形态：一个字节都没写出去（不是 200 也不是 405）
    assert replies == [b"", b"", b"", b""]
    assert "Traceback (most recent call last)" not in captured.err
    assert "ConnectionResetError" not in captured.err

    # 服务进程/线程存活：后续 GET 照常 200
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert list(json.loads(body.decode("utf-8"))["columns"]) == [
        "running",
        "queued",
        "blocked",
        "done",
    ]


@pytest.mark.parametrize(
    ("make_exc", "expect_note"),
    [
        (lambda: ConnectionAbortedError(10053, "connection aborted"), False),
        (lambda: OSError(28, "No space left on device"), True),
    ],
    ids=["connection-aborted", "other-oserror"],
)
def test_mid_write_failure_never_writes_second_status_line(
    board: tuple[Path, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    make_exc: Callable[[], BaseException],
    expect_note: bool,
) -> None:
    """200 头已发出后写出失败 → 客户端只收到 **1** 条状态行，不补 500。

    缺陷出处（PR #10 新引入）：``ConnectionAbortedError``（Windows 10053）不是
    ``BrokenPipeError``/``ConnectionResetError``，落进 ``except Exception`` 后
    在**同一连接**上又写了一遍 500 —— 客户端实测收到
    ``HTTP/1.0 200 OK …`` 紧跟 ``HTTP/1.0 500 Internal Server Error …``。

    取舍：``ConnectionError`` 家族静默；其它 ``OSError`` 只在 stderr 留一行
    （``写出中断``），绝不写第二条状态行。
    """
    path, _ = board
    original = web.BoardRequestHandler.end_headers
    armed = {"on": True}

    def flaky_end_headers(self: web.BoardRequestHandler) -> None:
        original(self)  # 200 的状态行与头真的写到 socket 上
        if armed["on"]:
            raise make_exc()

    monkeypatch.setattr(web.BoardRequestHandler, "end_headers", flaky_end_headers)

    with _running_server(path) as base:
        capsys.readouterr()
        replies = [_raw_bytes(base, "GET", "/api/board") for _ in range(2)]
        armed["on"] = False
        status, _, body = _http(base, "/api/board")
        captured = capsys.readouterr()

    for reply in replies:
        assert reply.count(b"HTTP/1.") == 1  # 只有一条状态行
        assert reply.startswith(b"HTTP/1.0 200 OK")
        assert reply.partition(b"\r\n\r\n")[2] == b""  # 体没写出去
    assert "Traceback (most recent call last)" not in captured.err
    assert ("写出中断" in captured.err) is expect_note

    # 头已发就放弃这条连接，但线程/进程没崩：下一个请求照常 200
    assert status == 200
    assert list(json.loads(body.decode("utf-8"))["columns"]) == [
        "running",
        "queued",
        "blocked",
        "done",
    ]


