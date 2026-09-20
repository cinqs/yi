#!/usr/bin/env bash
# 把仓库里的 OWNER 占位符换成你自己的 GitHub 用户名或组织名。
#
# 为什么要有这个脚本：OWNER 出现在 README 徽章、documentation 链接、
# issue 模板、CODEOWNERS、CHANGELOG 的 compare 链接里。手改容易漏，
# 漏一处就是一个 404 的徽章或者指向别人仓库的链接。
#
#   ./tools/set-owner.sh your-github-name
#   ./tools/set-owner.sh your-github-name "你的署名"     # 顺带写进 LICENSE
#
# 跑完记得 `git diff` 看一眼。
set -euo pipefail

cd "$(dirname "$0")/.."

owner="${1:-}"
author="${2:-$owner}"

if [ -z "$owner" ]; then
  echo "用法: ./tools/set-owner.sh <github用户名或组织名> [LICENSE 里的署名]" >&2
  echo >&2
  echo "当前还没替换的位置：" >&2
  grep -rn 'OWNER' --exclude-dir=.git --exclude-dir=.venv --exclude-dir=vendor \
    --exclude-dir=dist --exclude-dir=build --exclude=set-owner.sh \
    . 2>/dev/null | sed 's/^/  /' >&2
  exit 2
fi

if ! printf '%s' "$owner" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9-]*$'; then
  # 注意 ${owner} 的花括号：macOS 自带的是 bash 3.2，`$owner` 后面直接跟中文时
  # 它会把中文当成变量名的一部分，报 "unbound variable"。
  echo "「${owner}」不像合法的 GitHub 用户名/组织名（只允许字母、数字、连字符）。" >&2
  exit 2
fi

# 只处理**仓库跟踪的文件**：用 git ls-files 而不是全目录 grep，
# 否则 .venv / dist / 缓存目录里的副本也会被改，白改还可能改坏工具状态。
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  files="$(git ls-files -z | xargs -0 grep -l 'OWNER' 2>/dev/null | grep -v '^tools/set-owner.sh$' || true)"
else
  files="$(
    grep -rl 'OWNER' \
      --exclude-dir=.git --exclude-dir=.venv --exclude-dir=vendor \
      --exclude-dir=dist --exclude-dir=build --exclude-dir=.ruff_cache \
      --exclude-dir=.playwright-cli --exclude=set-owner.sh \
      --exclude='*.egg-info*' . 2>/dev/null || true
  )"
fi

if [ -n "$files" ]; then
  printf '%s\n' "$files" | while IFS= read -r f; do
    perl -pi -e "s|\\bOWNER\\b|$owner|g" "$f"
    echo "  已替换  $f"
  done
else
  echo "  没有找到 OWNER 占位符（可能已经替换过了）"
fi

# LICENSE 里是中文占位符，单独处理
if grep -q '你的名字或 GitHub 用户名' LICENSE 2>/dev/null; then
  perl -pi -e "s|<你的名字或 GitHub 用户名>|${author}|g; s|你的名字或 GitHub 用户名|${author}|g" LICENSE
  echo "  已替换  LICENSE（署名：${author}）"
fi

echo
echo "完成。建议接下来："
echo "  make check        # 确认全绿"
echo "  git diff          # 看一眼替换结果"
echo
echo "还有两件事只能你手动做："
echo "  1. 在仓库 Settings → General 里勾上 Wikis（wiki 内容见 wiki/ 目录）"
echo "  2. Settings → Pages 把 Source 选成 \"GitHub Actions\""
