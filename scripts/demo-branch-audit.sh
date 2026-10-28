#!/usr/bin/env bash
# 故意造偏差的演示：全部在 mktemp -d 里造仓，绝不碰真仓库。
# 依次跑：场景 A（跨卡提交）/ 场景 A'（未声明卡号）/ 场景 B（checkout 偏移）/ 绿色基线
# 每条都比对退出码与关键文案，任一不符则脚本自己 rc1。
set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# 真仓库路径：默认是宿主上的主 checkout；本机没有那个 checkout 时（例如 docker
# 沙箱、CI runner）用 TB_REAL_REPO 指到任意 checkout 来做同一套「未被污染」断言，
# 没有可指的对象就**明确 skip 并打提示**，绝不假装断言通过。
REAL_REPO=${TB_REAL_REPO:-/home/liu/project/team-board}
TMP=$(mktemp -d)
FAILS=0
RC=0

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

if [ -d "$REAL_REPO/.git" ] || git -C "$REAL_REPO" rev-parse --git-dir >/dev/null 2>&1; then
  REAL_REPO_OK=1
  echo "真仓库快照对象: $REAL_REPO"
else
  REAL_REPO_OK=0
  echo "⚠ 跳过真仓库污染断言：$REAL_REPO 不是可读的 git checkout"
  echo "  （宿主上跑会断言真 checkout；本机可用 TB_REAL_REPO=<其它 checkout> 指定对象）"
fi

if [ -n "${PYTHON:-}" ]; then
  PY=$PYTHON
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY=$ROOT/.venv/bin/python
else
  PY=python3
fi
export PYTHONPATH="$ROOT/src"

snapshot() {
  git -C "$REAL_REPO" status --porcelain --untracked-files=all
  echo "---- worktree list ----"
  git -C "$REAL_REPO" worktree list
}

g() { local dir=$1; shift; git -C "$dir" -c user.name=demo -c user.email=demo@example.com "$@"; }

new_repo() {
  local dir=$1
  mkdir -p "$dir"
  g "$dir" init -q -b main
  g "$dir" commit -q --allow-empty -m "chore: init"
}

commit_file() {
  local dir=$1 file=$2 msg=$3
  mkdir -p "$(dirname "$dir/$file")"
  echo demo > "$dir/$file"
  g "$dir" add -A
  g "$dir" commit -q -m "$msg"
}

run_audit() {  # run_audit <输出文件> <仓库> [额外参数...]
  local out=$1 repo=$2
  shift 2
  "$PY" -m team_board.cli branch-audit --repo "$repo" "$@" >"$out" 2>&1
  return $?
}

check() {  # check <场景名> <期望rc> <实际rc> <输出文件> <必须包含文案...>
  local name=$1 want=$2 got=$3 file=$4
  shift 4
  local ok=1 pat
  if [ "$got" != "$want" ]; then
    echo "  ✗ rc=$got，期望 rc=$want"
    ok=0
  fi
  for pat in "$@"; do
    if ! grep -qF -- "$pat" "$file"; then
      echo "  ✗ 输出里缺少：$pat"
      ok=0
    fi
  done
  if [ "$ok" = 1 ]; then
    echo "  ✓ $name（rc=$got）"
  else
    echo "  ✗ $name 不通过"
    FAILS=$((FAILS + 1))
  fi
}

if [ "$REAL_REPO_OK" = 1 ]; then
  echo "== 真仓库快照（开始） =="
  snapshot >"$TMP/before.txt"
  cat "$TMP/before.txt"
else
  echo "== 真仓库快照（开始）：已跳过 =="
fi

