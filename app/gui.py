"""
录音助手 GUI（托盘常驻版）
=========================
无感形态：
- 开机自启（HKCU\\Run，免提权），启动后**直接进托盘，不弹窗**
- 右下角托盘图标常驻，单击打开界面，右键出菜单（含退出）
- 关闭窗口 = 收进托盘，不退出；退出只能走托盘菜单
- 录音状态用托盘图标颜色区分（蓝=待命 / 红=录音中）

启动参数：
    --background   只进托盘，不显示窗口（自启用它）
    --show         强制显示窗口（默认）

开发调试：python app/launcher.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

# 目录约定：
#   APP_DIR   = app/               源码所在目录（本文件所在）
#   ROOT_DIR  = 发行包根目录          config.json / recordings / logs 在这里
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    ROOT_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent
    _parent = APP_DIR.parent
    ROOT_DIR = _parent if ((_parent / "config.json").exists()
                           or (_parent / "runtime").exists()) else APP_DIR

sys.path.insert(0, str(APP_DIR))

import autostart  # noqa: E402
import feishu_setup  # noqa: E402
import trayfix  # noqa: E402
from service import RecService  # noqa: E402
from singleton import InstanceLock, notify_existing  # noqa: E402
from tray import Tray, app_icon  # noqa: E402

CONFIG_PATH = ROOT_DIR / "config.json"
LOG_DIR = ROOT_DIR / "logs"

log = logging.getLogger("gui")

# ---------- 视觉规范 ----------
C_BG = "#ffffff"
C_TEXT = "#1b1f26"
C_SUB = "#6b7280"
C_LINE = "#e5e7eb"
C_CARD = "#f7f8fa"
C_BLUE = "#2f6bff"
C_BLUE_HOVER = "#1f5ae8"
C_RED = "#e5352b"
C_GREEN = "#1a9c62"

# 注意：Qt 里 QLabel 继承自 QFrame，用 `QFrame{...}` 选择器会把边框糊到所有标签上。
# 所以卡片一律走 objectName，标签走类型选择器显式清掉边框。
STYLE = f"""
QWidget {{ background:{C_BG}; color:{C_TEXT};
           font-family:"Microsoft YaHei UI","Microsoft YaHei",sans-serif; }}
QLabel {{ background:transparent; border:none; }}
QFrame#card {{ background:{C_CARD}; border:1px solid {C_LINE}; border-radius:14px; }}
QFrame#settingsBox {{ background:{C_BG}; border:1px solid {C_LINE}; border-radius:12px; }}
QLabel#logBox {{ background:#fbfbfc; border:1px solid {C_LINE}; border-radius:10px;
                 padding:10px; color:{C_SUB}; font-size:11px; }}
QLabel#sectionTitle {{ color:{C_SUB}; font-size:11px; font-weight:bold; }}
QLabel#subtitle {{ color:{C_SUB}; font-size:11px; }}
QLineEdit {{ background:{C_BG}; border:1px solid {C_LINE}; border-radius:8px;
             padding:5px 10px; selection-background-color:{C_BLUE}; }}
QLineEdit:focus {{ border:1px solid {C_BLUE}; }}
QLineEdit:read-only {{ background:#fbfbfc; color:{C_SUB}; }}
QPushButton#secondary {{ background:{C_BG}; border:1px solid #d4d8de; border-radius:8px;
                         padding:6px 14px; color:{C_TEXT}; }}
