"""
生成 promo/demo/index.html —— 单文件交互式使用演示
==================================================
把真实截图按「使用流程」串成一步一步的演示：聚光灯高亮当前该看的区域 +
说明文字 + 自动播放 / 手动翻页。图片全部 base64 内嵌，产物是**单文件**，
双击就能看，也能直接发给别人、嵌进网页，不依赖任何外部资源或网络。

为什么做成交互 HTML 而不是 mp4：
- 用户能自己控制节奏，停在某一屏细看（视频做不到，除非来回拖进度条）
- 单文件几百 KB，mp4 要几十 MB
- 不需要额外播放器，浏览器双击即开
- 截图本来就是静态的，做成「一步一步」比「逐帧动」更贴近真实使用

两个处理细节：
1. 托盘那两张是 11:1 的横条，直接放上去会被压成一条细线。这里裁出
   「^ 箭头 + 本实例图标 + 一点邻居」的区域再放大，比例才正常。
2. 各图比例不一（0.8:1 ~ 2.8:1）。舞台固定高度 + contain，聚光灯坐标在运行时
   按图片实际渲染矩形换算，这样切步时舞台不会跳。

用法：
    runtime\\python\\python.exe promo\\assets\\_tools\\build_demo.py
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

PROMO = Path(__file__).resolve().parent.parent.parent
ASSETS = PROMO / "guide" / "assets"
OUT = PROMO / "demo" / "index.html"

# 托盘条原图 1266x108（约 11:1）。整条缩到 600px 宽的话，32px 的图标只剩 15px，
# 高亮环根本看不清。所以裁成「^ 箭头 + 本图标 + 几个邻居」这一段（约 4:1），
# 铺满宽度时是轻微放大，图标够大，也还看得出它就在任务栏那一排里。
# 实测：本图标在原图 x=85-116, y=44-75。
TRAY_CROP = (10, 22, 330, 84)      # x, y, w, h
# 裁剪后归一化（实测图标 x=85-116, y=44-75）：
# left=(85-10)/330=0.2273  top=(44-22)/84=0.2619  w=32/330=0.0970  h=32/84=0.3810
# 四周各留一点余量，环才不会贴着图标边缘。
TRAY_HOT = (0.2180, 0.2480, 0.1160, 0.4100)

# 托盘菜单截图 234x292：右侧 223-233 列是露出的桌面边缘、底部 9px 是黑色边。
# 只保留菜单本体 x 0-223, y 4-283。
MENU_CROP = (0, 4, 223, 279)

STEPS = [
    dict(
        img="01-tray-icon.png", crop=TRAY_CROP,
        kicker="第 1 步 · 启动",
        title="双击启动，图标常驻右下角",
        body=(
            "双击 <b>启动录音助手.bat</b>，右下角通知区出现蓝色麦克风图标。"
            "首次启动会自动把它提到可见托盘区（Win11 默认会把新图标塞进 "
            "<b>^</b> 溢出区），所以一般不用你自己去 ^ 里翻。"
        ),
        # 整条托盘图（1266x108）里，本实例的蓝色麦克风图标实测 bbox：
        # x=85-116, y=44-75 → left 6.71% / top 40.74% / w 2.53% / h 29.63%
        # 四周留一点余量，环才不会贴着图标边缘。
        hot=[TRAY_HOT],
        tip="图标颜色就是状态：蓝色=待命，红色=录音中",
        wide=True,
    ),
    dict(
        img="02-main-window-idle.png",
        kicker="第 2 步 · 打开界面",
        title="单击图标，打开主窗口",
        body=(
            "鼠标<b>左键单击</b>托盘图标，窗口就出来了。"
            "状态卡片显示「待命 · 麦克风未占用」——待命时程序不占用麦克风，"
            "其它软件（会议、语音输入）随时能用。"
        ),
        hot=[(0.052, 0.163, 0.895, 0.130)],
        tip="关掉窗口只是收进托盘，程序继续在后台跑",
    ),
    dict(
        img="02-main-window-idle.png",
        kicker="第 3 步 · 开始录音",
        title="点「开始录音」",
        body=(
            "点蓝色大按钮，程序这时才真正打开麦克风开始采集。"
            "不想开窗口的话，也可以右键托盘图标直接选「开始录音」。"
        ),
        hot=[(0.052, 0.305, 0.895, 0.077)],
        tip="录音期间「保存并生效」会自动禁用，避免重启丢掉正在写的分片",
    ),
    dict(
        img="03-recording.png",
        kicker="第 4 步 · 录音中",
        title="计时在走，电平在跳",
        body=(
            "状态变成「录音中」+ 红点，右侧<b>计时器开始走</b>，"
            "下方显示「麦克风占用中　电平 N」——电平随说话跳动，"
            "这就是「真的录进去了」最直接的证据。按钮变成红框的「停止录音」。"
        ),
        hot=[(0.052, 0.163, 0.895, 0.130), (0.052, 0.305, 0.895, 0.077)],
        tip="不用开窗口也知道在录：托盘图标同步变红，悬停还能看到实时计时",
    ),
    dict(
        img="03-recording.png",
        kicker="第 5 步 · 停止",
        title="点「停止录音」",
        body=(
            "程序先停采集、<b>立刻释放麦克风</b>（其它软件马上就能用），"
            "再把分片合成一个 mp3，只对明显过轻的录音做增益补偿，最后删掉中间分片。"
        ),
        hot=[(0.052, 0.305, 0.895, 0.077)],
        tip="分片每 30 秒落盘一次，中途断电最多丢最近 30 秒",
    ),
    dict(
        img="08-after-saved.png",
        kicker="第 6 步 · 完成",
        title="mp3 已经存好了",
        body=(
            "运行日志给出三行结果：<code>已停止，录音 9 秒</code> / "
            "<code>完成：…-….mp3 0.2MB</code> / <code>已保存 …（0.2MB）</code>。"
            "文件落在 <code>recordings\\日期\\</code> 下，文件名就是起止时间。"
        ),
        hot=[(0.055, 0.833, 0.890, 0.133)],
        tip="想改保存位置：设置区「音频目录」→ 浏览… → 保存并生效",
    ),
    dict(
        img="04-tray-menu.png", crop=MENU_CROP,
        kicker="第 7 步 · 托盘菜单",
        title="右键图标，五个常用动作",
        body=(
            "显示界面 / 开始录音·停止录音 / 开机自启（可勾选）/ 打开录音目录 / 退出。"
            "<b>「退出」是真正退出的唯一入口</b>；正在录音时点它会先弹框确认，避免误丢录音。"
        ),
        # 菜单图本身就占满整张图，环贴着图片边缘即可（留 1% 余量）
        hot=[(0.010, 0.012, 0.980, 0.976)],
        tip="想再打开界面：再双击一次 bat 即可（单实例，不会启动第二份）",
        maxw=300,          # 原图 234 宽，放大到 300 显示更清楚，又不至于糊
    ),
    dict(
        img="05-settings-feishu.png",
        kicker="可选 · 飞书 1/3",
        title="填凭据，或点「一键创建飞书机器人」",
        body=(
            "不配这段也能正常用。要手机遥控录音，就在设置区填 App ID / App Secret，"
            "或者点右边的「一键创建飞书机器人」跟着向导走。"
        ),
        hot=[(0.725, 0.275, 0.220, 0.155)],
        tip="不填凭据时程序不加载飞书组件，其它功能完全不受影响",
    ),
    dict(
        img="07-feishu-wizard.png",
        kicker="可选 · 飞书 2/3",
        title="向导：把能自动的都自动了",
        body=(
            "飞书官方<b>没有</b>开放「用接口创建应用」的 API，所以它不是「点一下就建好」。"
            "向导负责：打开创建页、复制应用名、列出必须开通的两项权限、"
            "<b>真的调接口校验凭据</b>、通过后才写配置并启用。"
        ),
        # 实测按钮行在原图 1146x1120 的 x=72-470, y=715-766
        # → left 0.0628 / top 0.6384 / w 0.3473 / h 0.0455，上下留点余量
        hot=[(0.0580, 0.6320, 0.3560, 0.0560)],
        tip="权限就两项：im:message、im:message:send_as_bot",
    ),
    dict(
        img="07b-feishu-wizard-error.png",
        kicker="可选 · 飞书 3/3",
        title="凭据填错，当场告诉你原因",
        body=(
            "校验会真的连一次飞书服务器。上图是填错凭据时的真实返回："
            "<b>code=10003 invalid param</b>，并翻译成人话「多半是 App ID 与 App Secret "
            "不匹配，或复制时漏了字符」。不用等之后收不到消息才发现。"
        ),
        # 实测红字两行在原图 1146x1156 的 x=58-681, y=960-1030
        # → left 0.0506 / top 0.8304 / w 0.5445 / h 0.0614，四周留点余量
        hot=[(0.0440, 0.8230, 0.5620, 0.0760)],
        tip="断网、超时、应用被停用也各有各的提示，不会只甩一个错误码",
    ),
]

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --blue:#2f6bff;--blue-d:#2258e0;--red:#e5352b;--bg:#fff;--card:#f7f8fa;
  --line:#e5e7eb;--text:#1f2328;--sub:#6b7280;
}
html,body{height:100%}
body{
  font-family:"Microsoft YaHei UI","Microsoft YaHei",-apple-system,"Segoe UI",sans-serif;
  background:linear-gradient(180deg,#fbfcfe,#f3f5f9 100%);
  color:var(--text);padding:26px 20px 34px;
  display:flex;align-items:flex-start;justify-content:center;
}
.wrap{width:100%;max-width:1160px}

header{display:flex;align-items:center;gap:14px;margin-bottom:20px}
.logo{
  width:46px;height:46px;border-radius:13px;flex:none;
  background:linear-gradient(160deg,#5a8bff,#2f6bff);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 5px 16px rgba(47,107,255,.34);
}
.logo svg{width:26px;height:26px;display:block}
h1{font-size:20px;font-weight:600;letter-spacing:.2px}
.sub{font-size:12.5px;color:var(--sub);margin-top:4px}
.badge{
  margin-left:auto;font-size:11.5px;color:var(--sub);background:#fff;
  border:1px solid var(--line);padding:6px 12px;border-radius:999px;white-space:nowrap;
  box-shadow:0 1px 3px rgba(17,24,39,.04);
}

.main{
  display:grid;grid-template-columns:minmax(0,1.28fr) minmax(0,1fr);
  gap:20px;align-items:start;
}
@media (max-width:880px){
  .main{grid-template-columns:1fr}
  .stage{height:52vw!important}
  .panel{min-height:auto!important}
}

/* ---------- 舞台 ---------- */
.stage{
  position:relative;height:520px;
  border:1px solid var(--line);border-radius:16px;
  background:radial-gradient(120% 120% at 50% 0%,#ffffff,#eef1f6 100%);
  overflow:hidden;display:flex;align-items:center;justify-content:center;
  padding:22px;
  box-shadow:0 10px 30px rgba(17,24,39,.09);
  transition:height .35s cubic-bezier(.22,.9,.3,1);
}
/* 关键：图片和聚光灯放在同一个 frame 里，聚光灯用百分比定位，
   百分比相对的就是图片的框，不需要任何 JS 测量。
   （先前的版本靠 getBoundingClientRect 换算，回流没完成时坐标会错位。） */
/* 普通图：frame 收缩到图片实际宽度，百分比定位才贴着图。
   （若 frame 被撑到 100%，图片居中留白，环就会框到空白处。） */
.frame{position:relative;line-height:0;width:fit-content;max-width:100%}
.frame img{
  display:block;max-width:100%;max-height:620px;width:auto;height:auto;
  border-radius:6px;box-shadow:0 6px 22px rgba(17,24,39,.14);background:#fff;
}
/* 超宽扁图（托盘条）：铺满可用宽度、高度按比例走，整条任务栏才看得全。 */
.frame.wide{width:100%}
.frame.wide img{width:100%;max-height:none;height:auto}
/* .slide 是 flex 容器里的 item，不给宽度会收缩到图片原始宽度。 */
.slide{width:100%}
/* 聚光灯：只给高亮区加发光边框 + 脉冲，其余部分不变暗。
   之前试过用遮罩把四周压暗，但托盘那张图是 11:1 的扁条，
   一压暗连「它在任务栏哪一排」这个语境也没了。 */
.ring{
  position:absolute;border-radius:10px;pointer-events:none;
  border:2.5px solid rgba(47,107,255,.95);
  background:rgba(47,107,255,.06);
  box-shadow:0 0 0 3px rgba(47,107,255,.18),0 0 18px rgba(47,107,255,.42);
}
.ring.red{border-color:rgba(229,53,43,.95);background:rgba(229,53,43,.06);
  box-shadow:0 0 0 3px rgba(229,53,43,.18),0 0 18px rgba(229,53,43,.42)}
.ring::after{
  content:'';position:absolute;inset:-3px;border-radius:12px;
  animation:pulse 1.9s ease-in-out infinite;
}
.ring.red::after{animation:pulseRed 1.9s ease-in-out infinite}
@keyframes pulse{
  0%,100%{box-shadow:0 0 0 0 rgba(47,107,255,.5)}
  50%    {box-shadow:0 0 0 10px rgba(47,107,255,0)}
}
@keyframes pulseRed{
  0%,100%{box-shadow:0 0 0 0 rgba(229,53,43,.45)}
  50%    {box-shadow:0 0 0 10px rgba(229,53,43,0)}
}

/* ---------- 说明面板 ---------- */
.panel{
  border:1px solid var(--line);border-radius:16px;background:#fff;
  padding:22px 24px 20px;box-shadow:0 10px 30px rgba(17,24,39,.09);
  display:flex;
}
.panel-body{display:none;flex-direction:column;width:100%;animation:fadeIn .34s ease both}
.panel-body.on{display:flex}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.kicker{
  display:inline-flex;align-items:center;align-self:flex-start;
  font-size:11.5px;font-weight:600;letter-spacing:.5px;color:var(--blue);
  background:rgba(47,107,255,.09);border-radius:999px;padding:5px 12px;
}
.kicker.red{color:var(--red);background:rgba(229,53,43,.09)}
.panel h2{font-size:18.5px;font-weight:600;margin:14px 0 12px;line-height:1.42}
.panel .body{font-size:13.5px;line-height:1.9;color:#3f4753}
.panel .body b{color:var(--text);font-weight:600}
.panel .body code{
  font-family:"Cascadia Mono",Consolas,monospace;font-size:12.3px;
  background:var(--card);border:1px solid var(--line);padding:1px 6px;
  border-radius:5px;color:#0b7285;word-break:break-all;
}
.tip{
  margin-top:auto;padding-top:15px;border-top:1px dashed var(--line);
  font-size:12.3px;color:var(--sub);line-height:1.72;display:flex;gap:8px;
}
.tip .i{
  flex:none;width:16px;height:16px;border-radius:50%;margin-top:1px;
  background:var(--card);border:1px solid var(--line);color:var(--blue);
  font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center;
}

/* ---------- 控制条 ---------- */
.controls{display:flex;align-items:center;gap:11px;margin-top:18px;flex-wrap:wrap}
.btn{
  appearance:none;border:1px solid var(--line);background:#fff;color:var(--text);
  font:inherit;font-size:13px;font-weight:500;padding:9px 17px;border-radius:10px;
  cursor:pointer;transition:.16s;display:inline-flex;align-items:center;gap:6px;
  box-shadow:0 1px 3px rgba(17,24,39,.05);
}
.btn:hover:not(:disabled){border-color:#c4cad3;background:var(--card)}
.btn:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
.btn.primary{background:var(--blue);color:#fff;border-color:var(--blue);
  box-shadow:0 4px 14px rgba(47,107,255,.32)}
.btn.primary:hover:not(:disabled){background:var(--blue-d);border-color:var(--blue-d)}
.dots{display:flex;gap:7px;margin-left:auto;align-items:center}
.dot{width:9px;height:9px;border-radius:50%;background:#d5dae1;border:none;padding:0;
  cursor:pointer;transition:.2s}
.dot:hover{background:#aab3bf}
.dot.on{background:var(--blue);transform:scale(1.3);box-shadow:0 0 0 3px rgba(47,107,255,.16)}
.bar{height:3px;background:#e3e7ec;border-radius:2px;margin-top:14px;overflow:hidden}
.bar .fill{height:100%;width:0;background:var(--blue);border-radius:2px}
.hint{font-size:11.5px;color:var(--sub);margin-top:10px}
kbd{
  font-family:inherit;font-size:11px;background:#fff;border:1px solid var(--line);
  border-bottom-width:2px;border-radius:4px;padding:1px 5px;color:var(--sub);
}
"""

