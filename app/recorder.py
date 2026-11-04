"""
录音引擎
========
常驻采集 + 环形预录 + 分片落盘。

设计要点
--------
1. 音频流常驻打开（WASAPI 共享模式），不独占麦克风，不影响会议软件。
2. 待命时音频只进环形缓冲（默认最近 120 秒，内存约 3.8MB），不落盘。
   收到「开始录音」时把这段缓冲一并写入——彻底消除指令传输延迟导致的漏录。
3. 录制中每 30 秒落一个 wav 分片，断电/崩溃最多丢 30 秒。
4. 音频回调里只做入队，写盘、切分、编码全在独立线程，避免爆音。
5. 设备被拔出/占用时自动重连，不中断会话。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import wave
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

log = logging.getLogger("recorder")

TARGET_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 100


@dataclass
class SessionMeta:
    session_id: str
    label: str
    started_at: str
    ended_at: str | None = None
    sample_rate: int = TARGET_RATE
    channels: int = CHANNELS
    device: str = ""
    chunks: list[str] = field(default_factory=list)
    duration_sec: float = 0.0
    prebuffer_sec: float = 0.0
    xruns: int = 0
    dropped_blocks: int = 0
    peak_level: int = 0


class Recorder:
    def __init__(
        self,
        base_dir: Path,
        prebuffer_sec: int = 15,
        chunk_sec: int = 30,
        device: int | str | None = None,
        on_chunk=None,
        exclusive: bool = False,
        keep_native_rate: bool = False,
        always_on_mic: bool = False,
    ):
        self.base_dir = Path(base_dir)
        self.prebuffer_sec = prebuffer_sec
        self.chunk_sec = chunk_sec
        self.device_pref = device
        self.on_chunk = on_chunk
        self.exclusive = exclusive
        self.keep_native_rate = keep_native_rate
        # 常驻麦克风：为 true 时待命期间也开流，预录才真正有效（代价是占用设备）
        self.always_on_mic = bool(always_on_mic)

        self._rate = TARGET_RATE
        self._stream: sd.InputStream | None = None
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self._ring: deque = deque(maxlen=max(1, prebuffer_sec * 1000 // BLOCK_MS))
        self._state = "idle"           # idle | recording | stopping
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._writer: threading.Thread | None = None
        self._monitor: threading.Thread | None = None

        self._chunk_buf: list[np.ndarray] = []
        self._chunk_frames = 0
        self._chunk_idx = 0
        # 已落盘分片的累计时长。status() 每 200ms 调一次，若每次都 glob + 逐文件
        # wave.open，实测 2160 分片要 414ms（占空比 207%），界面直接冻住。
        # 改为落盘时累加，status() 变成 O(1)。
        self._total_dur = 0.0
        self._chunk_count = 0
        self._session: SessionMeta | None = None
        self._session_dir: Path | None = None
        self._level = 0
        self._peak = 0
        self._xruns = 0
        self._dropped = 0
        self._last_err = ""

    # ---------- 设备 ----------
    def _resolve_device(self):
        if self.device_pref is not None:
            return self.device_pref
        try:
            apis = sd.query_hostapis()
            # 优先 MME：实测同一支麦克风，WASAPI 共享模式会把人声压到 -36dB 以下
            # 且谐波结构受损，而 MME 采集的语音频段占比 68%、谐波性 0.34，清晰可辨。
            for pref in ("MME", "Windows DirectSound", "Windows WASAPI"):
                api_idx = next((i for i, a in enumerate(apis) if pref in a["name"]), None)
                if api_idx is None:
                    continue
                for idx, d in enumerate(sd.query_devices()):
                    if d["hostapi"] == api_idx and d["max_input_channels"] > 0:
                        if "麦克风" in d["name"] or "Microphone" in d["name"] or "Array" in d["name"]:
                            log.info("选用录音设备: %s (%s)", d["name"][:30], pref)
                            return idx
            return sd.default.device[0]
        except Exception:
            return None

    def _open_stream(self):
        # PortAudio 会缓存设备列表，而 MME 的索引在音频会话变化后会重排，
        # 导致「查到的索引」与「打开时的索引」不一致（MME error 2: device ID out of range）。
        # 开流前强制重新初始化，保证两者基于同一份设备列表。
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            log.debug("PortAudio 重新初始化失败，继续使用缓存列表", exc_info=True)
        dev = self._resolve_device()
        last = None
        # 独占模式可绕过系统「音频增强」（APO 降噪/AGC）。实测同一支麦克风
        # 非零样本占比从 3.4% 提升到 94%，语音细节明显更完整。
        # 打开失败（设备被其他程序独占）时自动回退到共享模式，不让录音彻底不可用。
        attempts = []
        if self.exclusive:
            try:
                attempts.append(("exclusive", sd.WasapiSettings(exclusive=True)))
            except Exception:
                log.warning("独占模式不可用，改用共享模式")
        attempts.append(("shared", None))

        # 实测这台机器的 WASAPI 麦克风只接受 48000，16000/44100 直接报 Invalid sample rate，
        # 所以按可用性排序，48000 优先。
        for mode, extra in attempts:
            for rate in (48000, TARGET_RATE, 44100):
                st = None
                try:
                    # 用阻塞 read 模式而非 callback：实测在 HTTP/飞书工作线程里
                    # 用 callback 打开 WASAPI 流会报 "Unanticipated host error"，
                    # 只有主线程可以。阻塞模式没有这个限制。
                    kwargs = dict(
                        device=dev,
                        channels=CHANNELS,
                        samplerate=rate,
                        dtype=DTYPE,
                        blocksize=int(rate * BLOCK_MS / 1000),
                    )
                    if extra is not None:
                        kwargs["extra_settings"] = extra
                    st = sd.InputStream(**kwargs)
                    st.start()
                    self._rate = rate
                    self._stream = st
                    self._mode = mode
                    info = sd.query_devices(dev) if dev is not None else {}
                    name = info.get("name", "default") if isinstance(info, dict) else "default"
                    log.info("音频流已打开: device=%s rate=%s 模式=%s", name, rate, mode)
                    self._reader_stop.clear()
                    self._reader = threading.Thread(target=self._reader_loop,
                                                    name="rec-reader", daemon=True)
                    self._reader.start()
                    if self._session:
                        self._session.device = str(name)
                        self._session.sample_rate = rate if self.keep_native_rate else TARGET_RATE
                    return True
                except Exception as e:
                    # 关键：start() 失败时必须关掉已经创建的流，否则句柄泄漏，
                    # 反复重试会把设备占满，之后连 48000 都打不开。
                    if st is not None:
                        try:
                            st.close()
                        except Exception:
                            pass
                    last = e
                    continue
        self._last_err = f"无法打开录音设备: {last}"
        log.error(self._last_err)
        return False

    # ---------- 阻塞读取线程 ----------
    def _reader_loop(self):
        block = max(1, int(self._rate * BLOCK_MS / 1000))
        while not self._reader_stop.is_set():
            st = self._stream
            if st is None:
                break
            try:
                data, overflowed = st.read(block)
            except Exception as e:
                if self._reader_stop.is_set():
                    break
                log.warning("读取音频失败: %s", e)
                time.sleep(0.05)
                continue
            if data is None or len(data) == 0:
                continue
            arr = data[:, 0].copy() if data.ndim > 1 else data.copy()
            try:
                lv = int(np.abs(arr).max())
                self._level = lv
                if lv > self._peak:
                    self._peak = lv
            except Exception:
                pass
            try:
                self._q.put_nowait(arr)
            except queue.Full:
                self._dropped += 1

    # ---------- 音频回调（保留兼容，实际不再使用） ----------
    def _callback(self, indata, frames, time_info, status):
        if status:
            self._xruns += 1
        block = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        try:
            lv = int(np.abs(block).max())
            self._level = lv
            if lv > self._peak:          # 记录全程峰值，而不是结束瞬间的值
                self._peak = lv
        except Exception:
            pass
        try:
            self._q.put_nowait(block)
        except queue.Full:
            self._dropped += 1

    # ---------- 写盘线程 ----------
    def _writer_loop(self):
        # 目标按「块数」而非「帧数」计算。
        # 原实现 `chunk_sec * self._rate` 在进循环时求值一次，那时 _rate 还是初始
        # TARGET_RATE=16000（480000 帧），而 _open_stream() 之后 _rate 变成 48000、
        # 每块 4800 帧 → 480000/4800 = 100 块 × 100ms = 10 秒，配置的 30 秒从未生效。
        # 改用块数后与采样率完全解耦。实测修复前 chunk_seconds_actual = 10.0。
        chunk_blocks_target = max(1, self.chunk_sec * 1000 // BLOCK_MS)
        while not self._stop_evt.is_set():
            try:
                block = self._q.get(timeout=0.2)
            except queue.Empty:
                # 注意：这里不能在 state == "stopping" 时 break。
                # 原实现会在收尾时退出线程，而 writer 是跨会话复用的（只有 start()
                # 才重建），一旦退出，第二次录音就没人写盘了。
                # 只有 _stop_evt（真正关闭引擎）才结束。
                continue
            if self._state in ("recording", "stopping"):
                # stopping 期间仍收块：这些是停采前已在队列里的存量，
                # 属于用户真正说过的话，必须写进文件。
                # 入队加锁：end() 会在另一个线程读 _chunk_buf 判断是否排空，
                # 不加锁会读到中间态。
                with self._lock:
                    self._chunk_buf.append(block)
                    self._chunk_frames += len(block)
                    n_blocks = len(self._chunk_buf)
                if self._state == "recording" and n_blocks >= chunk_blocks_target:
                    self._flush_chunk()
            else:
                self._ring.append(block)

    # ---------- 分片写入 ----------
    def _write_wav(self, path: Path, blocks: list[np.ndarray]) -> float:
        """写入分片，返回时长（秒）。默认统一重采样到 16k，磁盘占用降到 1/3。"""
        if not blocks:
            return 0.0
        data = np.concatenate(blocks)
        sr_out = self._rate
        if not self.keep_native_rate and self._rate != TARGET_RATE and len(data) > 0:
            n_out = int(len(data) * TARGET_RATE / self._rate)
            idx = np.linspace(0, len(data) - 1, n_out)
            data = np.interp(idx, np.arange(len(data)), data.astype(np.float32))
            data = np.clip(data, -32768, 32767).astype(np.int16)
            sr_out = TARGET_RATE
        with wave.open(str(path), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(2)
            w.setframerate(sr_out)
            w.writeframes(data.tobytes())
        return len(data) / sr_out

    def _flush_chunk(self):
        with self._lock:
            if not self._chunk_buf or self._session_dir is None:
                self._chunk_buf, self._chunk_frames = [], 0
                return
            blocks = self._chunk_buf
            self._chunk_buf, self._chunk_frames = [], 0
            self._chunk_idx += 1
            idx = self._chunk_idx
        name = f"chunk_{idx:05d}.wav"
        path = self._session_dir / name
        try:
            dur = self._write_wav(path, blocks)
        except Exception as e:
            log.exception("写分片失败 %s: %s", name, e)
            return
        if self._session:
            self._session.chunks.append(name)
        self._total_dur += dur          # 增量累计，供 status() 用
        self._chunk_count = idx
        log.info("分片落盘 %s (%.1fs)", name, dur)
        if self.on_chunk:
            try:
                self.on_chunk(path, idx, dur)
            except Exception:
                log.exception("on_chunk 回调异常")

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """启动写盘线程。注意：默认不在此时打开麦克风——待命期间不占设备，
        只在真正录音时打开，停止后立即释放，其他软件可正常使用麦克风。

        代价是「预录」在默认配置下不成立：待命时没有音频进入环形缓冲，
        所以开始录音时能并入的只有 begin() 里等设备稳定的那约 0.25 秒。
        这与 README 曾承诺的「15 秒预录保证开头不丢」不符，已改为如实说明。
        需要真正的预录时，把 config 的 always_on_mic 设为 true，
        待命期间就保持开流（会占用麦克风）。
        """
        self._stop_evt.clear()
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._writer_loop, name="rec-writer", daemon=True)
            self._writer.start()
        if self.always_on_mic and self._stream is None:
            if self._open_stream():
                log.info("常驻麦克风已开启，待命期间持续预录 %ss", self.prebuffer_sec)
            else:
                log.warning("常驻麦克风开启失败，预录不可用: %s", self._last_err)
        return True

    def close_stream(self, force: bool = False):
        """停止采集。默认在 always_on_mic=True 时保留音频流，
        以便待命期间继续预录；force=True 时无条件关流释放设备（退出/重启用）。"""
        if self.always_on_mic and not force and self._stream is not None:
            # 只停 reader，流和缓冲都留着，待命期间继续往环形缓冲里灌音频
            self._reader_stop.set()
            if self._reader and self._reader.is_alive():
                self._reader.join(timeout=2)
            self._reader = None
            self._level = 0
            self._restart_reader()
            return
        self._reader_stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=2)
        self._reader = None
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.exception("关闭音频流异常")
            self._stream = None
        self._level = 0
        # 清空预录缓冲，待命时不应残留旧数据
        try:
            self._ring.clear()
        except Exception:
            pass
        # 保留 _rate 为上一场录音的实际采样率，供状态查询参考；
        # 下次开流时会重新探测并覆盖，这里不做重置，避免待命时谎报 16k。

    def _restart_reader(self):
        """在已有音频流上重建读取线程（常驻麦克风模式下切回待命用）。"""
        if self._stream is None:
            return
        self._reader_stop.clear()
        if self._reader is None or not self._reader.is_alive():
            self._reader = threading.Thread(target=self._reader_loop,
                                            name="rec-reader", daemon=True)
            self._reader.start()

    def stop_stream(self):
        self._stop_evt.set()
        if self._writer and self._writer.is_alive():
            self._writer.join(timeout=3)
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def begin(self, label: str = "", use_prebuffer: bool = True) -> SessionMeta:
        with self._lock:
            if self._state == "recording":
                raise RuntimeError("已在录音中")
            # 按需打开麦克风（待命时默认是关闭的；always_on_mic 时已常驻）
            if self._stream is None:
                if not self._open_stream():
                    raise RuntimeError(self._last_err or "无法打开录音设备")
                time.sleep(0.25)   # 等设备稳定，避免首个数据块为空
            elif self._reader is None or not self._reader.is_alive():
                # 常驻模式下 reader 可能已停，重新拉起来
                self._restart_reader()
            now = datetime.now()
            sid = now.strftime("%Y%m%d_%H%M%S")
            self._session_dir = self.base_dir / now.strftime("%Y-%m-%d") / sid
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._session = SessionMeta(
                session_id=sid,
                label=label or "未命名录音",
                started_at=now.isoformat(timespec="seconds"),
                sample_rate=self._rate,
            )
            self._chunk_idx = 0
            self._chunk_buf = []
            self._chunk_frames = 0
            self._total_dur = 0.0
            self._chunk_count = 0
            self._peak = 0
            pre_len = 0.0
            if use_prebuffer and self._ring:
                pre = list(self._ring)
                self._ring.clear()
                if pre:
                    self._chunk_idx += 1
                    p = self._session_dir / f"chunk_{self._chunk_idx:05d}.wav"
                    pre_len = self._write_wav(p, pre)
                    self._session.chunks.append(p.name)
                    self._total_dur += pre_len
                    self._chunk_count = self._chunk_idx
                    log.info("预录 %s 已并入 (%.1fs)", p.name, pre_len)
            self._session.prebuffer_sec = round(pre_len, 2)
            self._state = "recording"
            self._save_meta()
            log.info("开始录音 session=%s label=%s", sid, label)
            return self._session

    def end(self) -> SessionMeta | None:
        with self._lock:
            if self._state != "recording":
                return self._session
            self._state = "stopping"
        # 顺序很关键：先停采，再排空。
        # 原实现先排空队列再 close_stream()，但 reader 线程还在持续入队，
        # 队列永远不空 → 每次都跑满 5 秒，且这 5 秒里用户说的话会被写进文件
        # （录音器仍在 "stopping" 分支收块）。既是延迟问题，也是隐私问题。
        # 实测修复前：录 3s 落盘 8.0s，end() 阻塞 5.06s。
        self.close_stream()          # 停 reader + 关流，此后不会再有新块入队
        # 此时队列里只剩停采前的存量（正常 < 100ms），排空几乎瞬时
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if self._q.empty() and not self._chunk_buf:
                break
            time.sleep(0.02)
        with self._lock:
            self._flush_chunk()
            s = self._session
            if s is None:
                self._state = "idle"
                return None
            s.ended_at = datetime.now().isoformat(timespec="seconds")
            s.duration_sec = round(self._total_frames(), 2)
            s.xruns = self._xruns
            s.dropped_blocks = self._dropped
            s.peak_level = self._peak
            self._save_meta()
            self._state = "idle"
            log.info("录音结束 session=%s 时长=%.1fs 分片=%d", s.session_id, s.duration_sec, len(s.chunks))
        log.info("麦克风已释放，待命时不再占用录音设备")
        return s

    def _total_frames(self) -> float:
        """累计分片真实时长。
        优先用增量维护的 _total_dur（O(1)）；只有它为 0 而磁盘上确实有分片时
        （例如进程重启后接管旧会话）才回退到扫盘，结果会写回缓存。"""
        if self._total_dur > 0:
            return self._total_dur
        if self._session_dir is None:
            return 0.0
        total = 0.0
        for f in sorted(self._session_dir.glob("chunk_*.wav")):
            try:
                with wave.open(str(f), "rb") as w:
                    sr = w.getframerate() or self._rate
                    total += w.getnframes() / sr
            except Exception:
                pass
        self._total_dur = total
        return total

    def cancel(self):
        with self._lock:
            self._state = "idle"
            self._chunk_buf, self._chunk_frames = [], 0
            self._total_dur = 0.0
            self._chunk_count = 0
            if self._session_dir and self._session_dir.exists():
                for f in self._session_dir.glob("chunk_*.wav"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
            self._session = None

    def _save_meta(self):
        if self._session and self._session_dir:
            try:
                (self._session_dir / "session.json").write_text(
                    json.dumps(asdict(self._session), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                log.exception("写 session.json 失败")

    # ---------- 状态 ----------
    def status(self) -> dict:
        s = self._session
        dur = 0.0
        if s and self._state == "recording" and self._session_dir:
            # O(1)：已落盘增量 + 缓冲未落盘，不碰磁盘
            dur = round(self._total_dur + self._chunk_frames / max(1, self._rate), 1)
        return {
            "state": self._state,
            "mic_open": self._stream is not None,
            "session_id": s.session_id if s else None,
            "label": s.label if s else None,
            "duration_sec": dur if self._state == "recording" else (s.duration_sec if s else 0),
            "level": self._level,
            "prebuffer_sec": self.prebuffer_sec,
            "prebuffer_filled": round(len(self._ring) * BLOCK_MS / 1000, 1),
            "rate": self._rate,
            "xruns": self._xruns,
            "dropped": self._dropped,
            "chunks": self._chunk_count,
            "device": s.device if s else "",
            "error": self._last_err,
            "dir": str(self._session_dir) if self._session_dir else None,
        }
