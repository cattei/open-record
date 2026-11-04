"""
录音助手主服务
==============
常驻进程，负责：
  1. 持有音频流（待命中，环形预录）
  2. 接收指令（本地 HTTP / 后续可挂飞书长连接）
  3. 录制、分片、增量转写
  4. 结束时生成逐字稿，交给通知适配器投递

对外 HTTP 接口（默认仅监听 127.0.0.1:8898）：
  GET  /status          当前状态
  POST /start           开始录音   body: {"label": "XXX"}
  POST /stop            停止并出稿
  POST /cancel          丢弃本次
  GET  /health          存活探测
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from asr import AsrEngine, SessionTranscriber
from config import DEFAULTS as DEFAULT_CONFIG, load as _cfg_load, normalize as _cfg_normalize
from recorder import Recorder
from miclock import MicLock as _MicLock, write_holder as miclock_write_holder, read_holder as miclock_read_holder, clear_holder as miclock_clear_holder

log = logging.getLogger("service")


def load_config(path: Path) -> dict:
    """兼容旧入口；真源与深合并逻辑在 config.py。"""
    return _cfg_load(path)


class RecService:
    def __init__(self, root: Path, cfg: dict, notifier=None):
        self.root = Path(root)
        # 无论 cfg 从哪来（GUI / main.py / 测试），先补齐默认值。
        # 原实现直接 cfg["prebuffer_sec"] / cfg["max_hours"] / cfg["asr"]["model"]，
        # 用户手写部分配置会 KeyError 崩溃（审计缺陷 8）。
        self.cfg = _cfg_normalize(cfg)
        # recordings_dir 允许是绝对路径（GUI 里用户选的目录）；相对路径则相对 root
        _rd = Path(self.cfg["recordings_dir"])
        self.rec_dir = (_rd if _rd.is_absolute() else (self.root / _rd)).resolve()
        self.rec_dir.mkdir(parents=True, exist_ok=True)

        # ASR 引擎懒加载：加载 faster-whisper + ctranslate2 会提交大量虚拟内存
        # （实测 1325MB），而转写默认关闭，不该为它付出代价。
        # 只有 transcribe=True 且真的要转写时才实例化。
        self._engine_cfg = self.cfg["asr"]
        self._engine = None
        self.recorder = Recorder(
            base_dir=self.rec_dir,
            prebuffer_sec=self.cfg["prebuffer_sec"],
            chunk_sec=self.cfg["chunk_sec"],
            device=self.cfg.get("device"),
            on_chunk=self._on_chunk,
            exclusive=self.cfg.get("exclusive", False),
            keep_native_rate=self.cfg.get("keep_native_rate", False),
            always_on_mic=self.cfg.get("always_on_mic", False),
        )
        self.notifier = notifier

        self._asr_q: list[tuple[int, Path, str]] = []
        self._asr_lock = threading.Condition()
        self._asr_thread: threading.Thread | None = None
        self._transcriber: SessionTranscriber | None = None
        self._phase = "idle"          # idle | recording | transcribing | archiving
        self._session_meta = None
        self._started_monotonic = 0.0
        self._last_result: dict | None = None
        self._busy_lock = threading.RLock()
        # 跨进程麦克风互斥（与旧服务共用命名互斥体）
        self._mic_lock = _MicLock(my_port=int(self.cfg["http"]["port"]))
        # 超时守护线程（boot() 里启动）
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        # finish_session 互斥：GUI 与 HTTP /stop 可能并发调用同一场会话，
        # 不加锁会并发合并 mp3，日志刷「分片文件被占用」（审计 P2）
        self._finish_lock = threading.Lock()

    @property
    def engine(self):
        """按需创建 AsrEngine（转写未启用时永远不创建）。"""
        if self._engine is None:
            self._engine = AsrEngine(
                self._engine_cfg["model"], self._engine_cfg["cpu_threads"],
                self._engine_cfg["compute_type"], self._engine_cfg.get("beam_size", 1)
            )
        return self._engine

    def stop_all(self):
        """停止录音并释放资源，供 GUI 退出/重启服务时调用。"""
        # 先停守护线程，避免它在释放过程中又触发一次收尾
        try:
            self.shutdown_watchdog()
        except Exception:
            pass
        try:
            if self.recorder.status().get("state") == "recording":
                self.recorder.cancel()
        except Exception:
            log.exception("stop_all: 取消录音失败")
        try:
            self.recorder.close_stream(force=True)
        except Exception:
            log.exception("stop_all: 关闭音频流失败")
        # 进程退出必须释放锁，否则另一套程序永远拿不到麦克风
        try:
            self._mic_lock.release()
            miclock_clear_holder()
        except Exception:
            log.exception("stop_all: 释放麦克风锁失败")
        self._phase = "idle"

    # ---------- 启动 ----------
    def boot(self) -> bool:
        ok = self.recorder.start()
        if not ok:
            log.error("音频流打开失败，服务仅提供状态查询")
        if self.cfg.get("transcribe", False):
            self._asr_thread = threading.Thread(target=self._asr_loop, name="asr-worker", daemon=True)
            self._asr_thread.start()
        # 超时保护：watchdog 原来只在 main.py 里被调用，而 GUI 版永不执行 main.py，
        # 所以 max_hours 在发行包里是死配置。放到 boot() 里，两条入口都覆盖。
        self._watchdog_stop.clear()
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watchdog_loop, name="watchdog", daemon=True)
            self._watchdog.start()
        log.info("服务就绪 录音目录=%s 超时上限=%sh 常驻麦克风=%s",
                 self.rec_dir, self.cfg.get("max_hours", 6), self.cfg.get("always_on_mic", False))
        return ok

    def _watchdog_loop(self):
        """每 10 秒检查一次，超过 max_hours 自动收尾，防止忘了说停导致盘满。"""
        while not self._watchdog_stop.wait(10):
            try:
                self.watchdog()
            except Exception:
                # 单次异常不能让守护线程死掉，否则超时保护永久失效
                log.exception("watchdog 检查异常，继续运行")

    def shutdown_watchdog(self):
        self._watchdog_stop.set()

    # ---------- 指令 ----------
    def cmd_start(self, label: str = "", use_prebuffer: bool = True) -> dict:
        with self._busy_lock:
            if self.recorder.status()["state"] == "recording":
                return {"ok": False, "msg": "已经在录音中", "status": self.status()}

            # 跨进程互斥：另一套程序正在用麦克风时拒绝，
            # 否则两个进程同时录会互相干扰并产出重复文件。
            if not self._mic_lock.acquire(timeout=0):
                holder = miclock_read_holder()
                who = f"（占用方：{holder}）" if holder else ""
                log.warning("麦克风被占用%s，拒绝录音", who)
                return {
                    "ok": False,
                    "msg": f"麦克风被占用，无法录音{who}",
                    "status": self.status(),
                }
            miclock_write_holder("open-record-GUI版")
            if self._phase == "transcribing":
                log.warning("上一场仍在转写，CPU 可能吃紧")
            try:
                meta = self.recorder.begin(label, use_prebuffer=use_prebuffer)
            except Exception as e:
                log.exception("开始录音失败")
                self._mic_lock.release()
                miclock_clear_holder()
                return {"ok": False, "msg": f"开始失败: {e}"}
            self._session_meta = meta
            self._started_monotonic = time.monotonic()
            self._transcriber = SessionTranscriber(
                self.recorder._session_dir, self.engine, meta.session_id
            )
            self._phase = "recording"
            log.info("开始录音 %s", meta.session_id)
            return {"ok": True, "session_id": meta.session_id, "msg": "已开始录音", "status": self.status()}

    def cmd_stop(self) -> dict:
        with self._busy_lock:
            if self.recorder.status()["state"] != "recording":
                return {"ok": False, "msg": "当前没有在录音"}
            meta = self.recorder.end()
            # 录音结束，释放麦克风锁
            self._mic_lock.release()
            miclock_clear_holder()
            self._phase = "transcribing" if self.cfg.get("transcribe", False) else "archiving"
        if meta is None:
            self._phase = "idle"
            return {"ok": False, "msg": "录音结束但未取到会话信息"}
        waiting = self._pending_count()
        recorded = max(0.0, meta.duration_sec - meta.prebuffer_sec)
        log.info("录音停止 %s 实际 %.1fs 预录 %.1fs 合计 %.1fs，待转写分片 %d",
                 meta.session_id, recorded, meta.prebuffer_sec, meta.duration_sec, waiting)
        return {
            "ok": True,
            "session_id": meta.session_id,
            "duration_sec": meta.duration_sec,
            "recorded_sec": round(recorded, 1),
            "prebuffer_sec": meta.prebuffer_sec,
            "pending_chunks": waiting,
            "msg": f"已停止，录音 {recorded:.0f} 秒",
        }

    def finish_session(self) -> dict | None:
        """停止后的收尾：按配置决定是否转写，最终产出 mp3（和可选逐字稿）。
        加锁防止并发调用重复合并同一场会话。"""
        with self._finish_lock:
            return self._finish_session_locked()

    def _finish_session_locked(self) -> dict | None:
        t = self._transcriber
        if t is None:
            self._phase = "idle"
            return None
        meta = self._session_meta
        do_asr = bool(self.cfg.get("transcribe", False))
        result = {
            "session_id": t.session_id,
            "label": meta.label if meta else "",
            "session_dir": str(t.dir),
            "duration_sec": round(meta.duration_sec if meta else 0, 1),
            "prebuffer_sec": round(meta.prebuffer_sec if meta else 0, 1),
            "recorded_sec": round(((meta.duration_sec - meta.prebuffer_sec) if meta else 0), 1),
            "peak_level": meta.peak_level if meta else 0,
            "finished_at": "",
            "chars": 0,
            "segments": 0,
            "transcribed": False,
        }
        if do_asr:
            self._phase = "transcribing"
            self.wait_transcribe_done()
            title = f"{t.session_id} {result['label']}".strip()
            md = t.finalize(title)
            result.update({
                "chars": len(t.data.text()),
                "segments": len(t.merge_paragraphs()),
                "transcript_md": str(md),
                "transcript_txt": str(t.dir / "transcript.txt"),
                "preview": t.data.text()[:300],
                "transcribed": True,
            })
        # 合并 mp3 并删除分片（此时转写已完成，删分片安全）
        self._phase = "archiving"
        try:
            from merge import merge_session
            # 文件名用「日期_开始时间-结束时间」，便于直接看出录音区间
            end_ts = ""
            if meta and meta.ended_at:
                try:
                    end_ts = datetime.fromisoformat(meta.ended_at).strftime("%H%M%S")
                except Exception:
                    end_ts = datetime.now().strftime("%H%M%S")
            elif meta:
                end_ts = datetime.now().strftime("%H%M%S")
            fname = f"{t.session_id}-{end_ts}.mp3" if end_ts else None
            m = merge_session(t.dir, bitrate=self.cfg.get("mp3_bitrate", 192),
                              delete_chunks=True, filename=fname,
                              max_gain=float(self.cfg.get("gain", 12.0)))
            if m:
                result["audio_mp3"] = m["mp3"]
                result["audio_bytes"] = m["bytes"]
                result["duration_sec"] = m["seconds"]
                result["recorded_sec"] = round(max(0.0, m["seconds"] - result["prebuffer_sec"]), 1)
        except Exception:
            log.exception("合并 mp3 失败，保留原分片")
        self._phase = "idle"
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self._last_result = result
        try:
            (t.dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            log.exception("写 result.json 失败")
        if self.notifier:
            try:
                self.notifier(result)
            except Exception:
                log.exception("通知投递失败")
        log.info("收尾完成 %s：mp3=%.1fMB 转写=%s", t.session_id,
                 result.get("audio_bytes", 0) / 1024 / 1024, result["transcribed"])
        return result

    def cmd_cancel(self) -> dict:
        with self._busy_lock:
            if self.recorder.status()["state"] != "recording":
                return {"ok": False, "msg": "当前没有在录音"}
            self.recorder.cancel()
            # 丢弃本次录音，释放麦克风锁
            self._mic_lock.release()
            miclock_clear_holder()
            self._phase = "idle"
            self._clear_asr_queue()
            return {"ok": True, "msg": "本次录音已丢弃"}

    # ---------- 转写调度 ----------
    def _on_chunk(self, path: Path, idx: int, dur: float):
        if self._transcriber is None:
            return
        # 关闭转写时不入队，省 CPU 和磁盘 I/O
        if not self.cfg.get("transcribe", False):
            return
        with self._asr_lock:
            self._asr_q.append((idx, path, self._transcriber.session_id))
            self._asr_lock.notify()

    def _pending_count(self) -> int:
        with self._asr_lock:
            return len(self._asr_q)

    def _clear_asr_queue(self):
        with self._asr_lock:
            self._asr_q.clear()

    def _asr_loop(self):
        while True:
            with self._asr_lock:
                while not self._asr_q:
                    self._asr_lock.wait()
                idx, path, sid = self._asr_q.pop(0)
            t = self._transcriber
            if t is None or t.session_id != sid:
                continue
            try:
                t.transcribe_chunk(idx)
            except Exception:
                log.exception("分片 %s 转写失败", path.name)

    def wait_transcribe_done(self, timeout: float = 3600) -> bool:
        t0 = time.time()
        while self._pending_count() > 0:
            if time.time() - t0 > timeout:
                return False
            time.sleep(0.5)
        return True

    def finalize(self) -> dict | None:
        """等转写清空，出稿，投递。"""
        ok = self.wait_transcribe_done()
        t = self._transcriber
        if t is None:
            self._phase = "idle"
            return None
        if not ok:
            log.warning("转写超时，输出部分结果")
        title = f"{t.session_id} {self._session_meta.label if self._session_meta else ''}".strip()
        md = t.finalize(title)
        text = t.data.text()
        result = {
            "session_id": t.session_id,
            "label": self._session_meta.label if self._session_meta else "",
            "duration_sec": t.data.duration,
            "chars": len(text),
            "segments": len(t.data.segments),
            "transcript_md": str(md),
            "transcript_txt": str(t.dir / "transcript.txt"),
            "session_dir": str(t.dir),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "preview": text[:300],
        }
        self._last_result = result
        self._phase = "idle"
        (t.dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("出稿完成 %s：%d 字 / %d 段", t.session_id, len(text), len(t.data.segments))
        if self.notifier:
            try:
                self.notifier(result)
            except Exception:
                log.exception("通知投递失败")

        # 归档：合并成 mp3 并删除分片（mp3 保留为唯一重转依据）
        if self.cfg.get("merge_mp3", True):
            try:
                from merge import merge_session
                m = merge_session(t.dir, bitrate=self.cfg.get("mp3_bitrate", 192),
                                  delete_chunks=True,
                                  max_gain=float(self.cfg.get("gain", 12.0)))
                if m:
                    result["audio_mp3"] = m["mp3"]
                    result["audio_bytes"] = m["bytes"]
                    result["chunks_removed"] = m["chunks_removed"]
                    (t.dir / "result.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            except Exception:
                log.exception("合并 mp3 失败，保留原分片")

        return result

    # ---------- 查询 ----------
    def status(self) -> dict:
        st = self.recorder.status()
        st["phase"] = self._phase
        st["pending_chunks"] = self._pending_count()
        if st["state"] == "recording" and self._started_monotonic:
            st["elapsed_sec"] = round(time.monotonic() - self._started_monotonic, 1)
        st["max_hours"] = self.cfg["max_hours"]
        st["model"] = self.cfg["asr"]["model"]
        if self._last_result:
            lr = self._last_result
            st["last_result"] = {
                "session_id": lr.get("session_id"),
                "chars": lr.get("chars", 0),
                "finished_at": lr.get("finished_at", ""),
                "duration_sec": lr.get("duration_sec", 0),
                "audio_mp3": lr.get("audio_mp3"),
                "audio_exists": os.path.exists(lr.get("audio_mp3") or ""),
                "transcribed": lr.get("transcribed", False),
            }
        return st

    def watchdog(self):
        """录音超时保护，防止忘了说停导致盘满。"""
        if self.recorder.status()["state"] != "recording":
            return
        elapsed = time.monotonic() - self._started_monotonic
        if elapsed > self.cfg["max_hours"] * 3600:
            log.warning("录音超过 %.1f 小时上限，自动停止", self.cfg["max_hours"])
            self.cmd_stop()
            threading.Thread(target=self.finalize, daemon=True).start()


# ---------------- HTTP ----------------
def make_handler(svc: RecService, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RecAssistant/1.0"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _auth(self) -> bool:
            if not token:
                return True
            got = self.headers.get("X-Token", "")
            return got == token

        def _send(self, code: int, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._send(200, {"ok": True, "ts": time.time()})
            if not self._auth():
                return self._send(401, {"ok": False, "msg": "unauthorized"})
            if self.path.startswith("/status"):
                return self._send(200, {"ok": True, "status": svc.status()})
            if self.path.startswith("/diag"):
                import sounddevice as sd
                info = {"mic_open": svc.recorder._stream is not None,
                        "rate": svc.recorder._rate, "tries": []}
                try:
                    info["default_input"] = str(sd.query_devices(kind="input"))
                except Exception as e:
                    info["default_input"] = "ERR %s" % e
                try:
                    info["hostapis"] = [a["name"] for a in sd.query_hostapis()]
                except Exception:
                    pass
                for i, d in enumerate(sd.query_devices()):
                    if d["max_input_channels"] <= 0:
                        continue
                    if not ("Microphone" in d["name"] or "麦克风" in d["name"]):
                        continue
                    api = sd.query_hostapis(d["hostapi"])["name"]
                    for rate in (48000, 16000):
                        st = None
                        try:
                            st = sd.InputStream(device=i, channels=1, samplerate=rate,
                                                dtype="int16", blocksize=4800)
                            st.start()
                            st.stop()
                            st.close()
                            info["tries"].append({"dev": i, "api": api, "rate": rate, "ok": True})
                        except Exception as e:
                            if st is not None:
                                try:
                                    st.close()
                                except Exception:
                                    pass
                            info["tries"].append({"dev": i, "api": api, "rate": rate,
                                                  "ok": False, "err": str(e)[:90]})
                return self._send(200, info)
            return self._send(404, {"ok": False, "msg": "not found"})

        def do_POST(self):
            if not self._auth():
                return self._send(401, {"ok": False, "msg": "unauthorized"})
            body = self._body()
            if self.path.startswith("/start"):
                return self._send(200, svc.cmd_start(body.get("label", ""), body.get("prebuffer", True)))
            if self.path.startswith("/stop"):
                r = svc.cmd_stop()
                if r.get("ok"):
                    threading.Thread(target=svc.finish_session, daemon=True).start()
                return self._send(200, r)
            if self.path.startswith("/cancel"):
                return self._send(200, svc.cmd_cancel())
            return self._send(404, {"ok": False, "msg": "not found"})

    return Handler


def serve(svc: RecService, host: str, port: int, token: str = "") -> ThreadingHTTPServer:
    # 关键：Windows 上 allow_reuse_address 默认 True，等价于允许另一个进程
    # 绑同一端口，结果是「端口被占用」永远不报错，请求还会被路由到旧进程。
    # 实测：旧实例在跑时新实例照样 bind 成功，新 token 失效、请求全落到旧实例。
    # 改成独占绑定，端口冲突就如实抛 OSError，调用方才能正确降级。
    class _ExclusiveServer(ThreadingHTTPServer):
        allow_reuse_address = False
        daemon_threads = True

    httpd = _ExclusiveServer((host, port), make_handler(svc, token))
    t = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    t.start()
    log.info("HTTP 控制接口已监听 %s:%s", host, port)
    return httpd
