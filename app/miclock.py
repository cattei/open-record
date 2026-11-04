"""
重写麦克风互斥
==============
原方案用 Windows 命名互斥体，但遇到一个致命缺陷：
占用方进程被强制结束时，Windows 把互斥体标记为 "abandoned"，
下一个进程的 WaitForSingleObject 返回 WAIT_ABANDONED，被误判为"获取成功"，
于是强行接管——实测就是这样导致互斥失效。

改为「对端探测」为主的方案：
1. 先问对端 HTTP 端口（旧服务 8899 / 本程序 8898）是否报告占用
2. 是 → 拒绝录音，并告知是谁在录
3. 否 → 再试互斥体（防止对方接口不可达但确实在录的极端情况）
4. abandoned 时，额外确认对端进程是否还活着，活着就不接管
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.request

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\RecAssistant_Microphone_Lock"

# 两套程序的 HTTP 端口，用于互相探测
PEERS = {
    "RecAssistant-旧服务": 8899,
    "open-record-GUI版": 8898,
}

_local = threading.local()
_guard = threading.Lock()
_holder_file = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "RecAssistant", "mic_holder.txt")


# ---------------- 占用者信息 ----------------

def write_holder(tag: str) -> None:
    try:
        os.makedirs(os.path.dirname(_holder_file), exist_ok=True)
        with open(_holder_file, "w", encoding="utf-8") as f:
            f.write(f"{tag}|{time.time()}")
    except Exception:
        pass


def read_holder() -> str:
    try:
        if not os.path.exists(_holder_file):
            return ""
        raw = open(_holder_file, encoding="utf-8").read().strip()
        parts = raw.split("|")
        tag = parts[0] if parts else ""
        ts = float(parts[1]) if len(parts) > 1 else 0
        if time.time() - ts > 12 * 3600:
            return ""
        return tag
    except Exception:
        return ""


def clear_holder() -> None:
    try:
        if os.path.exists(_holder_file):
            os.remove(_holder_file)
    except Exception:
        pass


# ---------------- 对端探测 ----------------

def probe_port(port: int, timeout: float = 1.5) -> dict | None:
    """查对端状态，返回 status dict；不可达返回 None。"""
    try:
        url = f"http://127.0.0.1:{port}/status"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return data.get("status") or data
    except Exception:
        return None


def probe_recording(exclude_port: int | None = None) -> tuple[bool, str]:
    """
    探测除自己之外的对端是否正在占用麦克风。
    返回 (是否被占用, 占用方名称)

    注意：不能只看 state == "recording"。录音停止后的收尾阶段
    （state == "stopping"，或 phase 仍在 recording/transcribing/archiving）
    麦克风通常还没释放，此时另一套程序开录会互相干扰。
    实测旧服务在 stopping 状态下 mic_open 仍为 true。
    """
    for name, port in PEERS.items():
        if exclude_port is not None and port == exclude_port:
            continue
        st = probe_port(port)
        if not st:
            continue
        state = st.get("state")
        phase = st.get("phase")
        mic_open = st.get("mic_open")
        busy = (
            state in ("recording", "stopping")
            or phase in ("recording", "transcribing", "archiving")
            or mic_open is True
        )
        if busy:
            return True, name
    return False, ""


def port_listening(port: int, timeout: float = 0.6) -> bool:
    """仅探测端口是否在监听（不解析内容，比 HTTP 更快更稳）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------- 互斥体（兜底） ----------------

class MicLock:
    """
    跨进程麦克风锁。
    优先看对端 HTTP 状态；HTTP 不可达时退回命名互斥体。
    """

    def __init__(self, name: str = MUTEX_NAME, my_port: int | None = None):
        self.name = name
        self.my_port = my_port
        self._handle = None
        self._owned = False

    # -- 互斥体底层 --
    def _create(self):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateMutexW(None, False, self.name)
        if not h:
            raise OSError("CreateMutexW 失败")
        self._k32 = k32
        return h

    def _wait(self, h, ms: int) -> int:
        return self._k32.WaitForSingleObject(h, ms)

    # -- 对外 --
    def acquire(self, timeout: float = 0.0) -> bool:
        """返回 True 表示成功占用麦克风。"""
        if getattr(_local, "depth", 0) > 0:
            _local.depth += 1
            return True

        with _guard:
            # 第一道：对端 HTTP 明确报告在录音
            busy, who = probe_recording(exclude_port=self.my_port)
            if busy:
                log.warning("对端【%s】正在录音，拒绝占用麦克风", who)
                write_holder(who)
                return False

            # 第二道：命名互斥体（防对端 HTTP 不可达但仍占用）
            if self._handle is None:
                try:
                    self._handle = self._create()
                except Exception:
                    log.exception("创建互斥体失败，仅依赖 HTTP 探测")
                    _local.depth = 1
                    self._owned = True
                    return True

            rc = self._wait(self._handle, int(timeout * 1000))
            if rc == 0x0:  # WAIT_OBJECT_0
                self._owned = True
                _local.depth = 1
                return True

            if rc == 0x80:  # WAIT_ABANDONED
                # 互斥体被遗留：持有者没正常 release 就退出了。
                # 判断标准必须看「对端是否真的在录音」，不能看端口是否监听
                # ——idle 状态同样会监听端口，会导致永远不接管。
                busy, who = probe_recording(exclude_port=self.my_port)
                if busy:
                    log.warning("互斥体遗留，且对端【%s】正在录音，不接管", who)
                    write_holder(who)
                    return False
                log.warning("互斥体由已退出的进程遗留（%s），接管", read_holder() or "未知")
                clear_holder()
                self._owned = True
                _local.depth = 1
                return True

            if rc == 0x102:  # WAIT_TIMEOUT
                holder = read_holder()
                log.warning("麦克风被占用（%s）", holder or "未知进程")
                return False

            raise OSError(f"WaitForSingleObject 返回 {rc}")

    def release(self) -> None:
        depth = getattr(_local, "depth", 0)
        if depth > 1:
            _local.depth = depth - 1
            return
        _local.depth = 0
        if self._owned and self._handle:
            try:
                self._k32.ReleaseMutex(self._handle)
            except Exception:
                log.exception("释放互斥体失败")
            self._owned = False

    def close(self):
        self.release()
        if self._handle:
            try:
                self._k32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
