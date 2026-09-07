@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo 请先运行“启动听码.cmd”安装运行环境，再关闭软件后打包。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller==6.22.2
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir --name Tingma --paths . --collect-all pyaudiowpatch --collect-all uiautomation scripts\windows_entry.py
if errorlevel 1 goto failed
echo 已生成 dist\Tingma\Tingma.exe，请复制整个 Tingma 文件夹到目标 Windows 电脑。
pause
exit /b 0
:failed
echo 打包失败，请检查上面的错误。
pause
exit /b 1
