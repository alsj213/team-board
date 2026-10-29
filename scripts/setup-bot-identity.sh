#!/usr/bin/env bash
# 给某个 bot 的 worktree 装上它自己的提交者身份（判据③ 的环境面来源）。
#
# 用法：
#     bash scripts/setup-bot-identity.sh <profile> <worktree-path>
#
# 做的事情（顺序固定）：
#   1. 从 <worktree>/.team-board/bots.tsv 查 <profile> 的 name / email
#      （查不到 → stderr 提示 + exit 2，**不猜**）
#   2. 主仓（git-common-dir 的父目录）打开 extensions.worktreeConfig true
#      —— 不开这个开关，`git config --worktree` 无处可写
#   3. 写本 worktree 的 user.name / user.email（只影响这个 worktree，
#      不动主仓、不动别的卡）
#   4. 打印写入后的 user.email（幂等：重复跑结果一样，不报错）
#
# 为什么按 worktree 写而不是全局写：一台机器上同时有 pm / engineer / qa 的
# worktree，全局身份只能有一个 —— 全局写等于把上一个 bot 的身份覆盖掉。
#
# 判定权在 `commit-audit` + CI job `commit-identity`，这个脚本只负责让提交
# 元数据里的 committer 真的是本 bot。
set -euo pipefail

usage() {
    printf '用法: bash scripts/setup-bot-identity.sh <profile> <worktree-path>\n' >&2
}

if [ "$#" -ne 2 ]; then
    usage
    exit 2
fi

profile="$1"
worktree="$2"

if [ -z "$profile" ] || [ -z "$worktree" ]; then
    usage
    exit 2
fi

if [ ! -d "$worktree" ]; then
    printf '错误: worktree 路径不存在或不是目录: %s\n' "$worktree" >&2
    exit 2
fi

bots_file="$worktree/.team-board/bots.tsv"
if [ ! -f "$bots_file" ]; then
    printf '错误: 身份表不存在: %s（格式为 profile<TAB>name<TAB>email）\n' "$bots_file" >&2
    exit 2
fi

row="$(awk -F'\t' -v p="$profile" '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    NF != 3 { next }
    $1 == p { print; exit }
' "$bots_file")"

if [ -z "$row" ]; then
    printf '错误: 身份表里没有 profile=%s\n' "$profile" >&2
    printf '可用 profile（%s 第 1 列）:\n' "$bots_file" >&2
    awk -F'\t' '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        NF == 3 { printf "  %s\n", $1 }
    ' "$bots_file" >&2
    exit 2
fi

bot_name="$(printf '%s' "$row" | cut -f2)"
bot_email="$(printf '%s' "$row" | cut -f3)"

if ! git -C "$worktree" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    printf '错误: 不是 git 工作树: %s\n' "$worktree" >&2
    exit 2
fi

common_dir="$(git -C "$worktree" rev-parse --path-format=absolute --git-common-dir)"
main_repo="$(dirname "$common_dir")"

git -C "$main_repo" config extensions.worktreeConfig true
git -C "$worktree" config --worktree user.name "$bot_name"
git -C "$worktree" config --worktree user.email "$bot_email"

git -C "$worktree" config --worktree --get user.email
