"""
单实例控制
==========
托盘常驻程序最怕「双击两次开两个」——两个实例各开一条录音流会抢麦克风。
用 127.0.0.1 上的一个独占端口做锁：能绑上就是唯一实例，绑不上说明已有实例，
把「显示窗口」指令送过去后自己退出。

用 SO_EXCLUSIVEADDRUSE 而不是 SO_REUSEADDR：
Windows 上 REUSEADDR 允许两个进程绑同一端口，锁就形同虚设（本项目实测过）。
"""
from __future__ import annotations

import json
import logging
import socket
import threading

LOCK_HOST = "127.0.0.1"
# 8888 / 8898 都被本地服务占用（8898 是本程序自己的 HTTP 口，
# 8899 是另一套 RecAssistant 的口），单实例锁另选一个冷门号段。
LOCK_PORT = 47_311

log = logging.getLogger(__name__)


def _exclusive_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.settimeout(2.0)
    return s


def notify_existing(cmd: str = "show") -> bool:
    """已有实例在跑：把指令发过去。返回是否送达。"""
    s = _exclusive_socket()
    try:
        s.connect((LOCK_HOST, LOCK_PORT))
        s.sendall(json.dumps({"cmd": cmd}).encode("utf-8"))
        try:
            s.recv(64)          # 等对方回 ack，确认真的处理了
        except OSError:
            pass
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


class InstanceLock:
    """拿住锁并起一个监听线程接收后续实例的指令。"""

    def __init__(self, on_command):
        self._on_command = on_command
        self._srv: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        s = _exclusive_socket()
        try:
            s.bind((LOCK_HOST, LOCK_PORT))
            s.listen(4)
            s.settimeout(0.5)
            self._srv = s
            return True
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            return False

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="instance-lock", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            srv = self._srv
            if srv is None:
                return
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.settimeout(2.0)
                raw = conn.recv(256)
                cmd = "show"
                if raw:
                    try:
                        cmd = json.loads(raw.decode("utf-8")).get("cmd", "show")
                    except Exception:
                        pass
                try:
                    conn.sendall(b"ok")
                except OSError:
                    pass
                self._on_command(cmd)
            except Exception:
                log.exception("处理实例指令失败")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def release(self):
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
