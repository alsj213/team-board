"""Web 层：payload 装配（纯函数）+ 只读 HTTP 服务。

上半部分是纯函数：三个 payload 构造函数与一个 HTML 常量函数，不读环境、
不打印、不碰数据库。下半部分（``BoardRequestHandler`` / ``make_server``）是
HTTP 适配：每个请求现开一条 ``mode=ro`` 连接、用完即关，只提供 GET，
任何写接口都不存在。
"""

from __future__ import annotations

import datetime
import http.server
import json
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import commands, db, fmt
from .model import TITLE_MAX, Task

#: 页面上的四列，顺序即渲染顺序（不再有第五列）
COLUMNS = ("running", "queued", "blocked", "done")

#: 状态 → 列。archived / triage / todo / review 不在表里 → 不进任何列
_STATUS_TO_COLUMN = {
    "running": "running",
    "ready": "queued",
    "scheduled": "queued",
    "blocked": "blocked",
    "done": "done",
}

#: 前端自动刷新间隔（毫秒），与 index_html 里的 REFRESH_MS 一致
REFRESH_MS = 5000


def card_payload(task: Task, now: int) -> dict:
    """一张卡的 JSON 形状：键顺序固定，类型固定 ``str, str, str, int, str``。

    字段口径与 ``commands.render_export`` 完全一致：``assignee`` / ``status`` /
    ``title`` 为 None 时输出空串（不套列表用的「—」），``title`` 不截断，
    ``duration_minutes`` 一律来自 ``Task.duration_minutes(now)``。
    """
    return {
        "id": task.id,
        "assignee": task.assignee or "",
        "status": task.status or "",
        "duration_minutes": task.duration_minutes(now),
        "title": task.title or "",
    }


def board_payload(tasks: list[Task], now: int) -> dict:
    """看板四列：``{"columns": {running, queued, blocked, done}}``。

    - 分组只看 ``_STATUS_TO_COLUMN``；不在表里的卡（archived / triage / todo /
      review）不进任何列，也不新建第五列；
    - 列内排序统一 ``(-duration_minutes(now), id)`` 升序；
    - ``columns`` 的键顺序恒为 ``COLUMNS``；空列是 ``[]``。
    """
    grouped: dict[str, list[Task]] = {name: [] for name in COLUMNS}
    for task in tasks:
        column = _STATUS_TO_COLUMN.get(task.status or "")
        if column is None:
            continue
        grouped[column].append(task)

    columns = {}
    for name in COLUMNS:
        ordered = sorted(
            grouped[name], key=lambda task: (-task.duration_minutes(now), task.id)
        )
        columns[name] = [card_payload(task, now) for task in ordered]
    return {"columns": columns}


def summary_payload(
    tasks: list[Task],
    rework: dict[str, int],
    now: int,
    day: datetime.date,
) -> dict:
    """summary 的 JSON 形状，数字全部来自 ``commands.summary_stats``。

    - ``snapshot``：``"%Y-%m-%d %H:%M"``，与 ``render_summary`` 首行同格式；
    - ``date``：``day.isoformat()``；
    - ``anomaly``：``len(stats.anomalies)``；``anomalies`` 顺序即 ``stats.anomalies``
      顺序，标题按 ``TITLE_MAX`` 截断（与文本一致）；
    - ``rework`` 本层不用，保留参数以便与 ``render_summary`` 签名对齐。
    """
    stats = commands.summary_stats(tasks, now, day)
    return {
        "snapshot": datetime.datetime.fromtimestamp(stats.snapshot_ts).strftime(
            "%Y-%m-%d %H:%M"
        ),
        "date": stats.day.isoformat(),
        "completed": stats.completed,
        "running": stats.running,
        "blocked": stats.blocked,
        "anomaly": len(stats.anomalies),
        "anomalies": [
            {
                "status": task.status or "",
                "minutes": minutes,
                "threshold": threshold,
                "id": task.id,
                "title": fmt.truncate(task.title or "", TITLE_MAX),
            }
            for minutes, task, threshold in stats.anomalies
        ],
    }


