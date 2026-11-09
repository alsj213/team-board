#!/usr/bin/env python3
"""看护件健康检查：看护件自己死了/报错了，这里能报出来。

判据：guard/<件名>.ok 的年龄 > 周期×2 + 5 分钟 → 红；.ok 里 rc≠0 → 红（报错）。
阈值来自 .ok 里记录的 period，新增看护件不用改本脚本。

用法：
  guard_health.py            # 人读：一行一个红件；全绿打印一行 OK
  guard_health.py --strict   # 机判：有任何红 → exit 1（cron/CI 用）
  guard_health.py --selftest # 自检：白名单必须覆盖所有 .ok，且排除其它团队的 profile
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time

GDIR = "/home/liu/.hermes/profiles/ops/workspace/guard"
# 只读自己团队的件；老板其他项目的 profile 一律不进范围（硬编码白名单，不读配置）
# 注意：guard_health 不检查自己 —— 自己上一次 rc≠0 会被自己读到，一次红就永远红（2026-10-31 19:10
# 起连红 73 轮的自锁就是这么来的）。本件自身的存活由 crontab 与 .ok 的 mtime 兜底。
ALLOW = ("unstick", "selfheal", "orphan_autoclose", "orphan_check",
         "stall_alert", "daily_metrics", "core_patch", "patrol_quick", "web_watchdog",
         "merged_reaper", "daily_metrics_weekly", "mgmt_plane")
FORBIDDEN = ("wsl-gp-agent", "wsl-uumit-0706")
GRACE_MIN = 5


def parse_ok(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            first = fh.readline().strip()
    except OSError:
        return {}
    kv = dict(re.findall(r"(\w+)=(\S+)", first))
    try:
        return {"ts": int(kv.get("ts", 0)), "rc": int(kv.get("rc", 1)),
                "lines": int(kv.get("lines", 0)), "period": int(kv.get("period", 0))}
    except ValueError:
        return {}


def check() -> list[str]:
    now = time.time()
    reds: list[str] = []
    seen = set()
    for path in sorted(glob.glob(f"{GDIR}/*.ok")):
        name = os.path.basename(path)[:-3]
        if name not in ALLOW:
            continue
        seen.add(name)
        info = parse_ok(path)
        if not info:
            reds.append(f"{name}: .ok 读不懂（心跳文件损坏）")
            continue
        period = info["period"] or 10
        age_min = (now - info["ts"]) / 60
        if age_min > period * 2 + GRACE_MIN:
            reds.append(f"{name}: 已 {age_min:.0f} 分钟没心跳（阈值 {period*2+GRACE_MIN} 分钟）")
        elif info["rc"] != 0:
            reds.append(f"{name}: 上次执行 rc={info['rc']}（报错，输出见 guard/{name}.log）")
    return reds


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        leaked = [x for x in ALLOW if x in FORBIDDEN]
        present = {
            os.path.basename(path)[:-3]
            for path in glob.glob(f"{GDIR}/*.ok")
        } - {"guard_health"}
        unmonitored = sorted(present - set(ALLOW))
        print(
            "selftest: 白名单件数", len(ALLOW),
            "| 越界:", leaked or "无",
            "| 漏报:", unmonitored or "无",
        )
        return 1 if leaked or unmonitored else 0

    reds = check()
    if reds:
        print("看护件异常：")
        for r in reds:
            print(" -", r)
        print("处置：先看 guard/<件名>.log 末尾；ops 15 分钟内确认并在板记原因；连续两轮红升级 paimeng。")
        return 1 if args.strict else 0
    print("看护件全部正常（心跳在阈值内、上次执行 rc=0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
