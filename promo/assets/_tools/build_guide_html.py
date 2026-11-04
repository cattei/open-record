"""
把使用指南 Markdown 渲染成自包含单文件 HTML
============================================
为什么自己写而不装 markdown 库：这个工具只服务本项目的文档，用到的语法很少
（标题/段落/表格/列表/代码块/图片/引用/加粗/行内代码/链接）。为了这点语法
在发行包里塞一个第三方依赖不值得，也会让「解压即用」多一个不确定因素。

图片一律转成 base64 内嵌：这样生成的是**单文件**，可以直接发给别人、双击打开，
不会出现「图片裂开」的情况。

用法：
    runtime\\python\\python.exe promo\\assets\\_tools\\build_guide_html.py
"""
from __future__ import annotations

import base64
import html
import re
import sys
from pathlib import Path

PROMO = Path(__file__).resolve().parent.parent.parent      # promo/
GUIDE_DIR = PROMO / "guide"
SRC = GUIDE_DIR / "使用指南.md"
OUT = GUIDE_DIR / "使用指南.html"

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}


def embed_images(md: str, base: Path) -> str:
    """把 ![](assets/x.png) 换成 base64 data URI，避免产物依赖外部文件。"""

    def repl(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        p = base / src
        if not p.exists():
            print(f"  [警告] 找不到图片：{src}", file=sys.stderr)
            return m.group(0)
        mime = MIME.get(p.suffix.lower(), "application/octet-stream")
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        print(f"  内嵌 {src} ({len(data)//1024}KB base64)")
        return f"![{alt}](data:{mime};base64,{data})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, md)


def inline(text: str) -> str:
    """行内标记：`code`、**bold**、[link](url)、![img](url)。

    顺序很关键：**图片必须在链接之前、且在 html.escape 之前**处理。
    - 图片语法 `![alt](url)` 比链接多一个前导 `!`，若只按链接规则替换，
      那个 `!` 会留在原地，页面上就会出现一个孤零零的感叹号。
    - 图片的 url 往往是几百 KB 的 base64，里面可能有 `+` `/` `=`，
      提前抽成占位符就不会被后续的正则误伤。
    """
    # 行内代码先抽出来占位，避免里面的 * 被当成加粗
    codes: list[str] = []
    imgs: list[tuple[str, str]] = []

    def stash_code(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes)-1}\x00"

    def stash_img(m: re.Match) -> str:
        imgs.append((m.group(1), m.group(2)))
        return f"\x01{len(imgs)-1}\x01"

    text = re.sub(r"`([^`]+)`", stash_code, text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", stash_img, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        return (f'<a href="{html.escape(url, quote=True)}" target="_blank" '
                f'rel="noopener">{label}</a>')

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, text)

    # 还原图片（src 里的 & 要转义，base64 不含 & 所以不会有副作用）
    for i, (alt, url) in enumerate(imgs):
        text = text.replace(
            f"\x01{i}\x01",
            f'<img alt="{html.escape(alt)}" src="{html.escape(url, quote=True)}">',
        )
    for i, c in enumerate(codes):
        text = text.replace(f"\x00{i}\x00", f"<code>{html.escape(c)}</code>")
    return text


