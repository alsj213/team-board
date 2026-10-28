"""真起 HTTP server 的集成测试：路由、只读、脏库、CLI 参数边界。

全部用 ``web.make_server(path, port=0)`` + 后台线程跑 ``serve_forever``，
请求走 ``urllib.request``，不碰真实网络对端。
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
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


@pytest.fixture()
def board(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "b.db"
    ids = build_board(path, now=FIXTURE_NOW)
    return path, ids


@contextlib.contextmanager
def _running_server(db_path: str | Path) -> Iterator[str]:
    """起一个临时端口的 server，退出 with 时 shutdown + server_close + join。"""
    server = web.make_server(db_path, port=0, clock=lambda: FIXTURE_NOW)
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


@pytest.mark.parametrize("port", ["1", "8787", "65535"])
def test_parse_port_accepts_edges(port: str) -> None:
    assert cli._parse_port(port) == int(port)


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1"])
def test_parse_bind_accepts_local_addresses(bind: str) -> None:
    assert cli._parse_bind(bind) == bind


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
