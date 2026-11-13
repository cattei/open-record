"""
进程守护
========
开机自启只保证「开机时启动一次」。如果进程中途退出（崩溃、误杀、
系统更新、内存压力），在下次开机前录音能力就完全没有了。

真实事故：某晚录音收尾后进程消失，机器没重启，于是接下来十几个小时
「看着在跑，其实没在跑」——用户完全无从察觉。

为什么用计划任务而不是常驻看门狗进程
------------------------------------
1. **常驻看门狗本身也会死**。一个每 5 分钟醒一次的计划任务由系统调度，
   比「一个 Python 进程盯着另一个 Python 进程」可靠得多。
2. 计划任务在休眠/唤醒后由 Task Scheduler 重新调度，不需要额外处理。
3. 零常驻内存：平时不占资源，只在触发时跑一次，检查完立刻退出。
4. 不需要管理员权限（注册在当前用户名下，不写 HKLM）。

判活方式
--------
用**单实例锁端口**判断，而不是数 pythonw 进程：
机器上可能有别的 Python 脚本在跑，数进程会误判；而锁端口只有
真正在提供服务的那个实例才占用，是权威信号。
拉起后由 launcher 自己抢锁，抢不到会退出，所以重复触发也不会起两个实例。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autostart  # noqa: E402
import singleton  # noqa: E402

TASK_NAME = "ProjectRecord-Guardian"
INTERVAL_MINUTES = 5
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _stop_flag() -> Path:
    """用户主动退出时留下的标记文件。

    没有它会出现很别扭的行为：用户右键「退出」，5 分钟后守护又把它拉起来——
    用户的明确意图被系统推翻。所以退出时写这个文件，守护见到就跳过；
    下次开机（或用户手动双击 bat）时由启动流程清掉。
    """
    return autostart.project_root() / ".user_quit"


def mark_user_quit() -> None:
    try:
        _stop_flag().write_text("1", encoding="utf-8")
    except OSError:
        pass


def clear_user_quit() -> None:
    try:
        _stop_flag().unlink(missing_ok=True)
    except OSError:
        pass


def user_quit() -> bool:
    return _stop_flag().exists()


def is_running() -> bool:
    """靠单实例锁端口判活。

    锁端口只有真正在提供服务的实例才占用，是权威信号；
    数 pythonw 进程会把机器上别的 Python 脚本也算进来，导致误判成
    「已经在跑」而放弃拉起——正是那次事故的失败模式。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1.5)
        return s.connect_ex((singleton.LOCK_HOST, singleton.LOCK_PORT)) == 0
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def start_app() -> bool:
    exe = autostart.pythonw()
    launcher = autostart.app_dir() / "launcher.py"
    if not exe.exists() or not launcher.exists():
        return False
    try:
        subprocess.Popen(
            [str(exe), str(launcher), autostart.FLAG],
            cwd=str(autostart.project_root()),
            creationflags=_NO_WINDOW,
            close_fds=True,
        )
        return True
    except Exception:
        return False


def check_and_heal() -> dict:
    # 用户主动退出过：尊重这个决定，不拉起。开机/手动启动会清掉标记。
    if user_quit():
        return {"running": is_running(), "started": False,
                "skipped": "user_quit",
                "pythonw": str(autostart.pythonw())}
    running = is_running()
    started = False
    if not running:
        started = start_app()
    return {"running": running, "started": started,
            "pythonw": str(autostart.pythonw())}


# ---------------- 计划任务 ----------------
def _ps(cmd: str, timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def install(interval: int = INTERVAL_MINUTES) -> tuple[bool, str]:
    """注册计划任务：每 N 分钟检查一次，进程不在就拉起。

    用 PowerShell 的 Register-ScheduledTask，避免依赖 pywin32。
    注册在当前用户下，不需要管理员权限。
    """
    script = autostart.app_dir() / "guardian.py"
    exe = autostart.pythonw()
    if not script.exists():
        return False, f"找不到 {script}"
    if not exe.exists():
        return False, f"找不到 {exe}"
    cmd = (
        "$ErrorActionPreference='Stop';"
        f"$a = New-ScheduledTaskAction -Execute '{exe}' "
        f"-Argument '\"{script}\" --check';"
        f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {interval});"
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable "
        "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5);"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t "
        f"-Settings $s -Force | Out-Null;"
        "Write-Output 'OK'"
    )
    rc, out = _ps(cmd)
    return (rc == 0 and "OK" in out), out.strip()[:400]


def uninstall() -> tuple[bool, str]:
    rc, out = _ps(
        "$ErrorActionPreference='SilentlyContinue';"
        f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false;"
        "Write-Output 'OK'", timeout=30)
    return rc == 0, out.strip()[:300]


def status() -> dict:
    rc, out = _ps(
        f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
        f"if ($t) {{ $i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}';"
        "Write-Output ('STATE=' + $t.State);"
        "Write-Output ('LAST=' + $i.LastRunTime);"
        "Write-Output ('NEXT=' + $i.NextRunTime) } "
        "else { Write-Output 'STATE=absent' }", timeout=30)
    info = {"installed": False, "state": "", "last_run": "", "next_run": "",
            "raw": out.strip()[:300]}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("STATE="):
            st = line[6:].strip()
            info["state"] = st
            info["installed"] = st not in ("", "absent", "NotFound", "Unknown")
        elif line.startswith("LAST="):
            info["last_run"] = line[5:].strip()
        elif line.startswith("NEXT="):
            info["next_run"] = line[5:].strip()
    return info


if __name__ == "__main__":
    act = sys.argv[1] if len(sys.argv) > 1 else "check"
    if act in ("--check", "check"):
        print(json.dumps(check_and_heal(), ensure_ascii=False))
    elif act == "install":
        ok, msg = install()
        print(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False, indent=2))
        sys.exit(0 if ok else 1)
    elif act == "uninstall":
        ok, msg = uninstall()
        print(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
