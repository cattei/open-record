"""
open-record 主入口
====================
启动录音助手（托盘常驻）。

用法：
    首次使用（会装开机自启，然后进托盘）：
        双击 启动录音助手.bat
    之后开机自动在后台启动，无需手动干预。
    想手动打开界面：再双击一次 bat（单实例会把已有窗口叫出来）。

命令行：
    --background   只进托盘，不弹窗（开机自启用它）
    --show         强制显示窗口
    --install      只安装开机自启，不启动
    --uninstall    只移除开机自启
    --status       打印自启诊断信息
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# app 目录加入 sys.path，保证扁平导入可用
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
os.chdir(APP_DIR)


def check_dependencies() -> list[str]:
    """检查运行所需依赖，返回缺失列表。

    lark_oapi 是可选的：只在飞书长连接启用时才需要，不列入必需项。
    """
    missing = []
    for mod, pkg in (
        ("PySide6", "PySide6"),
        ("sounddevice", "sounddevice"),
        ("numpy", "numpy"),
        ("lameenc", "lameenc"),
    ):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def main():
    args = sys.argv[1:]

    import autostart

    if "--status" in args:
        print(json.dumps(autostart.self_check(), ensure_ascii=False, indent=2))
        return 0
    if "--install" in args:
        ok, cmd = autostart.sync(True)
        print(("已安装开机自启: " if ok else "安装失败: ") + cmd)
        return 0 if ok else 1
    if "--uninstall" in args:
        ok, _ = autostart.sync(False)
        print("已移除开机自启" if ok else "移除失败")
        return 0 if ok else 1

    missing = check_dependencies()
    if missing:
        # 注意：发行包内置运行时没有 pip，不能建议 pip install。
        msg = (
            "缺少运行依赖：" + "、".join(missing) + "\n\n"
            "请重新解压完整发行包（必须保留 runtime\\python 全部子目录），\n"
            "不要单独移动 app 目录。"
        )
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication(sys.argv)
            QMessageBox.critical(None, "依赖缺失", msg)
        except Exception:
            print(msg)
        return 1

    from gui import main as gui_main
    return gui_main(args)


if __name__ == "__main__":
    sys.exit(main())
