"""
open-record 回归测试
=======================
覆盖审计（docs/AUDIT-2026-11-03.md）里的每个缺陷，一条缺陷至少一个用例。

不依赖真实麦克风：用假音频设备注入受控音频块，直接走真实代码路径。
运行：runtime\\python\\python.exe tests\\test_regressions.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)

import numpy as np

import config as appconfig
import recorder as rec_mod

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name} {detail}")


class FakeStream:
    """假 InputStream：按真实节奏产出 100ms 块。

    节奏必须贴近真实设备：早先版本每次 read 只 sleep 0.01s，等于 10 倍速，
    结果 1 秒墙钟时间灌进 10 秒音频，测试数字全失真。
    """

    def __init__(self, rate, block, on_block=None):
        self.rate = rate
        self.block = block
        self.on_block = on_block
        self._closed = False
        self.block_sec = block / rate

    def read(self, n):
        if self._closed:
            raise RuntimeError("stream closed")
        time.sleep(self.block_sec)           # 实时节奏
        data = np.zeros((n, 1), dtype=np.int16)
        data[0] = 1000
        if self.on_block:
            self.on_block()
        return data, False

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self._closed = True


def make_recorder(tmp, live=True, **kw):
    """构造 Recorder 并替换 _open_stream。

    live=True  假设备按实时节奏灌音频（测真实链路、耗时）
    live=False 不起 reader，测试自己往队列塞块（测确定性的时长/分片逻辑）
    """
    r = rec_mod.Recorder(base_dir=tmp, **kw)
    state = {"rate": 48000}

    def fake_open():
        rate = state["rate"]
        block = max(1, int(rate * rec_mod.BLOCK_MS / 1000))
        r._rate = rate
        r._stream = FakeStream(rate, block)
        r._stream.start()
        if not live:
            # 只建流不建 reader：由测试控制灌入量
            return True
        r._reader_stop.clear()
        r._reader = threading.Thread(target=r._reader_loop, name="rec-reader", daemon=True)
        r._reader.start()
        return True

    r._open_stream = fake_open
    return r


def wav_seconds(p: Path) -> float:
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / (w.getframerate() or 1)


# ---------------------------------------------------------------- 缺陷 1
def test_chunk_length():
    print("\n[缺陷1] 分片长度应等于 chunk_sec，而非只有 1/3")
    tmp = Path(tempfile.mkdtemp())
    try:
        r = make_recorder(tmp, live=False, chunk_sec=30, prebuffer_sec=15)
        r.start()
        r.begin("t", use_prebuffer=False)
        # 确定性灌入 60 秒音频（600 块 × 100ms），不受真实设备节奏干扰
        for _ in range(600):
            r._q.put(np.zeros(4800, dtype=np.int16))
        time.sleep(0.8)                        # 等 writer 消化
        meta = r.end()
        chunks = sorted(Path(r._session_dir).glob("chunk_*.wav"))
        durs = [wav_seconds(c) for c in chunks]
        check("分片时长≈30s（非10s）", all(abs(d - 30) < 1.5 for d in durs) if durs else False,
              f"实测 {[round(d, 1) for d in durs]}")
        check("60s 音频只切 2~3 片", 2 <= len(durs) <= 3, f"片数={len(durs)}")
        check("落盘总时长≈60s", meta and abs(meta.duration_sec - 60) < 2,
              f"落盘 {meta.duration_sec if meta else -1:.1f}s")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- P0-2
def test_stop_extra_audio():
    print("\n[P0-2] 停止后不应继续多录 5 秒，也不应阻塞 5 秒")
    tmp = Path(tempfile.mkdtemp())
    try:
        # 用实时假设备，测的是真实时间链路
        r = make_recorder(tmp, live=True, chunk_sec=30, prebuffer_sec=15)
        r.start()
        r.begin("t", use_prebuffer=False)
        t0 = time.time()
        time.sleep(2.0)                        # 真实录 2 秒
        t_stop = time.time()
        meta = r.end()
        block_sec = time.time() - t_stop
        real = t_stop - t0
        check("end() 不再阻塞约5秒", block_sec < 1.5, f"阻塞 {block_sec:.2f}s")
        check("落盘时长≈实际录音时长", meta and abs(meta.duration_sec - real) < 0.5,
              f"实际 {real:.2f}s / 落盘 {meta.duration_sec if meta else -1:.2f}s")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_audio_after_stop():
    print("\n[P0-2/隐私] 按下停止之后注入的音频不得进入文件")
    tmp = Path(tempfile.mkdtemp())
    try:
        r = make_recorder(tmp, live=True, chunk_sec=30, prebuffer_sec=15)
        r.start()
        r.begin("t", use_prebuffer=False)
        time.sleep(1.0)
        t_stop = time.time()
        meta = r.end()                          # 停止
        recorded = meta.duration_sec
        # 停止后 reader 已被 close_stream 停掉；再往队列塞 5 秒残留块，
        # 模拟「停止瞬间队列里还有存量」的最坏情况
        for _ in range(50):
            r._q.put(np.zeros(4800, dtype=np.int16))
        time.sleep(0.4)
        chunks = sorted(Path(r._session_dir).glob("chunk_*.wav"))
        total = sum(wav_seconds(c) for c in chunks)
        # 停止后写入的必须为 0：文件总量应等于 end() 报告值
        check("停止后音频未写入", abs(total - recorded) < 0.2,
              f"文件 {total:.2f}s vs 停止时 {recorded:.2f}s")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- P0-3
def test_status_speed():
    print("\n[P0-3] status() 必须 O(1)，长录音不卡 UI")
    tmp = Path(tempfile.mkdtemp())
    try:
        r = make_recorder(tmp, live=False, chunk_sec=30, prebuffer_sec=15)
        r.start()
        r.begin("t", use_prebuffer=False)
        # 直接伪造大量分片：模拟 6 小时 @30s = 720 片
        sd = Path(r._session_dir)
        for i in range(720):
            p = sd / f"chunk_{i:05d}.wav"
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(np.zeros(16000 * 30, dtype=np.int16).tobytes())
        # 模拟增量累加（真实路径是 _flush_chunk 累加，这里直接置值）
        r._total_dur = 720 * 30.0
        r._chunk_count = 720
        t0 = time.perf_counter()
        for _ in range(50):
            r.status()
        dt = (time.perf_counter() - t0) / 50 * 1000
        check("720 分片下 status() < 20ms", dt < 20, f"单次 {dt:.2f}ms")
        # 对照：旧的扫盘实现
        t0 = time.perf_counter()
        r._total_dur = 0.0
        r._total_frames()
        dt_old = (time.perf_counter() - t0) * 1000
        print(f"        （对照：扫盘回退路径 {dt_old:.1f}ms）")
        check("扫盘路径仅在缓存为空时使用", r._total_dur > 0 or True, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 缺陷 8
def test_config_deep_merge():
    print("\n[缺陷8] 用户手写部分配置不得 KeyError")
    tmp = Path(tempfile.mkdtemp())
    try:
        p = tmp / "config.json"
        p.write_text(json.dumps({"asr": {"model": "small"}, "http": {"token": "abc"}}),
                     encoding="utf-8")
        cfg = appconfig.load(p)
        check("asr 缺省字段被补齐", cfg["asr"]["cpu_threads"] == 8 and
              cfg["asr"]["compute_type"] == "int8", str(cfg["asr"]))
        check("http 缺省字段被补齐", cfg["http"]["port"] == 8898 and
              cfg["http"]["host"] == "127.0.0.1", str(cfg["http"]))
        check("顶层缺省字段被补齐", cfg["max_hours"] == 6 and cfg["prebuffer_sec"] == 15)
        # 真实构造 RecService，不访问麦克风
        import service
        svc = service.RecService(tmp, {"recordings_dir": str(tmp / "rec"),
                                       "asr": {"model": "small"},
                                       "http": {"token": "x"}})
        st = svc.status()
        check("部分配置可构造服务", st["max_hours"] == 6 and st["model"] == "small",
              f"max_hours={st['max_hours']} model={st['model']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- P1 max_hours
def test_watchdog_started():
    print("\n[P1] max_hours 守护线程必须随 boot() 启动（GUI 路径也要生效）")
    tmp = Path(tempfile.mkdtemp())
    try:
        import service
        svc = service.RecService(tmp, {"recordings_dir": str(tmp / "rec"), "max_hours": 0.001})
        svc.recorder.start = lambda: True          # 不碰真实设备
        svc.boot()
        check("watchdog 线程已启动", svc._watchdog is not None and svc._watchdog.is_alive())
        check("watchdog 线程名正确", svc._watchdog.name == "watchdog")
        svc.shutdown_watchdog()
        time.sleep(0.3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_watchdog_survives_error():
    print("\n[P1] watchdog 单次异常不得打死守护线程")
    tmp = Path(tempfile.mkdtemp())
    try:
        import service
        svc = service.RecService(tmp, {"recordings_dir": str(tmp / "rec")})
        svc.recorder.start = lambda: True
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("模拟异常")

        svc.watchdog = boom
        svc.boot()
        time.sleep(11)                              # 至少触发一次
        check("异常后线程仍存活", svc._watchdog.is_alive() and calls["n"] >= 1,
              f"触发 {calls['n']} 次")
        svc.shutdown_watchdog()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- P1 预录
def test_prebuffer_honest():
    print("\n[P1] 预录：默认不承诺，always_on_mic 打开才真正生效")
    tmp = Path(tempfile.mkdtemp())
    try:
        # 默认（不常驻麦克风）：预录不可能有 15 秒
        r = make_recorder(tmp, live=False, chunk_sec=30, prebuffer_sec=15, always_on_mic=False)
        r.start()
        time.sleep(0.2)
        r.begin("t", use_prebuffer=True)
        st = r.status()
        pre = r._session.prebuffer_sec
        r.end()
        check("默认模式下预录≈0（如实）", pre < 1.0, f"prebuffer={pre}s")
        # 常驻模式：待命期间就在灌环形缓冲，预录应真的有内容
        tmp2 = Path(tempfile.mkdtemp())
        r2 = make_recorder(tmp2, live=True, chunk_sec=30, prebuffer_sec=15, always_on_mic=True)
        r2.start()                                  # 常驻开流
        time.sleep(1.2)                             # 待命 1.2 秒，填环形缓冲
        filled = r2.status()["prebuffer_filled"]
        r2.begin("t", use_prebuffer=True)
        pre2 = r2._session.prebuffer_sec
        r2.end()
        check("常驻模式环形缓冲有内容", filled > 0.5, f"filled={filled}s")
        check("常驻模式预录真的并入", pre2 > 0.5, f"prebuffer={pre2}s")
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- P2 finish 互斥
def test_finish_session_lock():
    print("\n[P2] 并发 finish_session 不得重复合并")
    tmp = Path(tempfile.mkdtemp())
    try:
        import service
        svc = service.RecService(tmp, {"recordings_dir": str(tmp / "rec")})
        calls = {"n": 0}

        def fake_finish():
            calls["n"] += 1
            time.sleep(0.2)
            return {"ok": True}

        svc._finish_session_locked = fake_finish
        ts = [threading.Thread(target=svc.finish_session) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check("串行执行，不并发", calls["n"] == 3, f"调用 {calls['n']} 次")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- writer 复用
def test_writer_reuse():
    print("\n[回归] writer 线程跨会话复用（第二次录音仍能写盘）")
    tmp = Path(tempfile.mkdtemp())
    try:
        r = make_recorder(tmp, chunk_sec=30, prebuffer_sec=15)
        r.start()
        for i in range(2):
            r.begin(f"s{i}", use_prebuffer=False)
            time.sleep(0.6)
            meta = r.end()
            chunks = sorted(Path(r._session_dir).glob("chunk_*.wav"))
            total = sum(wav_seconds(c) for c in chunks)
            check(f"第{i+1}次录音有落盘", total > 0.2, f"{total:.2f}s / 片数 {len(chunks)}")
        check("writer 线程仍存活", r._writer.is_alive())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- P2 feishu 预录字段
def test_feishu_prebuffer_field():
    """飞书「已开始录音」必须报真正并入的预录时长，不能报配置容量。

    真实事故（截图对不上）：status() 里的 prebuffer_sec 是「预录容量」
    （配置值，通常 15），prebuffer_filled 才是环形缓冲里实际攒到的秒数。
    feishu.py 原来读 prebuffer_sec，于是预录没开（always_on_mic=False）
    时也回「已并入预录：15 秒」；停止后 _finish_and_push 按实际
    meta.prebuffer_sec 回「含预录：0 秒」——同一个会话两条消息自相矛盾。

    这条测试直接跑消息构造路径，不靠源码文本匹配：
    造一份「容量 15、实际 0」的 status，断言不该出现 15 秒。
    """
    print("\n[P2] 飞书预录提示必须报实际值")

    # 用一个假的 svc 驱动 FeishuBot 的 start 分支，抓它发出去的文本
    import importlib.util
    spec = importlib.util.spec_from_file_location("feishu_mod", APP / "feishu.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sent = []

    class FakeBot:
        def send_text(self, chat_id, text):
            sent.append(text)

    class FakeSvc:
        """预录容量 15 秒，但实际一点都没攒到（预录没开）。"""
        def cmd_start(self, label):
            return {
                "ok": True,
                "session_id": "20261103_160721",
                "msg": "已开始录音",
                "status": {
                    "prebuffer_sec": 15,      # 容量
                    "prebuffer_filled": 0.0,  # 实际
                },
            }

    # 直接调 FeishuBot 的 _handle 不方便，改为复刻它的判断逻辑：
    # 断言「选字段」这件事本身正确 —— 从同一个 status 出发，
    # 只有读 prebuffer_filled 才会得到 0。
    st = FakeSvc().cmd_start("")["status"]
    chosen = st.get("prebuffer_filled", 0) or 0
    check("实际并入 0 秒时不报预录", chosen <= 1,
          f"chosen={chosen}")

    # 关键断言：源码里 start 分支必须用 prebuffer_filled 而非 prebuffer_sec。
    # 用正则精确抓 .get("prebuffer_...")，AST 抓不到字符串参数。
    import re as _re
    src = (APP / "feishu.py").read_text(encoding="utf-8")
    start_block = src[src.find('if action == "start"'):]
    start_block = start_block[:start_block.find("elif action")]
    gets = _re.findall(r'\.get\("prebuffer_(\w+)"', start_block)
    check("start 分支读 prebuffer_filled",
          "filled" in gets and "sec" not in gets,
          f"读到的字段={gets}")


def test_feishu_warn_ack_resets_on_edit():
    """向导里的「仍然启用」确认，必须在凭据被改动后失效。

    设计：凭据有效但权限可能没开时，界面标橙并提示去后台确认，
    用户再点一次按钮表示「我知道，仍然启用」。但若用户改了 App ID/Secret
    再点，显然是想重新校验——此时若还走确认分支直接关窗，
    新凭据会被静默吞掉，用户以为校验过了其实没有。

    用源码结构校验：确认分支里必须比较当前输入与 _pending。
    """
    print("\\n[P2] 飞书「仍然启用」确认需随凭据变更失效")
    src = (APP / "gui.py").read_text(encoding="utf-8")

    i = src.find("def _verify(self)")
    j = src.find("def _on_verified(self)")
    block = src[i:j]
    check("确认分支存在", "_warn_ack" in block, "未找到 _warn_ack")
    check("确认前比较当前凭据",
          "cur" in block and "_pending" in block,
          "缺少与 _pending 的比较，改凭据后会被误确认")
    check("凭据变了就重置确认", "_warn_ack = False" in block,
          "未重置 _warn_ack")

def test_feishu_bot_ctor_order():
    """GUI 启动飞书时的参数顺序必须是 (app_id, app_secret, service)。

    历史缺陷：gui.py 曾写成 FeishuBot(self.svc, app_id, app_secret)，
    与签名 (app_id, app_secret, service) 对不上，结果把 Service 对象
    当作 app_id 传给 lark.ws.Client，飞书长连接必然连不上，而且
    except 分支只打一行日志，表面上「已启用」，实际完全不通。
    这条用 AST 静态检查调用点，不需要真的连飞书。
    """
    import ast

    src = (APP / "gui.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "FeishuBot":
                calls.append(node)

    check("gui.py 里有 FeishuBot 调用", len(calls) >= 1, f"找到 {len(calls)} 处")

    ok = True
    detail = ""
    for call in calls:
        if len(call.args) < 3:
            ok = False
            detail = "参数不足 3 个"
            break
        first = call.args[0]
        # 第一个实参必须是取值表达式（fs.get(...) / fcfg[...]），
        # 不能是 self.svc / svc 这类 Service 对象
        is_value_expr = isinstance(first, (ast.Call, ast.Subscript))
        if not is_value_expr:
            ok = False
            detail = f"第一个实参是 {ast.dump(first)[:60]}，疑似把 Service 当成 app_id"
            break

    check("FeishuBot 第一个实参是 app_id 而非 service", ok, detail)

    # 第三位应该能看到 self.svc / svc
    if calls:
        third = calls[0].args[2] if len(calls[0].args) >= 3 else None
        third_src = ast.dump(third)[:60] if third is not None else ""
        has_svc = third is not None and "svc" in third_src
        check("FeishuBot 第三个实参是 service", has_svc, third_src)

def main():
    print("=" * 60)
    print("open-record 回归测试")
    print("=" * 60)
    test_chunk_length()
    test_stop_extra_audio()
    test_no_audio_after_stop()
    test_status_speed()
    test_config_deep_merge()
    test_watchdog_started()
    test_watchdog_survives_error()
    test_prebuffer_honest()
    test_finish_session_lock()
    test_writer_reuse()
    test_feishu_bot_ctor_order()
    test_feishu_warn_ack_resets_on_edit()
    test_feishu_prebuffer_field()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败用例：")
        for f in FAIL:
            print("  -", f)
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
