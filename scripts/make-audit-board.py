#!/usr/bin/env python3
"""用 ``.team-board/cards.tsv`` 造一个只含单卡的临时看板库（给 CI 的 ``commit-audit`` 用）。

为什么需要：判据② 要读该卡**建卡时**的 assignee（``task_events`` 里
``kind='created'`` 的 ``payload.assignee``），而 GitHub runner 上没有真看板库
（默认路径 ``/home/liu/.hermes/kanban/boards/devteam/kanban.db`` 只在开发机上有）。
所以 CI 用仓库里一张显式镜像表 ``.team-board/cards.tsv`` 造临时库。

两张表都要造，缺一张 ``commit-audit`` 就 rc2：

- ``tasks(id TEXT PRIMARY KEY, assignee TEXT)`` —— 当前 assignee（判据① 语义）；
- ``task_events(task_id TEXT, kind TEXT, payload TEXT)`` —— 一条
  ``kind='created'``、``payload='{"assignee": "<建卡时 assignee>"}'``（判据② 读这个）。

fail closed：卡不在表里 / 表里有重复行 / 行格式坏 / assignee 为空 → stderr 说清 +
``exit 2``；**不跳过、不写空 assignee**。

用法::

    python scripts/make-audit-board.py --cards .team-board/cards.tsv \\
        --card t_51f5fad0 --out /tmp/commit-audit-board.db
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

#: 卡号：``t_`` + 8 位小写十六进制（与 team_board.commits.CARD_RE 同一口径）
CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")

#: 输出库里的退出码：0 成功 / 2 参数或环境错误（与 CLI 同一套数字）
RC_OK = 0
RC_USAGE = 2


def _fail(message: str) -> int:
    """把原因写到 stderr 并返回 rc2（fail closed，不静默跳过）。"""

    print(f"错误: {message}", file=sys.stderr)
    return RC_USAGE


def load_cards(path: Path) -> dict[str, str]:
    """读 ``card<TAB>assignee`` 表；``#`` 注释行与空行忽略。

    缺文件 / 不是 UTF-8 / 某行不是两列 / 卡号形态非法 / assignee 为空 / 卡号重复
    → ``ValueError``（调用方转 rc2）。
    """

    if not path.exists():
        raise ValueError(f"身份镜像表不存在：{path}")
    if not path.is_file():
        raise ValueError(f"身份镜像表不是普通文件：{path}")
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"身份镜像表不是 UTF-8 文本：{path}（第 {exc.start} 字节起）") from exc
    except OSError as exc:
        raise ValueError(f"身份镜像表读不了：{path}: {exc}") from exc

    cards: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise ValueError(
                f"第 {lineno} 行格式非法：需要 2 列制表符分隔（card<TAB>assignee），"
                f"实际 {len(fields)} 列：{line!r}"
            )
        card, assignee = (field.strip() for field in fields)
        if CARD_RE.fullmatch(card) is None:
            raise ValueError(f"第 {lineno} 行卡号非法：{card!r}（应形如 t_1234abcd）")
        if not assignee:
            raise ValueError(f"第 {lineno} 行 assignee 为空（卡 {card}）")
        if card in cards:
            raise ValueError(f"第 {lineno} 行卡号重复：{card!r}")
        cards[card] = assignee
    if not cards:
        raise ValueError(f"身份镜像表没有任何有效行（只有注释/空行）：{path}")
    return cards


def write_board(out: Path, card: str, assignee: str) -> None:
    """在 ``out`` 造两张表的最小看板库（先删旧文件，免得残留别的卡）。"""

    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"assignee": assignee}, ensure_ascii=False)
    conn = sqlite3.connect(out)
    try:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT)")
        conn.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT, payload TEXT)")
        conn.execute("INSERT INTO tasks (id, assignee) VALUES (?, ?)", (card, assignee))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload) VALUES (?, 'created', ?)",
            (card, payload),
        )
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make-audit-board.py",
        description="用 .team-board/cards.tsv 造单卡临时看板库（CI 的 commit-audit 用）",
    )
    parser.add_argument("--cards", required=True, metavar="PATH", help="card<TAB>assignee 表")
    parser.add_argument("--card", required=True, metavar="CARD", help="要写进库的卡号")
    parser.add_argument("--out", required=True, metavar="PATH", help="输出 sqlite 路径")
    args = parser.parse_args(argv)

    if CARD_RE.fullmatch(args.card) is None:
        return _fail(f"卡号非法：{args.card!r}（应形如 t_1234abcd）")
    try:
        cards = load_cards(Path(args.cards))
    except ValueError as exc:
        return _fail(str(exc))
    if args.card not in cards:
        return _fail(
            f"卡 {args.card} 不在身份镜像表 {args.cards} 里 —— 补一行"
            f"「{args.card}<TAB><建卡时 assignee>」再跑（不许跳过判据②）"
        )
    try:
        write_board(Path(args.out), args.card, cards[args.card])
    except (OSError, sqlite3.Error) as exc:
        return _fail(f"造临时看板库失败：{args.out}: {exc}")
    print(f"已造临时看板库：{args.out}（卡 {args.card} assignee={cards[args.card]}）")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
