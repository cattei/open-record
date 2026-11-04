"""
组装分发包
==========
把 app / runtime / 配置模板 / 启动脚本 组装成可解压即用的发行目录，
并打成 zip。

用法：python packaging/package.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DIST_NAME = "Project-record"
STAGE = PROJECT / "dist" / DIST_NAME
ZIP_OUT = PROJECT / "dist" / f"Project-record-{datetime.now():%Y%m%d}.zip"

# 默认值真源在 app/config.py，这里不再抄第四份（原来端口写 8899 已漂移）。
sys.path.insert(0, str(PROJECT / "app"))
from config import DEFAULTS as DEFAULT_CONFIG  # noqa: E402


def log(m):
    print(m, flush=True)


def sz(p: Path) -> float:
    if not p.exists():
        return 0.0
    if p.is_file():
        return p.stat().st_size / 1024 / 1024
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def main():
    log("=== 1. 准备发行目录 ===")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    # app 源码（排除缓存）
    shutil.copytree(PROJECT / "app", STAGE / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # runtime
    rt = PROJECT / "runtime" / "python"
    if not rt.exists():
        log("❌ runtime 不存在，请先运行 build_runtime.py")
        sys.exit(1)
    shutil.copytree(rt, STAGE / "runtime" / "python",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 启动脚本
    for f in ("启动录音助手.bat",):
        s = PROJECT / f
        if s.exists():
            shutil.copy2(s, STAGE / f)

    # 配置模板
    cfg = STAGE / "config.json"
    cfg.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    # 目录占位
    for d in ("recordings", "logs"):
        (STAGE / d).mkdir(exist_ok=True)
        (STAGE / d / ".gitkeep").write_text("", encoding="utf-8")

    # 文档
    docs = PROJECT / "docs"
    if docs.exists():
        for f in docs.glob("*.md"):
            shutil.copy2(f, STAGE / f.name)

    # 许可证与项目说明
    for name in ("LICENSE", "README.md"):
        src = PROJECT / name
        if src.exists():
            shutil.copy2(src, STAGE / name)

    log(f"  {sz(STAGE):.1f} MB")

    log("\n=== 2. 结构 ===")
    for item in sorted(STAGE.iterdir()):
        t = "DIR " if item.is_dir() else "FILE"
        log(f"  {t} {item.name:28s} {sz(item):7.1f} MB")

    log("\n=== 3. 打 zip ===")
    if ZIP_OUT.exists():
        ZIP_OUT.unlink()
    with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(STAGE):
            for f in files:
                full = Path(root) / f
                arc = Path(DIST_NAME) / full.relative_to(STAGE)
                z.write(full, arc.as_posix())
    log(f"  {ZIP_OUT.name}  {ZIP_OUT.stat().st_size/1024/1024:.1f} MB")

    log("\n=== 4. 完整性检查 ===")
    need = [
        "app/gui.py", "app/launcher.py", "app/service.py", "app/recorder.py",
        "app/tray.py", "app/singleton.py", "app/autostart.py",
        "app/config.py", "app/miclock.py",
        "app/assets/icon.ico", "app/assets/icon_64.png", "app/assets/icon_rec_64.png",
        "runtime/python/python.exe", "runtime/python/pythonw.exe",
        "runtime/python/Lib/site-packages/sounddevice.py",
        "runtime/python/Lib/site-packages/PySide6/Qt6Core.dll",
        "config.json", "启动录音助手.bat",
    ]
    ok = True
    for n in need:
        p = STAGE / n
        e = p.exists()
        log(f"  {'OK  ' if e else 'MISS'} {n}")
        if not e:
            ok = False
    log("\n结果: " + ("分发包就绪" if ok else "有缺失"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