# ------------------------------------------------ 场景 A：跨卡提交
echo
echo "== 场景 A：卡 t_aaaaaaaa 的分支上出现 t_bbbbbbbb 的提交 =="
A="$TMP/a/repo"
new_repo "$A"
g "$A" checkout -q -b wt/t_aaaaaaaa
commit_file "$A" w2.txt "feat: t_bbbbbbbb 的活跑到 A 分支"
g "$A" checkout -q main
run_audit "$TMP/a.out" "$A"; RC=$?
cat "$TMP/a.out"
check "场景 A" 1 "$RC" "$TMP/a.out" "t_aaaaaaaa" "t_bbbbbbbb" "跨卡提交"

# ------------------------------------------------ 场景 A'：未声明卡号
echo
echo "== 场景 A'：卡分支上的提交没有写卡号 =="
AP="$TMP/ap/repo"
new_repo "$AP"
g "$AP" checkout -q -b wt/t_aaaaaaaa
commit_file "$AP" w1.txt "chore: 顺手改点东西（没写卡号）"
g "$AP" checkout -q main
run_audit "$TMP/ap.out" "$AP"; RC=$?
cat "$TMP/ap.out"
check "场景 A'" 1 "$RC" "$TMP/ap.out" "未声明卡号" "t_aaaaaaaa"

# ------------------------------------------------ 场景 B：checkout 偏移
echo
echo "== 场景 B：主 checkout 停在卡分支 + 卡 worktree 目录名不对 =="
B="$TMP/b/repo"
new_repo "$B"
g "$B" checkout -q -b wt/t_aaaaaaaa
commit_file "$B" a.txt "feat(t_aaaaaaaa): 本卡的活"
g "$B" checkout -q main
g "$B" checkout -q -b wt/t_bbbbbbbb
commit_file "$B" b.txt "feat(t_bbbbbbbb): 本卡的活"
g "$B" checkout -q wt/t_aaaaaaaa   # 主 checkout 被切到卡分支（偏差 1）
mkdir -p "$TMP/b"
git -C "$B" worktree add -q "$TMP/b/wrongdir" wt/t_bbbbbbbb  # 目录名不对（偏差 2）
run_audit "$TMP/b.out" "$B"; RC=$?
cat "$TMP/b.out"
check "场景 B" 1 "$RC" "$TMP/b.out" "$B" "wt/t_aaaaaaaa" "（应为 main）" \
  "wrongdir 当前在 wt/t_bbbbbbbb"

# ------------------------------------------------ 绿色基线
echo
echo "== 绿色基线：main 干净 + 卡分支干净 + 卡 worktree 目录名=卡号 =="
GB="$TMP/g/repo"
new_repo "$GB"
g "$GB" checkout -q -b wt/t_aaaaaaaa
commit_file "$GB" a.txt "feat(t_aaaaaaaa): 本卡的活"
g "$GB" checkout -q main
mkdir -p "$TMP/g"
git -C "$GB" worktree add -q "$TMP/g/t_aaaaaaaa" wt/t_aaaaaaaa
run_audit "$TMP/g.out" "$GB"; RC=$?
cat "$TMP/g.out"
check "绿色基线" 0 "$RC" "$TMP/g.out" "结论：无偏差" "t_aaaaaaaa"

# ------------------------------------------------ 真仓库未被污染
echo
if [ "$REAL_REPO_OK" = 1 ]; then
  echo "== 真仓库快照（结束） =="
  snapshot >"$TMP/after.txt"
  cat "$TMP/after.txt"
  if diff -q "$TMP/before.txt" "$TMP/after.txt" >/dev/null; then
    echo "  ✓ 真仓库状态与演示前完全一致（未被污染）"
  else
    echo "  ✗ 真仓库状态发生变化："
    diff -u "$TMP/before.txt" "$TMP/after.txt" || true
    FAILS=$((FAILS + 1))
  fi
else
  echo "  ⚠ 真仓库污染断言已跳过（没有可读的 checkout，见开头提示）"
fi

echo
if [ "$FAILS" -eq 0 ]; then
  echo "演示结论：4 个场景全部符合预期"
  exit 0
fi
echo "演示结论：$FAILS 个场景不符合预期"
exit 1
