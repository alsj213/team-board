"""Web UI 的数据装配层：只做「算 + 拼数据」，不含任何 HTTP。

本模块只对外提供三个 payload 构造函数与一个 HTML 常量函数，全部是纯函数：
不读环境、不打印、不碰数据库、不 import ``http.server``。HTTP 层在 W2 里追加。
"""

from __future__ import annotations

import datetime

from . import commands, fmt
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
  #autorefresh-label { font-size: 13px; color: #57606a; user-select: none; }
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
  <label id="autorefresh-label"><input type="checkbox" id="autorefresh" checked> 自动刷新</label>
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
