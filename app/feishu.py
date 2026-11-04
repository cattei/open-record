"""
飞书长连接适配器
================
用企业自建应用的 App ID / App Secret 建立 WebSocket 长连接，接收消息事件。
相比「定时轮询」的根本优势是延迟：指令到达约 0.5 秒，不依赖任何定时任务存活。

指令很宽松，用户说话不用背命令：
  开始 / 开始录音 / 录音 / 录一下 / 记一下     → 开始录音
  停止 / 停 / 结束 / 好了                      → 停止并出稿
  取消 / 不要了 / 删掉                         → 丢弃本次
  状态 / 录多久了 / 还在录吗                    → 查询进度
其它任何话 → 回一条用法提示
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)

log = logging.getLogger("feishu")

CANCEL_WORDS = ("取消", "不要了", "删掉", "cancel")
STOP_WORDS = ("停止", "停", "结束", "好了", "可以了", "stop", "够了")
STATUS_WORDS = ("状态", "多久", "还在录", "进度", "status", "查询")
START_WORDS = ("开始", "录音", "录一下", "录一段", "记一下", "start", "录个")

HELP = (
    "录音助手用法：\n"
    "· 发「开始」或「录音」→ 开始录音\n"
    "· 发「停止」或「结束」→ 停止并出逐字稿\n"
    "· 发「状态」→ 查看已录时长\n"
    "· 发「取消」→ 丢弃本次录音"
)


class FeishuBot:
    def __init__(self, app_id: str, app_secret: str, service, default_chat: str | None = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.svc = service
        self.default_chat = default_chat
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._ws = None

    # ---------------- 发送 ----------------
    def send_text(self, chat_id: str, text: str) -> bool:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        try:
            resp = self.client.im.v1.message.create(req)
            if not resp.success():
                log.error("发送失败 code=%s msg=%s", resp.code, resp.msg)
                return False
            return True
        except Exception:
            log.exception("发送消息异常")
            return False

    def send_long_text(self, chat_id: str, text: str, limit: int = 3500) -> None:
        """飞书单条文本过长会被截断，按段落切片发送。"""
        if len(text) <= limit:
            self.send_text(chat_id, text)
            return
        buf = ""
        for para in text.split("\n"):
            if len(buf) + len(para) + 1 > limit:
                if buf:
                    self.send_text(chat_id, buf)
                    time.sleep(0.25)
                buf = para
            else:
                buf = (buf + "\n" + para) if buf else para
        if buf:
            self.send_text(chat_id, buf)

    def send_file(self, chat_id: str, path: Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        try:
            with open(path, "rb") as f:
                body = (
                    CreateFileRequestBody.builder()
                    .file_type("stream")
                    .file_name(path.name)
                    .file(f)
                    .build()
                )
                req = CreateFileRequest.builder().request_body(body).build()
                resp = self.client.im.v1.file.create(req)
            if not resp.success():
                log.error("上传文件失败 code=%s msg=%s", resp.code, resp.msg)
                return False
            file_key = resp.data.file_key
            msg_body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("file")
                .content(json.dumps({"file_key": file_key}))
                .build()
            )
            mreq = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(msg_body)
                .build()
            )
            mresp = self.client.im.v1.message.create(mreq)
            if not mresp.success():
                log.error("发送文件消息失败 code=%s msg=%s", mresp.code, mresp.msg)
                return False
            return True
        except Exception:
            log.exception("发送文件异常")
            return False

    # ---------------- 接收 ----------------
    def _clean(self, text: str) -> str:
        t = re.sub(r"@_user_\d+", "", text or "")
        t = re.sub(r"@\S+\s*", "", t)
        return t.strip()

    def _classify(self, text: str) -> str:
        t = text.lower()
        if any(w in t for w in CANCEL_WORDS):
            return "cancel"
        if any(w in t for w in STOP_WORDS):
            return "stop"
        if any(w in t for w in STATUS_WORDS):
            return "status"
        if any(w in t for w in START_WORDS):
            return "start"
        return "help"

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            ev = data.event
            msg = ev.message
            if msg.message_type != "text":
                return
            mid = getattr(msg, "message_id", "")
            with self._lock:
                if mid in self._seen:
                    return
                self._seen.add(mid)
                if len(self._seen) > 500:
                    self._seen = set(list(self._seen)[-250:])
            # 忽略机器人自己发的
            sender_type = getattr(getattr(ev, "sender", None), "sender_type", "")
            if sender_type == "app":
                return

            chat_id = msg.chat_id
            try:
                text = json.loads(msg.content).get("text", "")
            except Exception:
                text = ""
            text = self._clean(text)
            action = self._classify(text)
            log.info("收到飞书指令 [%s] chat=%s text=%r", action, chat_id[-6:], text[:40])

            if action == "start":
                label = self._extract_label(text)
                r = self.svc.cmd_start(label)
                if r.get("ok"):
                    pre = r["status"].get("prebuffer_sec", 0)
                    msg = f"🔴 已开始录音\n会话：{r['session_id']}\n"
                    if pre and pre > 1:
                        msg += f"已并入预录：{pre} 秒\n"
                    msg += "说完发「停止」即可。"
                    self.send_text(chat_id, msg)
                else:
                    self.send_text(chat_id, f"⚠️ {r.get('msg')}")
            elif action == "stop":
                r = self.svc.cmd_stop()
                if not r.get("ok"):
                    self.send_text(chat_id, f"⚠️ {r.get('msg')}")
                    return
                rec = r.get("recorded_sec", r.get("duration_sec", 0))
                pre = r.get("prebuffer_sec", 0)
                self.send_text(
                    chat_id,
                    f"⏹ 已停止录音\n"
                    f"录音时长：{self._fmt(rec)}\n"
                    f"正在合成音频文件，稍后发你。",
                )
                threading.Thread(
                    target=self._finish_and_push, args=(chat_id, rec, pre), daemon=True
                ).start()
            elif action == "cancel":
                r = self.svc.cmd_cancel()
                self.send_text(chat_id, f"🗑 {r.get('msg')}")
            elif action == "status":
                st = self.svc.status()
                if st["state"] == "recording":
                    self.send_text(
                        chat_id,
                        f"🔴 录音中\n"
                        f"已录：{self._fmt(st.get('elapsed_sec', 0))}\n"
                        f"当前电平：{st.get('level', 0)}\n"
                        f"待转写分片：{st.get('pending_chunks', 0)}",
                    )
                elif st["phase"] == "transcribing":
                    self.send_text(chat_id, f"⏳ 转写中，剩 {st.get('pending_chunks', 0)} 个分片")
                else:
                    lr = st.get("last_result")
                    if lr:
                        self.send_text(
                            chat_id,
                            f"⚪ 空闲\n上次录音：{lr['session_id']}，{lr['chars']} 字，{lr['finished_at']}",
                        )
                    else:
                        self.send_text(chat_id, "⚪ 空闲，等待指令。")
            else:
                self.send_text(chat_id, HELP)
        except Exception:
            log.exception("处理飞书消息异常")

    def _extract_label(self, text: str) -> str:
        """从「开始录音 产品周会」里取出场景名。"""
        m = re.search(r"(?:录音|开始|记一下)\s*[：: ]\s*(.+)", text)
        if m:
            return m.group(1).strip()[:40]
        m = re.search(r"(?:开始录音|录音)\s+(.+)", text)
        if m:
            return m.group(1).strip()[:40]
        return ""

    def _finish_and_push(self, chat_id: str, recorded: float, pre: float) -> None:
        """停止后直接把 mp3 发回，不等待转写。"""
        try:
            res = self.svc.finish_session()
            if not res:
                self.send_text(chat_id, "⚠️ 收尾失败，请查看服务日志")
                return
            mp3 = res.get("audio_mp3")
            mb = res.get("audio_bytes", 0) / 1024 / 1024
            peak = res.get("peak_level", 0)
            head = (f"🎧 录音文件\n"
                    f"录音时长：{self._fmt(recorded)}\n"
                    f"含预录：{pre:.0f} 秒\n"
                    f"文件大小：{mb:.1f} MB")
            if peak < 200:
                head += "\n\n⚠️ 全程峰值很低（{}），这段大概率没采到声音。".format(peak)
            self.send_text(chat_id, head)
            if mp3 and Path(mp3).exists():
                ok = self.send_file(chat_id, Path(mp3))
                if not ok:
                    self.send_text(chat_id, "⚠️ 文件发送失败，但已保存在本机会话目录。")
            else:
                self.send_text(chat_id, "⚠️ 音频合成失败，分片已保留在会话目录。")
            if res.get("transcribed"):
                if res.get("chars", 0) > 0:
                    self.send_long_text(
                        chat_id, f"📄 逐字稿（{res['chars']} 字）\n\n{res.get('preview', '')}"
                    )
                    self.send_file(chat_id, Path(res["transcript_md"]))
                else:
                    self.send_text(chat_id, "📄 转写未检测到有效人声。")
        except Exception:
            log.exception("收尾推送异常")
            self.send_text(chat_id, "⚠️ 收尾推送异常，请查看服务日志")

    def _finalize_and_push(self, chat_id: str) -> None:
        try:
            res = self.svc.finalize()
            if not res:
                self.send_text(chat_id, "⚠️ 出稿失败，请查看服务日志")
                return
            if res["chars"] == 0:
                self.send_text(
                    chat_id,
                    f"📄 录音已结束（{self._fmt(res['duration_sec'])}），"
                    f"但未检测到有效人声，未生成内容。\n"
                    f"建议检查麦克风音量，或把 config.json 的 exclusive 改为 true。",
                )
                return
            paras = res.get("preview", "")
            head = (
                f"📄 逐字稿已生成\n"
                f"时长：{self._fmt(res['duration_sec'])}\n"
                f"字数：{res['chars']}\n"
                f"段落：{res['segments']}\n\n"
                f"{paras}{'…' if res['chars'] > len(paras) else ''}"
            )
            self.send_long_text(chat_id, head)
            self.send_file(chat_id, Path(res["transcript_md"]))
        except Exception:
            log.exception("出稿推送异常")
            self.send_text(chat_id, "⚠️ 出稿推送异常，请查看服务日志")

    @staticmethod
    def _fmt(sec: float) -> str:
        sec = int(sec or 0)
        h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
        return f"{h}小时{m}分{s}秒" if h else (f"{m}分{s}秒" if m else f"{s}秒")

    # ---------------- 启动 ----------------
    def start(self) -> bool:
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        self._ws = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        t = threading.Thread(target=self._run, name="feishu-ws", daemon=True)
        t.start()
        log.info("飞书长连接线程已启动")
        return True

    def _run(self):
        try:
            self._ws.start()   # 阻塞，SDK 内部自动重连
        except Exception:
            log.exception("飞书长连接异常退出")
