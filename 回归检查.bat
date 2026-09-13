@echo off
chcp 936 >nul
title 回归检查：这次更新有没有改坏原有功能

rem ============================================================================
rem  双击运行：把当前代码的输出与基线快照逐项比对，并跑一遍功能测试套件
rem
rem  用法：双击本文件；或命令行加参数看细节（见 tests\regression_check.py --help）
rem  退出码：0 = 原有功能未受影响；1 = 有差异或套件失败
rem
rem  编码说明：本文件是 GBK + CRLF，别用记事本另存成 UTF-8。
rem ============================================================================

set "PROJECT=%~dp0"
set "PY=%PROJECT%.venv\Scripts\python.exe"
set "SUITES=%PROJECT%..\.dsh-scratch"

if not exist "%PY%" (
    echo [错误] 找不到虚拟环境解释器：
    echo        %PY%
    echo        请先双击「启动阅读器.bat」，按它的提示把环境准备好。
    goto :fail
)

echo ============================================================
echo   回归检查：当前输出 vs 基线快照
echo ============================================================
echo.

cd /d "%~dp0."
"%PY%" "tests\regression_check.py" --suites-dir "%SUITES%"
set "CODE=%errorlevel%"

echo.
echo ============================================================
if "%CODE%"=="0" echo   结论：原有功能未受影响
if not "%CODE%"=="0" echo   结论：有差异或套件失败，请把上面的输出发我
echo ============================================================
echo.
echo 小提示：确认差异都是本次故意改动之后，用这条命令刷新基线：
echo         .venv\Scripts\python.exe tests\regression_check.py --update-baseline
echo.
pause
exit /b %CODE%

:fail
pause
exit /b 1
