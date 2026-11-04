@echo off
setlocal
cd /d "%~dp0"

set "Pyw=%~dp0runtime\python\pythonw.exe"
set "Py=%~dp0runtime\python\python.exe"

if exist "%Pyw%" goto :run_w
if exist "%Py%" goto :run_c

echo.
echo  [错误] 找不到内置 Python 运行时
echo  请确认 runtime\python\python.exe 存在
echo  提示：解压时必须保留全部子目录
echo.
pause
exit /b 1

:run_w
start "" "%Pyw%" "%~dp0app\launcher.py"
exit /b 0

:run_c
start "" "%Py%" "%~dp0app\launcher.py"
exit /b 0
