"""
托盘提升（trayfix）测试
========================
覆盖两件事：
  1. 匹配逻辑本身（纯函数，注入假路径/假条目，不碰真实注册表）；
  2. 本机真实注册表里当前解释器的那条记录确实 IsPromoted=1。

第 2 项在没有图标的机器上会返回「找不到记录」，这不算失败 —— 测试要能在
「还没启动过应用」的环境里跑通，所以它按状态分支断言，而不是硬要求存在。

运行：runtime\\python\\python.exe tests\\test_tray_promotion.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))

import trayfix

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name} {detail}")


# ---------------------------------------------------------------- 匹配逻辑
def test_normalize():
    print("\n[匹配] 路径规范化")
    a = trayfix.normalize(r"C:\code\open-record\runtime\python\pythonw.exe")
    b = trayfix.normalize(r"c:\CODE\Open-Record\runtime\python\PYTHONW.EXE")
    check("大小写不敏感", a == b, f"{a} == {b}")
    c = trayfix.normalize('"C:\\code\\open-record\\runtime\\python\\pythonw.exe"')
    check("去掉引号", c == a)
    d = trayfix.normalize(r"C:/code/open-record/runtime/python/pythonw.exe")
    check("正斜杠等价", d == a, d)
    check("空值安全", trayfix.normalize("") == "" and trayfix.normalize(None) == "")


def test_match_entries():
    print("\n[匹配] 按 ExecutablePath 反查条目（不按 key 名硬编码）")
    exe = r"C:\code\open-record\runtime\python\pythonw.exe"
    entries = [
        ("111", {"executablepath": r"C:\other\app.exe", "initialtooltip": "别的应用"}),
        ("222", {"executablepath": exe.upper(), "initialtooltip": "录音助手 · 待命"}),
        ("333", {"executablepath": r"C:\code\open-record\runtime\python\python.exe",
                 "initialtooltip": ""}),
    ]
    hits = trayfix.match_entries(entries, exe)
    check("大小写不同也能命中", hits == ["222"], str(hits))
    check("不误伤同目录的 python.exe",
          "333" not in trayfix.match_entries(entries, r"C:\code\open-record\runtime\python\pythonw.exe"))
    check("命中 python.exe 那条",
          trayfix.match_entries(entries, r"C:\code\open-record\runtime\python\python.exe") == ["333"])

    # 同一个解释器承载多个托盘应用：靠提示挑出自己那条
    shared = [
        ("s1", {"executablepath": exe, "initialtooltip": "另一个脚本"}),
        ("s2", {"executablepath": exe, "initialtooltip": "录音助手 · 待命"}),
    ]
    check("同 exe 多条时按提示挑自己", trayfix.match_entries(shared, exe) == ["s2"],
          str(trayfix.match_entries(shared, exe)))

    # 都没带提示名时不能漏，全部提升
    plain = [("p1", {"executablepath": exe}), ("p2", {"executablepath": exe})]
    check("无提示名时不漏条目", set(trayfix.match_entries(plain, exe)) == {"p1", "p2"})


def test_missing_key_safe():
    print("\n[容错] 键不存在/路径为空时安全返回，不抛异常")
    fake_root = r"Software\open-record-nonexistent\trayfix-probe"
    try:
        st = trayfix.promote(exe_path=r"C:\no\such\app.exe", root=fake_root)
        check("不存在的注册表项 -> UNSUPPORTED", st == trayfix.UNSUPPORTED, st)
    except Exception as e:  # 任何异常都算失败
        check("不存在的注册表项不应抛异常", False, repr(e))

    try:
        st = trayfix.promote(exe_path="", root=fake_root)
        check("空路径 -> UNSUPPORTED", st == trayfix.UNSUPPORTED, st)
    except Exception as e:
        check("空路径不应抛异常", False, repr(e))

    try:
        names = trayfix.find_entries(r"C:\no\such\app.exe", root=fake_root)
        check("找不到记录返回空列表", names == [], str(names))
    except Exception as e:
        check("find_entries 不应抛异常", False, repr(e))

    # 真实现场：真实 NotifyIconSettings 下查一个绝不存在的路径
    if sys.platform.startswith("win"):
        names = trayfix.find_entries(r"C:\definitely\not\here\fake.exe")
        check("真实根键下查假路径 -> 空", names == [], str(names))


def test_promoter_retry():
    print("\n[时序] 重试器在「还没记录」时会继续试，达到上限后停")
    p = trayfix.TrayPromoter(exe_path=r"C:\no\such\app.exe", attempts=3,
                             root=r"Software\open-record-nonexistent\trayfix-probe")
    seq = [p.step() for _ in range(4)]
    check("第一次就判定不支持（根键不存在，无需重试）",
          seq[0] == trayfix.UNSUPPORTED and p.done, str(seq))
    check("done 之后不再消耗次数", p.tries == 1, f"tries={p.tries}")


def test_promoter_not_found_retry():
    print("\n[时序] NOT_FOUND 会持续重试，次数用尽才 done（这是 GUI 依赖的语义）")
    # 用一个「根键存在、但里面没有本进程记录」的真实注册表位置：
    # 探针根键 Software\open-record-probe-empty 由本测试自己建、自己删。
    import winreg
    probe_root = r"Software\open-record-probe-empty"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, probe_root):
            pass
    except OSError as e:
        print(f"  SKIP  建不了探针键（{e}）")
        return
    try:
        p = trayfix.TrayPromoter(exe_path=r"C:\no\such\app.exe", attempts=3,
                                 root=probe_root)
        seq = [p.step() for _ in range(5)]
        check("每次都是 NOT_FOUND（还没记录时不放弃）",
              seq[:3] == [trayfix.NOT_FOUND] * 3, str(seq))
        check("第 3 次（达到 attempts）后 done", p.done is True, f"done={p.done}")
        check("只消耗 attempts 次，不多试", p.tries == 3, f"tries={p.tries}")
        check("done 后 step 幂等，不再增长 tries",
              p.step() == trayfix.NOT_FOUND and p.tries == 3, f"tries={p.tries}")
        check("status 保留最后一次结果",
              p.status == trayfix.NOT_FOUND, str(p.status))
    finally:
        # 必须清掉探针键：留在注册表里会污染真实环境的排查
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, probe_root)
        except OSError:
            pass


def test_promoter_promotes_on_retry():
    print("\n[时序] 记录在重试中途才出现 -> 那次 step 就该拿到 PROMOTED")
    # 模拟 explorer 延迟写入：先用空探针根键跑一次（NOT_FOUND），
    # 再往根键里塞一条属于目标 exe 的假记录，下一次 step 必须提升成功。
    import winreg
    probe_root = r"Software\open-record-probe-late"
    fake_exe = r"C:\fake\tray\pythonw.exe"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, probe_root):
            pass
    except OSError as e:
        print(f"  SKIP  建不了探针键（{e}）")
        return
    try:
        p = trayfix.TrayPromoter(exe_path=fake_exe, attempts=3, root=probe_root)
        first = p.step()
        check("第一次还没有记录 -> NOT_FOUND 且未 done",
              first == trayfix.NOT_FOUND and not p.done, f"{first} done={p.done}")

        key = f"{probe_root}\\999"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "ExecutablePath", 0, winreg.REG_SZ, fake_exe)
            winreg.SetValueEx(k, "InitialTooltip", 0, winreg.REG_SZ, "录音助手")
            # 故意不写 IsPromoted，模拟 Win11 的原始状态

        second = p.step()
        check("第二次拿到 PROMOTED 并 done",
              second == trayfix.PROMOTED and p.done, f"{second} done={p.done}")
        check("假条目 IsPromoted 被写成 1",
              trayfix.is_promoted("999", root=probe_root) is True,
              str(trayfix.is_promoted("999", root=probe_root)))
        third = p.step()
        check("done 之后 step 不再改动，返回缓存状态",
              third == trayfix.PROMOTED, third)
    finally:
        for k in (f"{probe_root}\\999", probe_root):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except OSError:
                pass


def test_real_registry():
    print("\n[真实注册表] 当前解释器对应的条目")
    if not sys.platform.startswith("win"):
        print("  SKIP  非 Windows")
        return
    exe = trayfix.current_exe()
    check("能取到当前解释器路径", bool(exe), exe)
    names = trayfix.find_entries(exe)
    print(f"  当前 exe = {exe}")
    print(f"  命中的键 = {names}")
    if not names:
        # 没有记录说明图标还没显示过：此时函数必须安全返回 NOT_FOUND
        st = trayfix.promote(exe)
        check("无记录时安全返回 NOT_FOUND", st == trayfix.NOT_FOUND, st)
        print("  （本机尚无记录，跳过 IsPromoted 断言）")
        return
    st = trayfix.promote(exe)          # 幂等：已是 1 就不动
    check("promote 返回 promoted/already",
          st in (trayfix.PROMOTED, trayfix.ALREADY), st)
    for n in names:
        v = trayfix.is_promoted(n)
        check(f"条目 {n} 的 IsPromoted=1", v is True, f"is_promoted={v}")


def main():
    print("=" * 60)
    print("托盘提升测试 (trayfix)")
    print("=" * 60)
    test_normalize()
    test_match_entries()
    test_missing_key_safe()
    test_promoter_retry()
    test_promoter_not_found_retry()
    test_promoter_promotes_on_retry()
    test_real_registry()

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
