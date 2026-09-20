#!/usr/bin/env bash
# 检查文档里指出来的本地文件是否真的存在。
#
# 为什么需要：文档里的链接坏掉是**静默**的 —— 没人会点开每一页。
# 而这个项目的文档量已经不小（README + 11 篇 docs + 11 篇 wiki），
# 改文件名时漏改一处链接几乎是必然的。
#
# 只查本地相对路径；外链不查，因为网络抖动会把它变成随机红。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

fail=0

check_file() {
  local f="$1" dir link
  [ -f "$f" ] || return 0
  dir="$(dirname "$f")"
  # 只看 markdown 的 ](path) 形式，且 path 带常见扩展名（带锚点/行号的截掉）
  while IFS= read -r link; do
    case "$link" in
      http*|mailto:*|'#'*) continue ;;
    esac
    link="${link%%#*}"
    # 站内绝对路径（/foo.md）在 GitHub Pages 下另有解析规则，跳过
    case "$link" in /*) continue ;; esac
    if [ ! -e "$dir/$link" ] && [ ! -e "$link" ]; then
      printf '\033[31m✗ %s\033[0m → %s\n' "$f" "$link"
      fail=1
    fi
  done < <(
    grep -oE '\]\([^)]+\.(md|yml|yaml|json|sh|py|html|svg|png|toml|txt|vhd)\)' "$f" \
      | sed 's/^](//; s/)$//'
  )
}

echo "== 文档链接检查 =="
for f in README.md AGENTS.md CONTRIBUTING.md SECURITY.md CHANGELOG.md \
         CODE_OF_CONDUCT.md docs/*.md wiki/*.md .github/*.md .github/ISSUE_TEMPLATE/*.yml; do
  check_file "$f"
done

[ "$fail" -eq 0 ] && echo "  OK —— 本地链接都能落地"
exit "$fail"
