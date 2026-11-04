"""promo 截图工具 —— 真实运行 RecWindow 后截取真实界面。

关键设计：
- 不经过 gui.main()，所以不抢单实例锁（127.0.0.1:47311）；
- 不写 config.json（patch gui.save_config），不动注册表（patch autostart.sync）；
- 录音走真实 UI 方法 w.on_start() / w.on_stop()，麦克风真的开流，
  计时 / 电平 / 日志 / 保存提示 / mp3 落盘 全是程序自己跑出来的；
- 窗口像素用 PrintWindow(PW_RENDERFULLCONTENT) 从窗口自身渲染：
  不会被别的窗口遮挡，也保留原生标题栏；2x DPI 下是 2880x1920 级清晰度；
- 局部图（设置区等）从整窗图按控件几何裁切，不抓屏幕，避免串味。

用法：
    python shot.py idle        # 只截待命窗口（验证环境）
    python shot.py all         # 全量出图
    python shot.py tray        # 只补托盘相关（图标 / 右键菜单）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\code\open-record")
APP_DIR = ROOT / "app"
OUT = ROOT / "promo" / "guide" / "assets"
sys.path.insert(0, str(APP_DIR))

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QFrame

import autostart
import gui

# ---- 隔离副作用：不写配置、不写注册表 ----
autostart.sync = lambda want: (True, "stub")           # type: ignore[assignment]
gui.save_config = lambda cfg: None                     # type: ignore[assignment]

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi

SW_RESTORE = 9
SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
PW_RENDERFULLCONTENT = 2

DPR = 2.0
CREATED: list[str] = []


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def pump(sec: float) -> None:
    t0 = time.time()
    while time.time() - t0 < sec:
        QApplication.processEvents()
        time.sleep(0.015)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口可见外框（含标题栏与阴影），物理像素。"""
    r = RECT()
    ok = dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), 9,  # DWMWA_EXTENDED_FRAME_BOUNDS
                                     ctypes.byref(r), ctypes.sizeof(r))
    if ok != 0:
        user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def client_origin(hwnd: int) -> tuple[int, int]:
    """客户区左上角在屏幕上的物理坐标。"""
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt))
    return pt.x, pt.y


def raise_window(hwnd: int) -> None:
    user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
    user32.SetWindowPos(ctypes.c_void_p(hwnd), HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))


def drop_top(hwnd: int) -> None:
    user32.SetWindowPos(ctypes.c_void_p(hwnd), HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)


def _dib_to_image(hdc_mem, hbmp, w: int, h: int):
    from PySide6.QtGui import QImage
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h            # top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
    return QImage(bytes(buf), w, h, w * 4, QImage.Format_RGB32).copy()


def grab_window(hwnd: int):
    """PrintWindow 渲染窗口自身（不被遮挡、含原生标题栏）。

    用 GetWindowRect 而不是 DWM 扩展边框：后者含阴影留白，PrintWindow 不画阴影，
    截出来会多一圈黑边。
    """
    r = RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    x, y, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    # 先铺白底：PrintWindow 对未绘制区域会留黑
    gdi32.PatBlt(hdc_mem, 0, 0, w, h, 0x00FF0062)  # WHITENESS
    ok = user32.PrintWindow(ctypes.c_void_p(hwnd), hdc_mem, PW_RENDERFULLCONTENT)
    img = _dib_to_image(hdc_mem, hbmp, w, h)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    if not ok:
        print(f"WARN PrintWindow returned 0 for hwnd {hwnd}")
    # GetWindowRect 含不可见的阴影边，裁掉后再返回，rect 同步平移
    img, (dx, dy) = trim_shadow(img)
    return img, (x + dx, y + dy, img.width(), img.height())


def grab_screen(x: int, y: int, w: int, h: int):
    """屏幕区域抓取（物理像素），用于窗口之外的内容（托盘、资源管理器）。"""
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY)
    img = _dib_to_image(hdc_mem, hbmp, w, h)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    return img


def trim_shadow(img):
    """裁掉 PrintWindow 留下的阴影黑边（GetWindowRect 比实际窗口大一圈）。

    从四边向内扫，遇到第一行/列「基本不是纯黑」的内容就停。
    纯黑阈值放宽到 12：标题栏深色也远高于它。
    """
    from PySide6.QtGui import QImage
    w, h = img.width(), img.height()

    def row_dark(y: int) -> bool:
        for x in range(0, w, 3):
            c = img.pixelColor(x, y)
            if c.red() > 12 or c.green() > 12 or c.blue() > 12:
                return False
        return True

    def col_dark(x: int) -> bool:
        for y in range(0, h, 3):
            c = img.pixelColor(x, y)
            if c.red() > 12 or c.green() > 12 or c.blue() > 12:
                return False
        return True

    t = 0
    while t < h // 4 and row_dark(t):
        t += 1
    b = h - 1
    while b > h * 3 // 4 and row_dark(b):
        b -= 1
    l = 0
    while l < w // 4 and col_dark(l):
        l += 1
    r = w - 1
    while r > w * 3 // 4 and col_dark(r):
        r -= 1
    if t == 0 and b == h - 1 and l == 0 and r == w - 1:
        return img, (0, 0)
    return img.copy(QRect(l, t, r - l + 1, b - t + 1)), (l, t)


