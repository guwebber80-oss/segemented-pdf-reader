@echo off
chcp 936 >nul
setlocal
title 科研文献 PDF 智能阅读器（网页版）

rem ============================================================================
rem  网页版阅读器（自建前端）：本地起一个小服务，然后自动打开浏览器。
rem  与 Streamlit 版（启动阅读器.bat）互不影响：两套前端共用同一个引擎（utils/）。
rem  关闭这个窗口就是停止服务。
rem ============================================================================

set "PROJECT=%~dp0"
set "PY=%PROJECT%.venv\Scripts\python.exe"
set "PORT=8765"

if not exist "%PROJECT%webapp\server.py" (
    echo [错误] 没找到 webapp\server.py —— 请把本脚本放在项目根目录。
    pause
    exit /b 1
)
if not exist "%PY%" (
    echo [错误] 没找到虚拟环境解释器：%PY%
    pause
    exit /b 1
)

cd /d "%~dp0."

netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [提示] 端口 %PORT% 已在监听：网页版可能已经在运行。这就帮你打开浏览器。
    start "" "http://127.0.0.1:%PORT%/"
    pause
    exit /b 0
)

echo.
echo [启动] 浏览器会自动打开： http://127.0.0.1:%PORT%/
echo        第一次打开后：点左栏「选择 PDF 文献」选一篇论文即可。
echo        快捷键：左右方向键翻页 / L 中英切换 / I 沉浸模式
echo.
"%PY%" -m webapp.server --port %PORT%

echo.
echo 网页版已退出。
pause
