#!/usr/bin/env python3
"""生成 yi 的 App 图标（纯代码，确定性，不依赖任何画图库）。

为什么不用 AI 生成：图标是**几何图形**，代码画出来的边缘更干净、可复现、能随时调参，
而且不用引入外部服务。渲染思路是"按距离场算覆盖度"——每个像素算出到图形边界的
距离，用 smoothstep 变成 alpha，一次遍历就拿到抗锯齿效果（不需要多倍超采样）。

图形构成：
  · 深色圆角方（macOS 的 squircle 味道）
  · 一枚祖母绿地球（外圈 + 两条经线 + 一条赤道）
  · 中心一点高光，避免小尺寸下糊成一团

用法：uv run python app/make-icon.py [输出目录]
"""

from __future__ import annotations

import math
import os
import sys

import png  # 来自 pypng，纯 Python

DEFAULT_SIZE = 1024

BG_TOP = (30, 41, 59)  # slate-800
BG_BOTTOM = (12, 17, 27)  # 更深的底
GLOBE = (52, 211, 153)  # emerald-400
GLOBE_DIM = (16, 185, 129)  # emerald-500


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge0 == edge1:
        return 0.0 if x < edge0 else 1.0
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def mix(a, b, t: float):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def squircle_coverage(px: float, py: float, size: int, inset: float, power: float) -> float:
    """圆角方的覆盖度。px/py 是 -1..1 的归一化坐标。"""
    limit = 1.0 - inset
    d = (abs(px) ** power + abs(py) ** power) ** (1.0 / power)
    # 边缘 1.5px 内做过渡
    edge = 1.5 / (size / 2.0)
    return 1.0 - smoothstep(limit - edge, limit + edge, d)


def ring_coverage(dist: float, radius: float, width: float) -> float:
    return 1.0 - smoothstep(width * 0.5, width * 0.5 + 0.0015, abs(dist - radius))


def render(size: int) -> list:
    rows = []
    half = size / 2.0
    globe_r = 0.60  # 归一化半径
    ring_w = 0.055
    for y in range(size):
        # 归一化到 -1..1（中心为 0）
        py = (y + 0.5 - half) / half
        row = []
        for x in range(size):
            px = (x + 0.5 - half) / half

            # 背景：圆角方 + 垂直渐变
            bg_alpha = squircle_coverage(px, py, size, inset=0.0, power=5.0)
            if bg_alpha <= 0.0:
                row.extend((0, 0, 0, 0))
                continue
            grad = (py + 1.0) / 2.0
            color = list(mix(BG_TOP, BG_BOTTOM, grad))

            # 地球：外圈
            dist = math.hypot(px, py)
            globe = ring_coverage(dist, globe_r, ring_w)

            # 经线：两条椭圆（横向压扁）
            for k in (0.42, 0.78):
                ell = math.hypot(px / k, py) if k else 1e9
                globe = max(globe, ring_coverage(ell, globe_r, ring_w * 0.5))

            # 赤道：一条水平线（限制在地球内）
            if dist <= globe_r:
                equator = 1.0 - smoothstep(ring_w * 0.32, ring_w * 0.32 + 0.0015, abs(py))
                globe = max(globe, equator * (1.0 - smoothstep(globe_r - ring_w, globe_r, dist)))

            # 用圆角方裁掉地球超出画布的部分
            globe *= bg_alpha
            if globe > 0.0:
                tint = mix(GLOBE_DIM, GLOBE, 1.0 - min(1.0, dist / max(globe_r, 1e-6)))
                color = [color[i] * (1.0 - globe) + tint[i] * globe for i in range(3)]

            # 左上一点柔光，避免整块死板
            glow = 0.10 * max(0.0, 1.0 - math.hypot(px + 0.45, py + 0.5) / 1.1) ** 2
            color = [min(255.0, c + 255.0 * glow) for c in color]

            alpha = int(round(bg_alpha * 255))
            row.extend((int(round(color[0])), int(round(color[1])), int(round(color[2])), alpha))
        rows.append(row)
    return rows


def main(argv) -> int:
    out_dir = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    size = int(argv[2]) if len(argv) > 2 else DEFAULT_SIZE
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"icon-{size}.png")
    with open(path, "wb") as handle:
        writer = png.Writer(width=size, height=size, greyscale=False, alpha=True, bitdepth=8)
        writer.write(handle, render(size))
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