def render(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # 代码块
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(buf))}</code></pre>")
            continue

        # 分隔线
        if re.match(r"^\s*---+\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inline(m.group(2).strip())}</h{lv}>")
            i += 1
            continue

        # 表格：当前行含 | 且下一行是分隔行
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>")
            out.append("".join(f"<th>{inline(c)}</th>" for c in header))
            out.append("</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue

        # 引用块
        if line.strip().startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            # 引用块内部可能还有列表/段落，递归渲染
            out.append(f"<blockquote>{render(chr(10).join(buf))}</blockquote>")
            continue

        # 无序列表
        if re.match(r"^\s*[-*]\s+", line):
            out.append("<ul>")
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                item = re.sub(r"^\s*[-*]\s+", "", lines[i])
                out.append(f"<li>{inline(item)}</li>")
                i += 1
            out.append("</ul>")
            continue

        # 有序列表
        if re.match(r"^\s*\d+\.\s+", line):
            out.append("<ol>")
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                item = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                out.append(f"<li>{inline(item)}</li>")
                i += 1
            out.append("</ol>")
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 段落：合并到下一个空行
        buf = []
        while i < n and lines[i].strip() and not re.match(
                r"^(#{1,6}\s|\s*[-*]\s|\s*\d+\.\s|>|```|---)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            out.append(f"<p>{inline(' '.join(buf))}</p>")
        else:
            i += 1

    return "\n".join(out)


CSS = """
:root{--blue:#2f6bff;--text:#1b1f26;--sub:#6b7280;--line:#e5e7eb;--card:#f7f8fa;--red:#e5352b;--green:#1a9c62}
*{box-sizing:border-box}
body{margin:0;padding:0;background:#eef1f5;
     font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",system-ui,sans-serif;
     color:var(--text);line-height:1.75;font-size:15px}
.page{max-width:900px;margin:0 auto;background:#fff;min-height:100vh;
      padding:56px 64px 96px;box-shadow:0 0 40px rgba(0,0,0,.06)}
h1{font-size:30px;margin:0 0 8px;letter-spacing:-.5px}
h2{font-size:22px;margin:52px 0 16px;padding-top:20px;border-top:1px solid var(--line)}
h3{font-size:17px;margin:32px 0 12px;color:var(--text)}
h1+h2{border-top:none;margin-top:32px}
/* 源文档里 h2 前面常有一行 ---，此时不要再叠 h2 自身的上边框，否则出现双线 */
hr+h2{border-top:none;padding-top:0;margin-top:28px}
p{margin:12px 0;color:#333a45}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
hr{border:none;border-top:1px solid var(--line);margin:36px 0}
code{background:var(--card);border:1px solid var(--line);border-radius:5px;
     padding:1px 6px;font-family:"Cascadia Code",Consolas,monospace;font-size:13px;color:#c7254e}
pre{background:#1b1f26;border-radius:10px;padding:16px 18px;overflow-x:auto;margin:16px 0}
pre code{background:none;border:none;color:#e6e9ef;font-size:13px;line-height:1.6;padding:0}
blockquote{margin:18px 0;padding:14px 18px;background:#fff9e6;border-left:3px solid #f0b400;
           border-radius:0 8px 8px 0;color:#5a4a1f}
blockquote p{margin:6px 0;color:#5a4a1f}
blockquote code{background:#fff3d0;border-color:#f0d590;color:#8a6d00}
table{border-collapse:collapse;width:100%;margin:18px 0;font-size:14px;
      border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{background:var(--card);text-align:left;font-weight:600;padding:10px 14px;border-bottom:1px solid var(--line)}
td{padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
img{max-width:100%;height:auto;display:block;margin:20px auto;border:1px solid var(--line);
    border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.07)}
ul,ol{margin:12px 0;padding-left:26px}
li{margin:6px 0;color:#333a45}
strong{color:var(--text)}
.foot{margin-top:64px;padding-top:20px;border-top:1px solid var(--line);
      color:var(--sub);font-size:13px;text-align:center}
@media print{body{background:#fff}.page{box-shadow:none;padding:0;max-width:none}
             h2{page-break-after:avoid}img,table,pre{page-break-inside:avoid}}
@media (max-width:720px){.page{padding:28px 20px}h1{font-size:24px}h2{font-size:19px}}
"""


def main() -> int:
    if not SRC.exists():
        print(f"找不到源文件：{SRC}", file=sys.stderr)
        return 1

    print(f"读取 {SRC.name}")
    md = SRC.read_text(encoding="utf-8")
    print("内嵌图片：")
    md = embed_images(md, GUIDE_DIR)
    body = render(md)

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>录音助手 使用指南</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
{body}
<div class="foot">录音助手 使用指南 · 本文件自包含，可离线打开或直接转发</div>
</div>
</body>
</html>
"""
    OUT.write_text(doc, encoding="utf-8")
    kb = OUT.stat().st_size // 1024
    print(f"\n已生成 {OUT}  ({kb}KB, 单文件自包含)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
