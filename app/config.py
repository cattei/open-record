"""
配置真源
========
`service.py` 与 `gui.py` 各有一份默认值，早已漂移
（`prebuffer_sec` 120 vs 15、`keep_native_rate` False vs True），
用户到底跑在哪个值上取决于从哪条路径进来，非常隐蔽。

本模块是唯一真源：所有默认值、深合并、读写都集中在这里。
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

log = logging.getLogger("config")

# 唯一默认值表。改这里，service 和 gui 同时生效。
DEFAULTS: dict = {
    "recordings_dir": "recordings",
    # 预录：待命期间麦克风是关闭的（见 recorder.start 注释），因此这个值
    # 只有在 always_on_mic=True 时才真正有意义。默认关闭常驻麦克风。
    "prebuffer_sec": 15,
    "always_on_mic": False,
    "chunk_sec": 30,
    "max_hours": 6,
    "device": None,
    "exclusive": False,
    "keep_native_rate": True,
    "merge_mp3": True,
    "mp3_bitrate": 192,
    "gain": 12.0,
    "transcribe": False,
    "autostart": True,
    "asr": {"model": "medium", "cpu_threads": 8, "compute_type": "int8", "beam_size": 1},
    "http": {"host": "127.0.0.1", "port": 8898, "token": ""},
    "feishu": {"enabled": False, "app_id": "", "app_secret": ""},
    "notify": {"type": "file"},
}

# 需要在设置界面里露出的键（供 GUI 遍历，避免手写清单与默认值漂移）
UI_KEYS = ("recordings_dir", "prebuffer_sec", "always_on_mic", "chunk_sec",
           "max_hours", "mp3_bitrate", "gain", "autostart", "transcribe")


def deep_merge(base: dict, over: dict) -> dict:
    """递归合并嵌套 dict。

    只做顶层 setdefault 的后果是：用户手写 `"asr": {"model": "small"}`
    会丢掉 cpu_threads / compute_type，之后 `cfg["asr"]["cpu_threads"]`
    直接 KeyError 崩溃（审计缺陷 8）。
    """
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(path: Path) -> dict:
    """读配置并与默认值深合并。文件缺失/损坏都退回默认值，绝不抛。"""
    if path is None:
        return copy.deepcopy(DEFAULTS)
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return deep_merge(DEFAULTS, raw)
        log.error("config.json 顶层不是对象，改用默认值")
    except Exception:
        log.exception("config.json 解析失败，改用默认值")
    return copy.deepcopy(DEFAULTS)


def save(path: Path, cfg: dict) -> None:
    """原子写：先写临时文件再 replace，避免断电留下半个 json。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize(cfg: dict) -> dict:
    """确保任何来源的 cfg 都满足 service 的直接下标访问（cfg["max_hours"] 等）。"""
    return deep_merge(DEFAULTS, cfg or {})
