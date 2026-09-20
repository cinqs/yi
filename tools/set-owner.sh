#!/usr/bin/env bash
# 把仓库里写着的 GitHub 用户名换成你自己的。
#
# 两种用它的时机：
#   1. 从模板起步：仓库里是 `OWNER` 占位符
#   2. fork 了别人的仓库（比如 yi）：仓库里写的是原作者的 `cinqs`，
#      不换掉的话 README 徽章会指向别人的仓库
#
# 用户名出现在 README 徽章、文档链接、issue 模板、CODEOWNERS、CHANGELOG 的
# compare 链接、pyproject 的 project.urls 里。手改容易漏 —— 漏一处就是一个
# 404 的徽章，或者一条指向别人仓库的链接。
#
#   ./tools/set-owner.sh your-github-name
#   ./tools/set-owner.sh your-github-name "你的署名"        # 顺带写进 LICENSE
#   ./tools/set-owner.sh your-name "" some-old-owner        # 显式指定旧名字
#
# 跑完记得 `git diff` 看一眼。
set -euo pipefail

cd "$(dirname "$0")/.."

owner="${1:-}"
author="${2:-$owner}"
from_name="${3:-OWNER}"

if [ -z "$1" ]; then
  cat >&2 <<'USAGE'
用法: ./tools/set-owner.sh <github用户名或组织名> [LICENSE 里的署名] [旧名字]

  旧名字默认是 OWNER（模板形态）。如果是从别人仓库 fork 来的，
  它会自动从 pyproject.toml 的 Homepage 里认出当前的用户名。
USAGE
  echo >&2
  echo "当前还没替换的位置：" >&2
  grep -rn "$from_name" --exclude-dir=.git --exclude-dir=.venv --exclude-dir=vendor \
    --exclude-dir=dist --exclude-dir=build --exclude=set-owner.sh \
    . 2>/dev/null | sed 's/^/  /' >&2
  exit 2
fi

# 没写旧名字、仓库里也没有 OWNER 时，从 pyproject 的项目主页里推断
# —— fork 场景下一次就能跑对，不用用户自己翻出原作者的用户名。
if [ "$from_name" = "OWNER" ] && ! grep -rq 'OWNER' \
     --exclude-dir=.git --exclude-dir=.venv --exclude-dir=vendor \
     --exclude-dir=dist --exclude-dir=build --exclude=set-owner.sh . 2>/dev/null; then
  inferred="$(sed -n 's|^Homepage = "https://github.com/\([^/]*\)/.*|\1|p' pyproject.toml 2>/dev/null | head -1)"
  if [ -n "$inferred" ]; then
    from_name="$inferred"
    echo "没有 OWNER 占位符；按 pyproject 里的主页推断旧名字是「${from_name}」"
  fi
fi

if [ "$from_name" = "$owner" ]; then
  echo "旧名字和新名字一样（都是 ${owner}），没什么可换的。"
  exit 0
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
  files="$(git ls-files -z | xargs -0 grep -lF "$from_name" 2>/dev/null \
    | grep -v '^tools/set-owner.sh$' || true)"
else
  files="$(
    grep -rlF "$from_name" \
      --exclude-dir=.git --exclude-dir=.venv --exclude-dir=vendor \
      --exclude-dir=dist --exclude-dir=build --exclude-dir=.ruff_cache \
      --exclude-dir=.playwright-cli --exclude=set-owner.sh \
      --exclude='*.egg-info*' . 2>/dev/null || true
  )"
fi

if [ -n "$files" ]; then
  printf '%s\n' "$files" | while IFS= read -r f; do
    FROM="$from_name" TO="$owner" perl -pi -e 's/\Q$ENV{FROM}\E/$ENV{TO}/g' "$f"
    echo "  已替换  $f"
  done
else
  echo "  没有找到「${from_name}」（可能已经替换过了）"
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
