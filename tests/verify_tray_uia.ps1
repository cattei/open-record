# -*- coding: utf-8 -*-
# 托盘图标端到端验证（UI Automation）
# ===================================
# 为什么用 PowerShell 而不是 Python：发行包里的 runtime\python 没有
# uiautomation/pywinauto/comtypes，装不了也不该为此加依赖；而 UIAutomationClient
# 是 Windows 自带的 .NET 程序集，PowerShell 直接 Add-Type 就能用。
#
# 做法（与 Lead 手工验证一致）：
#     AutomationElement.RootElement
#       -> 找 ClassName=Shell_TrayWnd 的窗口
#       -> 找 ClassName 以 SystemTray 开头的后代按钮
#       -> 看 Name 是否含「录音助手」
#
# 输出：找到则打印图标名与屏幕坐标（说明它真的摆在可见托盘区，而不是溢出区），
# 并返回 0；找不到返回 1，便于脚本判断。
#
# 注意：本文件必须是「UTF-8 with BOM」保存 —— 没有 BOM 时 PowerShell 会按
# 系统 ANSI 解码，脚本里的中文关键词和参数默认值会变成乱码而解析失败。
#
# 用法：
#     powershell -NoProfile -ExecutionPolicy Bypass -File tests\verify_tray_uia.ps1
#     powershell -NoProfile -ExecutionPolicy Bypass -File tests\verify_tray_uia.ps1 -WaitSec 30
param(
    [string]$Keyword = "录音助手",
    [int]$WaitSec = 15,
    [int]$PollMs = 500
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$Any = [System.Windows.Automation.Condition]::TrueCondition

function Get-TrayButtons {
    $root = $AE::RootElement
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        $AE::ClassNameProperty, "Shell_TrayWnd")
    $tray = $root.FindFirst($TS::Children, $cond)
    if ($null -eq $tray) { return @() }

    $all = $tray.FindAll($TS::Descendants, $Any)
    $out = @()
    foreach ($e in $all) {
        $c = $e.Current
        if ($c.ClassName -like "SystemTray*") {
            $out += [pscustomobject]@{
                Name   = $c.Name
                Class  = $c.ClassName
                Rect   = $c.BoundingRectangle
                Offscr = $c.IsOffscreen
            }
        }
    }
    return $out
}

$deadline = (Get-Date).AddSeconds($WaitSec)
$hit = $null
while ((Get-Date) -lt $deadline) {
    $btns = Get-TrayButtons
    $hit = $btns | Where-Object { $_.Name -like "*$Keyword*" } | Select-Object -First 1
    if ($hit) { break }
    Start-Sleep -Milliseconds $PollMs
}

$btns = Get-TrayButtons
Write-Output ("托盘 SystemTray 按钮数: {0}" -f @($btns).Count)

if ($hit) {
    $r = $hit.Rect
    Write-Output "RESULT: FOUND"
    Write-Output ("  name  = {0}" -f $hit.Name)
    Write-Output ("  class = {0}" -f $hit.Class)
    Write-Output ("  rect  = x={0} y={1} w={2} h={3}" -f $r.X, $r.Y, $r.Width, $r.Height)
    Write-Output ("  offscreen = {0}" -f $hit.Offscr)
    if ($r.Y -lt 0 -or $r.X -lt 0 -or $r.Width -le 0) {
        Write-Output "  WARN: 坐标异常，可能被屏幕边界截断"
    }
    exit 0
} else {
    Write-Output "RESULT: NOT_FOUND"
    Write-Output "可见托盘区里的 SystemTray 按钮："
    foreach ($b in $btns) {
        $r = $b.Rect
        Write-Output ("  - '{0}' [{1}] x={2} y={3}" -f $b.Name, $b.Class, $r.X, $r.Y)
    }
    exit 1
}
