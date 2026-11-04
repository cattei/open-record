# -*- coding: utf-8 -*-
# 右键菜单端到端验证：右击托盘图标 -> 找到 QMenu -> 枚举项 -> 点「退出」-> 进程应消失
# 为什么单独写：合成鼠标事件在屏幕边界附近容易失真，所以这里右击前先把光标
# 精确放到 UIA 读到的图标矩形中心，并在同一次脚本里完成「弹菜单 -> 点退出」，
# 避免菜单在两次调用之间被焦点切换关掉。
param(
    [string]$Keyword = "录音助手",
    [switch]$OnlyList
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class MUIA {
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr v);
  public struct POINT { public int X; public int Y; }
}
'@

# 关键：PowerShell 默认不是 DPI-aware。高分屏（本机 150%）下，UIA 报的是物理
# 像素坐标，而 SetCursorPos 会被系统按逻辑像素缩放，光标实际落点会偏移几十像素，
# 右键就打不到图标上（表现为「右键无菜单」这种假失败）。
# 这里尝试声明 Per-Monitor V2（-4）；但 powershell.exe 宿主往往在脚本执行前就已
# 定好 DPI 感知级别，这个调用可能返回 False 而不生效 —— 所以真正的护栏是下面
# 「点之前核对光标是否落在图标矩形内」那一段，不靠这个调用。
[MUIA]::SetProcessDpiAwarenessContext([IntPtr]-4) | Out-Null

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$Any = [System.Windows.Automation.Condition]::TrueCondition

function Get-TrayButton {
    param($kw)
    $root = $AE::RootElement
    $cond = New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, "Shell_TrayWnd")
    $tray = $root.FindFirst($TS::Children, $cond)
    $all = $tray.FindAll($TS::Descendants, $Any)
    return ($all | Where-Object { $_.Current.Name -like "*$kw*" } | Select-Object -First 1)
}

# 右键可能被任务栏自身或别的窗口抢焦点，实测偶发一次点不出来，
# 所以重试几次；每次重新读一遍图标矩形（图标位置会随托盘内容变化）。
$menu = $null
for ($try = 1; $try -le 3; $try++) {
    $btn = Get-TrayButton $Keyword
    if ($null -eq $btn) { Write-Output "RESULT: NO_TRAY_ICON"; exit 1 }
    $r = $btn.Current.BoundingRectangle
    $cx = [int]($r.X + $r.Width / 2)
    $cy = [int]($r.Y + $r.Height / 2)
    Write-Output ("第{0}次尝试: 图标 name='{1}' 中心=({2},{3})" -f $try, $btn.Current.Name, $cx, $cy)

    [MUIA]::SetCursorPos($cx, $cy) | Out-Null
    Start-Sleep -Milliseconds 600
    # 点之前核对光标确实落在图标矩形内：DPI 缩放会让落点偏移，
    # 打偏了就会得到「右键无菜单」这种假失败。
    $p = New-Object MUIA+POINT
    [MUIA]::GetCursorPos([ref]$p) | Out-Null
    Write-Output ("  光标实际位置: ({0},{1})" -f $p.X, $p.Y)
    if ($p.X -lt $r.X -or $p.X -gt ($r.X + $r.Width) -or $p.Y -lt $r.Y -or $p.Y -gt ($r.Y + $r.Height)) {
        Write-Output "  CURSOR_MISSED（光标没落在图标上，本次点击不可信）"
        continue
    }
    [MUIA]::mouse_event(0x0008, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 150
    [MUIA]::mouse_event(0x0010, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 1200

    $menus = @($AE::RootElement.FindAll($TS::Children, $Any) | Where-Object { $_.Current.ClassName -eq "QMenu" })
    if ($menus.Count -gt 0) {
        Write-Output ("QMenu 数量: {0}（应恰好 1 个，>1 说明菜单弹了两次）" -f $menus.Count)
        $menu = $menus[0]
        break
    }
    Write-Output "  本次没有出现 QMenu，重试"
}
if ($null -eq $menu) {
    Write-Output "RESULT: NO_MENU（重试后仍没有出现 QMenu）"
    exit 1
}
$rr = $menu.Current.BoundingRectangle
Write-Output ("菜单 rect: x={0} y={1} w={2} h={3}" -f $rr.X, $rr.Y, $rr.Width, $rr.Height)

$items = $menu.FindAll($TS::Descendants, $Any)
Write-Output "菜单项:"
foreach ($i in $items) {
    $c = $i.Current
    if ($c.ControlType.ProgrammaticName -eq "ControlType.MenuItem") {
        Write-Output ("  - '{0}' enabled={1}" -f $c.Name, $c.IsEnabled)
    }
}

if ($OnlyList) {
    # 关菜单前必须先把光标挪开：菜单矩形底部正好是「退出」项，
    # 原地再点一下等于替用户按了退出，会把正在跑的应用顺手杀掉。
    [MUIA]::SetCursorPos([int]($rr.X - 300), [int]($rr.Y + 40)) | Out-Null
    Start-Sleep -Milliseconds 300
    [System.Windows.Forms.SendKeys]::SendWait("{ESC}")
    Start-Sleep -Milliseconds 600
    $left = @($AE::RootElement.FindAll($TS::Children, $Any) | Where-Object { $_.Current.ClassName -eq "QMenu" }).Count
    Write-Output ("关闭后 QMenu 数量: {0}" -f $left)
    Write-Output "RESULT: LISTED"
    exit 0
}

$quit = $items | Where-Object { $_.Current.Name -eq "退出" } | Select-Object -First 1
if ($null -eq $quit) {
    Write-Output "RESULT: NO_QUIT_ITEM"
    exit 1
}
$ip = $quit.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
$ip.Invoke()
Write-Output "RESULT: CLICKED_QUIT"
Start-Sleep -Seconds 3

$still = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*open-record*" }
if ($null -eq $still) {
    Write-Output "进程已退出（托盘「退出」生效）"
    exit 0
} else {
    Write-Output ("进程仍在: {0}" -f ($still | Select-Object -ExpandProperty ProcessId))
    exit 1
}