QPushButton#secondary:hover {{ background:#f2f4f7; border-color:#c3c8d0; }}
QPushButton#secondary:disabled {{ color:#aab0b9; border-color:{C_LINE}; }}
QPushButton#secondary:pressed {{ background:#e9ecf1; }}
QCheckBox {{ color:{C_TEXT}; spacing:8px; }}
QMenu {{ background:{C_BG}; border:1px solid {C_LINE}; border-radius:8px; padding:6px; }}
QMenu::item {{ padding:7px 26px 7px 14px; border-radius:6px; }}
QMenu::item:selected {{ background:#eef3ff; color:{C_BLUE}; }}
QMenu::separator {{ height:1px; background:{C_LINE}; margin:5px 8px; }}
"""



def resource_path(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", APP_DIR))
    return base / rel


# ---------- 配置 ----------
# 默认值与深合并的唯一真源在 config.py（原来 gui/service 各一份，已经漂移：
# prebuffer_sec 15 vs 120、keep_native_rate True vs False）。
from config import DEFAULTS, deep_merge, normalize as _cfg_normalize

_DEFAULTS = DEFAULTS
_deep_merge = deep_merge


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return _deep_merge(DEFAULTS, raw)
        except Exception:
            log.exception("config.json 解析失败，使用默认值")
    return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / "gui.log", encoding="utf-8")],
    )


class RecWindow(QMainWindow):
    """主窗口。关掉只是收进托盘，真正退出走托盘菜单。"""

    log_signal = Signal(str)
    state_signal = Signal(dict)
    _alert_signal = Signal(str)   # 需要弹框告知的严重提示
    quit_signal = Signal()
    # 单实例把指令从监听线程送到主线程。
    # 不能用 QTimer.singleShot：它投递到「当前线程」的事件循环，
    # 而监听线程是纯 Python 线程、没有 Qt 事件循环，定时器永远不会触发。
    cmd_signal = Signal(str)

    def __init__(self, start_hidden: bool = False):
        super().__init__()
        self.cfg = load_config()
        self.svc: RecService | None = None
        self._busy = False
        self._recording = False
        self._really_quit = False
        self._dead = False          # 窗口已销毁时置位，拦住后台线程的 emit
        self._start_hidden = start_hidden
        self._httpd = None
        self._feishu_bot = None
        # 托盘 show() 是否成功。注意它探测不到 Win11 溢出区（那种情况也是 True），
        # 只对应「托盘根本不可用」；False 时窗口必须强制亮出。
        self._tray_visible = True

        self.setWindowTitle("录音助手")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(660, 600)
        self._build_ui()
        self._apply_config_to_ui()

        self.log_signal.connect(self._append_log)
        self._alert_signal.connect(self._on_alert)
        self.state_signal.connect(self._on_state)
        self.cmd_signal.connect(self._on_command)

        self.tray = Tray(autostart_on=self.cfg.get("autostart", True), parent=self)
        self.tray.show_requested.connect(self.show_window)
        self.tray.quit_requested.connect(self.request_quit)
        self.tray.toggle_record.connect(self._tray_toggle)
        self.tray.autostart_toggled.connect(self._on_autostart_toggled)
        self.tray.open_folder_requested.connect(self._open_folder)
        # 图标没显示出来时不能让应用变成「隐形进程」：进程被 setQuitOnLastWindowClosed(False)
        # 吊着，用户既没有图标也没有窗口，只能去任务管理器杀。
        self._tray_visible = self.tray.show()
        if not self._tray_visible:
            log.warning("托盘图标不可见，改为直接显示窗口以免应用无入口")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)

        self._boot_service()
        self._first_run_autostart()
        self._promote_tray_icon()

    # ---------- 托盘可见性 ----------
    def _promote_tray_icon(self):
        """把托盘图标从 Win11 的「^」溢出区提到可见托盘区。

        为什么需要：Win11 默认把新出现的托盘图标收进溢出区，用户右下角
        看不到图标，就会以为程序没启动 —— 这正是用户实际反馈的问题。

        为什么要重试：explorer 是在图标**真正显示之后**才写注册表记录的，
        启动那一刻往往还查不到，所以用定时器反复试几次，拿到确定结果就停。
        """
        self._promoter = trayfix.TrayPromoter(attempts=5)
        self._promote_timer = QTimer(self)
        self._promote_timer.timeout.connect(self._promote_step)
        self._promote_timer.start(1000)      # 1 秒一次，最多 5 次

    def _promote_step(self):
        st = self._promoter.step()
        if not self._promoter.done:
            return
        self._promote_timer.stop()
        if st in (trayfix.PROMOTED, trayfix.ALREADY):
            log.info("托盘图标已提升到可见区域（状态=%s）", st)
        elif st == trayfix.NOT_FOUND:
            # 用完重试次数仍没找到记录：图标可能压根没显示（托盘被禁用等）
            log.warning("托盘图标提升失败：注册表里始终没有本进程的记录")
            self._emit_log("托盘图标可能被收进了 ^ 溢出区，可手动拖到任务栏")
        elif st == trayfix.UNSUPPORTED:
            log.info("托盘图标提升：当前系统不需要/不支持")
        elif st == trayfix.ERROR:
            log.warning("托盘图标提升失败：注册表写入被拒绝")

    # ---------- 界面 ----------
    def _build_ui(self):
        root = QWidget()
        root.setStyleSheet(STYLE)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(14)

        # 标题行：图标 + 名称 + 副标题
        head = QHBoxLayout()
        head.setSpacing(12)
        ic = QLabel()
        ic.setPixmap(app_icon().pixmap(40, 40))
        ic.setFixedSize(44, 44)
        head.addWidget(ic)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        t1 = QLabel("录音助手")
        f1 = QFont(); f1.setPointSize(17); f1.setBold(True)
        t1.setFont(f1)
        t2 = QLabel("常驻后台 · 关闭窗口即收进托盘")
        t2.setObjectName("subtitle")
        titles.addWidget(t1)
        titles.addWidget(t2)
        head.addLayout(titles)
        head.addStretch()
        outer.addLayout(head)

        # 状态卡片
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.lbl_dot = QLabel("●")
        self.lbl_dot.setStyleSheet(f"color:{C_SUB};font-size:15px;")
        self.lbl_state = QLabel("待命")
        f2 = QFont(); f2.setPointSize(14); f2.setBold(True)
        self.lbl_state.setFont(f2)
        row.addWidget(self.lbl_dot)
        row.addWidget(self.lbl_state)
        row.addStretch()
        self.lbl_elapsed = QLabel("")
        ef = QFont(); ef.setPointSize(15); ef.setBold(True)
        self.lbl_elapsed.setFont(ef)
        self.lbl_elapsed.setStyleSheet(f"color:{C_TEXT};")
        row.addWidget(self.lbl_elapsed)
        cl.addLayout(row)

        self.lbl_detail = QLabel("麦克风未占用，等待开始")
        self.lbl_detail.setObjectName("subtitle")
        self.lbl_detail.setStyleSheet(f"color:{C_SUB};font-size:12px;")
        cl.addWidget(self.lbl_detail)
        outer.addWidget(card)

        # 主按钮：一个键切换开始/停止
        self.btn_main = QPushButton("开始录音")
        self.btn_main.setMinimumHeight(52)
        fm = QFont(); fm.setPointSize(14); fm.setBold(True)
        self.btn_main.setFont(fm)
        self.btn_main.setCursor(Qt.PointingHandCursor)
        self.btn_main.clicked.connect(self._on_main_clicked)
        outer.addWidget(self.btn_main)

        # 设置区
        st = QLabel("设置")
        fs = QFont(); fs.setPointSize(11); fs.setBold(True)
        st.setFont(fs)
        st.setObjectName("sectionTitle")
        outer.addWidget(st)

        box = QFrame()
        box.setObjectName("settingsBox")
        grid = QGridLayout(box)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(11)

        grid.addWidget(QLabel("音频目录"), 0, 0)
        self.ed_dir = QLineEdit()
        self.ed_dir.setReadOnly(True)
        self.ed_dir.setMinimumHeight(30)
        btn_dir = QPushButton("浏览…")
        btn_dir.setObjectName("secondary")
        btn_dir.setMaximumWidth(76)
        btn_dir.setCursor(Qt.PointingHandCursor)
        btn_dir.clicked.connect(self.on_pick_dir)
        grid.addWidget(self.ed_dir, 0, 1)
        grid.addWidget(btn_dir, 0, 2)

        grid.addWidget(QLabel("App ID"), 1, 0)
        self.ed_app_id = QLineEdit()
        self.ed_app_id.setPlaceholderText("cli_xxxxxxxx")
        self.ed_app_id.setMinimumHeight(30)
        grid.addWidget(self.ed_app_id, 1, 1)

        # 一键创建：飞书不开放建应用接口，所以这里做「引导 + 自动校验 + 自动回填」。
        # 放在 App ID 行右侧，紧挨着用户要填的地方，减少来回找。
        self.btn_feishu_setup = QPushButton("一键创建飞书机器人")
        self.btn_feishu_setup.setObjectName("secondary")
        self.btn_feishu_setup.setMinimumHeight(30)
        # 限宽：否则它会挤掉 App ID 输入框，和下面 App Secret 的右边缘对不齐
        self.btn_feishu_setup.setMaximumWidth(150)
        self.btn_feishu_setup.setCursor(Qt.PointingHandCursor)
        self.btn_feishu_setup.setToolTip(
            "打开飞书开发者后台创建应用，并自动校验你填的 App ID / App Secret")
        self.btn_feishu_setup.clicked.connect(self.on_feishu_setup)
        grid.addWidget(self.btn_feishu_setup, 1, 2)

        grid.addWidget(QLabel("App Secret"), 2, 0)
        self.ed_secret = QLineEdit()
        self.ed_secret.setEchoMode(QLineEdit.Password)
        self.ed_secret.setPlaceholderText("飞书应用密钥")
        self.ed_secret.setMinimumHeight(30)
        grid.addWidget(self.ed_secret, 2, 1, 1, 2)

        self.chk_autostart = QCheckBox("开机自动启动（后台常驻，不弹窗）")
        self.chk_autostart.setChecked(bool(self.cfg.get("autostart", True)))
        self.chk_autostart.setCursor(Qt.PointingHandCursor)
        grid.addWidget(self.chk_autostart, 3, 0, 1, 3)

        # 预录开关：默认关（待命不占麦克风，其他软件可随时用）。
        # 打开后待命期间常驻开流，点击录音能真正补上 prebuffer_sec 秒。
        self.chk_always_on_mic = QCheckBox("待命时也占用麦克风（开启预录，开头不丢）")
        self.chk_always_on_mic.setChecked(bool(self.cfg.get("always_on_mic", False)))
        self.chk_always_on_mic.setCursor(Qt.PointingHandCursor)
        self.chk_always_on_mic.setToolTip(
            "关闭时待命不占麦克风，其他软件可随时录音，但开始录音的头 ~0.2 秒补不回来。\n"
            "开启后待命期间保持开流，能补上完整的预录时长，代价是麦克风被常驻占用。")
        grid.addWidget(self.chk_always_on_mic, 4, 0, 1, 3)

        grid.setColumnStretch(1, 1)
        outer.addWidget(box)

        save_row = QHBoxLayout()
        self.btn_save = QPushButton("保存并生效")
        self.btn_save.setObjectName("secondary")
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setMinimumWidth(120)
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.on_save)
        self.lbl_fs = QLabel("飞书未启用")
        self.lbl_fs.setObjectName("subtitle")
        save_row.addWidget(self.btn_save)
        save_row.addSpacing(12)
        save_row.addWidget(self.lbl_fs)
        save_row.addStretch()
        outer.addLayout(save_row)

        lt = QLabel("运行日志")
        lt.setFont(fs)
        lt.setObjectName("sectionTitle")
        outer.addWidget(lt)
        self.txt_log = QLabel("")
        self.txt_log.setObjectName("logBox")
        self.txt_log.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.txt_log.setWordWrap(True)
        self.txt_log.setMinimumHeight(84)
        outer.addWidget(self.txt_log)

        self._set_buttons(recording=False, busy=False)

    def _apply_config_to_ui(self):
        rd = self.cfg.get("recordings_dir", "recordings")
        p = Path(rd)
        if not p.is_absolute():
            p = ROOT_DIR / rd
        self.ed_dir.setText(str(p))
        fs = self.cfg.get("feishu", {}) or {}
        self.ed_app_id.setText(fs.get("app_id", ""))
        self.ed_secret.setText(fs.get("app_secret", ""))
        self.chk_always_on_mic.setChecked(bool(self.cfg.get("always_on_mic", False)))
        self._refresh_feishu_label()

    def _refresh_feishu_label(self):
        fs = self.cfg.get("feishu", {}) or {}
        if fs.get("enabled") and fs.get("app_id"):
            self.lbl_fs.setText("飞书已启用")
            self.lbl_fs.setStyleSheet(f"color:{C_GREEN};font-size:11px;")
        else:
            self.lbl_fs.setText("飞书未启用")
            self.lbl_fs.setStyleSheet(f"color:{C_SUB};font-size:11px;")

    # ---------- 服务 ----------
    def _boot_service(self):
        root = ROOT_DIR
        rd = Path(self.cfg.get("recordings_dir", "recordings"))
        target = rd if rd.is_absolute() else root / rd
        target.mkdir(parents=True, exist_ok=True)

        def notifier(result):
            self._emit_log(
                f"完成：{Path(result.get('audio_mp3', '')).name} "
                f"{result.get('audio_bytes', 0) / 1024 / 1024:.1f}MB"
            )

        try:
            self.svc = RecService(root, self.cfg, notifier=notifier)
            ok = self.svc.boot()
            self._emit_log("服务就绪" if ok else "服务启动失败，检查日志")
        except Exception as e:
            self._emit_log(f"服务启动异常：{e}")
            return

        self._start_http()

        fs = self.cfg.get("feishu") or {}
        if fs.get("enabled") and fs.get("app_id") and fs.get("app_secret"):
            self._start_feishu(fs)
        else:
            self.lbl_fs.setText("飞书未启用")

    def _stop_http(self):
        """重启前必须关掉旧监听，否则旧 server 仍在响应（Windows 允许双绑定）。"""
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                log.exception("关闭旧 HTTP 服务失败")
            self._httpd = None

    def _start_http(self):
        hc = self.cfg.get("http") or {}
        host = hc.get("host", "127.0.0.1")
        port = int(hc.get("port", 8898))
        token = hc.get("token", "")

        def work():
            try:
                from service import serve
                self._httpd = serve(self.svc, host, port, token)
                self._emit_log(f"本地接口已监听 {host}:{port}")
            except OSError as e:
                self._emit_log(f"本地接口未启动（端口 {port} 被占用）：{e}")
            except Exception as e:
                self._emit_log(f"本地接口启动失败：{e}")

        threading.Thread(target=work, daemon=True).start()

    def _start_feishu(self, fs: dict):
        def work():
            try:
                from feishu import FeishuBot
                # 注意参数顺序是 (app_id, app_secret, service)，
                # 与 main.py 的调用保持一致；写反会把 Service 当 app_id 传给 SDK。
                bot = FeishuBot(fs.get("app_id", ""), fs.get("app_secret", ""), self.svc)
                bot.start()
                self._feishu_bot = bot
                self._emit_log("飞书长连接已启用")
            except Exception as e:
                self._emit_log(f"飞书启动失败：{e}")

        threading.Thread(target=work, daemon=True).start()

    # ---------- 交互 ----------
    def on_pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择音频存储目录", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)
            self.log_signal.emit("目录已修改，点「保存并生效」后应用")

    # ---------- 飞书一键创建 ----------
    def on_feishu_setup(self):
        """打开引导向导：浏览器建应用 → 粘贴凭据 → 真校验 → 自动启用。

        飞书不开放「创建应用」的接口，所以这里不做假的一键建成，而是把
        用户必须在网页上点的几下之外的全部环节（开页面、填名、列权限、
        校验、回填、启用）都自动化掉。
        """
        dlg = FeishuSetupDialog(self)
        dlg.exec()
        res = dlg.result_payload
        if not res:
            return
        # 校验通过才走到这里：回填输入框、写配置、重启服务
        self.ed_app_id.setText(res.get("app_id", ""))
        self.ed_secret.setText(res.get("app_secret", ""))
        feishu_setup.apply_to_config(self.cfg, res["app_id"], res["app_secret"])
        try:
            save_config(self.cfg)
        except Exception:
            log.exception("保存飞书配置失败")
            QMessageBox.warning(self, "保存失败", "配置写入失败，请检查目录权限。")
            return
        self._refresh_feishu_label()
        self.log_signal.emit("飞书凭据校验通过，已启用长连接")
        if self._recording:
            # 录音中不重启服务（会删掉正在写的分片），提示用户稍后生效
            self.log_signal.emit("正在录音，飞书连接将在停止录音后生效")
            return
        self._restart_service()

    def on_save(self):
        # 录音中改配置会重启服务，而重启路径会 cancel() 删掉正在写的分片。
        # 审计实测：这是静默丢录音的路径，必须先拦住。
        if self._recording:
            QMessageBox.warning(
                self, "正在录音",
                "录音进行中不能改配置。\n请先停止录音再保存。")
            return
        new_dir = self.ed_dir.text().strip()
        if new_dir and not Path(new_dir).exists():
            QMessageBox.warning(self, "目录不存在", "请选择一个已存在的目录。")
            return
        self.cfg["recordings_dir"] = new_dir
        fs = self.cfg.setdefault("feishu", {})
        fs["app_id"] = self.ed_app_id.text().strip()
        fs["app_secret"] = self.ed_secret.text().strip()
        fs["enabled"] = bool(fs["app_id"] and fs["app_secret"])
        want_auto = bool(self.chk_autostart.isChecked())
        self.cfg["autostart"] = want_auto
        # 预录开关：改变它需要重启服务才生效（Recorder 构造时读取）
        self.cfg["always_on_mic"] = bool(self.chk_always_on_mic.isChecked())
        save_config(self.cfg)
        self._apply_autostart(want_auto, quiet=True)
        self._refresh_feishu_label()
        self.log_signal.emit("配置已保存，正在重启服务…")
        self._restart_service()

    def _restart_service(self):
        if self.svc:
            try:
                self.svc.stop_all()
            except Exception:
                pass
        self._stop_http()
        self._boot_service()

    # ---------- 跨线程投递 ----------
    # 后台线程持有 self，窗口销毁后 emit 会抛 RuntimeError: Signal source has been deleted。
    # 真实场景：点「开始录音」后立刻从托盘退出。统一走带守卫的包装。
    def _emit_log(self, msg: str):
        if self._dead:
            return
        try:
            self.log_signal.emit(msg)
        except RuntimeError:
            pass

    def _emit_state(self, d: dict):
        if self._dead:
            return
        try:
            self.state_signal.emit(d)
        except RuntimeError:
            pass

    # ---------- 录音 ----------
    def _on_main_clicked(self):
        if self._busy:
            return
        if self._recording:
            self.on_stop()
        else:
            self.on_start()

    def _tray_toggle(self):
        if self._busy:
            return
        if self._recording:
            self.on_stop()
        else:
            self.on_start()

    def on_start(self):
        if not self.svc or self._busy:
            return
        self._busy = True
        self._set_buttons(recording=False, busy=True)

        def work():
            try:
                r = self.svc.cmd_start("", use_prebuffer=True)
                msg = r.get("msg", "")
                self._emit_log(msg)
                # 麦克风被另一套程序占用时，弹框明确告知（仅 UI 层提示，服务层已拒绝）
                if not r.get("ok") and "麦克风被占用" in msg:
                    self._alert_signal.emit(msg)
                self._emit_state({"recording": bool(r.get("ok")),
                                  "error": None if r.get("ok") else msg})
            except Exception as e:
                self._emit_log(f"开始失败：{e}")
                self._emit_state({"recording": False})
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def on_stop(self):
        if not self.svc or self._busy:
            return
        self._busy = True
        self._set_buttons(recording=True, busy=True)

        def work():
            import traceback
            try:
                r = self.svc.cmd_stop()
                self._emit_log(r.get("msg", ""))
                res = self.svc.finish_session()
                if res and res.get("audio_mp3"):
                    name = Path(res["audio_mp3"]).name
                    mb = res.get("audio_bytes", 0) / 1024 / 1024
                    self._emit_log(f"已保存 {name}（{mb:.1f}MB）")
                    if not self._dead:
                        self.tray.notify("录音已保存", f"{name}（{mb:.1f}MB）")
                else:
                    self._emit_log("停止完成，但未生成 mp3，请查看日志")
                self._emit_state({"recording": False})
            except Exception as e:
                self._emit_log(f"停止失败：{type(e).__name__}: {e}")
                log.exception("stop failed")
                traceback.print_exc()
                self._emit_state({"recording": False})
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    # ---------- 状态刷新 ----------
    def _tick(self):
        if not self.svc:
            return
        try:
            st = self.svc.status()
        except Exception:
            return
        phase = st.get("phase", "idle")
        state = st.get("state", "idle")
        if state == "recording":
            self._recording = True
            el = st.get("elapsed_sec") or 0
            self.lbl_dot.setStyleSheet(f"color:{C_RED};font-size:15px;")
            self.lbl_state.setText("录音中")
            self.lbl_elapsed.setText(self._fmt(el))
            lv = st.get("level") or 0
            self.lbl_detail.setText(f"麦克风占用中　电平 {lv}")
            self._set_buttons(recording=True, busy=self._busy)
            self.tray.set_state("recording", elapsed=el)
        elif phase in ("archiving", "transcribing"):
            self._recording = False
            self.lbl_dot.setStyleSheet(f"color:#f08c00;font-size:15px;")
            self.lbl_state.setText("处理中")
            self.lbl_elapsed.setText("")
            self.lbl_detail.setText("正在合成 mp3…")
            self._set_buttons(recording=False, busy=True)
            self.tray.set_state("busy")
        else:
            self._recording = False
            self.lbl_dot.setStyleSheet(f"color:{C_SUB};font-size:15px;")
            self.lbl_state.setText("待命")
            self.lbl_elapsed.setText("")
            self.lbl_detail.setText("麦克风未占用，等待开始")
            self._set_buttons(recording=False, busy=self._busy)
            self.tray.set_state("idle")

    def _on_state(self, d: dict):
        if d.get("error"):
            self.log_signal.emit(str(d["error"]))
        self._set_buttons(recording=bool(d.get("recording")), busy=self._busy)

    def _set_buttons(self, recording: bool, busy: bool):
        self.btn_main.setEnabled(not busy)
        # 录音中禁止保存：保存会重启服务，重启路径会删掉正在写的分片
        self.btn_save.setEnabled(not busy and not recording)
        if recording:
            self.btn_main.setText("停止录音")
            self.btn_main.setStyleSheet(
                f"QPushButton{{background:{C_BG};color:{C_RED};border:2px solid {C_RED};"
                f"border-radius:12px;}}"
                f"QPushButton:hover{{background:#fef2f2;}}"
                f"QPushButton:disabled{{color:#c9ced6;border-color:#e5e7eb;}}"
            )
        else:
            self.btn_main.setText("开始录音")
            self.btn_main.setStyleSheet(
                f"QPushButton{{background:{C_BLUE};color:#fff;border:none;border-radius:12px;}}"
                f"QPushButton:hover{{background:{C_BLUE_HOVER};}}"
                f"QPushButton:disabled{{background:#c9ced6;color:#fff;}}"
            )

    @staticmethod
    def _fmt(sec: float) -> str:
        sec = int(sec or 0)
        h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _on_alert(self, msg: str):
        """麦克风被占用等需要用户知晓的提示，弹框而非只写日志。"""
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "无法录音", msg)
        except Exception:
            pass

    def _append_log(self, msg: str):
        lines = (self.txt_log.text() + "\n" + msg).strip().split("\n")
        self.txt_log.setText("\n".join(lines[-3:]))

    # ---------- 自启 ----------
    def _first_run_autostart(self):
        """首次运行默认打开自启；之后每次启动都对齐一次注册表。

        为什么每次都要对齐：用户可能把整个目录挪到别处（甚至换台机器再解压），
        这时注册表里还留着旧路径，开机会去启动一个不存在的位置 —— 静默失效。
        sync(True) 发现路径不一致就会重写，等于自愈。
        """
        first_run = "autostart" not in self.cfg
        want = bool(self.cfg.get("autostart", True))

        if first_run:
            self.cfg["autostart"] = want
            try:
                save_config(self.cfg)
            except Exception:
                log.exception("首次运行写配置失败")

        if not want:
            return

        ok, cmd = autostart.sync(True)
        self.tray.set_autostart(bool(ok))
        self.chk_autostart.blockSignals(True)
        self.chk_autostart.setChecked(bool(ok))
        self.chk_autostart.blockSignals(False)

        if first_run and ok:
            self.tray.notify("已设为开机自启",
                             "录音助手会随系统启动并常驻右下角，右键图标可退出。")
            self.log_signal.emit("首次运行：已自动设为开机自启")
        elif not ok:
            self.log_signal.emit("开机自启未生效（注册表写入被拒绝），可在设置里手动勾选")
        else:
            log.info("开机自启已就绪：%s", cmd)

    def _apply_autostart(self, want: bool, quiet: bool = False) -> bool:
        ok, cmd = autostart.sync(want)
        self.tray.set_autostart(want if ok else autostart.is_enabled())
        if not quiet:
            if ok:
                self.log_signal.emit("已开启开机自启" if want else "已关闭开机自启")
            else:
                self.log_signal.emit("开机自启设置失败（注册表写入被拒绝）")
        return ok

    def _on_autostart_toggled(self, on: bool):
        self.cfg["autostart"] = bool(on)
        try:
            save_config(self.cfg)
        except Exception:
            log.exception("保存自启配置失败")
        ok = self._apply_autostart(bool(on))
        self.chk_autostart.blockSignals(True)
        self.chk_autostart.setChecked(bool(on) if ok else autostart.is_enabled())
        self.chk_autostart.blockSignals(False)

    def _open_folder(self):
        rd = Path(self.cfg.get("recordings_dir", "recordings"))
        p = rd if rd.is_absolute() else ROOT_DIR / rd
        try:
            os.startfile(str(p))          # noqa: S606  Windows 专用
        except Exception:
            self.log_signal.emit(f"打开目录失败：{p}")

    # ---------- 窗口 ----------
    def _on_command(self, cmd: str):
        """在主线程处理单实例指令（信号跨线程投递，Qt 自动排队）。"""
        log.info("收到实例指令: %s", cmd)
        if cmd == "show":
            self.show_window()
        elif cmd == "quit":
            self.request_quit()

    def show_window(self):
        # Qt 的 show() 只置内部标志，后台启动的窗口在 Win32 层可能仍是隐藏态
        # （实测 isVisible()=True 但 IsWindowVisible()=False）。
        # 所以显式走一次 Win32 ShowWindow，确保真的出现在屏幕上。
        if not self.isVisible():
            self.show()
        self.showNormal()
        try:
            import ctypes
            hwnd = int(self.winId())
            ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            log.exception("Win32 强制显示失败，退回 Qt 方式")
        self.raise_()
        self.activateWindow()
        log.info("show_window: visible=%s", self.isVisible())

    def closeEvent(self, ev):
        """关窗 = 收进托盘。真正退出走 request_quit()。"""
        if self._really_quit:
            self._shutdown()
            ev.accept()
            return
        ev.ignore()
        self.hide()
        self.tray.notify("录音助手仍在后台运行",
                         "已收进右下角托盘。单击图标可重新打开，右键可退出。")

    def request_quit(self):
        if self._recording:
            r = QMessageBox.question(
                self, "正在录音", "当前正在录音，退出会丢弃本次录音。确定退出？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return
            try:
                self.svc.cmd_cancel()
            except Exception:
                pass
        self._really_quit = True
        self._shutdown()
        self.tray.hide()
        QApplication.instance().quit()

    def _shutdown(self):
        # 先置 _dead，再停服务：后台线程可能正好在收尾时 emit
        self._dead = True
        self.timer.stop()
        self._stop_http()
        if self.svc:
            try:
                self.svc.stop_all()
            except Exception:
                pass


class FeishuSetupDialog(QDialog):
    """飞书机器人创建向导。

    为什么不是「点一下就建好」：飞书开放平台没有创建应用的 API。
    这里把能自动化的部分全部自动化（打开页面、复制应用名、列权限、校验、
    回填），只在必须由用户在网页上操作的两步让用户动手，并明确说清楚。
    """

    verified = Signal(object)   # 跨线程回主线程

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("创建飞书机器人")
        self.setMinimumWidth(560)
        self.result_payload: dict | None = None
        self._pending: tuple[str, str] | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        head = QLabel("创建飞书机器人")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        head.setFont(f)
        lay.addWidget(head)

        body = QLabel(feishu_setup.guide_text(feishu_setup.DEFAULT_APP_NAME))
        body.setWordWrap(True)
        body.setStyleSheet(f"color:{C_SUB};font-size:12px;line-height:1.6;")
        lay.addWidget(body)

        # 两个动作按钮：打开创建页 / 打开权限页（后者要等有 app_id 才有意义）
        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        self.btn_open_create = QPushButton("① 打开创建应用页")
        self.btn_open_create.setObjectName("secondary")
        self.btn_open_create.setMinimumHeight(32)
        self.btn_open_create.setCursor(Qt.PointingHandCursor)
        self.btn_open_create.clicked.connect(self._open_create)
        act_row.addWidget(self.btn_open_create)

        self.btn_copy_name = QPushButton("② 复制应用名")
        self.btn_copy_name.setObjectName("secondary")
        self.btn_copy_name.setMinimumHeight(32)
        self.btn_copy_name.setCursor(Qt.PointingHandCursor)
        self.btn_copy_name.clicked.connect(self._copy_name)
        act_row.addWidget(self.btn_copy_name)
        act_row.addStretch()
        lay.addLayout(act_row)

        # 凭据输入
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(9)
        form.addWidget(QLabel("App ID"), 0, 0)
        self.ed_id = QLineEdit()
        self.ed_id.setPlaceholderText("cli_xxxxxxxxxxxx")
        self.ed_id.setMinimumHeight(30)
        form.addWidget(self.ed_id, 0, 1)
        form.addWidget(QLabel("App Secret"), 1, 0)
        self.ed_secret = QLineEdit()
        self.ed_secret.setEchoMode(QLineEdit.Password)
        self.ed_secret.setPlaceholderText("在「凭证与基础信息」里复制")
        self.ed_secret.setMinimumHeight(30)
        form.addWidget(self.ed_secret, 1, 1)
        form.setColumnStretch(1, 1)
        lay.addLayout(form)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet(f"color:{C_SUB};font-size:11px;")
        lay.addWidget(self.lbl_status)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.btn_verify = QPushButton("③ 校验并启用")
        self.btn_verify.setMinimumHeight(36)
        self.btn_verify.setMinimumWidth(130)
        self.btn_verify.setCursor(Qt.PointingHandCursor)
        self.btn_verify.setStyleSheet(
            f"QPushButton{{background:{C_BLUE};color:#fff;border:none;border-radius:8px;}}"
            f"QPushButton:hover{{background:{C_BLUE_HOVER};}}"
            f"QPushButton:disabled{{background:#c9ced6;color:#fff;}}")
        self.btn_verify.clicked.connect(self._verify)
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("secondary")
        btn_cancel.setMinimumHeight(36)
        btn_cancel.setCursor(Qt.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)
        foot.addStretch()
        foot.addWidget(btn_cancel)
        foot.addWidget(self.btn_verify)
        lay.addLayout(foot)

        self.verified.connect(self._on_verified)

        # 预填已有凭据，避免用户重填
        cfg = load_config()
        fs = cfg.get("feishu") or {}
        if fs.get("app_id"):
            self.ed_id.setText(fs.get("app_id", ""))
        if fs.get("app_secret"):
            self.ed_secret.setText(fs.get("app_secret", ""))

    # ---- 动作 ----
    def _open_create(self):
        """打开飞书创建应用页，同时把应用名放剪贴板。"""
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(feishu_setup.CREATE_URL))
        except Exception:
            log.exception("打开创建页失败")
        self._copy_name(silent=True)
        self.lbl_status.setText("已打开开发者后台，应用名已复制到剪贴板，直接粘贴即可。")

    def _copy_name(self, silent: bool = False):
        try:
            QApplication.clipboard().setText(feishu_setup.DEFAULT_APP_NAME)
            if not silent:
                self.lbl_status.setText(
                    f"已复制「{feishu_setup.DEFAULT_APP_NAME}」，到后台新建应用时粘贴。")
        except Exception:
            log.exception("写剪贴板失败")

    def _verify(self):
        app_id = self.ed_id.text().strip()
        app_secret = self.ed_secret.text().strip()
        if not app_id or not app_secret:
            self.lbl_status.setText("App ID 和 App Secret 都要填。")
            return
        self.btn_verify.setEnabled(False)
        self.btn_verify.setText("校验中…")
        self.lbl_status.setStyleSheet(f"color:{C_SUB};font-size:11px;")
        self.lbl_status.setText("正在连接飞书服务器校验凭据，最长 8 秒…")
        self._pending = (app_id, app_secret)
        # 网络请求必须离开主线程，否则窗口会假死
        feishu_setup.verify_async(app_id, app_secret, self.verified.emit)

    def _on_verified(self, res):
        self.btn_verify.setEnabled(True)
        self.btn_verify.setText("③ 校验并启用")
        if getattr(res, "ok", False):
            app_id, app_secret = self._pending or ("", "")
            self.result_payload = {"app_id": app_id, "app_secret": app_secret}
            self.lbl_status.setStyleSheet(f"color:{C_GREEN};font-size:12px;")
            self.lbl_status.setText("✓ 凭据有效，已启用。")
            # 让用户看到成功再关窗，避免一闪而过
            QTimer.singleShot(600, self.accept)
        else:
            self.lbl_status.setStyleSheet(f"color:{C_RED};font-size:12px;")
            self.lbl_status.setText(getattr(res, "error", "校验失败"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    start_hidden = "--background" in argv
    if "--show" in argv:
        start_hidden = False

    setup_logging()

    # ---- 单实例：先抢锁 ----
    # 抢不到说明已有实例在跑。此时把「显示窗口」指令送过去，自己退出。
    # 只有显式带 --background 且已有实例时，才静默退出（开机自启重复触发的情况）。
    lock = InstanceLock(on_command=lambda cmd: None)
    have_lock = lock.acquire()
    if not have_lock:
        sent = notify_existing("show")
        if sent:
            log.info("已有实例在运行，已请求其显示窗口，本进程退出")
        else:
            # 锁拿不到但指令也送不出去：多半是上次异常退出留下的残留 socket。
            # 不硬撑起第二个实例（会抢麦克风），如实报错退出。
            log.error("检测到残留的单实例锁但无法通信，请稍后重试或重启系统")
        return 0 if sent else 1

    app = QApplication(sys.argv)
    app.setApplicationName("录音助手")
    app.setApplicationDisplayName("录音助手")
    app.setWindowIcon(app_icon())
    # 关掉窗口不能退出进程 —— 否则托盘就没了
    app.setQuitOnLastWindowClosed(False)

    from PySide6.QtWidgets import QSystemTrayIcon
    tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
    if not tray_ok:
        # 托盘不可用时必须让窗口自己站出来：否则进程被 quitOnLastWindowClosed=False
        # 吊着，用户既没有图标也没有窗口，只能去任务管理器里找。
        log.warning("系统托盘不可用，改为直接显示窗口（关闭窗口后进程仍会驻留）")

    w = RecWindow(start_hidden=start_hidden)

    # 指令从监听线程投递到主线程：用 Signal，Qt 会做队列投递。
    lock._on_command = w.cmd_signal.emit
    lock.start()
    app._instance_lock = lock          # 保引用，防止被回收

    # 后台模式本该只留托盘；但托盘不可用或图标 show() 后不可见时，
    # 静默驻留等于让应用变成隐形进程 —— 这时候宁可弹出窗口。
    if start_hidden and tray_ok and w._tray_visible:
        log.info("后台模式启动，仅驻留托盘")
    else:
        if start_hidden:
            log.info("托盘不可用或图标未显示，改为显示窗口")
        w.show()

    rc = app.exec()
    lock.release()
    return rc


if __name__ == "__main__":
    sys.exit(main())
