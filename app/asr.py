"""
转写引擎
========
基于 faster-whisper（CPU int8），支持增量转写 + 幻觉治理。

为什么做增量
------------
1 小时录音若等停止后再转，CPU 上要跑 10-20 分钟，人在外面等不起。
因此录音过程中每落一片就丢进后台队列转写，停止时通常只剩最后 1-2 片要补。

为什么必须做幻觉治理
--------------------
实测：49 分钟静音录音，whisper 凭空输出了「请不吝点赞 订阅 转发 打赏支持明镜与
点点栏目中文字幕组合订阅」。这是模型在无语音输入时复现训练数据里的字幕文本。
录静音产出假内容比没有内容更危险，所以设三道闸：
  1. 静音分片直接不送模型（帧能量 P95 低于阈值）
  2. no_speech_prob 高、avg_logprob 低的段丢弃
  3. 命中已知字幕幻觉特征串的段丢弃
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# 国内必须走镜像，否则模型下载会卡死
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

log = logging.getLogger("asr")

ZH_PROMPT = "以下是普通话对话或会议录音，请用简体中文输出，保留专有名词，不要翻译。"

# whisper 在中文静音/噪声段常见的训练数据残留
HALLUCINATION_PATTERNS = [
    "请不吝点赞", "点赞订阅", "点赞 订阅", "订阅转发", "订阅 转发",
    "转发打赏", "打赏支持", "明镜与点点", "明镜", "中文字幕",
    "字幕由", "字幕组", "感谢观看", "谢谢观看", "本字幕",
    "amara.org", "amara", "请订阅", "别忘了订阅",
]

# 静音判定阈值。早期设 0.003，实测把正常录音也误判为静音：
# 本机 MME 采集的电平常在 -32dBFS 左右，帧能量 P95 只有 0.003 上下，
# 一刀切会整段跳过。改为同时看「峰值」——真静音的峰值接近 0，
# 有语音的录音峰值至少在 0.01（约 -40dBFS）以上。
SILENCE_THRESHOLD = 0.0008
NO_SPEECH_MAX = 0.65        # no_speech_prob 高于此值丢弃
LOGPROB_MIN = -1.10         # avg_logprob 低于此值丢弃
COMPRESSION_MAX = 2.30      # compression_ratio 高于此值多为重复幻觉


@dataclass
class Segment:
    start: float
    end: float
    text: str
    chunk: int = 0


@dataclass
class Transcript:
    session_id: str
    engine: str = ""
    model: str = ""
    language: str = "zh"
    duration: float = 0.0
    segments: list[dict] = field(default_factory=list)
    done_chunks: list[int] = field(default_factory=list)
    silent_chunks: list[int] = field(default_factory=list)
    dropped_segments: int = 0

    def text(self) -> str:
        return "".join(s["text"] for s in self.segments).strip()

    def merge_paragraphs(self, gap: float = 1.5, max_len: int = 260) -> list[dict]:
        """把相邻且停顿很短的碎片合并成自然段落。"""
        out: list[dict] = []
        for s in self.segments:
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            if out and (s["start"] - out[-1]["end"] <= gap) and (len(out[-1]["text"]) + len(txt) <= max_len):
                out[-1]["text"] += txt
                out[-1]["end"] = s["end"]
            else:
                out.append({"start": s["start"], "end": s["end"], "text": txt})
        return out

    def to_markdown(self, title: str = "") -> str:
        paras = self.merge_paragraphs()
        lines = [f"# {title or self.session_id}", ""]
        lines.append(f"- 录音时长：{fmt_ts(self.duration)}")
        lines.append(f"- 转写引擎：{self.engine} / {self.model}")
        lines.append(f"- 有效段落：{len(paras)}")
        if self.silent_chunks:
            lines.append(f"- 静音跳过：{len(self.silent_chunks)} 个分片")
        if self.dropped_segments:
            lines.append(f"- 幻觉过滤：{self.dropped_segments} 段")
        lines.append("")
        if not paras:
            lines.append("## 逐字稿")
            lines.append("")
            lines.append("> 本次录音未检测到有效人声，未产生逐字稿内容。")
            return "\n".join(lines)
        lines.append("## 逐字稿")
        lines.append("")
        for para in paras:
            lines.append(f"**[{fmt_ts(para['start'])}]** {para['text']}")
            lines.append("")
        return "\n".join(lines)


def fmt_ts(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def load_audio(path, target_sr: int = 16000):
    """读音频为 float32 单声道 16k，返回 [-1,1] 区间的 ndarray。

    wav 用标准库直读（绕开 faster-whisper 内部的 av 解码路径：av 19 移除
    metadata_errors 参数导致不兼容，而 av 14 无 cp313 wheel）。
    mp3 等压缩格式用 av 自解码——归档成 mp3 之后仍能重转写。
    """
    p = Path(path)
    if p.suffix.lower() == ".wav":
        with wave.open(str(p), "rb") as w:
            sr = w.getframerate()
            n = w.getnframes()
            ch = w.getnchannels()
            width = w.getsampwidth()
            raw = w.readframes(n)
        if width != 2:
            raise ValueError(f"仅支持 16bit PCM，当前 {width*8}bit: {p}")
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
    else:
        import av
        container = av.open(str(p))
        try:
            st = container.streams.audio[0]
            sr = st.codec_context.sample_rate
            parts = []
            for frame in container.decode(st):
                arr = frame.to_ndarray()
                if arr.ndim == 2:
                    arr = arr.mean(axis=0)
                if np.issubdtype(arr.dtype, np.integer):
                    arr = arr.astype(np.float32) / 32768.0
                else:
                    arr = arr.astype(np.float32)
                parts.append(arr)
        finally:
            container.close()
        a = np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)

    if sr != target_sr and len(a) > 0:
        idx = np.linspace(0, len(a) - 1, int(len(a) * target_sr / sr))
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    return np.ascontiguousarray(a, dtype=np.float32)


def frame_energy_p95(audio: np.ndarray, sr: int = 16000, win_ms: int = 100) -> float:
    """分帧 RMS 的 95 分位，用于区分「有持续人声」和「偶发噪声/纯静音」。"""
    win = max(1, sr * win_ms // 1000)
    n = len(audio) // win
    if n == 0:
        return 0.0
    frames = audio[: n * win].reshape(n, win)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    return float(np.percentile(rms, 95))


def looks_hallucinated(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    low = t.replace(" ", "").lower()
    if len(low) <= 1:
        return True
    for p in HALLUCINATION_PATTERNS:
        if p.replace(" ", "").lower() in low:
            return True
    # 同一短语重复出现（如「我们的东西是有个人的东西」连说两遍），正常会议不会这样
    if len(low) >= 16:
        for span in (8, 10, 12):
            for i in range(len(low) - span * 2 + 1):
                unit = low[i:i + span]
                if len(unit) == span and low.count(unit) >= 2:
                    return True
    if re.search(r"(.)\1{5,}", low):
        return True
    return False


class AsrEngine:
    def __init__(self, model_name: str = "medium", cpu_threads: int = 8,
                 compute_type: str = "int8", beam_size: int = 1,
                 silence_threshold: float = SILENCE_THRESHOLD):
        self.model_name = model_name
        self.cpu_threads = cpu_threads
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.silence_threshold = silence_threshold
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            t0 = time.time()
            log.info("加载模型 %s (cpu/%s/%d线程)...", self.model_name, self.compute_type, self.cpu_threads)
            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                download_root=str(Path.home() / ".cache" / "faster-whisper"),
            )
            log.info("模型就绪，耗时 %.1fs", time.time() - t0)
        return self._model

    def transcribe_file(self, path: Path, offset: float, chunk_idx: int) -> list[dict]:
        return self.transcribe_array(load_audio(path), offset, chunk_idx)

    def transcribe_array(self, audio, offset: float = 0.0, chunk_idx: int = 1,
                         initial_prompt: str | None = None, beam_size: int | None = None,
                         vad_filter: bool = True, language: str = "zh") -> list[dict]:
        # 闸一：静音分片直接不送模型。
        # 判据用「峰值 + 能量」双重确认：只有两者都极低才算静音，
        # 避免低电平的正常录音被误杀。
        energy = frame_energy_p95(audio)
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        if energy < self.silence_threshold and peak < 0.006:
            log.info("分片 %d 静音(峰值=%.5f 能量P95=%.5f)，跳过转写", chunk_idx, peak, energy)
            return []

        segs, info = self.model.transcribe(
            audio,
            language=language,
            beam_size=beam_size if beam_size is not None else self.beam_size,
            vad_filter=vad_filter,
            vad_parameters={"min_silence_duration_ms": 500} if vad_filter else None,
            initial_prompt=initial_prompt or ZH_PROMPT,
            condition_on_previous_text=False,
            no_speech_threshold=NO_SPEECH_MAX,
            compression_ratio_threshold=COMPRESSION_MAX,
            log_prob_threshold=LOGPROB_MIN,
        )
        out = []
        dropped = 0
        for s in segs:
            txt = (s.text or "").strip()
            # 闸二 / 闸三：模型自评 + 特征串
            if getattr(s, "no_speech_prob", 0.0) > NO_SPEECH_MAX:
                dropped += 1
                continue
            if getattr(s, "avg_logprob", 0.0) < LOGPROB_MIN:
                dropped += 1
                continue
            if looks_hallucinated(txt):
                dropped += 1
                log.debug("丢弃疑似幻觉段: %s", txt[:40])
                continue
            out.append({
                "start": round(offset + s.start, 2),
                "end": round(offset + s.end, 2),
                "text": txt,
                "chunk": chunk_idx,
            })
        if dropped:
            log.info("分片 %d 过滤 %d 段疑似幻觉", chunk_idx, dropped)
        return out


class SessionTranscriber:
    """管理一个 session 的增量转写状态。"""

    def __init__(self, session_dir: Path, engine: AsrEngine, session_id: str):
        self.dir = Path(session_dir)
        self.engine = engine
        self.session_id = session_id
        self.json_path = self.dir / "transcript.json"
        self.data = self._load()

    def _load(self) -> Transcript:
        if self.json_path.exists():
            try:
                d = json.loads(self.json_path.read_text(encoding="utf-8"))
                known = {f for f in Transcript.__dataclass_fields__}
                return Transcript(**{k: v for k, v in d.items() if k in known})
            except Exception:
                log.exception("transcript.json 损坏，重建")
        return Transcript(session_id=self.session_id, engine="faster-whisper",
                          model=self.engine.model_name, language="zh")

    def _save(self):
        self.json_path.write_text(
            json.dumps(self.data.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def chunk_offset(self, chunk_idx: int) -> float:
        """按分片顺序累计前面所有分片的真实时长。"""
        total = 0.0
        for i in range(1, chunk_idx):
            f = self.dir / f"chunk_{i:05d}.wav"
            if f.exists():
                total += wav_duration(f)
        return total

    def transcribe_chunk(self, chunk_idx: int) -> int:
        if chunk_idx in self.data.done_chunks:
            return 0
        f = self.dir / f"chunk_{chunk_idx:05d}.wav"
        if not f.exists():
            return 0
        t0 = time.time()
        offset = self.chunk_offset(chunk_idx)
        segs = self.engine.transcribe_file(f, offset, chunk_idx)
        if not segs:
            self.data.silent_chunks.append(chunk_idx)
        self.data.segments.extend(segs)
        self.data.segments.sort(key=lambda x: x["start"])
        self.data.done_chunks.append(chunk_idx)
        self.data.duration = max([s["end"] for s in self.data.segments], default=self.data.duration)
        self.data.engine = "faster-whisper"
        self.data.model = self.engine.model_name
        self._save()
        log.info("分片 %d 转写完成：%d 段，耗时 %.1fs", chunk_idx, len(segs), time.time() - t0)
        return len(segs)

    def finalize(self, title: str = "") -> Path:
        md = self.data.to_markdown(title)
        out = self.dir / "transcript.md"
        out.write_text(md, encoding="utf-8")
        (self.dir / "transcript.txt").write_text(self.data.text(), encoding="utf-8")
        log.info("逐字稿已生成 %s (%d 字)", out, len(self.data.text()))
        return out


def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0
