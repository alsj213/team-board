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
import string
import sys
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

#: 前端自动刷新间隔（毫秒）。``index_html`` 里的 ``const REFRESH_MS`` 由它渲染，
#: 全仓只有这一处可改（改常量页面就跟着变，不存在第二份字面量）。
REFRESH_MS = 5000

#: 500 响应体允许出现的稳定错误类别 → 一句话原因。
#: 原因里**不含**绝对路径、表名、SQLite 原文，也不含 ``str(exc)``；
#: 细节只写 stderr（一行，含请求方法与路径）。
PUBLIC_ERRORS = {
    "source_unavailable": "看板数据源不可用，细节见服务端日志",
    "data_invalid": "看板数据里存在无法表示的时间戳，细节见服务端日志",
    "internal_error": "看板数据构建失败，细节见服务端日志",
}


class PayloadError(Exception):
    """payload 构建期的数据问题（时间戳超出 ``datetime`` 可表示范围）。

    由 HTTP 层收口成 500 + ``data_invalid``：对外只说类别，``str(exc)``
    （任务 id、具体数值）只进 stderr。
    """


def _check_time_range(tasks: list[Task]) -> None:
    """卡上的时间戳必须能被 ``datetime.fromtimestamp`` 表示。

    13 位毫秒（``1793160000000``）这类脏值落在起点上是**静默**夹成 0 分钟
    （``Task.duration_minutes`` 的 ``max(0, …)``），落在 ``completed_at`` 上
    却在 ``summary_stats`` 里抛 ``ValueError`` —— 同一类脏数据两种行为。
    这里统一成前者也报错：起点与终点都校验，两个接口口径一致，不再返回一个
    看起来正常的 0 分钟。
    """
    for task in tasks:
        for value in (task.start_ts(), task.completed_at):
            if value is None:
                continue
            try:
                datetime.datetime.fromtimestamp(value)
            except (OverflowError, OSError, ValueError) as exc:
                raise PayloadError(
                    f"任务 {task.id} 的时间戳超出可表示范围: {value!r}"
                ) from exc


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
    - ``columns`` 的键顺序恒为 ``COLUMNS``；空列是 ``[]``；
    - 卡上的时间戳越界（13 位毫秒等）→ ``PayloadError``（HTTP 层收口成 500），
      不静默按 0 分钟出数。
    """
    _check_time_range(tasks)
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
    now: int,
    day: datetime.date,
) -> dict:
    """summary 的 JSON 形状，数字全部来自 ``commands.summary_stats``。

    - ``snapshot``：``"%Y-%m-%d %H:%M"``，与 ``render_summary`` 首行同格式；
    - ``date``：``day.isoformat()``；
    - ``anomaly``：``len(stats.anomalies)``；``anomalies`` 顺序即 ``stats.anomalies``
      顺序，标题按 ``TITLE_MAX`` 截断（与文本一致）；
    - **不读 ``task_events``**：``render_summary`` 的 ``rework`` 参数本层用不上，
      所以这里不再接它、HTTP 层也不再查（缺 ``task_events`` 表时本接口与
      ``/api/board`` 行为一致 —— 都不 500，见 README「Web 看板（serve）」）；
    - 卡上的时间戳越界（13 位毫秒等）→ ``PayloadError``，与 ``/api/board`` 同口径。
    """
    _check_time_range(tasks)
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


#: 整页 HTML 模板。用 ``string.Template`` 而不是 f-string：页面里 CSS 有上百个
#: ``{`` / ``}``，f-string 得逐个转义、且以后加样式就会忘；``${REFRESH_MS}`` 是
#: 唯一需要渲染的占位符（刷新间隔只有 ``REFRESH_MS`` 一个来源）。
_PAGE_TEMPLATE = string.Template(
    """<!doctype html>
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
  #error {
    display: none; margin: 0 0 14px; padding: 6px 10px; border-radius: 6px;
    font-size: 13px; background: #ffebe9; border: 1px solid #ffc1bc; color: #b31d28;
  }
  #error.visible { display: block; }
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
<div id="error" role="alert"></div>
<main id="main">
  <section class="col" data-col="running"><h2>running</h2><div class="cards"></div></section>
  <section class="col" data-col="queued"><h2>queued</h2><div class="cards"></div></section>
  <section class="col" data-col="blocked"><h2>blocked</h2><div class="cards"></div></section>
  <section class="col" data-col="done"><h2>done</h2><div class="cards"></div></section>
</main>
<script>
const REFRESH_MS = ${REFRESH_MS};
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

function showError(text) {
  const box = document.getElementById("error");
  box.textContent = text;
  box.className = "visible";
}

