"""
把 promo/demo 的 10 步演示合成为 mp4。

设计取舍：
- 不用 Edge 逐帧截图：headless 下 getBoundingClientRect / 动画帧时序不可靠，
  而且 10 步 x 每秒 30 帧 = 上千次进程启动，慢且容易错位。
- 改为在 Python 里重绘同一套版式：图片 + 聚光灯环（百分比坐标，与 demo 一致）
  + 右侧文案面板 + 进度条。这样每一帧都是确定的，聚光灯位置绝不会飘。
"""
from __future__ import annotations

import io
import json
import math
import os
import sys

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

BUILD = os.path.join(os.environ["TEMP"], "mp4build")
STEPS = os.path.join(BUILD, "steps.json")
# 本脚本位于 promo/assets/_tools/，仓库根往上四层
ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
OUT = os.path.join(ROOT, "promo", "demo", "使用演示.mp4")

# ---------- 版式常量 ----------
W, H = 1600, 900            # 画布
MARGIN = 32
LEFT_W = 960                # 舞台宽
PANEL_X = LEFT_W + MARGIN * 2
PANEL_W = W - PANEL_X - MARGIN
STAGE_PAD = 26

# 配色（与 demo 一致）
C_BLUE = (47, 107, 255)
C_RED = (229, 53, 43)
C_BG_TOP = (251, 252, 254)
C_BG_BOT = (243, 245, 249)
C_CARD = (255, 255, 255)
C_LINE = (229, 231, 235)
C_TEXT = (31, 35, 40)
C_SUB = (107, 114, 128)

FPS = 30
SEC_PER_STEP = 5.4          # 与 demo 的 DUR 一致
TAIL_SEC = 1.6              # 最后一步多留一点

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]
FONT_REG = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]


