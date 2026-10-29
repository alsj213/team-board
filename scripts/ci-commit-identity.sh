#!/usr/bin/env bash
# CI 唯一入口：`commit-identity` job 的全部判定逻辑（卡 → 建卡 assignee 的来源 +
# author 白名单 / committer 是本 bot 的稽核）。
#
# 为什么有这个脚本：判据② 要读该卡**建卡时**的 assignee（看板库 `task_events` 里
# `kind='created'` 的 `payload.assignee`），而 GitHub runner 上没有看板库 —— 于是 CI 用
# 仓库内一份镜像表 `card<TAB>assignee` 造临时库。镜像表**只从 main 读**
# （`git show origin/main:.team-board/cards.tsv`）：PR 分支里改这张表不参与判定，
# 否则就是「PR 自己放宽判据②」的自证通道。
#
# 卡不在 main 的镜像表里时**不 fail closed**：往副本追加 `$CARD<TAB>engineer` 并打印一行
# 说明，白名单回落到安全默认 `{engineer, qa}`（pm / hatch / chief 写代码本来就违规）——
# 既不让新卡把 CI 锁死，也不放宽判定。
#
# 用法：
#   bash scripts/ci-commit-identity.sh <repo-path> <branch> <rev>
#     repo-path  要稽核的 git 仓库（可以是别的 worktree；判定仍跑本脚本所在的这棵树）
#     branch     被稽核的分支名（`wt/<卡号>` 才判；其它分支 skip，与卡面口径一致）
#     rev        区间终点（CI 传 pull_request.head.sha，push 时传 origin/main）
#
# 退出码：0 无问题（含「不是卡分支，跳过」）/ 1 发现身份问题 / 2 参数或环境错误
#
# 实现注记：
# - `make-audit-board.py`、`team_board.cli`、`bots.tsv` 一律取**本脚本所在的树**
#   （CI 里就是被审的这棵树，与 branch-audit job「audit must run this build」同口径），
#   所以把 `<repo-path>` 指向别的 worktree 时，跑的仍是本树的构建与身份表。
# - `TB_CI_CARDS_FILE` / `TB_CI_BOARD_DB` 只为测试隔离存在，默认就是下面这两个 /tmp 路径。

set -uo pipefail

#: 参数或环境错（与 team_board CLI 同一套数字）
readonly RC_USAGE=2

