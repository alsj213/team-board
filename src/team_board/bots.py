"""bot 名册与在跑状态：按 ``tasks.assignee`` 汇总（只读、自动判定）。

看板网页的「bot 标签页」只消费这一层：谁在名册里、谁是 working、各有几张
running 卡。名册来源是 ``tasks`` 表出现过的全部 ``assignee``（**含 archived
卡**）——只看未归档卡会让只剩归档卡的 bot 从名册里消失。

口径（逐条都是判据）：

1. ``name = task.assignee or ""``；``name.strip() == ""`` 跳过（NULL / 空串 /
   纯空白）；``name in NON_BOT_ASSIGNEES`` 跳过——**精确等值**，``Nobody``
   与 ``nobody`` 不是一回事，前者仍算 bot。
2. 名册不按 status 过滤：archived / triage / todo / review / blocked / done
   的卡都把 ``assignee`` 带进名册。
3. ``status`` 为 ``"working"`` 当且仅当该 name 存在 ``status == "running"``
   的卡（精确等值）；否则 ``"idle"``。状态是算出来的，不接受外部写入。
4. ``running`` 是该 name 的 running 卡数（``int``）；idle 的行恒为 0。
5. 排序 ``(0 if working else 1, name)`` 升序：working 在前，同类按 name 码位
   升序（不用 locale，不同机器的排序结果必须一致）。
6. 返回 ``{"bots": [{"name": str, "status": str, "running": int}, ...]}``，
   每行的键顺序固定 ``name, status, running``；空输入 → ``{"bots": []}``。
7. 纯函数：不读时钟、环境变量、文件、数据库，签名里也没有时间参数——同一
   份 ``tasks`` 永远得到同一个 payload，测试不必冻结时钟。
"""

from __future__ import annotations

from typing import Any

from .model import Task

#: 不是 bot 的 assignee 哨兵（精确等值比较，大小写不同即视为真 bot）
NON_BOT_ASSIGNEES: frozenset[str] = frozenset({"nobody"})

#: 有 running 卡 = 工作中
STATUS_WORKING = "working"

#: 没有 running 卡 = 空闲
STATUS_IDLE = "idle"

#: 判定「工作中」的状态字面量（与 ``model.STATUS_ORDER`` 里的 running 同值；
#: 这里单独写常量是为了不在名册层引入 status 流转顺序的依赖）
_RUNNING_STATUS = "running"


def bots_payload(tasks: list[Task]) -> dict[str, Any]:
    """按 ``assignee`` 汇总成 ``{"bots": [...]}``（规则见模块 docstring）。

    - 名册与计数一次遍历完成：命中 running 卡就给该 name 计数 +1，没命中也要
      把 name 登记进名册（计数初值 0），所以「只有 archived 卡」的 bot 照样
      出现且是 idle；
    - 排序在登记完成后统一做，名册顺序不依赖 ``tasks`` 的输入顺序；
    - 不做去重之外的任何加工：name 原样输出（不 strip、不折叠大小写），
      payload 里看到的字符串就是库里存的那个。
    """
    counts: dict[str, int] = {}
    for task in tasks:
        name = task.assignee or ""
        if name.strip() == "":
            continue
        if name in NON_BOT_ASSIGNEES:
            continue
        count = counts.get(name, 0)
        counts[name] = count + 1 if task.status == _RUNNING_STATUS else count

    rows: list[dict[str, Any]] = [
        {
            "name": name,
            "status": STATUS_WORKING if count else STATUS_IDLE,
            "running": count,
        }
        for name, count in counts.items()
    ]
    rows.sort(key=lambda row: (0 if row["status"] == STATUS_WORKING else 1, row["name"]))
    return {"bots": rows}
