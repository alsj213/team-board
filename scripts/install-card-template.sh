#!/usr/bin/env bash
# 把仓库里的卡模板 docs/card-template.md 装到内核读取路径
# <kanban_home>/templates/card-template.md，并提供漂移检查（--check）。
#
# 内核读取侧（本机实测 hermes_cli/kanban_db.py:4302）：
#   _card_template_path() = $HERMES_CARD_TEMPLATE（该文件存在时优先）
#                           否则 <kanban_home>/templates/card-template.md
#   kanban_home() = $HERMES_KANBAN_HOME.strip() 否则 get_default_hermes_root()
# 已知缺口：$HERMES_CARD_TEMPLATE 覆盖读取路径时，本脚本不会自动跟随（另卡处理）。
# 注：早期约定落点曾多一层 /kanban；按本机内核实测（kanban_db.py:640 / 4302）与 #62 文档，
# 本脚本对齐内核 fallback（无 /kanban）。
#
# 用法：
#     bash scripts/install-card-template.sh [--check] [--dest <路径>]
#
# 目标路径（HERMES_KANBAN_HOME 先 strip，strip 后为空视同未设，与内核
# kanban_db.py 的 os.environ.get(...).strip() 同口径）：
#     ${HERMES_KANBAN_HOME:-${HERMES_HOME:-$HOME/.hermes}}/templates/card-template.md
# 用 --dest 可覆盖目标文件（测试与隔离取证都走这条）。
#
# 目标策略（写死）：
#   · 目标已存在且不是普通文件（目录 / FIFO / socket / 设备）→ rc2，不写、不挂死
#   · 目标是符号链接 → rc2 拒绝（不跟随改写被链文件；--check 同样拒绝）
#   · 写入走「同目录 temp + rename」，写失败（含 RLIMIT_FSIZE）不动原文件
#   · 路径以 `-` 开头 / 含换行 / 含回车 → rc2（避免被 dirname、cp 当成选项或日志串行）
#
# 退出码（写死，任何一处偏离都是缺陷）：
#     0  已装且两侧 sha256 一致（--check 时=没有漂移）
#     1  仅 --check 时：两侧 sha256 不等（漂移）或目标不存在
#     2  参数错误或 IO 错误（stderr 恰一行 `错误: …`，零 traceback）
#
# --check 只比对、不写盘（不建目录、不复制）。
set -uo pipefail

usage() {
    printf '用法: bash scripts/install-card-template.sh [--check] [--dest <路径>]\n' >&2
    printf '  --check        只比对源与目标的 sha256，不写盘；漂移或缺目标 → rc1\n' >&2
    printf '  --dest <路径>  覆盖目标文件（默认 ${HERMES_KANBAN_HOME:-${HERMES_HOME:-$HOME/.hermes}}/templates/card-template.md）\n' >&2
}

# 所有 rc2 只走这里：stderr 恰好一行 `错误: …`，不打印 traceback。
# 调用方传进来的消息必须不含换行（路径里的换行在入口处就挡掉）。
die() {
    printf '错误: %s\n' "$1" >&2
    exit 2
}

# 去首尾空白（与内核 .strip() 同口径：空白串当空串）。
trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# 描述一个已存在的非普通文件类型，供 stderr 单行消息使用。
kind_of() {
    if [ -d "$1" ]; then
        printf '目录'
    elif [ -p "$1" ]; then
        printf 'FIFO'
    elif [ -S "$1" ]; then
        printf 'socket'
    elif [ -b "$1" ] || [ -c "$1" ]; then
        printf '设备'
    else
        printf '其它非普通文件'
    fi
}

# 算单个文件的 sha256，只回哈希本体（两侧用同一函数，避免一处 sha256sum、
# 一处 shasum 时格式不同导致误判漂移）。
# 两个坑都在这一个函数里收口：
#   · sha256sum 报错（EACCES 等）必须 2>/dev/null，否则 rc2 时 stderr 多一行；
#   · GNU sha256sum 对含反斜杠 / 换行的路径会在行首加 `\` 转义标记，
#     不剥掉就会被 cut 当成哈希前缀 → 装成功反而判「两侧不一致」。
hash_of() {
    local file="$1"
    [ -r "${file}" ] || return 1
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -- "${file}" 2>/dev/null | sed -e 's/^\\//' | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -- "${file}" 2>/dev/null | sed -e 's/^\\//' | cut -d' ' -f1
    else
        return 127
    fi
}

check=0
dest=""
dest_set=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            check=1
            shift
            ;;
        --dest)
            [ "$#" -ge 2 ] || die "--dest 缺少路径参数"
            dest="$2"
            dest_set=1
            shift 2
            ;;
        --dest=*)
            dest="${1#--dest=}"
            dest_set=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "未知参数: $1（可用: --check / --dest <路径>）"
            ;;
    esac
done

if [ "${dest_set}" -eq 1 ]; then
    if [ -z "${dest//[[:space:]]/}" ]; then
        die "--dest 路径不能为空"
    fi
    case "${dest}" in
        -*)
            die "--dest 路径不能以 - 开头: ${dest}"
            ;;
        *$'\n'*|*$'\r'*)
            die "--dest 路径不能含换行或回车"
            ;;
    esac