def index_html() -> str:
    """整页 HTML（单文件、零外部引用：样式与脚本都内联）。

    - 顶部 ``#summary`` 放四个计数 + 异常数；
    - 右上角 ``#autorefresh`` 复选框控制定时刷新（取消即 clearInterval）；
    - 四列 ``section.col[data-col]`` 的 ``<h2>`` 文本就是列名小写；
    - 所有渲染走 ``textContent`` / ``replaceChildren``，绝不把数据拼进 innerHTML。
    """
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>team-board</title>
<style>
  :root { color-scheme: light; }
  body {
    margin: 0; padding: 16px; background: #f6f7f9; color: #1f2328;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  h1 { font-size: 18px; margin: 0; }
  header label { font-size: 13px; color: #57606a; user-select: none; }
  #summary {
    display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center;
    font-size: 13px; color: #444d56; margin: 10px 0 16px;
  }
  #summary .anomaly { color: #b35900; font-weight: 600; }
  #main {
    display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px; align-items: start;
  }
  .col { background: #fff; border: 1px solid #d8dee4; border-radius: 8px; padding: 8px 10px 4px; }
  .col h2 {
    font-size: 12px; letter-spacing: .08em; text-transform: uppercase;
    color: #57606a; margin: 0 0 8px;
  }
  .card {
    border: 1px solid #e1e6eb; border-left: 3px solid #9aa4ad; border-radius: 6px;
    padding: 6px 8px; margin-bottom: 8px;
  }
  .card .id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: #57606a; }
  .card .title { font-size: 13px; margin-top: 2px; overflow-wrap: anywhere; }
  .card .meta { font-size: 11px; color: #6e7781; margin-top: 3px; }
  @media (max-width: 720px) { #main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>team-board</h1>
  <label><input type="checkbox" id="autorefresh" checked> 自动刷新</label>
</header>
<div id="summary"></div>
<main id="main">
  <section class="col" data-col="running"><h2>running</h2><div class="cards"></div></section>
  <section class="col" data-col="queued"><h2>queued</h2><div class="cards"></div></section>
  <section class="col" data-col="blocked"><h2>blocked</h2><div class="cards"></div></section>
  <section class="col" data-col="done"><h2>done</h2><div class="cards"></div></section>
</main>
<script>
const REFRESH_MS = 5000;
const COLUMN_NAMES = ["running", "queued", "blocked", "done"];
let timer = null;

function make(tag, className, value) {
  const node = document.createElement(tag);
  if (className) { node.className = className; }
  if (value !== undefined) { node.textContent = value; }
  return node;
}

function renderSummary(data) {
  const box = document.getElementById("summary");
  box.replaceChildren(
    make("span", "", "快照 " + data.snapshot),
    make("span", "", "日期 " + data.date),
    make("span", "", "完成 " + data.completed),
    make("span", "", "在跑 " + data.running),
    make("span", "", "阻塞 " + data.blocked),
    make("span", data.anomaly > 0 ? "anomaly" : "", "异常 " + data.anomaly)
  );
}

function renderCard(card) {
  const box = make("div", "card");
  const meta = card.assignee + " | " + card.status + " | " + card.duration_minutes + " 分";
  box.replaceChildren(
    make("div", "id", card.id),
    make("div", "title", card.title),
    make("div", "meta", meta)
  );
  return box;
}

function renderBoard(data) {
  for (const name of COLUMN_NAMES) {
    const slot = document.querySelector('section[data-col="' + name + '"] .cards');
    if (!slot) { continue; }
    const cards = data.columns[name] || [];
    slot.replaceChildren.apply(slot, cards.map(renderCard));
  }
}

async function refresh() {
  try {
    const responses = await Promise.all([
      fetch("/api/summary"),
      fetch("/api/board")
    ]);
    const summary = await responses[0].json();
    const board = await responses[1].json();
    renderSummary(summary);
    renderBoard(board);
  } catch (err) {
    // 刷新失败保留上一帧，等下一次定时器再试
  }
}

function start() {
  if (timer !== null) { return; }
  refresh();
  timer = setInterval(refresh, REFRESH_MS);
}

function stop() {
  if (timer === null) { return; }
  clearInterval(timer);
  timer = null;
}

const toggle = document.getElementById("autorefresh");
toggle.addEventListener("change", function () {
  if (toggle.checked) {
    stop();
    start();
  } else {
    stop();
  }
});

start();
</script>
</body>
</html>
"""


class BoardRequestHandler(http.server.BaseHTTPRequestHandler):
    """只读看板的 HTTP 入口：GET /、/api/board、/api/summary，其余一律拒绝。

    - 路由只看 ``urlsplit(self.path).path``：查询串被忽略，``/api/board?x=1``
      等同 ``/api/board``；
    - 每个 API 请求现开一条只读连接、``finally`` 里 close，绝不把连接挂到
      server 上长期复用；
    - 数据库不可用（``db.BoardError``）→ 500 + ``{"error": ...}``，不打
      traceback、不崩线程；
    - payload 构建期的其它异常（13 位毫秒时间戳的 ``ValueError``、``sorted``
      遇到 NULL id 的 ``TypeError`` 等）由 ``except Exception`` 同样收口成
      500 + ``{"error": ...}``：``str(exc)`` 为空时回 ``type(exc).__name__``；
    - 非 GET → 405 + ``{"error": "method not allowed"}``，没有任何写接口。
    """

    server_version = "team-board"

    def _send_body(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj: Any) -> None:
        self._send_body(
            status,
            "application/json; charset=utf-8",
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        )

    def do_GET(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        path = urllib.parse.urlsplit(self.path).path
        server: Any = self.server

        if path == "/":
            self._send_body(
                200, "text/html; charset=utf-8", index_html().encode("utf-8")
            )
            return

        if path not in ("/api/board", "/api/summary"):
            self._send_json(404, {"error": "not found"})
            return

        now = server.board_clock()
        conn = None
        try:
            conn = db.connect(server.board_db_path)
            tasks = db.load_tasks(conn)
            if path == "/api/board":
                payload = board_payload(tasks, now)
            else:
                rework = db.load_rework_counts(conn)
                payload = summary_payload(
                    tasks,
                    rework,
                    now,
                    datetime.datetime.fromtimestamp(now).date(),
                )
        except db.BoardError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        except Exception as exc:  # 兜底：payload 层任何非 BoardError 异常都在这里收口
            # payload 层任何非 BoardError 异常（如 completed_at 是 13 位毫秒时
            # datetime.fromtimestamp 抛 ValueError、id 为 NULL 时 sorted 抛
            # TypeError）都在这里收口：回 500 + JSON、不把 traceback 交给
            # socketserver、不让异常穿出线程。
            self._send_json(500, {"error": str(exc) or type(exc).__name__})
            return
        finally:
            if conn is not None:
                conn.close()

        self._send_json(200, payload)

    def _method_not_allowed(self) -> None:
        self._send_json(405, {"error": "method not allowed"})

    def do_POST(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定
        self._method_not_allowed()

    def do_PUT(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定
        self._method_not_allowed()


class BoardHTTPServer(http.server.ThreadingHTTPServer):
    """带看板数据源与时钟注入的 ThreadingHTTPServer。

    只补类型声明，不改行为：``make_server`` 建完 server 后赋的正是这两个属性。
    """

    board_db_path: str
    board_clock: Callable[[], int]


def make_server(
    db_path: str | Path,
    *,
    bind: str = "127.0.0.1",
    port: int = 8787,
    clock: Callable[[], int] | None = None,
) -> BoardHTTPServer:
    """建一个未启动的只读 HTTP server。

    - 端口合法性不在这里拦：``port=0`` 表示由内核挑临时端口（测试用，取
      ``server.server_address[1]``），范围校验归 CLI 层；
    - ``clock`` 注入当前时间（epoch 秒），缺省 ``time.time()``，测试靠它固定
      ``snapshot`` / ``date`` / 耗时。
    """
    server = BoardHTTPServer((bind, port), BoardRequestHandler)
    server.daemon_threads = True
    server.board_db_path = str(db_path)
    server.board_clock = clock or (lambda: int(time.time()))
    return server
