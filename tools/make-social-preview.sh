#!/usr/bin/env bash
# 把 tools/social-preview.html 渲染成 docs/assets/social-preview.png（1280×640）。
#
# 这张图会出现在 GitHub 仓库卡片、Slack / Twitter 的链接预览里 ——
# 也就是"别人第一次看到这个项目"的那一眼。
#
# 需要 playwright-cli。装法（任选其一）：
#   npm install -g @playwright/cli@latest
#   或直接用 Codex 的 playwright skill 里的包装脚本
set -euo pipefail

cd "$(dirname "$0")/.."

SRC="tools/social-preview.html"
OUT="docs/assets/social-preview.png"
BIND="127.0.0.1"
PORT="${PREVIEW_PORT:-8791}"

find_pwcli() {
  if command -v playwright-cli >/dev/null 2>&1; then
    command -v playwright-cli
    return
  fi
  # Codex 的 playwright skill 包装脚本
  local c
  for c in "$HOME/.codex/skills/playwright/scripts/playwright_cli.sh" \
           "${CODEX_HOME:-$HOME/.codex}/skills/playwright/scripts/playwright_cli.sh"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  # npx 缓存里已经装好 @playwright/cli 时直接用，免得每次都要联网
  local hit
  hit="$(find "$HOME/.npm/_npx" -maxdepth 4 -name playwright-cli -type f 2>/dev/null | head -1)"
  [ -n "$hit" ] && { echo "$hit"; return; }
  return 1
}

if ! PWCLI="$(find_pwcli)"; then
  echo "找不到 playwright-cli。装上再来：" >&2
  echo "  npm install -g @playwright/cli@latest" >&2
  exit 2
fi

echo "使用 $PWCLI"

# file: 协议在 playwright 里默认被禁，所以起一个本地只读服务。
python3 -m http.server "$PORT" --bind "$BIND" >/dev/null 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
sleep 1

"$PWCLI" open "http://$BIND:$PORT/$SRC" >/dev/null
# 视口必须和设计稿一致，否则会被缩放，字就糊了
"$PWCLI" resize 1280 640 >/dev/null
"$PWCLI" screenshot --filename "$PWD/$OUT" >/dev/null

echo
echo "✓ 已生成 $OUT"
python3 - "$OUT" <<'PY'
import struct, sys
with open(sys.argv[1], "rb") as fh:
    head = fh.read(24)
w, h = struct.unpack(">II", head[16:24])
print(f"  {w}×{h} —— GitHub 社交预览图的标准尺寸是 1280×640")
if (w, h) != (1280, 640):
    sys.exit("尺寸不对：改 tools/social-preview.html 的 html/body 宽高")
PY