#: 本脚本所在的树：make-audit-board.py / src / bots.tsv 都从这里取。
#: 必须在 `cd "$REPO_PATH"` **之前**解析 —— 否则相对路径 `scripts/ci-commit-identity.sh`
#: 会跟着新 cwd 落到被稽核的那个仓库里（实测：指向别的 worktree 时 rc2）。
SCRIPT_SELF="${BASH_SOURCE[0]}"
case "$SCRIPT_SELF" in
  /*) ;;
  *) SCRIPT_SELF="$PWD/$SCRIPT_SELF" ;;
esac
SCRIPT_ROOT="$(cd "$(dirname "$SCRIPT_SELF")/.." && pwd)" || {
  printf '错误: 解析不了脚本所在目录：%s\n' "$SCRIPT_SELF" >&2
  exit 2
}
readonly SCRIPT_ROOT

die() {
  printf '错误: %s\n' "$*" >&2
  exit "$RC_USAGE"
}

if [ "$#" -ne 3 ]; then
  die "用法: bash scripts/ci-commit-identity.sh <repo-path> <branch> <rev>（需要 3 个参数，实际 $# 个）"
fi

readonly REPO_PATH="$1"
readonly BRANCH="$2"
readonly REV="$3"

[ -n "$REPO_PATH" ] || die "repo-path 为空"
[ -d "$REPO_PATH" ] || die "repo-path 不是目录：$REPO_PATH"
cd "$REPO_PATH" || die "进不去 repo-path：$REPO_PATH"
# 要求它就是工作树的根：git 会向上发现父仓库，路径给错（比如随手给个子目录）时会静默
# 稽核到外面那棵仓库上去（实测：某台机器上 $HOME 本身是个 git 仓库，临时目录全在里面）。
TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null)" || die "不是 git 工作树：$REPO_PATH"
[ "$TOPLEVEL" = "$(pwd -P)" ] || die "repo-path 不是工作树的根：$REPO_PATH（实际根：$TOPLEVEL）"

[ -n "$BRANCH" ] || die "branch 为空（应形如 wt/t_1234abcd）"
[ -n "$REV" ] || die "rev 为空"
git rev-parse --verify --quiet "${REV}^{commit}" >/dev/null 2>&1 || die "rev 解析不成提交：$REV"

#: 卡号：`wt/` + `t_` + 8 位小写十六进制（与 commits.CARD_RE 同一口径）
CARD="$(printf '%s' "$BRANCH" | sed -n 's|^wt/\(t_[0-9a-f]\{8\}\)$|\1|p')"
if [ -z "$CARD" ]; then
  printf '不是 wt/<卡号> 分支（%s），跳过（判据①由 merge-accountability 承担）\n' "$BRANCH"
  exit 0
fi

#: 本脚本所在的树：make-audit-board.py / src / bots.tsv 都从这里取（见文件头的解析段）
readonly CARDS_MIRROR="${TB_CI_CARDS_FILE:-/tmp/cards-main.tsv}"
readonly BOARD_DB="${TB_CI_BOARD_DB:-/tmp/commit-audit-board.db}"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PY="python3"
  elif command -v python >/dev/null 2>&1; then
    PY="python"
  else
    die "找不到 python3 / python"
  fi
fi
readonly PY

mkdir -p "$(dirname "$CARDS_MIRROR")" || die "建不了镜像表目录：$(dirname "$CARDS_MIRROR")"

# 1) 镜像只认 main 上的那份；取不到（main 上还没有这张表 / 没 fetch）→ 按空镜像表处理，
#    随后 2) 会把本次的卡追加进去，判定照样走，不会因此把别的卡锁死。
if ! git show origin/main:.team-board/cards.tsv >"$CARDS_MIRROR" 2>/dev/null; then
  : >"$CARDS_MIRROR"
  printf '提示: 取不到 origin/main:.team-board/cards.tsv，按空镜像表处理\n' >&2
fi

# 2) 卡不在 main 的镜像表里 → 追加默认 engineer（白名单 → {engineer, qa}），并说明一行。
if ! awk -F'\t' -v card="$CARD" '
  /^[[:space:]]*#/ { next }
  {
    first = $1
    gsub(/^[[:space:]]+/, "", first)
    gsub(/[[:space:]]+$/, "", first)
    if (first == card) { found = 1 }
  }
  END { exit(found ? 0 : 1) }
' "$CARDS_MIRROR"; then
  printf '%s\t%s\n' "$CARD" engineer >>"$CARDS_MIRROR" || die "写不进镜像表副本：$CARDS_MIRROR"
  printf '卡 %s 不在 main 的镜像表 → author 白名单取默认 {engineer, qa}\n' "$CARD"
fi

BOARD_SCRIPT="$SCRIPT_ROOT/scripts/make-audit-board.py"
[ -f "$BOARD_SCRIPT" ] || die "本树里没有 $BOARD_SCRIPT"

# 造单卡临时看板库（fail closed 仍在它自己身上：表里行坏了就 rc2，这里只透传）
"$PY" "$BOARD_SCRIPT" --cards "$CARDS_MIRROR" --card "$CARD" --out "$BOARD_DB" ||
  die "造临时看板库失败（scripts/make-audit-board.py 非零退出），不跳过判据②"

BOTS="$SCRIPT_ROOT/.team-board/bots.tsv"
[ -f "$BOTS" ] || BOTS="$PWD/.team-board/bots.tsv"
[ -f "$BOTS" ] || die "找不到 bot 身份表：$SCRIPT_ROOT/.team-board/bots.tsv 与 $PWD/.team-board/bots.tsv 都不存在"

[ -d "$SCRIPT_ROOT/src/team_board" ] || die "本树里没有 src/team_board：$SCRIPT_ROOT"

export PYTHONPATH="$SCRIPT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

"$PY" -m team_board.cli commit-audit \
  --repo "$PWD" \
  --card "$CARD" \
  --rev "$REV" \
  --db "$BOARD_DB" \
  --bots "$BOTS"
rc=$?
exit "$rc"
