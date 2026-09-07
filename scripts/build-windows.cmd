@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" goto build
py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo 请先安装 Python 3.12 64 位 Windows 版，再重新打开本文件。
  echo 下载：https://www.python.org/downloads/windows/
  goto failed
)
py -3.12 -m venv .venv
if errorlevel 1 goto failed
:build
".venv\Scripts\python.exe" scripts\build_windows.py
if errorlevel 1 goto failed
echo 已生成 dist\Tingma.exe。使用者不需要安装 Python。
echo 完整交付文件在 dist\windows-release。
pause
exit /b 0
:failed
echo 打包失败，请保留上面的错误信息。
pause
exit /b 1