def save(img, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    img.save(str(p))
    print(f"saved {p.name}  {img.width()}x{img.height()}")
    return p


def shot_window(win, name: str, settle: float = 0.6):
    """截整窗，返回 (路径, 图, 窗口物理矩形)。"""
    hwnd = int(win.winId())
    raise_window(hwnd)
    pump(settle)
    win.setFocus()
    fw = QApplication.focusWidget()
    if fw is not None and fw is not win:
        fw.clearFocus()
    pump(0.3)
    img, rect = grab_window(hwnd)
    drop_top(hwnd)
    return save(img, name), img, rect


def build_window():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    cfg = gui.load_config()
    cfg["autostart"] = True            # 已有该键 -> 不触发"首次运行"分支
    w = gui.RecWindow(start_hidden=False)
    w.cfg = cfg
    scr = QApplication.primaryScreen().availableGeometry()
    w.move(scr.x() + (scr.width() - w.width()) // 2,
           scr.y() + max(16, (scr.height() - w.height()) // 2 - 30))
    w.show()
    pump(1.2)
    print("screen", scr, "win", w.pos(), w.size())
    return app, w


# ---------------- 各个镜头 ----------------

def preflight() -> bool:
    """开跑前确认 8898 没被别的实例占着。

    RecWindow.__init__ 内部会启 HTTP 服务（绑 127.0.0.1:8898）。如果此时已有
    另一个录音助手实例在跑，本进程日志里就会出现「端口被占用」，截图不干净。
    这里只检测、不杀进程 —— 杀用户正在跑的实例是破坏性操作。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 8898))
        s.close()
        return True
    except OSError:
        s.close()
        print("WARN: 127.0.0.1:8898 已被占用 —— 另一个录音助手实例在跑，"
              "截图日志里会出现端口冲突提示。请先退出它再重跑。")
        return False


def tray_geom(w) -> tuple[int, int, int, int]:
    g = w.tray.tray.geometry()
    return g.x(), g.y(), g.width(), g.height()


def shot_tray_icon(w, name: str) -> Path:
    """整条右下角通知区（^ 展开箭头 + 本实例图标 + 系统图标 + 时钟）。

    为什么不只截图标本身：只给一个 40px 方块，读者无法判断它到底在
    「可见托盘区」还是被收进了溢出区。带上 ^ 箭头和时钟，一眼就能对上
    自己屏幕右下角的位置。
    """
    was_visible = w.isVisible()
    if was_visible:
        w.hide()
        pump(0.8)
    x, y, iw, ih = tray_geom(w)
    scr = QApplication.primaryScreen().geometry()
    left = x - 34
    top = y - 6
    width = scr.width() - left - 4
    height = min(ih + 8, scr.height() - top)
    img = grab_screen(int(left * DPR), int(top * DPR),
                      int(width * DPR), int(height * DPR))
    if was_visible:
        w.show()
        pump(0.6)
    return save(img, name)


def shot_tray_menu(w) -> Path:
    """真实右键菜单：菜单项、勾选、顺序都是程序自己构建的那一份。

    必须先藏起主窗口 —— 窗口抢焦点会让 QMenu 立刻关掉。
    """
    if w.isVisible():
        w.hide()
        pump(0.6)
    x, y, iw, ih = tray_geom(w)
    menu = w.tray.menu
    menu.popup(QPoint(x + iw // 2, y - 2))
    pump(0.9)
    mg = menu.geometry()
    print("menu geom", mg, "visible", menu.isVisible())
    img = grab_screen(int((mg.x() - 4) * DPR), int((mg.y() - 4) * DPR),
                      int((mg.width() + 8) * DPR), int((mg.height() + 8) * DPR))
    menu.hide()
    pump(0.2)
    return save(img, "04-tray-menu.png")


def crop_widget(img, win_rect, hwnd: int, widget, pad: int, name: str) -> Path:
    """从整窗图裁切某个控件区域。

    控件几何是逻辑坐标、相对窗口客户区；整窗图起点是窗口外框。
    两者之差 = 标题栏/边框偏移（用 ClientToScreen 与窗口矩形求出）。
    """
    wx, wy, _, _ = win_rect
    cx, cy = client_origin(hwnd)
    tl = widget.mapTo(widget.window(), QPoint(0, 0))
    x = (cx - wx) / DPR + tl.x() - pad
    y = (cy - wy) / DPR + tl.y() - pad
    w = widget.width() + pad * 2
    h = widget.height() + pad * 2
    r = QRect(int(x * DPR), int(y * DPR), int(w * DPR), int(h * DPR))
    r = r.intersected(img.rect())
    return save(img.copy(r), name)


def shot_settings(w, img, rect):
    box = w.findChild(QFrame, "settingsBox")
    return crop_widget(img, rect, int(w.winId()), box, 10, "05-settings-feishu.png")


def shot_wizard(w) -> Path:
    """真实「创建飞书机器人」向导对话框（只渲染，不联网、不点校验）。"""
    dlg = gui.FeishuSetupDialog(w)
    dlg.show()
    pump(1.2)
    hwnd = int(dlg.winId())
    raise_window(hwnd)
    pump(0.6)
    img, _rect = grab_window(hwnd)
    drop_top(hwnd)
    p = save(img, "07-feishu-wizard.png")
    dlg.close()
    pump(0.3)
    return p


def shot_wizard_error(w) -> Path | None:
    """向导的「校验失败」态：真的调一次飞书接口，用假凭据拿到真实报错。

    这条路径完全走程序自己的 _verify -> feishu_setup.verify_credentials，
    红字内容是飞书服务器返回的 code/msg，不是编的。断网时会退化成网络错误提示。
    """
    dlg = gui.FeishuSetupDialog(w)
    dlg.show()
    pump(0.8)
    dlg.ed_id.setText("cli_xxxxxxxxxxxx")
    dlg.ed_secret.setText("not-a-real-secret")
    dlg._verify()
    for _ in range(60):
        if dlg.btn_verify.isEnabled():
            break
        pump(0.25)
    pump(0.4)
    hwnd = int(dlg.winId())
    raise_window(hwnd)
    pump(0.5)
    img, _rect = grab_window(hwnd)
    drop_top(hwnd)
    p = save(img, "07b-feishu-wizard-error.png")
    print("wizard error text:", repr(dlg.lbl_status.text()))
    dlg.close()
    pump(0.3)
    return p


def shot_explorer(dir_path: Path, name: str) -> Path | None:
    """真实文件资源管理器窗口（展示 mp3 落在 recordings 下）。"""
    import subprocess
    subprocess.Popen(["explorer.exe", str(dir_path)])
    pump(1.2)

    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "CabinetWClass":
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if str(dir_path.name) in title or "open-record" in title:
            found.append(int(hwnd))
            return False
        return True

    for _ in range(40):
        found.clear()
        user32.EnumWindows(cb, 0)
        if found:
            break
        pump(0.25)
    if not found:
        print("explorer window not found, skip")
        return None

    hwnd = found[0]
    raise_window(hwnd)
    pump(1.4)
    img, _rect = grab_window(hwnd)
    drop_top(hwnd)
    p = save(img, name)
    user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)   # WM_CLOSE
    pump(0.5)
    return p


def run_all(w):
    shot_tray_icon(w, "01-tray-icon.png")
    _p, img, rect = shot_window(w, "02-main-window-idle.png")
    shot_settings(w, img, rect)
    shot_wizard(w)
    shot_wizard_error(w)

    # ---- 真实录音 ----
    w.on_start()
    pump(1.0)
    for _ in range(40):
        if w._recording:
            break
        pump(0.25)
    print("recording:", w._recording, w.lbl_state.text())
    pump(7.0)
    print("elapsed:", w.lbl_elapsed.text(), "| detail:", w.lbl_detail.text())
    shot_window(w, "03-recording.png", settle=0.15)
    shot_tray_icon(w, "06-tray-icon-recording.png")

    # ---- 真实停止 + 合并 mp3 ----
    w.on_stop()
    pump(2.0)
    for _ in range(80):
        if not w._busy:
            break
        pump(0.3)
    pump(1.2)
    print("log after stop:", repr(w.txt_log.text()))
    shot_window(w, "08-after-saved.png", settle=0.25)

    last = (w.svc.status() or {}).get("last_result") or {}
    mp3 = last.get("audio_mp3")
    print("LAST_MP3", mp3)
    if mp3:
        CREATED.append(str(Path(mp3).parent))
        shot_explorer(Path(mp3).parent.parent, "09-recordings-folder.png")

    pump(0.8)
    shot_tray_menu(w)


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "idle"
    if stage != "tray":
        preflight()
    app, w = build_window()
    if stage == "idle":
        shot_window(w, "02-main-window-idle.png")
    elif stage == "tray":
        shot_tray_icon(w, "01-tray-icon.png")
        shot_tray_menu(w)
    elif stage == "all":
        run_all(w)
    w._really_quit = True
    try:
        w._shutdown()
    except Exception as e:
        print("shutdown:", e)
    if CREATED:
        logp = Path(__file__).with_name("_created_sessions.txt")
        prev = logp.read_text(encoding="utf-8").splitlines() if logp.exists() else []
        logp.write_text("\n".join(sorted(set(prev + CREATED))), encoding="utf-8")
        print("created sessions recorded:", CREATED)
    print("done")


if __name__ == "__main__":
    main()
