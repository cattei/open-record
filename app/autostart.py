"""
开机自启管理
============
写当前用户注册表 HKCU\\...\\Run，**不需要管理员权限**，也不弹 UAC。

设计取舍：
- 用 HKCU 而非 HKLM：免提权，普通用户双击即生效。
- 用 pythonw.exe 而非 .bat：bat 会闪一个黑窗，「无感」就废了。
- 用绝对路径 + 双引号：路径含空格（如「程序 Files」）也不会失效。
"""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ProjectRecord"
FLAG = "--background"


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def project_root() -> Path:
    """发行结构下是包根（含 runtime/），源码结构下就是 app/ 本身。"""
    d = app_dir()
    parent = d.parent
    if (parent / "runtime").exists() or (parent / "config.json").exists():
        return parent
    return d


def pythonw() -> Path:
    """优先 pythonw.exe —— 它不带控制台，托盘常驻时不该有黑窗。"""
    root = project_root()
    cand = root / "runtime" / "python" / "pythonw.exe"
    if cand.exists():
        return cand
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return w
    return exe


def command() -> str:
    """注册表里写入的命令行。"""
    return f'"{pythonw()}" "{app_dir() / "launcher.py"}" {FLAG}'


def _open(key, access):
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, access)


def is_enabled() -> bool:
    try:
        with _open(RUN_KEY, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def current_value() -> str:
    try:
        with _open(RUN_KEY, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return str(val)
    except OSError:
        return ""


def enable() -> bool:
    """返回是否真的写入成功（供 GUI 如实反馈，不假装成功）。"""
    cmd = command()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
        return True
    except OSError:
        return False


def disable() -> bool:
    try:
        with _open(RUN_KEY, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def sync(want: bool) -> tuple[bool, str]:
    """把注册表状态对齐到 want。返回 (是否已达成, 实际命令)。"""
    if want:
        if is_enabled() and current_value() == command():
            return True, current_value()
        return enable(), command()
    return disable(), ""


def self_check() -> dict:
    """诊断用：把关键事实一次性打印出来。"""
    return {
        "pythonw": str(pythonw()),
        "pythonw_exists": pythonw().exists(),
        "launcher": str(app_dir() / "launcher.py"),
        "command": command(),
        "enabled": is_enabled(),
        "current_value": current_value(),
        "value_matches": current_value() == command(),
    }


if __name__ == "__main__":
    import json
    act = sys.argv[1] if len(sys.argv) > 1 else "status"
    if act == "enable":
        ok, cmd = sync(True)
    elif act == "disable":
        ok, cmd = sync(False)
    else:
        ok, cmd = True, current_value()
    print(json.dumps({**self_check(), "action_ok": ok}, ensure_ascii=False, indent=2))
