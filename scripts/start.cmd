@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" goto run
py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo 请先安装 Python 3.12 64 位 Windows 版，再重新打开本文件。
  echo 下载：https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
echo 正在创建运行环境…
py -3.12 -m venv .venv
if errorlevel 1 goto failed
echo 首次启动正在下载依赖，需要联网，可能需要几分钟…
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo ready>".venv\tingma-ready-douyin-v1"
:run
if not exist ".venv\tingma-ready-douyin-v1" (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  echo ready>".venv\tingma-ready-douyin-v1"
)
".venv\Scripts\python.exe" -m tingma
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动失败，请保留上面的错误信息以便排查。
pause
exit /b 1
