"""
录音归档
========
把 30 秒分片合并成一个 mp3，然后删掉分片，省约 75% 空间。

为什么要保留 mp3 而不是直接删干净
--------------------------------
mp3 是唯一的重转写依据。逐字稿改了模型、加了热词、修了参数想重跑时，
拿 mp3 就能重新转（load_audio 支持 mp3 解码），不需要原始 wav。

编码用 lameenc，不依赖 ffmpeg。流式处理，1 小时录音内存占用 < 5MB。

单独使用：
    python app/merge.py <session_dir>
    python app/merge.py <session_dir> --keep-chunks
"""
from __future__ import annotations

import json
import logging
import sys
import time
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("merge")

SAMPLE_RATE = 16000
BITRATE = 64          # 单声道语音 64kbps 足够，1 小时约 28MB


def _probe_rate(chunks) -> int:
    """取分片的实际采样率。录音端若保留原生 48k，这里就用 48k 编码，
    不再强制降到 16k——降采样是不可逆的信息损失。"""
    for c in chunks:
        try:
            with wave.open(str(c), "rb") as w:
                return w.getframerate()
        except Exception:
            continue
    return SAMPLE_RATE


def _read_chunk_pcm(path: Path, target_sr: int | None = None) -> tuple[bytes, float]:
    """读分片为单声道 int16 PCM，返回 (bytes, 秒数)。

    target_sr 为 None 时保持原始采样率，不做任何重采样。
    """
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError(f"仅支持 16bit PCM: {path}")
    a = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if target_sr and sr != target_sr and len(a) > 0:
        idx = np.linspace(0, len(a) - 1, int(len(a) * target_sr / sr))
        a = np.interp(idx, np.arange(len(a)), a.astype(np.float32))
        a = np.clip(a, -32768, 32767).astype(np.int16)
        sr = target_sr
    return a.tobytes(), len(a) / sr


def scan_peak(chunks) -> float:
    """扫一遍分片，返回 99.5 分位绝对值（0-1）。用分位而非绝对峰值，
    避免单个爆音把整体增益压低。"""
    peak = 0.0
    for c in chunks:
        try:
            with wave.open(str(c), "rb") as w:
                raw = w.readframes(w.getnframes())
            if not raw:
                continue
            a = np.frombuffer(raw, dtype=np.int16)
            if len(a):
                peak = max(peak, float(np.percentile(np.abs(a), 99.5)) / 32768.0)
        except Exception:
            continue
    return peak