MIC_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.9" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="9" y="2.5" width="6" height="10.5" rx="3" fill="#fff" stroke="none"/>'
    '<path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/>'
    '<path d="M12 18v3.5"/><path d="M8.5 21.5h7"/></svg>'
)


def image_uri(name: str, crop: tuple[int, int, int, int] | None) -> str:
    """取图，必要时裁剪，返回 base64 data URI。

    用 Qt 而不是 PIL：runtime 里没有 PIL，但 PySide6 一定有（截图工具也靠它）。
    裁剪坐标是原图物理像素，比例不变，聚光灯的百分比坐标才对得上。
    """
    p = ASSETS / name
    if crop is None:
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")

    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImage

    src = QImage(str(p))
    x, y, w, h = crop
    # 裁剪框不能超出原图，否则 copy 得到空图
    w = min(w, src.width() - x)
    h = min(h, src.height() - y)
    sub = src.copy(QRect(x, y, w, h))

    tmp = PROMO / "demo" / "_tmp_crop.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    sub.save(str(tmp), "PNG")
    data = tmp.read_bytes()
    tmp.unlink()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def build() -> str:
    slides, panels, dots = [], [], []
    for i, s in enumerate(STEPS):
        uri = image_uri(s["img"], s.get("crop"))
        is_red = any(k in s["kicker"] for k in ("录音中", "停止"))
        pct = lambda v: f"{v*100:.4f}%"
        # 支持一张图多个高亮区（录音那步要同时框住状态卡和按钮）
        rings = "".join(
            f'<div class="ring{" red" if is_red else ""}" style="'
            f'left:{pct(a)};top:{pct(b)};width:{pct(c)};height:{pct(d)}"></div>'
            for a, b, c, d in s["hot"]
        )
        mw = f' style="max-width:{s["maxw"]}px"' if s.get("maxw") else ""
        wide = " wide" if s.get("wide") else ""
        slides.append(
            f'<div class="slide" data-i="{i}">'
            f'<div class="frame{wide}"><img src="{uri}" alt="{s["title"]}"{mw}>{rings}</div>'
            f"</div>"
        )
        panels.append(
            f'<div class="panel-body{" on" if i == 0 else ""}" data-i="{i}">'
            f'<div class="kicker{" red" if is_red else ""}">{s["kicker"]}</div>'
            f"<h2>{s['title']}</h2>"
            f'<div class="body">{s["body"]}</div>'
            f'<div class="tip"><span class="i">i</span><span>{s["tip"]}</span></div>'
            f"</div>"
        )
        dots.append(
            f'<button class="dot{" on" if i == 0 else ""}" data-go="{i}" '
            f'aria-label="第 {i+1} 步"></button>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>录音助手 · 使用演示</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">{MIC_SVG}</div>
    <div>
      <h1>录音助手 · 使用演示</h1>
      <div class="sub">点一下开始，点一下停止，录音自动存成 mp3 · 全程本地运行</div>
    </div>
    <div class="badge">截图均为真实运行画面</div>
  </header>

  <div class="main">
    <div class="stage" id="stage">
      {''.join(slides)}
    </div>
    <div class="panel" id="panel">
      {''.join(panels)}
    </div>
  </div>

  <div class="controls">
    <button class="btn" id="prev">← 上一步</button>
    <button class="btn primary" id="play">▶ 自动播放</button>
    <button class="btn" id="next">下一步 →</button>
    <div class="dots">{''.join(dots)}</div>
  </div>
  <div class="bar"><div class="fill" id="fill"></div></div>
  <div class="hint">键盘 <kbd>←</kbd> <kbd>→</kbd> 翻页 · <kbd>空格</kbd> 播放/暂停 · 点圆点跳转</div>
</div>

<script>
(function(){{
  var N = {len(STEPS)};
  var i = 0, playing = false, timer = null, DUR = 5400;

  var fill   = document.getElementById('fill');
  var playBtn= document.getElementById('play');
  var stage  = document.getElementById('stage');
  var panel  = document.getElementById('panel');
  var slides = [].slice.call(document.querySelectorAll('.slide'));
  var panels = [].slice.call(document.querySelectorAll('.panel-body'));
  var dots   = [].slice.call(document.querySelectorAll('.dot'));

  /* 舞台高度跟着当前图的比例走。
     注意这里只算高度、不做任何位置测量 —— 聚光灯是 .frame 里的百分比定位，
     图片一缩放它就自动跟上了，不需要 getBoundingClientRect。
     （之前那版靠量 rect 换算坐标，回流没完成时环就飘到空白处。） */
  function fit(){{
    var img = slides[i].querySelector('img');
    var pad = 44;                                   // .stage 的 padding 合计
    var boxW = stage.clientWidth - pad;
    var ratio = img.naturalHeight / img.naturalWidth;
    var drawH = Math.min(boxW * ratio, 620);
    var h = Math.round(drawH + pad);
    stage.style.height = Math.max(240, h) + 'px';
    panel.style.minHeight = stage.style.height;
  }}

  function show(n){{
    i = (n + N) % N;
    slides.forEach(function(el,k){{ el.style.display = (k === i) ? 'block' : 'none'; }});
    panels.forEach(function(el,k){{ el.classList.toggle('on', k === i); }});
    dots.forEach(function(d,k){{ d.classList.toggle('on', k === i); }});
    document.getElementById('prev').disabled = (i === 0);
    document.getElementById('next').disabled = (i === N - 1);
    fit();
  }}

  function restartBar(){{
    fill.style.transition = 'none';
    fill.style.width = '0%';
    void fill.offsetWidth;               // 强制回流，否则下一次 transition 不生效
    fill.style.transition = 'width ' + DUR + 'ms linear';
    fill.style.width = '100%';
  }}

  function play(){{
    if (i === N - 1) show(0);
    playing = true;
    playBtn.textContent = '⏸ 暂停';
    restartBar();
    clearInterval(timer);
    timer = setInterval(function(){{
      if (i >= N - 1) {{ pause(); return; }}
      show(i + 1);
      restartBar();
    }}, DUR);
  }}

  function pause(){{
    playing = false;
    playBtn.textContent = '▶ 自动播放';
    clearInterval(timer); timer = null;
    fill.style.transition = 'none'; fill.style.width = '0%';
  }}

  function step(d){{
    var was = playing;
    pause();
    show(i + d);
    if (was && i < N - 1) play();
  }}

  document.getElementById('prev').onclick = function(){{ step(-1); }};
  document.getElementById('next').onclick = function(){{ step(1); }};
  playBtn.onclick = function(){{ playing ? pause() : play(); }};
  dots.forEach(function(d,k){{
    d.onclick = function(){{ var w = playing; pause(); show(k); if (w) play(); }};
  }});

  document.addEventListener('keydown', function(e){{
    if (e.key === 'ArrowLeft')  {{ e.preventDefault(); step(-1); }}
    if (e.key === 'ArrowRight') {{ e.preventDefault(); step(1); }}
    if (e.key === ' ')          {{ e.preventDefault(); playing ? pause() : play(); }}
  }});

  window.addEventListener('resize', function(){{ fit(); }});
  window.addEventListener('load', function(){{ fit(); }});

  show(0);
}})();
</script>
</body>
</html>
"""


def main():
    html = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"已生成 {OUT}  ({len(html)//1024}KB, {len(STEPS)} 步, 单文件自包含)")


if __name__ == "__main__":
    main()
