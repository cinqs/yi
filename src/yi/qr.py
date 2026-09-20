"""Best-effort QR rendering.

Android import is the one step where typing hurts, so we try, in order:
the ``qrcode`` package, the ``qrencode`` binary, and finally just print the link.
No third-party dependency is required for the tool to work.
"""

from __future__ import annotations

import shutil
import subprocess


def render(text: str) -> list[str] | None:
    """Return QR lines to print, or None when no renderer is available."""
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        return _to_half_blocks(matrix)
    except Exception:
        pass

    if shutil.which("qrencode"):
        try:
            completed = subprocess.run(
                ["qrencode", "-t", "ANSIUTF8", "-o", "-"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=10,
            )
            if completed.returncode == 0:
                return completed.stdout.decode("utf-8", "replace").splitlines()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def _to_half_blocks(matrix: list[list[bool]]) -> list[str]:
    lines: list[str] = []
    for row in range(0, len(matrix), 2):
        top = matrix[row]
        bottom = matrix[row + 1] if row + 1 < len(matrix) else [False] * len(top)
        chars = []
        for col, top_dark in enumerate(top):
            bottom_dark = bottom[col]
            if top_dark and bottom_dark:
                chars.append("\u2588")
            elif top_dark and not bottom_dark:
                chars.append("\u2580")
            elif not top_dark and bottom_dark:
                chars.append("\u2584")
            else:
                chars.append(" ")
        lines.append("".join(chars))
    return lines


def print_qr(text: str) -> bool:
    lines = render(text)
    if not lines:
        print("（本机没有二维码渲染器：pip install 'yi[qr]' 或 brew install qrencode）")
        return False
    for line in lines:
        print(line)
    return True
