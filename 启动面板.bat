@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ============================================================
REM  游戏人生 GameLife 本地面板启动器
REM  双击即可启动本地服务并自动打开浏览器。
REM  账号：默认不指定 —— 打开面板后在顶部「添加账号」里选即可；
REM  也可以把账号ID当成参数传进来，例如：启动面板.bat 123456789
REM
REM  启动优先级：源码比打包程序新 -> 用源码；否则用打包程序；
REM  都没有/不可用时回退到系统 python。这样改了源码不会静默跑旧 exe。
REM ============================================================
set ACCOUNT=%~1
set ACCOUNT_ARG=
if not "%ACCOUNT%"=="" set ACCOUNT_ARG=--account-id %ACCOUNT%

set EXE=dist\game-life.exe
set PY=.venv\Scripts\python.exe
set MAIN=game-life\main.py

echo ============================================
echo   游戏人生 GameLife - 本地面板
if "%ACCOUNT%"=="" echo   账号: 未指定（打开面板后添加）
if not "%ACCOUNT%"=="" echo   账号: %ACCOUNT%
echo ============================================
echo.
echo 正在启动，几秒后自动打开浏览器。
echo 地址 http://127.0.0.1:8765/ （端口被占用会自动顺延，以本窗口为准）
echo.
echo 关闭面板：关掉本窗口即可（或按 Ctrl+C）。
echo.

REM ---- 判断源码是否比打包程序新 ----
set SRC_NEWER=0
if not exist "%EXE%" goto scan_done
if not exist "%MAIN%" goto scan_done
for /r game-life\core %%F in (*.py *.js *.css *.html) do call :mark_newer "%%F"
for /r game-life\engine %%F in (*.py *.js *.css *.html) do call :mark_newer "%%F"
for /r game-life\webapp %%F in (*.py *.js *.css *.html) do call :mark_newer "%%F"
for /r game-life\render %%F in (*.py *.js *.css *.html) do call :mark_newer "%%F"
REM 只扫描「只读」源码与数据表。data 下被程序运行期写出的缓存
REM （names_cn.json / series_official.json / fx_rates.json / accounts.json 等）
REM 绝不能进这个列表：它们被写过之后时间戳必然比 exe 新，
REM 会让每次正常启动都误报「源码更新」。
for %%F in ("game-life\main.py" "game-life\rules.json" "game-life\data\series_map.json" "game-life\data\name_cn_extra.json" "game-life\data\genre_cn.json") do if exist %%F call :mark_newer "%%~fF"
:scan_done

if "%SRC_NEWER%"=="1" goto prefer_source
goto prefer_exe

REM ---- 源码更新：优先用源码 ----
:prefer_source
echo [提示] 源码比打包程序新，本次用源码运行。
echo        想消除提示，重新打包：.venv\Scripts\python.exe game-life\build\build_exe.py
echo.
if not exist "%PY%" goto source_unavailable
if not exist "%MAIN%" goto source_unavailable
goto probe_venv

:source_unavailable
echo [提示] 缺少 .venv 或 main.py，回退到打包程序。
echo.
goto prefer_exe

REM ---- 打包程序可用：优先用打包程序 ----
:prefer_exe
if not exist "%EXE%" goto probe_venv
"%EXE%" panel --help >nul 2>&1
if not errorlevel 1 goto run_exe
echo [提示] %EXE% 版本较旧、没有面板功能，改用源码。
echo.

:probe_venv
if not exist "%PY%" goto probe_sys
if not exist "%MAIN%" goto probe_sys
"%PY%" "%MAIN%" panel --help >nul 2>&1
if not errorlevel 1 goto run_venv
echo [提示] 源码方式不可用，改用系统 python。
echo.

:probe_sys
if not exist "%MAIN%" goto notfound
python "%MAIN%" panel --help >nul 2>&1
if not errorlevel 1 goto run_sys
goto noready

REM ---- 标记：参数为源文件绝对路径，比 exe 新时置 SRC_NEWER=1 ----
:mark_newer
if "!SRC_NEWER!"=="1" exit /b
if not exist "%~1" exit /b
xcopy /d /l /y "%~1" "%EXE%" 2>nul | findstr /i /l /c:"%~1" >nul
if not errorlevel 1 set SRC_NEWER=1
exit /b

:run_exe
echo [启动] 打包程序 %EXE%
"%EXE%" panel %ACCOUNT_ARG%
goto done

:run_venv
echo [启动] 源码方式 %PY%
"%PY%" "%MAIN%" panel %ACCOUNT_ARG%
goto done

:run_sys
echo [启动] 系统 python
python "%MAIN%" panel %ACCOUNT_ARG%
goto done

:notfound
echo [错误] 找不到可用的启动方式：
echo        - 缺少 %EXE%
echo        - 缺少 %PY% 且缺少 %MAIN%
echo        请把本文件放在仓库根目录（与 game-life 同级）。
pause
exit /b 1

:noready
echo [错误] 当前程序还没有面板功能（panel 子命令）。
echo        代码或 exe 还是旧版，请更新后重试。
pause
exit /b 1

:done
echo.
echo 面板已停止。
pause
