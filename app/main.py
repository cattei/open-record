"""录音助手服务入口：python app/main.py"""
from __future__ import annotations

import json
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

# Windows 控制台中文乱码修正
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# HuggingFace 国内镜像 + 禁用 Xet（镜像不支持，会 401）
import os
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def setup_logging(level=logging.INFO):
    log_dir = APP_DIR.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    # 工作区在网络盘上，同步客户端可能短暂锁住日志文件，这里重试若干次
    fh = None
    for attempt in range(12):
        try:
            fh = RotatingFileHandler(log_dir / "service.log", maxBytes=5 * 1024 * 1024,
                                     backupCount=5, encoding="utf-8")
            break
        except PermissionError:
            time.sleep(1)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = []
    if fh:
        fh.setFormatter(fmt)
        root.handlers.append(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.handlers.append(ch)
    if fh is None:
        root.warning("日志文件被占用，本次仅输出到控制台")


def make_file_notifier():
    def notify(result: dict):
        src_path = result.get("transcript_md")
        if not src_path:
            return          # 纯录音模式，没有逐字稿
        out = APP_DIR.parent / "transcripts" / f"{result['session_id']}.md"
        out.parent.mkdir(exist_ok=True)
        src = Path(src_path)
        out.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        logging.getLogger("notify").info("逐字稿副本已归档 %s", out)
    return notify


def main():
    setup_logging()
    log = logging.getLogger("main")
    from service import RecService, load_config, serve

    cfg = load_config(APP_DIR.parent / "config.json")
    log.info("配置加载完成: model=%s port=%s", cfg["asr"]["model"], cfg["http"]["port"])

    svc = RecService(APP_DIR.parent, cfg, notifier=make_file_notifier())
    ok = svc.boot()
    if not ok:
        log.error("音频不可用，请检查麦克风占用或权限")

    httpd = serve(svc, cfg["http"]["host"], cfg["http"]["port"], cfg["http"]["token"])

    # 飞书长连接
    fcfg = cfg.get("feishu") or {}
    if fcfg.get("enabled") and fcfg.get("app_id") and fcfg.get("app_secret"):
        try:
            from feishu import FeishuBot
            bot = FeishuBot(fcfg["app_id"], fcfg["app_secret"], svc)
            bot.start()
            log.info("飞书长连接已启用 app_id=%s", fcfg["app_id"])
        except Exception:
            log.exception("飞书通道启动失败，服务继续以本地接口运行")
    else:
        log.info("飞书通道未启用")

    stopping = {"flag": False}

    def _shutdown(signum, frame):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        log.info("收到退出信号，正在收尾…")
        try:
            if svc.recorder.status()["state"] == "recording":
                log.info("检测到录音中，先停止并出稿")
                svc.cmd_stop()
                svc.finalize()
        except Exception:
            log.exception("退出收尾异常")
        try:
            httpd.shutdown()
            svc.recorder.stop_stream()
        except Exception:
            pass
        log.info("已退出")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        signal.signal(signal.SIGBREAK, _shutdown)
    except Exception:
        pass

    log.info("录音助手已启动，等待指令…")
    while True:
        try:
            svc.watchdog()
        except Exception:
            log.exception("watchdog 异常")
        time.sleep(2)


if __name__ == "__main__":
    main()
