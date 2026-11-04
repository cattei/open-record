"""
生成图标资源（应用图标 + 托盘图标）
====================================
需要带 Pillow 的 Python（工作区 Python 即可）：

    python packaging/make_icons.py

产物写入 app/assets/：
    icon_16..256.png      待命态（蓝）
    icon_rec_16..256.png  录音态（红）
    icon.ico              多尺寸 Windows 图标（PNG 内嵌）
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

PROJECT = Path(__file__).resolve().parent.parent
ASSETS = PROJECT / "app" / "assets"

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

# 品牌色：待命=蓝，录音=红。两者都用白麦克风，状态靠颜色区分，
# 16px 下也能一眼分辨。
IDLE = ((96, 140, 255), (36, 82, 222))
REC = ((255, 118, 118), (208, 38, 38))


def _vertical_gradient(size: int, top, bottom) -> Image.Image:
    g = Image.new("RGB", (1, size))
    px = g.load()
    for y in range(size):
        t = y / (size - 1)
        t = t * t * (3 - 2 * t)          # smoothstep，线性渐变会有硬边
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return g.resize((size, size), Image.BILINEAR)


def _squircle_mask(size: int, ratio: float = 0.235) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * ratio), fill=255)
    return m


def _round_line(d: ImageDraw.ImageDraw, p1, p2, width: float, fill):
    w = max(1, int(round(width)))
    d.line([p1, p2], fill=fill, width=w)
    r = width / 2
    for x, y in (p1, p2):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _mic(d: ImageDraw.ImageDraw, S: int, bold: float):
    """白色麦克风字形。bold 给小尺寸加粗，否则 16px 下笔画会消失。"""
    W = (255, 255, 255, 255)
    cx = 0.5 * S
    cap_w = (0.150 + 0.022 * (bold - 1.0)) * S
    d.rounded_rectangle(
        [cx - cap_w / 2, 0.220 * S, cx + cap_w / 2, 0.556 * S],
        radius=cap_w / 2, fill=W)
    aw = 0.044 * bold * S
    rx, ry, cy = 0.212 * S, 0.198 * S, 0.498 * S
    d.arc([cx - rx, cy - ry, cx + rx, cy + ry], start=0, end=180,
          fill=W, width=max(1, int(round(aw))))
    _round_line(d, (cx, cy + ry - aw / 2), (cx, 0.770 * S), aw, W)
    _round_line(d, (cx - 0.125 * S, 0.802 * S), (cx + 0.125 * S, 0.802 * S), aw, W)


def render(size: int, colors) -> Image.Image:
    k = 4                                   # 4 倍超采样后再缩，边缘干净
    S = size * k
    mask = _squircle_mask(S)

    base = _vertical_gradient(S, *colors).convert("RGBA")
    base.putalpha(mask)

    # 顶部一层极淡白光，做出一点体积感（不是纯平色块）
    hl = Image.new("L", (1, S), 0)
    px = hl.load()
    for y in range(S):
        t = max(0.0, 1.0 - y / (S * 0.52))
        px[0, y] = int(40 * t * t)
    hl = ImageChops.multiply(hl.resize((S, S), Image.BILINEAR), mask)
    overlay = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    overlay.putalpha(hl)
    base = Image.alpha_composite(base, overlay)

    bold = 1.9 if size <= 20 else (1.5 if size <= 32 else 1.0)
    _mic(ImageDraw.Draw(base), S, bold)

    return base.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, png_bytes: dict[int, bytes]) -> None:
    """手写 ICO，直接内嵌各尺寸 PNG —— 避免 Pillow 从单图重采样，保住小尺寸的加粗字形。"""
    n = len(png_bytes)
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    body = b""
    offset = 6 + 16 * n
    for s in sorted(png_bytes):
        blob = png_bytes[s]
        b = s if s < 256 else 0
        entries += struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        body += blob
    path.write_bytes(header + entries + body)


def preview(pngs: dict[str, dict[int, Image.Image]], out: Path) -> None:
    """放大对照图，便于肉眼检查小尺寸是否糊成一团。"""
    names = ["icon", "icon_rec"]
    pad, zoom = 16, 3
    cell = 256 * zoom
    w = pad + (cell + pad) * len(SIZES)
    h = pad + (cell + pad) * len(names)
    sheet = Image.new("RGBA", (w, h), (32, 34, 38, 255))
    for r, name in enumerate(names):
        for c, s in enumerate(SIZES):
            im = pngs[name][s].resize((s * zoom, s * zoom), Image.NEAREST)
            x = pad + c * (cell + pad) + (cell - im.width) // 2
            y = pad + r * (cell + pad) + (cell - im.height) // 2
            sheet.alpha_composite(im, (x, y))
    sheet.save(out)


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    sets = {"icon": IDLE, "icon_rec": REC}
    imgs: dict[str, dict[int, Image.Image]] = {}

    for name, colors in sets.items():
        imgs[name] = {}
        for s in SIZES:
            im = render(s, colors)
            imgs[name][s] = im
            im.save(ASSETS / f"{name}_{s}.png")

    ico_bytes = {}
    for s in SIZES:
        buf = io.BytesIO()
        imgs["icon"][s].save(buf, format="PNG")
        ico_bytes[s] = buf.getvalue()
    write_ico(ASSETS / "icon.ico", ico_bytes)

    out = PROJECT / "dist" / "icon-preview.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    preview(imgs, out)

    total = sum(f.stat().st_size for f in ASSETS.glob("*.png")) + (ASSETS / "icon.ico").stat().st_size
    print(f"OK  {len(list(ASSETS.iterdir()))} 个文件  {total/1024:.1f} KB  -> {ASSETS}")
    print(f"预览: {out}")


if __name__ == "__main__":
    main()