function clearError() {
  const box = document.getElementById("error");
  box.textContent = "";
  box.className = "";
}

async function refresh() {
  try {
    const responses = await Promise.all([
      fetch("/api/summary"),
      fetch("/api/board")
    ]);
    if (!responses[0].ok || !responses[1].ok) {
      // 只有两个接口都是 2xx 才渲染；否则保留上一帧，只把错误行写出来。
      // 500 的响应体是 {"error": ...}，直接 .json() 会解析成功并被当数据渲染。
      const failed = responses.filter(function (response) {
        return !response.ok;
      })[0];
      let detail = "";
      try {
        const body = await failed.json();
        if (body && body.error) { detail = "：" + body.error; }
      } catch (parseErr) {
        detail = "";
      }
      showError("刷新失败（HTTP " + failed.status + "）" + detail + "，保留上一帧");
      return;
    }
    const summary = await responses[0].json();
    const board = await responses[1].json();
    renderSummary(summary);
    renderBoard(board);
    clearError();
  } catch (err) {
    // 网络层失败（断网、连接被重置、JSON 解析不通过）同样保留上一帧
    showError("刷新失败（" + err + "），保留上一帧");
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
)


def index_html() -> str:
    """整页 HTML（单文件、零外部引用：样式与脚本都内联）。

    - 顶部 ``#summary`` 放四个计数 + 异常数；
    - ``#summary`` 相邻的 ``#error`` 是刷新失败提示行：只在有接口不满足
      ``response.ok`` 时出现，文案带 HTTP 状态码；刷新失败**不清空**上一帧，
      成功一次后自动消失；
    - 右上角 ``#autorefresh`` 复选框控制定时刷新（取消即 clearInterval）；
    - 四列 ``section.col[data-col]`` 的 ``<h2>`` 文本就是列名小写；
    - 所有渲染走 ``textContent`` / ``replaceChildren``，绝不把数据拼进 innerHTML；
    - ``const REFRESH_MS`` 由模块常量 ``REFRESH_MS`` 渲染（模板占位
      ``${REFRESH_MS}``）：页面里没有第二份字面量，改常量页面就跟着变。
    """
    return _PAGE_TEMPLATE.substitute(REFRESH_MS=REFRESH_MS)


class BoardRequestHandler(http.server.BaseHTTPRequestHandler):
    """只读看板的 HTTP 入口：GET /、/api/board、/api/summary，其余一律拒绝。

    - 路由只看 ``urlsplit(self.path).path``：查询串被忽略，``/api/board?x=1``
      等同 ``/api/board``；
    - 每个 API 请求现开一条只读连接、``finally`` 里 close，绝不把连接挂到
      server 上长期复用；
    - 数据库不可用（``db.BoardError``）→ 500 + ``{"error": "source_unavailable: …"}``，
      不打 traceback、不崩线程；
    - payload 构建期的其它异常（卡上时间戳超出可表示范围的 ``PayloadError``、
      ``sorted`` 遇到 NULL id 的 ``TypeError``、payload 里混进 bytes 导致
      ``json.dumps`` 抛 ``TypeError`` 等）同样收口成 500：``PayloadError`` →
      ``data_invalid``，其余 → ``internal_error``；
    - **500 响应体一律脱敏**：只有 ``{"error": "<类别>: <一句话原因>"}``，
      不含绝对路径、表名、SQLite 原文，也不含 ``str(exc)``；异常细节
      （类型 + 原文，含请求方法/路径）写**一行** stderr，供运维排查；
    - 序列化在发状态行之前：``json.dumps`` 失败时一个字节都还没出去，因此还能
      干净地改回 500 + JSON（客户端拿到状态码，不是断连）；
    - **所有方法**（GET / HEAD / OPTIONS / TRACE / POST / PUT / DELETE / PATCH）
      都经 ``_dispatch``：客户端断开属于 ``ConnectionError`` 家族
      （``BrokenPipeError`` / ``ConnectionResetError`` / ``ConnectionAbortedError``
      ……），此时写什么都写不出去 —— 静默丢弃、不打 traceback、不重试、不让
      ``socketserver.handle_error`` 打出整段栈；
    - **一条连接只写一条状态行**：``_start_response`` 一进来就置
      ``_response_started``，此后任何写出失败都不再补 500（否则客户端会收到
      ``200 …`` 紧跟 ``500 …`` 两条状态行，代理直接判协议错），只留一行 stderr；
    - 非 GET → 405 + ``{"error": "method not allowed"}``，没有任何写接口；
      HEAD 按 HTTP 语义只给状态码与头、不写响应体。
    """

    server_version = "team-board"

    #: 状态行是否已经进入写出流程。``_start_response`` 一进来就置 True：从这里
    #: 起只允许这一条响应 —— 写出中途再失败**不能**改写成 500，否则同一连接上
    #: 会出现两条状态行。这是「不许写第二遍」的唯一判据。
    _response_started = False

    def _dispatch(self, action: Callable[[], None]) -> None:
        """所有 ``do_*`` 的公共入口：断连与写出失败在这里收口。

        - ``ConnectionError`` 家族（``BrokenPipeError`` / ``ConnectionResetError``
          / Windows 常见的 ``ConnectionAbortedError(10053)``）都是「对端已经走
          了」：静默丢弃，不重试、不打 traceback；
        - 其它异常交给 ``_respond_error``：状态行还没发才补 500。
        """
        self._response_started = False
        try:
            action()
        except ConnectionError:
            return
        except Exception as exc:  # 兜底：任何异常交给 _respond_error 收口（BLE001 不启用，无需 noqa）
            self._respond_error(exc)

    def _respond_error(self, exc: BaseException) -> None:
        """异常的统一收口：头未发 → 500 + 脱敏 JSON；头已发 → 只留一行 stderr。"""
        if self._response_started:
            print(
                f"[team-board] 写出中断 {self.command} {self.path} "
                f"<- {type(exc).__name__}: {exc}，已放弃本连接",
                file=sys.stderr,
                flush=True,
            )
            return
        if isinstance(exc, db.BoardError):
            category = "source_unavailable"
        elif isinstance(exc, PayloadError):
            category = "data_invalid"
        else:
            category = "internal_error"
        try:
            self._send_server_error(category, exc)
        except ConnectionError:
            # 连 500 都写不出去：对端已断，静默。
            return

    def _start_response(self, status: int, content_type: str, length: int) -> None:
        """写状态行与头，并置 ``_response_started``（见其注释）。"""
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_body(self, status: int, content_type: str, body: bytes) -> None:
        self._start_response(status, content_type, len(body))
        self.wfile.write(body)

    def _send_json(self, status: int, obj: Any) -> None:
        # 先序列化再发头：``json.dumps`` 失败时还没写出任何状态行，
        # 外层才能干净地改回 500 + ``{"error": ...}``。
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_body(status, "application/json; charset=utf-8", body)

    def _send_server_error(self, category: str, exc: BaseException) -> None:
        """500 + 脱敏响应体；细节写一行 stderr（含请求方法 + 路径）。

        响应体固定 ``{"error": "<category>: <一句话>"}``，内容全部来自
        ``PUBLIC_ERRORS`` —— 不含绝对路径、表名、SQLite 原文，也不含
        ``str(exc)``（那里可能就是 ``no such table: task_events``）。
        """
        print(
            f"[team-board] 500 {category} {self.command} {self.path} "
            f"<- {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        self._send_json(500, {"error": f"{category}: {PUBLIC_ERRORS[category]}"})

    def do_GET(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        self._dispatch(self._get)

    def _get(self) -> None:
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
                # 不查 task_events：summary_payload 用不上 rework（见其 docstring），
                # 每请求白做一次全表 GROUP BY 还会让缺表把整个接口拖成 500。
                payload = summary_payload(
                    tasks,
                    now,
                    datetime.datetime.fromtimestamp(now).date(),
                )
            self._send_json(200, payload)
        finally:
            # 这里的异常一律交给 ``_dispatch`` 收口（断连静默 / 头未发才补 500）：
            # 先把只读连接关掉，免得写响应期间一直占着它。
            if conn is not None:
                conn.close()

    def _method_not_allowed(self, *, with_body: bool = True) -> None:
        if with_body:
            self._send_json(405, {"error": "method not allowed"})
            return
        # HEAD：与 GET 同头不同体，只给状态码与头（Content-Length 恒 0）
        self._start_response(405, "application/json; charset=utf-8", 0)

    def _reject(self) -> None:
        self._method_not_allowed()

    def _reject_head(self) -> None:
        self._method_not_allowed(with_body=False)

    def do_HEAD(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        self._dispatch(self._reject_head)

    def do_OPTIONS(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        self._dispatch(self._reject)

    def do_TRACE(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        self._dispatch(self._reject)

    def do_POST(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        self._dispatch(self._reject)

    def do_PUT(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        self._dispatch(self._reject)

    def do_DELETE(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        self._dispatch(self._reject)

    def do_PATCH(self) -> None:  # 必需名：BaseHTTPRequestHandler 约定（N802 不启用，无需 noqa）
        self._dispatch(self._reject)


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
