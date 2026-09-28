@echo off
rem 红警2修改器启动入口：双击启动；加 --check 只检查环境。
rem 本脚本只查找 Python 并透传参数，不下载、不安装、不提权。
setlocal
chcp 65001 >nul 2>&1

set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%ra2-trainer\main.py" (
    set "TRAINER_DIR=%SCRIPT_DIR%ra2-trainer"
) else if exist "%SCRIPT_DIR%main.py" (
    set "TRAINER_DIR=%SCRIPT_DIR%"
) else (
    echo 找不到修改器目录：脚本旁既没有 main.py，也没有 ra2-trainer\main.py。
    echo 请把本脚本放在 ra2-trainer 目录内，或放在它的上一级目录。
    pause
    exit /b 1
)

rem 按顺序找可用的 Python 3.10+：本机安装版、py 启动器、PATH 里的 python。
set "PYCMD="
for %%V in (314 313 312 311 310) do (
    if not defined PYCMD (
        if exist "%LOCALAPPDATA%\Programs\Python\Python3%%V\python.exe" (
            set "PYCMD=\"%LOCALAPPDATA%\Programs\Python\Python3%%V\python.exe\""
        )
    )
)
if not defined PYCMD (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYCMD=py -3"
    )
)
if not defined PYCMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYCMD=python"
    )
)
if not defined PYCMD (
    echo 未找到 Python 3.10 或更新版本，请先安装 Python 再双击本脚本。
    pause
    exit /b 1
)

cd /d "%TRAINER_DIR%"
%PYCMD% main.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo 修改器退出，代码 %RC%。
    echo 缺少依赖时请在 ra2-trainer 目录执行：%PYCMD% -m pip install -r requirements.txt
    echo 启动脚本不会自行下载或安装任何东西。
    pause
)
exit /b %RC%
