#!/usr/bin/env bash
# 孤儿 PR 检查：找出「开着但没有任何未结卡提到它」的 PR。
# 背景：今天三次出现「实现卡交付后开了 PR，但没有合并卡」→ PR 无声堆积。
# 本脚本只报告，不建卡、不合并（建卡归属归 chief/pm）。
set -u
export PATH=/home/liu/.local/bin:/usr/local/bin:/usr/bin:/bin
REPO=/home/liu/project/team-board
DB=/home/liu/.hermes/kanban/boards/devteam/kanban.db
LOG=/home/liu/.hermes/profiles/paimeng/workspace/pr-orphan-check.log

cd "$REPO" || exit 1
unset HTTPS_PROXY
prs=$("$HOME/.local/bin/gh" pr list --state open --json number --jq '.[].number' 2>/dev/null)
[ -z "$prs" ] && exit 0

orphans=""
for n in $prs; do
  hit=$(python3 - "$DB" "$n" <<'PY'
import sqlite3, sys
db, pr = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
pat = f"%#{pr}%"
q = """select count(*) from tasks t where t.status not in ('done','archived')
       and (t.body like ? or exists (select 1 from task_comments c where c.task_id=t.id and c.body like ?))"""
print(con.execute(q, (pat, pat)).fetchone()[0])
PY
)
  [ "${hit:-0}" = "0" ] && orphans="$orphans #$n"
done

if [ -n "$orphans" ]; then
  echo "$(date '+%F %T') 孤儿 PR（无任何未结卡提及）:$orphans" | tee -a "$LOG"
else
  echo "$(date '+%F %T') 无孤儿 PR（开放 $(echo $prs | wc -w) 个）" >> "$LOG"
fi