def font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def vgrad(w: int, h: int, top: tuple[int, int, int], bot: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line(
            [(0, y), (w, y)],
            fill=(
                int(top[0] + (bot[0] - top[0]) * t),
                int(top[1] + (bot[1] - top[1]) * t),
                int(top[2] + (bot[2] - top[2]) * t),
            ),
        )
    return img


def rounded(draw: ImageDraw.ImageDraw, box, r: int, fill=None, outline=None, width=1) -> None:
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def wrap_text(text: str, fnt, max_w: int) -> list[str]:
    """按像素宽度折行。中文按字符断；连续的 ASCII 单词整体不拆开
    （否则 `recordings\\` 会被切成 recording / s\\ 两行，很难看）。"""
    lines: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            lines.append(cur)
            cur = ""

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            flush()
            i += 1
            continue

        # 连续 ASCII 字母数字（含 \ . _ : -）视作一个不可拆的词。
        # 注意不把 / 算进去：正文里常用「A / B / C」列举，把 / 粘进词里
        # 会让三行日志连成一长串，很难读。
        if ch.isascii() and (ch.isalnum() or ch in "\\._:-"):
            j = i
            while j < n and text[j].isascii() and (text[j].isalnum() or text[j] in "\\._:-"):
                j += 1
            word = text[i:j]
            if fnt.getlength(cur + word) <= max_w:
                cur += word
            else:
                flush()
                if fnt.getlength(word) <= max_w:
                    cur = word
                else:
                    # 单词本身超宽，退化成逐字符
                    for c in word:
                        if fnt.getlength(cur + c) <= max_w:
                            cur += c
                        else:
                            flush()
                            cur = c
            i = j
            continue

        if fnt.getlength(cur + ch) <= max_w:
            cur += ch
        else:
            flush()
            cur = ch
        i += 1

    flush()
    return lines


def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def load_steps() -> list[dict]:
    return json.loads(io.open(STEPS, encoding="utf-8").read())


def compose_frame(
    step: dict,
    img: Image.Image,
    t_in_step: float,
    global_t: float,
    total_sec: float,
    idx: int,
    n: int,
) -> Image.Image:
    canvas = vgrad(W, H, C_BG_TOP, C_BG_BOT)
    d = ImageDraw.Draw(canvas, "RGBA")

    # ---------- 头部 ----------
    f_title = font(FONT_CANDIDATES, 30)
    f_sub = font(FONT_REG, 17)
    d.text((MARGIN, 26), "录音助手 · 使用演示", font=f_title, fill=C_TEXT)
    d.text((MARGIN, 64), "点一下开始，点一下停止，录音自动存成 mp3 · 默认不联网", font=f_sub, fill=C_SUB)

    badge = f"第 {idx + 1} / {n} 步"
    fb = font(FONT_REG, 17)
    bw = fb.getlength(badge) + 32
    rounded(d, [W - MARGIN - bw, 30, W - MARGIN, 68], 19, fill=C_CARD, outline=C_LINE)
    d.text((W - MARGIN - bw + 16, 39), badge, font=fb, fill=C_SUB)

    top = 100
    stage_h = H - top - 120

    # ---------- 舞台 ----------
    rounded(d, [MARGIN, top, MARGIN + LEFT_W, top + stage_h], 16, fill=C_CARD, outline=C_LINE)
    inner_w = LEFT_W - STAGE_PAD * 2
    inner_h = stage_h - STAGE_PAD * 2

    iw, ih = img.size
    scale = min(inner_w / iw, inner_h / ih)
    # 进场缩放（只对整图轻微推近，不做位移，避免聚光灯错位）
    grow = 1.0 + 0.012 * (1 - ease_out(min(1.0, t_in_step / 0.6)))
    scale *= grow
    dw, dh = int(iw * scale), int(ih * scale)
    px = MARGIN + STAGE_PAD + (inner_w - dw) // 2
    py = top + STAGE_PAD + (inner_h - dh) // 2

    scaled = img.resize((dw, dh), Image.LANCZOS)
    canvas.paste(scaled, (px, py))

    # ---------- 聚光灯环 ----------
    # 环的坐标用百分比，相对的就是图片框 —— 与 demo 完全一致，
    # 所以不需要任何测量，缩放后也自动贴合。
    for ring in step["rings"]:
        rx = px + int(dw * ring["left"] / 100)
        ry = py + int(dh * ring["top"] / 100)
        rw = int(dw * ring["width"] / 100)
        rh = int(dh * ring["height"] / 100)
        col = C_RED if ring["red"] else C_BLUE

        # 脉冲：亮度呼吸
        pulse = 0.5 + 0.5 * math.sin(global_t * 3.2)
        alpha_glow = int(46 + 40 * pulse)
        alpha_fill = int(14 + 12 * pulse)

        glow = Image.new("RGBA", (rw + 40, rh + 40), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        rounded(gd, [6, 6, rw + 34, rh + 34], 12, outline=(*col, alpha_glow), width=3)
        canvas.paste(glow, (rx - 20, ry - 20), glow)

        d.rounded_rectangle(
            [rx, ry, rx + rw, ry + rh],
            radius=10,
            fill=(*col, alpha_fill),
            outline=(*col, 235),
            width=3,
        )

    # ---------- 右侧文案面板 ----------
    rounded(d, [PANEL_X, top, PANEL_X + PANEL_W, top + stage_h], 16, fill=C_CARD, outline=C_LINE)
    cx = PANEL_X + 28
    cy = top + 26
    cw = PANEL_W - 56

    # kicker 逐字出现
    kick = step.get("kicker", "")
    f_kick = font(FONT_REG, 16)
    kcol = C_RED if step.get("kicker_red") else C_BLUE
    n_show = int(len(kick) * min(1.0, t_in_step / 0.5))
    if n_show > 0:
        d.text((cx, cy), kick[:n_show], font=f_kick, fill=kcol)
    cy += 30

    # 标题
    f_h2 = font(FONT_CANDIDATES, 27)
    for ln in wrap_text(step.get("title", ""), f_h2, cw):
        d.text((cx, cy), ln, font=f_h2, fill=C_TEXT)
        cy += 40
    cy += 6

    # 正文（淡入 + 折行）
    body_alpha = int(255 * ease_out(min(1.0, max(0.0, (t_in_step - 0.25) / 0.7))))
    f_body = font(FONT_REG, 19)
    body = step.get("body", "")
    # 去掉 markdown 残留的 ** 与 ` 号，视频里不渲染富文本
    body = body.replace("**", "").replace("`", "")
    for ln in wrap_text(body, f_body, cw):
        d.text((cx, cy), ln, font=f_body, fill=(*C_TEXT, body_alpha))
        cy += 30

    # tip 卡片
    tip = step.get("tip", "").replace("**", "").replace("`", "")
    if tip:
        ty = top + stage_h - 130
        rounded(d, [cx - 8, ty, PANEL_X + PANEL_W - 20, ty + 104], 10, fill=(247, 248, 250), outline=C_LINE)
        d.ellipse([cx + 6, ty + 16, cx + 30, ty + 40], outline=C_SUB, width=2)
        d.text((cx + 14, ty + 18), "i", font=font(FONT_REG, 15), fill=C_SUB)
        f_tip = font(FONT_REG, 16)
        yy = ty + 14
        for ln in wrap_text(tip, f_tip, cw - 56):
            d.text((cx + 44, yy), ln, font=f_tip, fill=C_SUB)
            yy += 24

    # ---------- 底部进度 ----------
    bar_y = H - 52
    bar_w = W - MARGIN * 2
    rounded(d, [MARGIN, bar_y, MARGIN + bar_w, bar_y + 8], 4, fill=(226, 230, 236))
    prog = min(1.0, global_t / total_sec)
    fw = max(8, int(bar_w * prog))
    rounded(d, [MARGIN, bar_y, MARGIN + fw, bar_y + 8], 4, fill=C_BLUE)

    f_hint = font(FONT_REG, 15)
    hint = "双击启动 → 常驻托盘 → 点开始录音 → 点停止 → mp3 自动存好"
    d.text((MARGIN, H - 36), hint, font=f_hint, fill=C_SUB)

    return canvas


def main() -> int:
    steps = load_steps()
    n = len(steps)
    if n == 0:
        print("没有步骤数据", file=sys.stderr)
        return 1

    total_sec = SEC_PER_STEP * (n - 1) + SEC_PER_STEP + TAIL_SEC
    n_frames = int(total_sec * FPS)
    print(f"共 {n} 步，{total_sec:.1f}s，{n_frames} 帧 @ {FPS}fps -> {OUT}")

    imgs = [Image.open(s["png"]).convert("RGB") for s in steps]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    container = av.open(OUT, mode="w")
    stream = container.add_stream("libx264", rate=FPS)
    stream.width, stream.height = W, H
    stream.pix_fmt = "yuv420p"
    # g=60 每 2 秒一个关键帧：播放器拖进度条才跟手。
    # 默认 gop 太大（整段只有 1 个关键帧），seek 会卡。
    stream.options = {"crf": "20", "preset": "medium", "g": "60", "keyint_min": "60"}

    for fi in range(n_frames):
        gt = fi / FPS
        si = min(n - 1, int(gt // SEC_PER_STEP))
        t_in = gt - si * SEC_PER_STEP
        frame = compose_frame(steps[si], imgs[si], t_in, gt, total_sec, si, n)
        arr = np.asarray(frame)
        vf = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in stream.encode(vf):
            container.mux(pkt)
        if fi % 60 == 0:
            print(f"  {fi}/{n_frames} ({si + 1}/{n} 步)")

    for pkt in stream.encode():
        container.mux(pkt)
    container.close()

    size = os.path.getsize(OUT)
    print(f"完成：{OUT}  {size / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
