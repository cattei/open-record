"""
托盘图标自动提升（Windows 11）
==============================
为什么需要这个模块：Win11 把新托盘图标默认塞进「^」溢出区，用户在右下角
根本看不见应用在跑，于是以为程序没启动。系统在
    HKCU\\Control Panel\\NotifyIconSettings\\<哈希键>
下为每个托盘图标维护一条记录，只有 IsPromoted=1 的才会被摆到可见托盘区。
这个键名是 explorer 自己算的哈希，换机器、换安装路径都会变，所以只能按
ExecutablePath 反查，不能按 key 名硬编码。

时序上还有个坑：这条记录是「图标真正显示之后」才由 explorer 写入的，
进程启动那一瞬间往往还查不到。因此这里不追求一次成功，而是提供一个
可重复调用的 promote()，由 GUI 用定时器重试若干次。

只用标准库（winreg），且全程容错：注册表读不到/写不了（权限、非 Win11、
被安全软件拦截）一律只记日志，绝不抛异常打断启动 —— 图标看不见是体验
问题，启动失败才是功能问题。
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

# 记录所在位置（HKCU 下，免提权即可写）
NOTIFY_ROOT = r"Control Panel\NotifyIconSettings"

# 同一个 pythonw.exe 可能承载多个托盘应用（它们共用解释器），
# 这时用悬停提示来挑出属于本应用的那条，避免把别的应用也一起提升。
APP_HINT = "录音助手"

# promote() 的返回值。调用方靠它判断「要不要继续重试」。
PROMOTED = "promoted"        # 本次写入成功
ALREADY = "already"          # 已经是 1，未改动
NOT_FOUND = "not_found"      # 注册表里还没有本进程的记录（explorer 尚未写入）
UNSUPPORTED = "unsupported"  # 非 Windows 或没有该注册表项
ERROR = "error"              # 读/写失败（权限等）

TERMINAL = (PROMOTED, ALREADY, UNSUPPORTED)


def normalize(path: str | None) -> str:
    """规范化可执行路径用于比较。

    注册表里的值和 sys.executable 常常大小写不一致（Windows 不区分大小写），
    也可能带引号或正斜杠，所以统一成 normcase+normpath 再比。
    """
    if not path:
        return ""
    p = str(path).strip().strip('"').strip()
    if not p:
        return ""
    try:
        p = os.path.expandvars(os.path.expanduser(p))
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    except Exception:  # 路径畸形不该影响主流程
        return os.path.normcase(p)


def current_exe() -> str:
    """本进程的可执行文件。

    launcher 用 pythonw.exe 跑，所以这里是解释器路径；PyInstaller 打包后
    是 exe 本身 —— 两种形态都要能对上，因此不写死文件名。
    """
    try:
        return sys.executable or ""
    except Exception:
        return ""


def match_entries(entries, exe_path: str | None, hint: str = APP_HINT) -> list[str]:
    """纯逻辑：从注册表条目里挑出属于本进程的那些。

    entries: [(子键名, {值名小写: 值})]
    单独抽出来是为了能脱离真实注册表做单测（传假路径、假条目即可）。

    匹配规则：
      1. ExecutablePath 规范化后必须等于 exe_path（大小写/斜杠无关）；
      2. 命中多条时（同一个解释器承载多个托盘应用），优先取提示里带应用名的；
         若都没带，就全部返回 —— 宁可多提升一条自己的图标，也不能漏掉。
    """
    want = normalize(exe_path)
    if not want:
        return []
    hits: list[tuple[str, str]] = []
    for name, vals in entries or []:
        got = normalize(vals.get("executablepath"))
        if got and got == want:
            hits.append((name, str(vals.get("initialtooltip") or "")))
    if not hits:
        return []
    if hint and len(hits) > 1:
        hinted = [n for n, tip in hits if hint in tip]
        if hinted:
            return hinted
    return [n for n, _ in hits]


def _read_entries(root: str = NOTIFY_ROOT, hive=None) -> list[tuple[str, dict]]:
    """读出 root 下所有子键的关键值。

    IconSnapshot 是二进制 PNG，可能上百 KB，这里不读，避免无谓开销。
    """
    import winreg

    hive = winreg.HKEY_CURRENT_USER if hive is None else hive
    entries: list[tuple[str, dict]] = []
    with winreg.OpenKey(hive, root, 0, winreg.KEY_READ) as parent:
        count = winreg.QueryInfoKey(parent)[0]
        names = []
        for i in range(count):
            try:
                names.append(winreg.EnumKey(parent, i))
            except OSError:
                break
    for name in names:
        try:
            with winreg.OpenKey(hive, f"{root}\\{name}", 0, winreg.KEY_READ) as k:
                vals: dict[str, object] = {}
                for vname in ("ExecutablePath", "InitialTooltip", "IsPromoted"):
                    try:
                        vals[vname.lower()] = winreg.QueryValueEx(k, vname)[0]
                    except OSError:
                        pass
            entries.append((name, vals))
        except OSError:
            # 单个键读不了（权限/正在被 explorer 改写）就跳过，不影响其它键
            continue
    return entries


def find_entries(exe_path: str | None = None, root: str = NOTIFY_ROOT,
                 hive=None) -> list[str]:
    """返回属于该可执行文件的注册表子键名（可能为空、也可能多条）。

    找不到根键（非 Win11、注册表被策略锁住）一律返回空列表：这个函数只用来
    诊断和测试，不该因为「系统没这个键」把调用方带崩。
    """
    exe_path = current_exe() if exe_path is None else exe_path
    try:
        entries = _read_entries(root=root, hive=hive)
    except OSError as e:
        log.debug("托盘提升：读不到 %s（%s）", root, e)
        return []
    return match_entries(entries, exe_path)


def is_promoted(key_name: str, root: str = NOTIFY_ROOT, hive=None) -> bool | None:
    """读某条记录当前是否已提升；None = 值不存在或读不到。"""
    import winreg

    hive = winreg.HKEY_CURRENT_USER if hive is None else hive
    try:
        with winreg.OpenKey(hive, f"{root}\\{key_name}", 0, winreg.KEY_READ) as k:
            return int(winreg.QueryValueEx(k, "IsPromoted")[0]) == 1
    except OSError:
        return None


def promote(exe_path: str | None = None, root: str = NOTIFY_ROOT, hive=None,
            hint: str = APP_HINT, dry_run: bool = False) -> str:
    """把本进程的托盘记录提升到可见托盘区，返回上面某个状态字符串。

    幂等：已经是 1 就不写。绝不抛异常 —— 图标看不见是体验问题，
    启动失败是功能问题，这里永远选前者。
    """
    if not sys.platform.startswith("win"):
        return UNSUPPORTED
    exe_path = current_exe() if exe_path is None else exe_path
    if not normalize(exe_path):
        return UNSUPPORTED

    import winreg

    hive = winreg.HKEY_CURRENT_USER if hive is None else hive

    try:
        entries = _read_entries(root=root, hive=hive)
    except OSError as e:
        # 非 Win11 没有这个键，或注册表被策略锁住：记一笔就返回
        log.warning("托盘提升：读不到 %s（%s）", root, e)
        return UNSUPPORTED

    names = match_entries(entries, exe_path, hint)
    if not names:
        log.debug("托盘提升：还没有 %s 的记录，稍后重试", exe_path)
        return NOT_FOUND

    changed = False
    failures = 0
    for name in names:
        key_path = f"{root}\\{name}"
        try:
            with winreg.OpenKey(hive, key_path, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as k:
                try:
                    cur = int(winreg.QueryValueEx(k, "IsPromoted")[0])
                except OSError:
                    cur = None
                if cur == 1:
                    continue
                if dry_run:
                    changed = True
                    continue
                winreg.SetValueEx(k, "IsPromoted", 0, winreg.REG_DWORD, 1)
                changed = True
                log.info("托盘提升：%s 的 IsPromoted 置 1（原值=%s）", key_path, cur)
        except OSError as e:
            # 权限不足或被安全软件拦截：这条失败不影响其它条目
            failures += 1
            log.warning("托盘提升：写 %s 失败（%s）", key_path, e)

    if changed:
        return PROMOTED
    if failures:
        return ERROR
    return ALREADY


class TrayPromoter:
    """带重试计数的提升器，由 GUI 的定时器逐次驱动。

    为什么不在这里自己起线程/定时器：Qt 里跨线程改状态容易踩事件循环的坑，
    而 GUI 本来就有 QTimer 在跑，把节奏交给它最省事，也让这个模块保持
    「纯标准库、可单测」。
    """

    def __init__(self, exe_path: str | None = None, attempts: int = 5,
                 root: str = NOTIFY_ROOT, hive=None, hint: str = APP_HINT):
        self.exe_path = current_exe() if exe_path is None else exe_path
        self.attempts = max(1, int(attempts))
        self.root = root
        self.hive = hive
        self.hint = hint
        self.tries = 0
        self.status: str | None = None
        self.done = False

    def step(self) -> str:
        """走一次；一旦有了确定结果（成功/已设置/不支持）就置 done。"""
        if self.done:
            return self.status or ALREADY
        self.tries += 1
        self.status = promote(self.exe_path, root=self.root, hive=self.hive,
                              hint=self.hint)
        if self.status in TERMINAL:
            self.done = True
        elif self.tries >= self.attempts:
            # 用完次数还没找到记录：停手，把结论留给调用方写日志/提示
            self.done = True
        return self.status
