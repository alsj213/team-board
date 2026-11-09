#!/usr/bin/env bash
# 孤儿 PR 检查：找出「开着但没有任何未结卡认领它」的 PR。
#
# 认领判据 = repo 唯一判据 team_board.prclaim（R17；口径由 pm 在 t_0551e9b6 定死，
# 与 pr_orphan_autoclose.py 的 t_290ef106 用同一份）：未结卡（status ∉ {done, archived}）
# 正文里顶格整行 `PR-CLAIM: #<n>` 才算认领；评论提到、正文别处提到、`PR-REF:`、标题都不算。
#
# 旧判据是子串匹配（正文或评论任意位置出现 `#<n>` 即算认领）→ 真孤儿 #56 被漏报、
# 证据 PR #12 被误报。2026-10-31 起子串判据彻底删除：无开关、无回退。
# 本脚本只报告，不建卡、不合并（建卡归属归 chief/pm）。
set -u
export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
REPO=/home/liu/project/team-board
DB=/home/liu/.hermes/kanban/boards/devteam/kanban.db
LOG=/home/liu/.hermes/profiles/ops/workspace/pr-orphan-check.log
# 认领判据的实现来源；内嵌 python 只 import、不复制逻辑。TB_SRC 仅用于验收注入坏路径。
SRC=${TB_SRC:-/home/liu/project/team-board/src}
PY=/usr/bin/python3
GH="$HOME/.local/bin/gh"

cd "$REPO" || exit 1
unset HTTPS_PROXY

# 证据/不合并类不进清单（与 pr_orphan_autoclose.py 同规则）：head 前缀 evidence/
# 或标题含「证据 PR」「不合并」—— 这类 PR 本来就不该有卡认领，报出来是永久误报。
prs=$("$GH" pr list --state open --json number,title,headRefName --jq '
  .[]
  | select((((.headRefName // "") | startswith("evidence/")) | not))
  | select((([ ((.title // "") | contains("证据 PR")),
                ((.title // "") | contains("不合并")) ] | any) | not))
  | .number' 2>/dev/null)
[ -z "$prs" ] && exit 0

# 认领该 PR 的未结卡数。rc=0 → stdout 末行是一个十进制数字（0 = 无人认领，即孤儿）；
# rc≠0 = import / 版本钉 / 读库任一失败 → 环境错误，调用方按「判不了」处理，不回退任何旧判据。
claim_count() {
  "$PY" - "$DB" "$1" "$SRC" <<'PY'
import os
import sqlite3
import sys

db_path, pr, src = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sys.path.insert(0, src)
try:
    from team_board import db, prclaim
except Exception as exc:  # noqa: BLE001 —— import 失败一律环境错误
    print(f"import team_board 失败（SRC={src}）: {exc}", file=sys.stderr)
    raise SystemExit(3)

# 来源钉：import 成功但拿到的不是 SRC 下那份 = 环境不对（别拿别处的旧实现冒充判据）
pkg = getattr(sys.modules.get("team_board"), "__file__", "") or ""
if not pkg.startswith(os.path.join(src, "")):
    print(f"team_board 不是从 {src} 导入的（{pkg}）", file=sys.stderr)
    raise SystemExit(3)

# 版本钉：判据形态逐字一致，否则视为环境错误（不猜、不放宽）
want = r"^PR-CLAIM: #([1-9][0-9]*)$"
if prclaim.CLAIM_RE.pattern != want:
    print(f"prclaim.CLAIM_RE 版本钉失配: {prclaim.CLAIM_RE.pattern!r} != {want!r}",
          file=sys.stderr)
    raise SystemExit(3)

con = None
try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.iter_claim_rows(con)
except Exception as exc:  # noqa: BLE001 —— 读库失败同样是环境错误
    print(f"读库失败 {db_path}: {exc}", file=sys.stderr)
    raise SystemExit(3)
finally:
    if con is not None:
        con.close()

print(len(prclaim.audit(rows, pr).claims))
PY
}

orphans=""
envfail=0
errfile=$(mktemp "${TMPDIR:-/tmp}/pr-orphan.XXXXXX") || exit 1
for n in $prs; do
  out=$(claim_count "$n" 2>"$errfile")
  rc=$?
  cnt=$(printf '%s\n' "$out" | tail -n 1)
  if [ "$rc" -ne 0 ] || ! printf '%s' "$cnt" | grep -Eq '^[0-9]+$'; then
    why=$(tr '\n' ' ' < "$errfile" | cut -c1-300)
    echo "$(date '+%F %T') 判不了: #$n（环境错误）rc=$rc ${why:-stdout 无数字}" >> "$LOG"
    envfail=1
    continue
  fi
  [ "$cnt" = "0" ] && orphans="$orphans #$n"
done
rm -f "$errfile"

if [ -n "$orphans" ]; then
  echo "$(date '+%F %T') 孤儿 PR（无任何未结卡提及）:$orphans" | tee -a "$LOG"
fi
if [ "$envfail" -ne 0 ]; then
  echo "$(date '+%F %T') 本轮有判不了的 PR（环境错误）：未计入孤儿、未回退旧判据，rc=1" >> "$LOG"
  exit 1
fi
if [ -z "$orphans" ]; then
  echo "$(date '+%F %T') 无孤儿 PR（开放 $(echo $prs | wc -w) 个）" >> "$LOG"
fi