def merge_session(session_dir: Path, bitrate: int = BITRATE,
                  delete_chunks: bool = True, filename: str | None = None,
                  normalize: bool = True, target_peak: float = 0.18,
                  max_gain: float = 12.0, floor_peak: float = 0.0015,
                  highpass_hz: float = 0.0) -> dict | None:
    session_dir = Path(session_dir)
    chunks = sorted(session_dir.glob("chunk_*.wav"),
                    key=lambda p: int(p.stem.split("_")[1]))
    if not chunks:
        log.info("%s 没有分片，跳过合并", session_dir.name)
        return None

    import lameenc

    # 跟随分片实际采样率，不再强制 16k
    rate = _probe_rate(chunks)

    # 增益策略：MME 采集的原始电平常在 -36dBFS 左右，不放大听不清，
    # 但也不能像早期那样放 10 倍——那是把 WASAPI 降噪后的失真信号一起放大，
    # 结果更糊。现在改成：先确保采集走 MME（语音完整），再点到 -15dB 左右。
    # 目标峰值 0.18、上限 12 倍，实测 8 倍即可听清。
    orig_peak = scan_peak(chunks)
    gain = 1.0
    if normalize and orig_peak >= floor_peak:
        # 只在明显过小时才放大。实测原始 -24dB 的录音被放 10 倍后低频噪声炸开、
        # 人声反而更糊，所以这里把触发线压到 -32dB，并限制最大 8 倍。
        if orig_peak < 0.06:           # 约 -24 dBFS
            gain = min(max_gain, target_peak / orig_peak)
            if gain < 1.4:
                gain = 1.0
        # 介于 -32dB 与目标之间时不动，避免放大噪声
    elif normalize and orig_peak > 0:
        log.info("原始峰值过低（%.1f dBFS），判定为静音，不施加增益",
                 20 * np.log10(max(orig_peak, 1e-9)))
    gain_db = 20 * np.log10(gain) if gain > 0 else 0.0
    log.info("电平：原始峰值 %.1f dBFS，增益 %.1fx (+%.1f dB)，高通 %.0fHz",
             20 * np.log10(max(orig_peak, 1e-9)), gain, gain_db, highpass_hz)

    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate)
    enc.set_in_sample_rate(rate)
    enc.set_channels(1)
    enc.set_quality(2)

    out = session_dir / (filename or "audio.mp3")
    total = 0.0
    frames = 0
    try:
        with open(out, "wb") as f:
            # 高通滤波需要 scipy。默认 highpass_hz=0 即关闭，
            # 此时不导入 scipy（分发包有意排除它，体积省约 100MB）。
            sos = None
            zi = None
            if highpass_hz > 0:
                try:
                    from scipy.signal import butter, sosfilt, sosfilt_zi
                    sos = butter(2, highpass_hz, btype="high", fs=rate, output="sos")
                except ImportError:
                    log.warning("未安装 scipy，跳过高通滤波（不影响录音）")
                    highpass_hz = 0
            for c in chunks:
                pcm, sec = _read_chunk_pcm(c, target_sr=None)
                if pcm:
                    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    # 高通滤波：切掉低频轰鸣（实测 0-300Hz 占总能量 47%，是"糊"的主因）
                    if sos is not None:
                        if zi is None:
                            zi = sosfilt_zi(sos) * arr[0]
                        arr, zi = sosfilt(sos, arr, zi=zi)
                    if gain != 1.0:
                        arr = arr * gain
                    pcm = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()
                total += sec
                frames += len(pcm) // 2
                f.write(enc.encode(pcm))
            f.write(enc.flush())
    except Exception:
        log.exception("合并失败，保留原分片")
        if out.exists():
            out.unlink(missing_ok=True)
        return None

    size = out.stat().st_size if out.exists() else 0
    log.info("合并完成 %s：%.1f 秒，%.1f MB，%dHz %dkbps",
             out.name, total, size / 1024 / 1024, bitrate)

    if delete_chunks:
        removed = 0
        failed = []
        for c in chunks:
            done = False
            for _ in range(4):
                try:
                    c.unlink()
                    removed += 1
                    done = True
                    break
                except Exception:
                    time.sleep(0.3)   # 同步客户端可能短暂锁定文件
            if not done:
                failed.append(c.name)
        log.info("已删除 %d/%d 个分片", removed, len(chunks))
        if failed:
            log.warning("残留分片（文件被占用）：%s", ", ".join(failed))

    # 记录到 session.json
    meta_path = session_dir / "session.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["audio_mp3"] = out.name
            meta["audio_mp3_bytes"] = size
            meta["chunks_removed"] = bool(delete_chunks)
            meta["merged_sec"] = round(total, 2)
            meta["original_peak_dbfs"] = round(20 * np.log10(max(orig_peak, 1e-9)), 1)
            meta["applied_gain"] = round(gain, 2)
            meta["applied_gain_db"] = round(gain_db, 1)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            log.warning("更新 session.json 失败")

    return {"mp3": str(out), "seconds": round(total, 2), "bytes": size,
            "chars_frames": frames, "chunks_removed": len(chunks) if delete_chunks else 0,
            "original_peak_dbfs": round(20 * np.log10(max(orig_peak, 1e-9)), 1),
            "applied_gain": round(gain, 2), "applied_gain_db": round(gain_db, 1)}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) < 2:
        print(__doc__)
        return
    d = Path(sys.argv[1])
    if not d.is_dir():
        print(f"目录不存在: {d}")
        return
    keep = "--keep-chunks" in sys.argv
    r = merge_session(d, delete_chunks=not keep)
    print(json.dumps(r, ensure_ascii=False, indent=2) if r else "无分片可合并")


if __name__ == "__main__":
    main()
