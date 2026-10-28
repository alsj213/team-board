"""真起 HTTP server 的集成测试：路由、只读、脏库、CLI 参数边界。

全部用 ``web.make_server(path, port=0)`` + 后台线程跑 ``serve_forever``，
请求走 ``urllib.request``，不碰真实网络对端。
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
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
    assert [card["id"] for card in columns["running"]][0] == "t_running_stale"
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
        assert "不是合法的 SQLite 库" in error["error"]

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
