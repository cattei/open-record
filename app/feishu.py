"""
飞书长连接适配器
================
用企业自建应用的 App ID / App Secret 建立 WebSocket 长连接，接收消息事件。
相比「定时轮询」的根本优势是延迟：指令到达约 0.5 秒，不依赖任何定时任务存活。

指令很宽松，用户说话不用背命令：
  开始 / 开始录音 / 录音 / 录一下 / 记一下     → 开始录音
  停止 / 停 / 结束 / 好了                      → 停止，把 mp3 发回聊天
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

# 飞书 im/v1/file/create 的硬性上限（官方文档：文件大小不得超过 30 MB）。
# 长录音很容易超过：192kbps 下 30MB 只够约 22 分钟，一小时就有 82MB。
# 超限时接口必然失败，所以提前判断并把原因说清楚，而不是笼统报「发送失败」。
FEISHU_FILE_LIMIT_MB = 30

# 分段时每段的目标大小。比上限再留一点余量：接口按 HTTP body 计，
# 多段 form 的边界字节也算在内，卡在 30.0 会偶发超限。
SPLIT_TARGET_MB = 28

# MPEG 帧长查表。注意要覆盖 MPEG1 和 MPEG2/2.5 —— lameenc 在低采样率
# （≤24kHz，本程序默认 16kHz 就属于这类）下输出的是 **MPEG2 Layer III**，
# 帧头 version bits = 2 而不是 3。只查 MPEG1 表会把合法帧判成非帧头，
# 切分就退化成按字节硬切，从帧中间断开、播放器报「文件损坏」。
_MP3_BITRATES = {
    # (版本组, layer) -> 位率表（kbps）。版本组 1=MPEG1，2=MPEG2/2.5
    (1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    (2, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}
_MP3_RATES = {
    1: (44100, 48000, 32000, 0),      # MPEG1
    2: (22050, 24000, 16000, 0),      # MPEG2
}
# 每帧样本数：MPEG1 L3 是 1152，MPEG2/2.5 L3 也是 1152
_MP3_FRAME_SAMPLES = 1152

SPLIT_WORDS = ("切分", "分割", "分片", "分段", "拆分", "split")
CANCEL_WORDS = ("取消", "不要了", "删掉", "cancel")
STOP_WORDS = ("停止", "停", "结束", "好了", "可以了", "stop", "够了")
STATUS_WORDS = ("状态", "多久", "还在录", "进度", "status", "查询")
START_WORDS = ("开始", "录音", "录一下", "录一段", "记一下", "start", "录个")

HELP = (
    "录音助手用法：\n"
    "· 发「开始」或「录音」→ 开始录音\n"
    "· 发「停止」或「结束」→ 停止并把 mp3 发回这个聊天\n"
    "· 发「状态」→ 查看已录时长\n"
    "· 发「切分」→ 把上一段超限的录音切段发过来\n"
    "· 发「取消」→ 丢弃本次录音"
)


def _mp3_frame_len(data: bytes, i: int) -> int:
    """在偏移 i 处按 MPEG Layer III 帧头算出该帧字节数；不是帧头则返回 0。

    同时支持 MPEG1（version bits=3）与 MPEG2/2.5（version bits=2/0）。
    本程序默认 16kHz 采样，lameenc 会输出 MPEG2 L3——只认 MPEG1 是错的。

    遇到不认识的字节返回 0，让上层退回按字节切——那种切法偶尔会在帧中间
    断开，但绝大多数播放器仍能容错播放，总比完全发不出去好。
    """
    if i + 4 > len(data):
        return 0
    b1 = data[i + 1]
    if data[i] != 0xFF or (b1 & 0xE0) != 0xE0:
        return 0
    ver_bits = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    if layer_bits != 1:                 # 只处理 Layer III
        return 0
    if ver_bits == 3:                   # MPEG1
        vgrp, rate_tbl = 1, _MP3_RATES[1]
    elif ver_bits == 2:                 # MPEG2
        vgrp, rate_tbl = 2, _MP3_RATES[2]
    elif ver_bits == 0:                 # MPEG2.5
        vgrp, rate_tbl = 2, (11025, 12000, 8000, 0)
    else:                               # 保留值
        return 0
    br_tbl = _MP3_BITRATES.get((vgrp, 3))
    if not br_tbl:
        return 0
    br_idx = (data[i + 2] >> 4) & 0x0F
    sr_idx = (data[i + 2] >> 2) & 0x03
    pad = (data[i + 2] >> 1) & 0x01
    br = br_tbl[br_idx] * 1000
    sr = rate_tbl[sr_idx]
    if br <= 0 or sr <= 0:
        return 0
    # MPEG1 L3: 1152 samples -> 144*br/sr；MPEG2/2.5 L3: 576 samples -> 72*br/sr
    k = 144 if vgrp == 1 else 72
    return k * br // sr + pad


def split_mp3(path: Path, target_mb: int = SPLIT_TARGET_MB,
              target_bytes: int | None = None) -> list[Path]:
    """把大 mp3 切成若干段，每段都能独立播放。

    按帧边界切：mp3 是帧流，从帧中间切会丢同步头，部分播放器报「文件损坏」。
    本程序产出的 CBR 单声道 mp3 帧长固定，从帧头开始切每段都是合法 mp3。

    原文件保留不动——用户明确要求「文件是在的」，切分只为能发出去，
    不能动他的原始录音。分段写成同目录的 .part01.mp3 等，随时可删。

    target_bytes 供测试用小样本验证（真实使用按 target_mb 走）。
    """
    path = Path(path)
    size = path.stat().st_size
    target = target_bytes if target_bytes else target_mb * 1024 * 1024
    if size <= target:
        return []

    # 跳过可能的 ID3 标签，找到第一个真帧头
    start = 0
    with open(path, "rb") as f:
        head = f.read(min(4096, size))
    for i in range(max(0, len(head) - 4)):
        if _mp3_frame_len(head, i) > 0:
            start = i
            break

    parts: list[Path] = []
    with open(path, "rb") as src:
        pos = start
        idx = 0
        while pos < size:
            remain = size - pos
            if remain <= target:
                take = remain
            else:
                # 在目标位置附近向前回溯找帧边界。一帧 626 字节，回溯 4KB 足够。
                src.seek(pos + target - 4096)
                probe = src.read(4096 + 4)
                base = pos + target - 4096
                take = target
                for back in range(min(4096, len(probe))):
                    cand_abs = base + back
                    if cand_abs <= pos:
                        break
                    if _mp3_frame_len(probe, back) > 0:
                        take = cand_abs - pos
                        break
                if take <= 0:
                    take = target
            idx += 1
            out = path.with_name(f"{path.stem}.part{idx:02d}{path.suffix}")
            src.seek(pos)
            written = 0
            with open(out, "wb") as dst:
                while written < take:
                    buf = src.read(min(1 << 20, take - written))
                    if not buf:
                        break
                    dst.write(buf)
                    written += len(buf)
            if written <= 0:
                out.unlink(missing_ok=True)
                break
            parts.append(out)
            log.info("切分 %s -> %s（%.1f MB）", path.name, out.name,
                     written / 1024 / 1024)
            pos += written
    return parts


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
        # 上一段因超限没发出去的 mp3，供「切分」指令取用。
        # 存最近一次即可：用户看到超限提示后立刻回复「切分」，
        # 这种交互本来就紧邻发生，留历史队列反而会让「切分」切错文件。
        self._oversize: Path | None = None
        self._oversize_chat: str | None = None

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
            # 成功也留痕：原来只记失败，导致「收尾后到底发了什么、发没发成功」
            # 在日志里完全查不到（真实事故：19:41 收尾完成后日志一片空白，
            # 无法判断是发送失败还是根本没走到发送）。
            log.info("已发文本 chat=%s len=%d 摘要=%r",
                     chat_id[-6:], len(text), text[:40].replace("\n", " "))
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
            try:
                mb = path.stat().st_size / 1024 / 1024
            except OSError:
                mb = 0
            log.info("已发文件 chat=%s %s（%.1f MB）", chat_id[-6:], path.name, mb)
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
        # 切分要最先判：它只在大文件超限后使用，且「分割/分段」这类词
        # 不该被「取消」「停止」的模糊匹配抢先命中。
        # 用「切」单字兜住「切一下吧」「帮我切了」这类口语说法。
        if any(w in t for w in SPLIT_WORDS) or ("切" in t and len(t) <= 12):
            return "split"
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
                    # 注意：status() 里的 prebuffer_sec 是「预录容量」（配置值，
                    # 比如 15），不是真正并入的时长；真正并入量看 prebuffer_filled。
                    # 之前这里读 prebuffer_sec，导致预录没开时也会报「已并入预录 15 秒」，
                    # 而停止后又按实际并入量报「含预录 0 秒」，两条消息自相矛盾。
                    st = r.get("status") or {}
                    pre = st.get("prebuffer_filled", 0) or 0
                    msg = f"🔴 已开始录音\n会话：{r['session_id']}\n"
                    if pre > 1:
                        msg += f"已并入预录：{pre:.0f} 秒\n"
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
            elif action == "split":
                p, src_chat = self._oversize, self._oversize_chat
                if not p or not p.exists():
                    self.send_text(
                        chat_id,
                        "现在没有需要切分的文件。\n"
                        "「切分」用于处理刚提示过超限的那段录音；"
                        "其它时候直接发「录音」/「停止」即可。",
                    )
                    return
                self.send_text(
                    chat_id,
                    f"✂️ 开始切分：{p.name}\n"
                    f"按 {SPLIT_TARGET_MB}MB 一段，切好逐段发你。原文件不动。",
                )
                threading.Thread(
                    target=self._split_and_push, args=(chat_id, p), daemon=True
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
                p = Path(mp3)
                try:
                    size_mb = p.stat().st_size / 1024 / 1024
                except OSError:
                    size_mb = mb
                # 飞书 im/v1/file/create 的硬上限是 30MB（官方文档「不超过 30 MB」）。
                # 超限时整段必然传不上去，与其等接口报错再给一句笼统的「发送失败」，
                # 不如提前把原因和可行范围讲清楚——文件本身完好，用户不用慌。
                if size_mb > FEISHU_FILE_LIMIT_MB:
                    # 按本次实际码率估算可发时长，不写死「半小时」这类数字：
                    # 192kbps 只够 22 分钟，128kbps 才到 33 分钟，写死会误导。
                    rate_mb_per_min = (mb / (recorded / 60)) if recorded > 0 else 0
                    fit_min = (FEISHU_FILE_LIMIT_MB / rate_mb_per_min
                               if rate_mb_per_min > 0 else 0)
                    tip = (f"约 {fit_min:.0f} 分钟以内的录音可以正常发送"
                           if fit_min > 0 else "较短的录音可以正常发送")
                    # 记下这个文件，用户回「切分」时直接取用
                    self._oversize = p
                    self._oversize_chat = chat_id
                    self.send_text(
                        chat_id,
                        f"⚠️ 文件太大，发不到飞书（这段 {size_mb:.0f} MB，"
                        f"飞书限制 {FEISHU_FILE_LIMIT_MB} MB）。\n"
                        f"录音本身完好，已保存在本机：\n{p}\n"
                        f"{tip}，更长的话到电脑上取文件即可。\n\n"
                        f"如果现在就要，回一句「切分」，我按 "
                        f"{SPLIT_TARGET_MB}MB 一段切好发你（原文件不动）。",
                    )
                elif not self.send_file(chat_id, p):
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

    def _split_and_push(self, chat_id: str, path: Path) -> None:
        """把超限的 mp3 切段并逐段发送。

        切分在后台线程做：300MB 的文件读一遍就要几秒，不能卡住长连接的事件线程。
        每段发完立刻删掉该段文件——它们只是中转产物，留着白占磁盘。
        原文件始终保留。
        """
        try:
            parts = split_mp3(path)
            if not parts:
                self.send_text(chat_id, "⚠️ 切分没有产生分段，文件可能小于上限。")
                return
            total = len(parts)
            self.send_text(chat_id, f"✂️ 切好了，共 {total} 段，开始发送…")
            ok_n = 0
            for i, part in enumerate(parts, 1):
                try:
                    size_mb = part.stat().st_size / 1024 / 1024
                except OSError:
                    size_mb = 0
                if self.send_file(chat_id, part):
                    ok_n += 1
                else:
                    self.send_text(
                        chat_id,
                        f"⚠️ 第 {i}/{total} 段发送失败（{part.name}，"
                        f"{size_mb:.1f} MB），文件仍在本地。",
                    )
                # 发完即删：这些分段只是中转产物，原文件不动
                try:
                    part.unlink(missing_ok=True)
                except Exception:
                    log.warning("删除分段失败（被占用）：%s", part.name)
            self.send_text(
                chat_id,
                f"✅ 切分发送完成：{ok_n}/{total} 段。\n"
                f"原文件仍在：{path}",
            )
        except Exception:
            log.exception("切分发送异常")
            self.send_text(chat_id, "⚠️ 切分发送异常，请查看服务日志。")

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
            # 飞书还会推一批我们不需要处理的事件。不注册处理器时 SDK 会打
            # 「processor not found」的 ERROR，看着像故障其实无害。
            # 这里统一注册空处理器吞掉，保持日志干净——真正出问题时才显眼。
            #   im.message.message_read_v1               消息已读回执
            #   im.chat.access_event.bot_p2p_chat_entered_v1  用户进入机器人单聊
            .register_p2_im_message_message_read_v1(lambda _d: None)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(lambda _d: None)
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
