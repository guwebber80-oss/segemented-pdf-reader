@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 科研文献 PDF 智能阅读器

rem ============================================================================
rem  一键启动：GROBID（可选，用于交叉校验） + Streamlit 阅读器
rem
rem  用法：双击本文件即可；也可以在命令行加 --check 只做自检、不启动服务。
rem
rem  为什么它是「路径无关」的：所有路径都由 %~dp0（本脚本所在目录）推导，
rem  所以整个项目文件夹改名、搬到别的盘也照样能用——只要本脚本和 app.py 在
rem  同一个文件夹里。不要把本脚本单独拷到桌面去用（那样会找不到 app.py）。
rem
rem  编码说明：本文件是 GBK + CRLF，请不要用记事本另存成 UTF-8，
rem  否则 cmd 逐字节解析中文时会错乱（会出现 "xxx is not recognized" 之类报错）。
rem
rem  维护约定：若 app.py 改名、或 .venv 换位置，本脚本要同步改这三处：
rem      set "PY=..."                    解释器路径
rem      set "ENTRY=app.py"             入口文件名
rem      依赖自检那一行的 import 列表
rem ============================================================================

set "PROJECT=%~dp0"
set "PY=%PROJECT%.venv\Scripts\python.exe"
set "ENTRY=app.py"
set "APP_PORT=8501"
set "GROBID_PORT=8070"
set "GROBID_IMAGE=grobid/grobid:0.9.1-crf"

echo.
echo ============================================================
echo   科研文献 PDF 智能阅读器
echo ============================================================
echo.

if not exist "%PROJECT%%ENTRY%" (
    echo [错误] 没找到 %ENTRY% —— 请把本脚本放在项目根目录（和 %ENTRY% 同一个文件夹）。
    echo        脚本当前所在目录：%PROJECT%
    goto :fail
)
if not exist "%PY%" (
    echo [错误] 没找到虚拟环境解释器：
    echo        %PY%
    echo        如果这是刚拿到的项目，先在本目录执行一次：
    echo            python -m venv .venv
    echo            .venv\Scripts\python.exe -m pip install -r requirements.txt
    goto :fail
)

rem 注意 %~dp0 末尾自带反斜杠：cd /d "%PROJECT%" 会因为结尾的 \" 转义掉引号而失败，
rem 所以补一个点，cd /d "%~dp0." 才是安全写法。
cd /d "%~dp0."

echo [..]   检查依赖与模块...
"%PY%" -c "import utils.pdf_parser, utils.table_finder, utils.metadata, utils.metadata_compare, utils.grobid_client, utils.image_extractor, utils.translator, utils.store, ui.persist, ui.theme" 1>nul 2>nul
if errorlevel 1 (
    echo [警告] 依赖或模块导入失败，阅读器启动不了。常见原因有两种：
    echo         一：新版本新增了依赖，执行下面这条命令即可：
    echo             .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo         二：某个模块文件缺失，请检查项目文件是否完整。
    echo.
    set /p ANSWER=现在自动安装依赖吗？输入 y 再回车即安装，直接回车则退出：
    if /i "!ANSWER!"=="y" (
        echo [..]   正在安装（需要联网，可能要一两分钟）...
        "%PY%" -m pip install -r "%PROJECT%requirements.txt"
        if errorlevel 1 (
            echo [错误] 安装失败，请把上面的报错发我。
            goto :fail
        )
        echo [OK]   依赖安装完成。
    ) else (
        goto :fail
    )
)

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
            echo [..]   正在后台启动 GROBID（首次约十几秒）...
            "%DOCKER%" run -d --rm --name grobid --init --ulimit core=0 -p %GROBID_PORT%:%GROBID_PORT% %GROBID_IMAGE% >nul 2>nul
            if errorlevel 1 (
                echo [提示] GROBID 启动失败（多半是 Docker Desktop 没开）。不影响阅读。
            ) else (
                echo [OK]   GROBID 已启动，「GROBID 交叉校验」面板稍后自动可用。
            )
        )
    )
)

netstat -ano | findstr ":%APP_PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [提示] 端口 %APP_PORT% 已被占用：应该已经有一个阅读器在运行。
    echo        这就帮你打开浏览器；要重启请先关掉那个窗口。
    start "" "http://localhost:%APP_PORT%"
    goto :end
)

if /i "%~1"=="--check" (
    echo.
    echo [自检完成] 上面没有「错误」就说明环境正常，双击运行即可。
    echo            按任意键关闭本窗口。
    pause >nul
    goto :eof
)

echo.
echo [启动] 浏览器会自动打开： http://localhost:%APP_PORT%
echo        记住：关闭这个窗口就是停止阅读器（或在本窗口按 Ctrl+C）。
echo        想停 GROBID：另开窗口执行  docker stop grobid
echo.
"%PY%" -m streamlit run "%PROJECT%%ENTRY%" --server.port %APP_PORT%

:end
echo.
echo 阅读器已退出。
pause
goto :eof

:fail
echo.
pause
exit /b 1
