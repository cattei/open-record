"""
构建可分发 runtime
==================
从本机 Python 环境抽取录音 GUI 所需的最小依赖，生成 runtime/python。

设计要点：
- 标准库整目录复制（排除测试/无用大包），避免手工清单遗漏模块
- 第三方依赖列白名单，飞书 SDK 的依赖用递归探测自动补齐
- 只保留 PySide6 的 Core/Gui/Widgets/Network 四个模块与必须插件

用法：python packaging/build_runtime.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(os.environ.get("REC_SRC_PYTHON", r"C:\env\python"))
PROJECT = Path(__file__).resolve().parent.parent
RT = PROJECT / "runtime" / "python"
APP = PROJECT / "app"

# Python 本体必需文件
ROOT_FILES = [
    "python.exe", "pythonw.exe", "python313.dll", "python3.dll",
    "vcruntime140.dll", "vcruntime140_1.dll",
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "concrt140.dll",
]

# 标准库中排除（体积大且用不到）
EXCLUDE_PKG = {
    "site-packages", "test", "tests", "idlelib", "tkinter", "turtledemo",
    "ensurepip", "distutils", "lib2to3", "venv", "pydoc_data", "curses",
    "dbm", "msilib",
}

# 第三方白名单（基础）
SITE_BASE = [
    "sounddevice.py",
    "_sounddevice.py",
    "_cffi_backend.cp313-win_amd64.pyd",
    "lameenc.cp313-win_amd64.pyd",
    "_sounddevice_data",
    "numpy",
    "numpy.libs",
    "lark_oapi",
]

# PySide6 只保留必需
PYSIDE6_FILES = [
    "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Network.dll",
    "pyside6.abi3.dll",
    "QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd", "QtNetwork.pyd",
    "__init__.py",
]
PYSIDE6_DIRS = ["glue"]
PYSIDE6_PLUGINS = [
    ("platforms", ["qwindows.dll"]),
    ("styles", ["qwindowsvistastyle.dll"]),
    ("imageformats", ["qico.dll", "qjpeg.dll"]),
]
SHIBOKEN_FILES = [
    "shiboken6.abi3.dll", "__init__.py", "shiboken6.abi3.lib",
    "msvcp140.dll", "msvcp140_2.dll", "vcruntime140.dll",
    "concrt140.dll", "vcamp140.dll", "vccorlib140.dll", "vcomp140.dll",
]


def log(m):
    print(m, flush=True)


def sz(p: Path) -> float:
    if not p.exists():
        return 0.0
    if p.is_file():
        return p.stat().st_size / 1024 / 1024
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def ig(*extra):
    return shutil.ignore_patterns("__pycache__", "*.pyc", "test", "tests", *extra)


def main():
    if not (SRC / "python.exe").exists():
        log(f"❌ 源 Python 不存在：{SRC}")
        sys.exit(1)

    log("=== 1. 清理 ===")
    if RT.exists():
        shutil.rmtree(RT)
    RT.mkdir(parents=True, exist_ok=True)

    log("\n=== 2. Python 本体 ===")
    for f in ROOT_FILES:
        s = SRC / f
        if s.exists():
            shutil.copy2(s, RT / f)
            log(f"  {f:24s} {sz(RT / f):6.2f} MB")

    log("\n=== 3. DLLs ===")
    d = SRC / "DLLs"
    if d.exists():
        shutil.copytree(d, RT / "DLLs", dirs_exist_ok=True, ignore=ig())
        log(f"  {sz(RT / 'DLLs'):.1f} MB")

    log("\n=== 4. 标准库（整目录）===")
    lib_src, lib_dst = SRC / "Lib", RT / "Lib"
    lib_dst.mkdir(exist_ok=True)
    n = 0
    for item in sorted(lib_src.iterdir()):
        if item.name in EXCLUDE_PKG or item.name.startswith("__pycache__"):
            continue
        dst = lib_dst / item.name
        if dst.exists():
            continue
        try:
            if item.is_dir():
                shutil.copytree(item, dst, ignore=ig())
            elif item.suffix in (".py", ".pyd", ".dll"):
                shutil.copy2(item, dst)
            n += 1
        except Exception as e:
            log(f"  跳过 {item.name}: {e}")
    log(f"  {n} 项，{sz(lib_dst):.1f} MB")

    log("\n=== 5. 第三方依赖 ===")
    sp_src, sp_dst = SRC / "Lib" / "site-packages", RT / "Lib" / "site-packages"
    sp_dst.mkdir(parents=True, exist_ok=True)
    for name in SITE_BASE:
        s = sp_src / name
        if not s.exists():
            log(f"  ⚠️  {name} 缺失")
            continue
        if s.is_dir():
            shutil.copytree(s, sp_dst / name, ignore=ig())
        else:
            shutil.copy2(s, sp_dst / name)
        log(f"  {name:34s} {sz(sp_dst / name):6.1f} MB")

    log("\n=== 6. PySide6 精简 ===")
    ps6_src, ps6_dst = sp_src / "PySide6", sp_dst / "PySide6"
    ps6_dst.mkdir(parents=True, exist_ok=True)
    for f in PYSIDE6_FILES:
        s = ps6_src / f
        if s.exists():
            shutil.copy2(s, ps6_dst / f)
    for dn in PYSIDE6_DIRS:
        s = ps6_src / dn
        if s.exists():
            shutil.copytree(s, ps6_dst / dn, ignore=ig(), dirs_exist_ok=True)
    for sub, files in PYSIDE6_PLUGINS:
        for f in files:
            s = ps6_src / "plugins" / sub / f
            if s.exists():
                (ps6_dst / "plugins" / sub).mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, ps6_dst / "plugins" / sub / f)
    log(f"  {sz(ps6_dst):.1f} MB")

    log("\n=== 7. shiboken6 ===")
    sh_src, sh_dst = sp_src / "shiboken6", sp_dst / "shiboken6"
    sh_dst.mkdir(parents=True, exist_ok=True)
    for f in SHIBOKEN_FILES:
        s = sh_src / f
        if s.exists():
            shutil.copy2(s, sh_dst / f)
    log(f"  {sz(sh_dst):.1f} MB")

    log("\n=== 8. 递归补齐第三方依赖 ===")

    def run_rt(code, timeout=90):
        r = subprocess.run([str(RT / "python.exe"), "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(APP))
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    for mod in ("lark_oapi", "PySide6.QtWidgets", "sounddevice", "numpy", "lameenc"):
        for _ in range(10):
            rc, out = run_rt(f"import {mod}; print('ok')")
            if rc == 0:
                log(f"  ✅ {mod}")
                break
            miss = None
            for line in out.splitlines():
                if "No module named" in line:
                    miss = line.split("'")[1].split(".")[0]
            if not miss:
                log(f"  ❌ {mod} 无法解析缺失依赖：{out[-150:]}")
                break
            s = sp_src / miss
            if not s.exists():
                cands = list(sp_src.glob(f"{miss}.py"))
                s = cands[0] if cands else None
            if s and s.exists():
                if s.is_dir():
                    shutil.copytree(s, sp_dst / miss, ignore=ig(), dirs_exist_ok=True)
                else:
                    shutil.copy2(s, sp_dst / s.name)
                log(f"    + 补 {miss}")
            else:
                log(f"    ❌ {miss} 源环境没有")
                break

    log(f"\n=== runtime 总计 {sz(RT):.1f} MB ===")

    log("\n=== 9. 最终校验 ===")
    ok = True
    for m in ("service", "recorder", "merge", "feishu", "gui"):
        rc, out = run_rt(f"import {m}; print('ok')")
        log(f"  {'✅' if rc == 0 else '❌'} {m}")
        if rc != 0:
            ok = False
            log("      " + out.splitlines()[-1][:120] if out else "")
    log("\n结果: " + ("全部可用" if ok else "存在缺失"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
