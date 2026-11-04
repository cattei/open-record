"""
系统托盘
========
右下角常驻图标。单击=打开界面，右键=菜单（含退出）。

状态用图标颜色区分：蓝=待命，红=录音中。鼠标悬停提示里带实时计时，
不打开窗口也能知道录了多久。
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent / "assets"


def _icon(name: str) -> QIcon:
    """优先用 64px（托盘在高 DPI 下清晰），缺图时退回 .ico。"""
    for cand in (f"{name}_64.png", f"{name}_32.png", f"{name}_256.png"):
        p = ASSETS / cand
        if p.exists():
            return QIcon(str(p))
    p = ASSETS / "icon.ico"
    if p.exists():
        return QIcon(str(p))
    return QIcon()


def app_icon() -> QIcon:
    for cand in ("icon_256.png", "icon_128.png", "icon.ico"):
        p = ASSETS / cand
        if p.exists():
            return QIcon(str(p))
    return QIcon()


def _fmt(sec: float) -> str:
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Tray(QObject):
    """托盘图标 + 右键菜单。所有动作以信号形式抛给主窗口处理。"""

    show_requested = Signal()
    quit_requested = Signal()
    toggle_record = Signal()
    autostart_toggled = Signal(bool)
    open_folder_requested = Signal()

    def __init__(self, autostart_on: bool, parent=None):
        super().__init__(parent)
        self._icon_idle = _icon("icon")
        self._icon_rec = _icon("icon_rec")
        self._state = "idle"
        self._last_text = ""

        self.tray = QSystemTrayIcon(self._icon_idle, self)
        self.tray.setToolTip("录音助手 · 待命")

        menu = QMenu()
        self.act_show = QAction("显示界面", self)
        self.act_show.triggered.connect(self.show_requested.emit)
        menu.addAction(self.act_show)
        menu.addSeparator()

        self.act_record = QAction("开始录音", self)
        self.act_record.triggered.connect(self.toggle_record.emit)
        menu.addAction(self.act_record)
        menu.addSeparator()

        self.act_autostart = QAction("开机自启", self)
        self.act_autostart.setCheckable(True)
        self.act_autostart.setChecked(bool(autostart_on))
        self.act_autostart.toggled.connect(self.autostart_toggled.emit)
        menu.addAction(self.act_autostart)

        self.act_folder = QAction("打开录音目录", self)
        self.act_folder.triggered.connect(self.open_folder_requested.emit)
        menu.addAction(self.act_folder)
        menu.addSeparator()

        self.act_quit = QAction("退出", self)
        self.act_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(self.act_quit)

        self.menu = menu
        self.tray.setContextMenu(menu)
        # 单击直接打开界面；右键由 contextMenu 处理
        self.tray.activated.connect(self._on_activated)

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_requested.emit()

    def show(self):
        self.tray.show()

    def hide(self):
        self.tray.hide()

    def set_autostart(self, on: bool):
        """外部改了自启状态时同步勾选，且不要回触发 toggled。"""
        self.act_autostart.blockSignals(True)
        self.act_autostart.setChecked(bool(on))
        self.act_autostart.blockSignals(False)

    def notify(self, title: str, body: str, ms: int = 4000):
        if QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, body, self._icon_idle, ms)

    def set_state(self, state: str, elapsed: float = 0.0, busy: bool = False):
        """state: idle | recording | busy"""
        if busy:
            state = "busy"
        # 录音中计时每 tick 都在变，不能早退；其余状态无变化时不必重设图标
        if state == self._state and state != "recording":
            return
        self._state = state
        if state == "recording":
            self.tray.setIcon(self._icon_rec)
            text = f"录音助手 · 录音中 {_fmt(elapsed)}"
            self.act_record.setText("停止录音")
        elif state == "busy":
            self.tray.setIcon(self._icon_idle)
            text = "录音助手 · 处理中…"
            self.act_record.setText("处理中…")
        else:
            self.tray.setIcon(self._icon_idle)
            text = "录音助手 · 待命"
            self.act_record.setText("开始录音")
        self.act_record.setEnabled(state != "busy")
        if text != self._last_text:
            self.tray.setToolTip(text)
            self._last_text = text
