"""
从归档 mp3 重新转写
==================
分片删掉之后，audio.mp3 是唯一的音频来源。本工具把它按 5 分钟切块重新转写，
覆盖原来的 transcript。适用于：换了模型、加了热词、修了过滤规则想重跑。

用法：
    python app/retranscribe.py <session_dir>
    python app/retranscribe.py <session_dir> --model medium
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

from asr import AsrEngine, Transcript, fmt_ts, load_audio

log = logging.getLogger("retranscribe")

BLOCK_SEC = 300


def check_deps() -> str:
    """检查重转所需依赖。发行包的精简 runtime 不含 av / faster_whisper，
    直接跑会抛 ModuleNotFoundError。返回空串表示可用，否则返回缺失说明。"""
    missing = []
    for mod, why in (("av", "解码 mp3"), ("faster_whisper", "语音识别")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(f"{mod}（{why}）")
    if missing:
        return (
            "重转需要以下依赖，但当前运行时没有：" + "、".join(missing) + "\n"
            "发行包里的 runtime 是精简版，不含这些包。\n"
            "如需重转，请在装有 faster-whisper 的完整 Python 环境里运行本脚本：\n"
            "    pip install faster-whisper av\n"
            "    python app/retranscribe.py <session_dir>\n"
            "原始 mp3 已完整保留，不会因无法重转而丢失。"
        )
    return ""


def retranscribe(session_dir: Path, model: str = "large-v3-turbo",
                 cpu_threads: int = 8, force: bool = False) -> Path | None:
    session_dir = Path(session_dir)
    # 先查依赖：缺依赖时给出可执行的说明，而不是抛一堆 traceback
    problem = check_deps()
    if problem:
        print(problem)
        return None
    mp3s = sorted(session_dir.glob("*.mp3"))
    if not mp3s:
        print(f"找不到 mp3 文件：{session_dir}")
        return None
    mp3 = mp3s[0]

    out_md = session_dir / "transcript.md"
    if out_md.exists() and not force:
        print(f"已存在逐字稿，加 --force 覆盖：{out_md}")
        return out_md

    t0 = time.time()
    audio = load_audio(mp3)
    dur = len(audio) / 16000
    print(f"音频 {dur:.1f} 秒，开始重转（{model}）…")

    engine = AsrEngine(model, cpu_threads=cpu_threads, compute_type="int8", beam_size=1)
    tr = Transcript(session_id=session_dir.name, engine="faster-whisper",
                    model=model, language="zh")

    step = BLOCK_SEC * 16000
    for i in range(0, len(audio), step):
        part = audio[i:i + step]
        idx = i // step + 1
        segs = engine.transcribe_array(part, offset=i / 16000, chunk_idx=idx)
        tr.segments.extend(segs)
        tr.done_chunks.append(idx)
        if not segs:
            tr.silent_chunks.append(idx)
        print(f"  块 {idx}: {len(segs)} 段")

    tr.segments.sort(key=lambda x: x["start"])
    tr.duration = max([s["end"] for s in tr.segments], default=dur)

    out_md.write_text(tr.to_markdown(session_dir.name), encoding="utf-8")
    (session_dir / "transcript.txt").write_text(tr.text(), encoding="utf-8")
    (session_dir / "transcript.json").write_text(
        json.dumps(tr.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"完成：{len(tr.text())} 字，{len(tr.merge_paragraphs())} 段，"
          f"耗时 {time.time()-t0:.1f}s → {out_md}")
    return out_md


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return
    model = "large-v3-turbo"
    if "--model" in sys.argv:
        model = sys.argv[sys.argv.index("--model") + 1]
    retranscribe(Path(args[0]), model=model, force="--force" in sys.argv)


if __name__ == "__main__":
    main()