fi

script_dir="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || die "无法定位脚本所在目录"
repo_root="$(cd "${script_dir}/.." && pwd)" || die "无法定位仓库根目录"
src="${repo_root}/docs/card-template.md"

[ -f "${src}" ] || die "源模板不存在: ${src}"

if [ "${dest_set}" -eq 0 ]; then
    kanban_home="$(trim "${HERMES_KANBAN_HOME:-}")"
    if [ -z "${kanban_home}" ]; then
        root_dir="$(trim "${HERMES_HOME:-}")"
        if [ -z "${root_dir}" ]; then
            home_dir="$(trim "${HOME:-}")"
            if [ -z "${home_dir}" ]; then
                die "无法确定默认目标：HOME 未设置且未给 --dest"
            fi
            root_dir="${home_dir%/}/.hermes"
        fi
        root_dir="${root_dir%/}"
        [ -n "${root_dir}" ] || root_dir="/"
        kanban_home="${root_dir}"
    fi
    dest="${kanban_home%/}/templates/card-template.md"
fi

# 目标合法性：两种模式都先挡（rc2 单行）。符号链接必须在 -e 判定之前 ——
# 悬空软链的 -e 是假，落到后面就会「目标不存在 → 建目录 → 写穿」。
if [ -L "${dest}" ]; then
    die "目标是符号链接，拒绝跟随改写: ${dest}（请改用普通文件路径或先删除该链接）"
fi
if [ -e "${dest}" ] && [ ! -f "${dest}" ]; then
    die "目标已存在且不是普通文件（$(kind_of "${dest}")）: ${dest}"
fi

src_hash="$(hash_of "${src}")" || die "无法计算 sha256（缺 sha256sum / shasum 或源不可读）: ${src}"
[ -n "${src_hash}" ] || die "无法计算 sha256: ${src}"

if [ "${check}" -eq 1 ]; then
    if [ ! -e "${dest}" ]; then
        printf '缺失：目标模板不存在: %s\n' "${dest}"
        printf '请先运行安装（不带 --check）：bash scripts/install-card-template.sh --dest %s\n' "${dest}"
        exit 1
    fi
    dest_hash="$(hash_of "${dest}")" || die "无法读取目标文件: ${dest}"
    [ -n "${dest_hash}" ] || die "无法计算 sha256: ${dest}"

    printf '%s  %s\n' "${src_hash}" "${src}"
    printf '%s  %s\n' "${dest_hash}" "${dest}"

    if [ "${src_hash}" != "${dest_hash}" ]; then
        printf '漂移：%s 与源模板 sha256 不一致（源 %s，目标 %s）\n' \
            "${dest}" "${src_hash}" "${dest_hash}"
        exit 1
    fi

    printf '一致：%s\n' "${dest}"
    exit 0
fi

dest_dir="$(dirname -- "${dest}")" || die "无法确定目标目录: ${dest}"
mkdir -p -- "${dest_dir}" 2>/dev/null || die "无法创建目标目录: ${dest_dir}"

# 同目录 temp + rename：写失败（含 RLIMIT_FSIZE / 磁盘满）时旧模板一个字节都不动。
tmp="${dest}.tmp.$$.${RANDOM}"
cleanup_tmp() {
    if [ -n "${tmp:-}" ]; then
        rm -f -- "${tmp}"
    fi
}
trap cleanup_tmp EXIT

# `{ ...; } 2>/dev/null` 而不是 `cp ... 2>/dev/null`：cp 被信号杀死（RLIMIT_FSIZE
# → SIGXFSZ）时，bash 自己还会往 stderr 打一行 `line N: PID File size limit
# exceeded` —— 只有把这一组的 stderr 一起重定向才能保证 rc2 时 stderr 恰一行。
{ cp -f -- "${src}" "${tmp}"; } 2>/dev/null || die "复制失败（原文件未改动）: ${src} -> ${dest}"
tmp_hash="$(hash_of "${tmp}")" || die "无法读取写入的临时文件: ${tmp}"
[ -n "${tmp_hash}" ] || die "无法计算 sha256: ${tmp}"
[ "${tmp_hash}" = "${src_hash}" ] || die "写入后 sha256 与源不一致（原文件未改动）: ${dest}"

mv -f -- "${tmp}" "${dest}" 2>/dev/null || die "无法写入目标文件: ${dest}"
tmp=""
dest_hash="$(hash_of "${dest}")" || die "无法读取刚写入的目标文件: ${dest}"
[ -n "${dest_hash}" ] || die "无法计算 sha256: ${dest}"

printf '%s  %s\n' "${src_hash}" "${src}"
printf '%s  %s\n' "${dest_hash}" "${dest}"

if [ "${src_hash}" != "${dest_hash}" ]; then
    die "安装后两侧 sha256 仍不一致: ${dest}"
fi

printf '已安装：%s\n' "${dest}"
exit 0
