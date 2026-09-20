#!/bin/bash
#
# 把 app/ 打成一个可以双击的 macOS 应用：dist/yi.app
#
# 这个壳只做三件事：起 agent、开窗口、把 agent 的界面装进 WebView。
# 用 CommandLineTools 自带的 swiftc 就能编，不需要装 Xcode。
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/dist/Yi.app"
PYTHON="$ROOT/.venv/bin/python"
AGENT="$HERE/agent.py"

if [ ! -x "$PYTHON" ]; then
    echo "找不到 $PYTHON —— 先跑：uv venv --python 3.12 && uv pip install -e ." >&2
    exit 1
fi

echo "==> 生成 Swift 源（写入真实路径）"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
sed -e "s|__PYTHON__|$PYTHON|" -e "s|__AGENT__|$AGENT|" "$HERE/macos/main.swift" > "$BUILD_DIR/main.swift"

# 界面脚本先做语法检查：上次一个 "const sub 重复声明" 让整个脚本不执行、
# 界面永远停在"正在读取状态"，而单元测试完全看不见这类问题。
if command -v node >/dev/null 2>&1; then
    echo "==> 检查界面脚本语法"
    node -e "
      const fs = require('fs');
      const html = fs.readFileSync('$HERE/ui/index.html', 'utf8');
      const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
      blocks.forEach((m, i) => new Function(m[1]));
      console.log('   ' + blocks.length + ' 段脚本语法 OK');
    "
fi

echo "==> 准备图标"
ICON_PNG="$HERE/assets/icon-1024.png"
if [ ! -f "$ICON_PNG" ] || [ "$HERE/make-icon.py" -nt "$ICON_PNG" ]; then
    "$PYTHON" "$HERE/make-icon.py" "$HERE/assets" 1024 >/dev/null
fi
ICONSET="$BUILD_DIR/AppIcon.iconset"
mkdir -p "$ICONSET"
for spec in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" \
            "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" \
            "512 512x512" "1024 512x512@2x"; do
    px="${spec%% *}"; name="${spec##* }"
    sips -z "$px" "$px" "$ICON_PNG" --out "$ICONSET/icon_${name}.png" >/dev/null 2>&1
done
# iconutil 在某些受限环境（沙箱、最小化的 CI 镜像）里会报 "Invalid Iconset"。
# 图标缺失只影响 Finder/Dock 里的观感，不该让整个构建失败——
# 所以降级成警告：有现成的 .icns 就用，没有就不带图标继续。
if ! iconutil -c icns "$ICONSET" -o "$BUILD_DIR/AppIcon.icns" 2>/dev/null; then
    echo "   ⚠ iconutil 生成 .icns 失败，本次不带图标（界面里的图标不受影响）" >&2
    [ -f "$HERE/assets/AppIcon.icns" ] && cp "$HERE/assets/AppIcon.icns" "$BUILD_DIR/AppIcon.icns"
fi
# 界面里也用一张小图（agent 直接把它当 /icon.png 提供）
sips -z 256 256 "$ICON_PNG" --out "$HERE/assets/icon-256.png" >/dev/null 2>&1

echo "==> 编译"
swiftc -O -o "$BUILD_DIR/yi" "$BUILD_DIR/main.swift" \
    -framework Cocoa -framework WebKit

echo "==> 组装 .app"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp "$BUILD_DIR/yi" "$OUT/Contents/MacOS/yi"
[ -f "$BUILD_DIR/AppIcon.icns" ] && cp "$BUILD_DIR/AppIcon.icns" "$OUT/Contents/Resources/AppIcon.icns"

# 把 agent、界面、yi 包的源码一起装进去。
#
# 这一步是"这个 .app 能不能给别人用"的分水岭：
# 只写死构建机的绝对路径的话，App 换个目录就打不开，
# Release 里发的 zip 更是完全没用（路径指向 CI runner）。
# 带上这几百 KB，App 就能从任意位置启动。
echo "==> 打包运行时资源"
mkdir -p "$OUT/Contents/Resources/ui" "$OUT/Contents/Resources/src" "$OUT/Contents/Resources/assets"
cp "$HERE/agent.py" "$OUT/Contents/Resources/agent.py"
cp "$HERE/ui/index.html" "$OUT/Contents/Resources/ui/index.html"
cp -R "$ROOT/src/yi" "$OUT/Contents/Resources/src/yi"
cp "$HERE/assets/icon-256.png" "$OUT/Contents/Resources/assets/icon-256.png"
# 顺手清掉自带的 __pycache__：它是构建机的产物，换台机器可能不兼容
find "$OUT/Contents/Resources" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

cat > "$OUT/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Yi</string>
  <key>CFBundleDisplayName</key><string>驿</string>
  <key>CFBundleIdentifier</key><string>com.yi.app</string>
  <key>CFBundleExecutable</key><string>yi</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict>
</plist>
PLIST

echo "==> 临时签名（本地运行需要）"
# ad-hoc 签名：本地运行需要它；没有签名工具的环境跳过，只是第一次打开时
# Gatekeeper 会多问一句，不影响使用。
if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "$OUT" 2>&1 | tail -2 || \
        echo "   ⚠ 签名失败，不影响本机运行" >&2
else
    echo "   跳过（没有 codesign）"
fi

echo
echo "完成：$OUT"
echo "双击打开，或：open '$OUT'"
