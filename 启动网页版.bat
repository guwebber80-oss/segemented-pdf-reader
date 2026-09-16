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
set "GROBID_PORT=8070"
set "GROBID_IMAGE=grobid/grobid:0.9.1-crf"

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

rem ---- GROBID（可选，用于交叉校验）：与「启动阅读器.bat」同一套逻辑 ----
rem 之前本脚本没做这一步，所以用户"启动了 GROBID"其实指的是另一个脚本/手动 docker run，
rem 网页版这边一直显示"未检测到"。这里补上：找不到 docker 就跳过，不影响阅读。
set "DOCKER="
where docker >nul 2>nul
if not errorlevel 1 set "DOCKER=docker"
if not defined DOCKER (
    if exist "%PROJECT%..\docker4GROBID\DockerDesktop\resources\bin\docker.exe" (
        set "DOCKER=%PROJECT%..\docker4GROBID\DockerDesktop\resources\bin\docker.exe"
    )
)
if not defined DOCKER (
    echo [提示] 没找到 docker：跳过 GROBID。不影响阅读，只是少一个交叉校验。
) else (
    "%DOCKER%" image inspect %GROBID_IMAGE% >nul 2>nul
    if errorlevel 1 (
        echo [提示] 本地还没有 GROBID 镜像，跳过。想用时先执行：
        echo            docker pull %GROBID_IMAGE%
    ) else (
        "%DOCKER%" ps --filter "name=grobid" --format "{{.Names}}" 2>nul | findstr /i "grobid" >nul
        if not errorlevel 1 (
            echo [OK]   GROBID 已在运行（端口 %GROBID_PORT%）。
        ) else (
            echo [..]   正在后台启动 GROBID（首次约十几秒，准备好了网页版会自动检测到）...
            "%DOCKER%" run -d --rm --name grobid --init --ulimit core=0 -p %GROBID_PORT%:%GROBID_PORT% %GROBID_IMAGE% >nul 2>nul
            if errorlevel 1 (
                echo [提示] GROBID 启动失败（多半是 Docker Desktop 没开）。不影响阅读。
            ) else (
                echo [OK]   GROBID 已启动，网页版右栏的「GROBID 交叉校验」稍后可点。
            )
        )
    )
)
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
