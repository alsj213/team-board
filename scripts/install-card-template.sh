#!/usr/bin/env bash
# 把仓库里的卡模板 docs/card-template.md 装到内核注入侧读的模板目录，
# 并提供漂移检查（--check）。
#
# 用法：
#     bash scripts/install-card-template.sh [--check] [--dest <路径>]
#
# 目标路径（与内核侧共用同一口径）：
#     ${HERMES_KANBAN_HOME:-${HERMES_HOME:-$HOME/.hermes}}/templates/card-template.md
# 用 --dest 可覆盖目标文件（测试与隔离取证都走这条）。
#
# 退出码（写死，任何一处偏离都是缺陷）：
#     0  已装且两侧 sha256 一致（--check 时=没有漂移）
#     1  仅 --check 时：两侧 sha256 不等（漂移）或目标不存在
#     2  参数错误或 IO 错误（stderr 一行 `错误: …`，零 traceback）
#
# --check 只比对、不写盘（不建目录、不复制）。
set -uo pipefail

usage() {
    printf '用法: bash scripts/install-card-template.sh [--check] [--dest <路径>]\n' >&2
    printf '  --check        只比对源与目标的 sha256，不写盘；漂移或缺目标 → rc1\n' >&2
    printf '  --dest <路径>  覆盖目标文件（默认 ${HERMES_KANBAN_HOME:-${HERMES_HOME:-$HOME/.hermes}}/templates/card-template.md）\n' >&2
}

# 所有 rc2 只走这里：stderr 恰好一行 `错误: …`，不打印 traceback。
die() {
    printf '错误: %s\n' "$1" >&2
    exit 2
}

# 算单个文件的 sha256，只回哈希本体（两侧用同一函数，避免一处 sha256sum、
# 一处 shasum 时格式不同导致误判漂移）。
hash_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -- "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -- "$1" | cut -d' ' -f1
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

if [ "${dest_set}" -eq 1 ] && [ -z "${dest//[[:space:]]/}" ]; then
    die "--dest 路径不能为空"
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || die "无法定位脚本所在目录"
repo_root="$(cd "${script_dir}/.." && pwd)" || die "无法定位仓库根目录"
src="${repo_root}/docs/card-template.md"

[ -f "${src}" ] || die "源模板不存在: ${src}"

if [ "${dest_set}" -eq 0 ]; then
    kanban_home="${HERMES_KANBAN_HOME:-}"
    [ -n "${kanban_home}" ] || kanban_home="${HERMES_HOME:-${HOME}/.hermes}"
    dest="${kanban_home%/}/templates/card-template.md"
fi

src_hash="$(hash_of "${src}")" || die "无法计算 sha256（缺 sha256sum / shasum）: ${src}"
[ -n "${src_hash}" ] || die "无法计算 sha256: ${src}"

if [ "${check}" -eq 1 ]; then
    if [ ! -e "${dest}" ]; then
        printf '缺失：目标模板不存在: %s\n' "${dest}"
        printf '请先运行安装（不带 --check）：bash scripts/install-card-template.sh --dest %s\n' "${dest}"
        exit 1
    fi
    [ -f "${dest}" ] || die "目标不是普通文件: ${dest}"
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

[ -d "${dest}" ] && die "目标是目录，不能当模板文件写: ${dest}"

dest_dir="$(dirname "${dest}")"
mkdir -p "${dest_dir}" 2>/dev/null || die "无法创建目标目录: ${dest_dir}"

cp -f "${src}" "${dest}" 2>/dev/null || die "复制失败: ${src} -> ${dest}"

dest_hash="$(hash_of "${dest}")" || die "无法读取刚写入的目标文件: ${dest}"
[ -n "${dest_hash}" ] || die "无法计算 sha256: ${dest}"

printf '%s  %s\n' "${src_hash}" "${src}"
printf '%s  %s\n' "${dest_hash}" "${dest}"

if [ "${src_hash}" != "${dest_hash}" ]; then
    die "安装后两侧 sha256 仍不一致: ${dest}"
fi

printf '已安装：%s\n' "${dest}"
exit 0
