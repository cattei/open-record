"""从 promo/demo/index.html 提取每一步的图片、聚光灯坐标与文案，供视频合成用。"""
import base64
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEMO = os.path.join(ROOT, "promo", "demo", "index.html")
OUTDIR = os.path.join(os.environ["TEMP"], "mp4build")


def main() -> None:
    src = io.open(DEMO, encoding="utf-8").read()
    os.makedirs(OUTDIR, exist_ok=True)

    # 1) 提取 slide 块（含 base64 图 + ring 定位）
    slides = []
    for m in re.finditer(r'<div class="slide" data-i="(\d+)">(.*?)</div></div>', src, re.S):
        idx = int(m.group(1))
        inner = m.group(2)
        img = re.search(r'<img src="data:image/png;base64,([A-Za-z0-9+/=]+)"[^>]*alt="([^"]*)"', inner)
        style = re.search(r'<img[^>]*style="([^"]*)"', inner)
        rings = []
        for r in re.finditer(
            r'<div class="ring( red)?" style="left:([\d.]+)%;top:([\d.]+)%;'
            r'width:([\d.]+)%;height:([\d.]+)%"',
            inner,
        ):
            rings.append({
                "red": bool(r.group(1)),
                "left": float(r.group(2)),
                "top": float(r.group(3)),
                "width": float(r.group(4)),
                "height": float(r.group(5)),
            })
        if not img:
            print(f"[警告] 第 {idx} 步找不到图片", file=sys.stderr)
            continue
        png = os.path.join(OUTDIR, f"step{idx}.png")
        io.open(png, "wb").write(base64.b64decode(img.group(1)))
        slides.append({
            "idx": idx,
            "png": png,
            "alt": img.group(2),
            "imgstyle": style.group(1) if style else "",
            "rings": rings,
        })

    # 2) 提取 panel 文案
    panels = {}
    for m in re.finditer(r'<div class="panel-body[^"]*" data-i="(\d+)">(.*?)</div></div>', src, re.S):
        idx = int(m.group(1))
        inner = m.group(2)
        kicker = re.search(r'<div class="kicker[^"]*">(.*?)</div>', inner, re.S)
        h2 = re.search(r"<h2>(.*?)</h2>", inner, re.S)
        body = re.search(r'<div class="body">(.*?)</div>', inner, re.S)
        tip = re.search(r'<span class="i">i</span><span>(.*?)</span>', inner, re.S)

        def strip_tags(t: str | None) -> str:
            if not t:
                return ""
            t = re.sub(r"<[^>]+>", "", t)
            t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            return re.sub(r"\s+", " ", t).strip()

        panels[idx] = {
            "kicker": strip_tags(kicker.group(1) if kicker else ""),
            "kicker_red": "kicker red" in inner,
            "title": strip_tags(h2.group(1) if h2 else ""),
            "body": strip_tags(body.group(1) if body else ""),
            "tip": strip_tags(tip.group(1) if tip else ""),
        }

    steps = []
    for s in slides:
        p = panels.get(s["idx"], {})
        steps.append({**s, **p})
    steps.sort(key=lambda x: x["idx"])

    out = os.path.join(OUTDIR, "steps.json")
    io.open(out, "w", encoding="utf-8").write(json.dumps(steps, ensure_ascii=False, indent=1))

    print(f"提取 {len(steps)} 步 -> {OUTDIR}")
    for s in steps:
        print(f"  {s['idx']}: {s['title'][:34]:36s} rings={len(s['rings'])}")


if __name__ == "__main__":
    main()
