"""``gh pr checks`` 的分类口径（纯函数：不跑子进程、不联网、不碰文件系统）。

为什么 ``NO_CHECKS`` 要单列
---------------------------
R6 ① 要求「合并前 CI 全绿」。而 ``gh pr checks`` 在一张**从没跑过 workflow** 的 PR 上输出
``no checks reported on the '<branch>' branch`` 且 ``rc=1`` —— 它既不是 pass 也不是失败，
是第三种「压根没判过」的状态。把这一档折进通过，等于给没跑过 CI 的分支颁发绿灯（本仓
PR #2 / #3 就是这么合进去的）；折进笼统的失败，又会让调用方以为 CI 真红了、去查不存在的
红步骤。所以单列 :data:`CHECK_NO_CHECKS`，合并闸门的拒绝文案点明「不算绿（R6）」。

四档与退出码的对应（唯一来源是卡 ``t_1a042fbb`` 契约 v1 · E6 段）::

    rc == 0                            → CHECK_PASS        （gh 自己保证 rc0 即全 pass）
    rc == 1 且输出含 no checks reported → CHECK_NO_CHECKS
    rc == 1 其它                        → CHECK_FAIL
    其它 rc（127 找不到 gh / 超时 / 被杀…）→ CHECK_UNAVAILABLE

``CHECK_UNAVAILABLE`` 与 ``CHECK_FAIL`` 必须分开：不知道不等于通过，也不等于失败 —— 两者
都拒绝合并，但文案要能让人一眼分清「CI 红了，去修代码」与「CI 状态没读到，去看 gh / 网络
出了什么事」。分类与文案分开，是因为前者是判定、后者是给人看的解释。

本模块是**纯判定**：取数（跑 ``gh pr checks``）在 :mod:`team_board.merge_guard` 的
``checks_gate`` 里，那里把 rc 与输出交给 :func:`classify`。这样四条判据可以在不碰进程、
不碰网络的情况下逐条测。
"""

from __future__ import annotations

import re

#: ``gh pr checks`` 全绿（rc=0）。
#: bandit B105 假阳性：它把字面量 ``"PASS"`` 当硬编码口令 —— 这里是「CI 判定结果」。
#: 卡评论 E6 A 段把该常量写成 ``"***"``、验收标准第 1 条写的是「``rc=0`` → ``PASS``」，
#: 本模块取 ``"PASS"``：与步骤行的状态字段同一个词，不引入第三套说法（判定值不上屏，
#: 上屏的是 ``CI | PASS | …`` 那一行，见 ``merge_guard.checks_gate``）。
CHECK_PASS = "PASS"  # nosec B105 - 不是口令：CI 分类结果字面量
#: ``rc=1`` 且输出含 :data:`NO_CHECKS_MARKER`：这个分支上没有任何检查项 —— **不算绿**
CHECK_NO_CHECKS = "NO_CHECKS"
#: ``rc=1`` 的其它情形：有检查项且没全过
CHECK_FAIL = "FAIL"
#: 其它退出码：判不了（找不到 gh、超时、被杀…）
CHECK_UNAVAILABLE = "UNAVAILABLE"

#: 区分 ``NO_CHECKS`` 与 ``FAIL`` 的唯一标记（逐字对齐 gh 的
#: ``no checks reported on the '<branch>' branch``；大小写不敏感，见 :func:`classify`）
NO_CHECKS_MARKER = "no checks reported"

#: 贴进步骤行的「关键输出一行」最大长度（字符）：一行证据不许把整段输出带进 stdout
KEY_LINE_LIMIT = 200

#: 输出里一行非空行都没有时的占位（与 ``merge_guard.key_line`` 同口径）
NO_OUTPUT = "(无输出)"

#: 控制字符（保留 ``\n`` 由 ``splitlines`` 处理）：``gh`` 的进度条 / 颜色码会带 ``\r``
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def classify(rc: int, output: str) -> str:
    """把 ``gh pr checks`` 的退出码 + 输出判成四档之一（判定不看输出内容，除了 NO_CHECKS）。

    ``rc == 0`` 时**不看**输出内容：gh 自己保证 rc0 = 所有检查项没有失败。去解析每一行
    反而会把 gh 的表格格式当成契约（列宽 / 颜色 / 新增列都会让它误判），而真要有红项 gh
    会返回 rc1。

    ``rc == 1`` 分两种：输出里出现 :data:`NO_CHECKS_MARKER` = 根本没有检查项（``NO_CHECKS``，
    **不算绿**）；否则 = 确有检查项没过（``FAIL``）。标记匹配大小写不敏感 —— 判定的是
    「gh 说的这句话在不在」，不是猜它的排版；把大小写写成判定条件只会让同一句话在 gh
    的小版本升级后变成 ``UNAVAILABLE``。

    其余退出码（``127`` 找不到可执行文件、``-9`` 被杀、超时被外层转成的哨兵码…）一律
    ``UNAVAILABLE``：判不了就是判不了，不许当成 PASS，也不许当成 FAIL。
    """

    if rc == 0:
        return CHECK_PASS
    if rc == 1:
        if NO_CHECKS_MARKER in output.lower():
            return CHECK_NO_CHECKS
        return CHECK_FAIL
    return CHECK_UNAVAILABLE


def key_line(output: str) -> str:
    """输出里**最后一行非空行**，去控制字符、截断 :data:`KEY_LINE_LIMIT` 字符。

    为什么取最后一行：``gh pr checks`` 把汇总 / 失败项打在末尾，最后一行最可能是人想看
    的那句（``X of Y checks failed`` 之类）。整段输出不许进 stdout —— 步骤行只留一行证据，
    全量留着去翻 gh 自己的日志。

    去控制字符是必要的：``gh`` 在非 TTY 下也会带 ``\r``（进度条 / 颜色复位），把它原样
    拼进 ``<步骤> | ... | <关键输出>`` 会让下游按行切的解析器把一个步骤切成两行。
    """

    for line in reversed(output.splitlines()):
        cleaned = _CONTROL_RE.sub("", line).strip()
        if cleaned:
            return cleaned[:KEY_LINE_LIMIT]
    return NO_OUTPUT
