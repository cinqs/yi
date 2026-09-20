#!/usr/bin/env bash
# 把主仓库 wiki/ 目录里的内容同步到 GitHub Wiki（独立仓库 <repo>.wiki.git）。
#
# 默认是**演练**：只 clone、覆盖、看 diff，不推送。
# 确认无误后加 --push 才真的推上去。
set -euo pipefail

cd "$(dirname "$0")/.."

push=0
[ "${1:-}" = "--push" ] && push=1
[ -n "${1:-}" ] && [ "${1:-}" != "--push" ] && {
  echo "用法: ./tools/sync-wiki.sh [--push]" >&2; exit 2;
}

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "当前目录不是 git 仓库。先 git init 并加一个 origin remote。" >&2
  exit 2
fi

origin="$(git remote get-url origin 2>/dev/null || true)"
if [ -z "$origin" ]; then
  echo "没有 origin remote。先 git remote add origin git@github.com:<你>/yi.git" >&2
  exit 2
fi

# 从 HTTPS 或 SSH 形式的 origin 推出 wiki 仓库地址。
#   https://github.com/cinqs/yi.git  → https://github.com/cinqs/yi.wiki.git
#   git@github.com:cinqs/yi.git      → git@github.com:cinqs/yi.wiki.git
case "$origin" in
  *.wiki.git) wiki_url="$origin" ;;
  *.git)      wiki_url="${origin%.git}.wiki.git" ;;
  *)          wiki_url="$origin.wiki.git" ;;
esac

echo "源目录  : wiki/"
echo "目标仓库: $wiki_url"
echo

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

head_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# wiki 仓库在"一页都没有"时是空的（clone 下来是个没有分支的空仓库），
# 这时 clone 会打印警告但退出码是 0 —— 所以不能用 clone 成功与否判断。
if ! git clone --quiet "$wiki_url" "$work/wiki" 2>/dev/null; then
  echo "✗ 克隆 wiki 仓库失败。常见原因：" >&2
  echo "  1. 仓库还没开 Wiki（Settings → General → Features → Wikis）" >&2
  echo "  2. wiki 从来没被创建过（先去网页上随便建一页，或直接 --push 试试）" >&2
  echo "  3. 没有推送权限" >&2
  exit 1
fi

# 先清掉旧页面，否则"删掉一个 wiki 页"这个动作永远同步不过去。
find "$work/wiki" -maxdepth 1 -name '*.md' -delete
# README.md 是"怎么维护这个目录"的说明，不是 wiki 页面，别发上去。
for f in wiki/*.md; do
  [ "$(basename "$f")" = "README.md" ] && continue
  cp "$f" "$work/wiki/"
done

cd "$work/wiki"
if [ -z "$(git status --porcelain)" ]; then
  echo "✓ 已经和远端一致，没有要同步的内容。"
  exit 0
fi

echo "将要同步的改动："
git add -A
git status --short | sed 's/^/  /'
echo

if [ "$push" -eq 0 ]; then
  echo "(演练模式，没有推送。加 --push 才会真的推上去)"
  exit 0
fi

git -c user.name="$(git config user.name || echo yi)" \
    -c user.email="$(git config user.email || echo yi@localhost)" \
    commit --quiet -m "docs: sync wiki from main@$head_sha"
git push --quiet origin HEAD
echo "✓ 已同步到 GitHub Wiki。"
