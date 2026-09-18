@echo off
REM =====================================================================
REM  CanaryGuard AntiFraud v1.0.0 - master server one-click launcher
REM  (Windows cmd) - three-state logic:
REM    state 1: master NOT running  -> start master window + health probe
REM             -> open browser at /ui/ once healthy
REM    state 2: master already running -> do NOT start a second one;
REM             just open another browser tab at /ui/
REM    state 3: health probe failed -> keep the master window, warn the
REM             user to check its log, do not exit silently
REM
REM  This script is pure ASCII on purpose: it parses correctly under ANY
REM  Windows code page (GBK 936, UTF-8 65001, ...). No Chinese characters
REM  in comments or echo lines, so there is no multi-byte parse hazard.
REM
REM  The project root is derived from this script's own location (%~dp0)
REM  instead of a hardcoded path, which also survives a non-ASCII folder
REM  name in the path (e.g. the Chinese directory this project lives in).
REM =====================================================================

setlocal
set "PROJECT_ROOT=%~dp0"
REM strip the trailing backslash that %~dp0 always adds
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
set "PORT=9200"
set "GLOBAL_DSH_PORT=3080"
set "HEALTH_URL=http://127.0.0.1:%PORT%/api/v1/system/health"
set "UI_URL=http://127.0.0.1:%PORT%/ui/"
set "DSH_BIN=D:\dsh_3080\deepseek_harness\dsh-app\node_modules\@deepseek-ai\dsh\lib\bin.js"
set "DSH_DSH_HOME=D:\dsh_3080\deepseek_harness\dsh-home"
REM NOTE (P1-C1): this block manages the DEVICE-GLOBAL DSH on GLOBAL_DSH_PORT (3080).
REM The project-EMBEDDED DSH (default 3092, CANARY_DSH_PORT) is managed via dsh\start.bat
REM / dsh\stop.bat; the master only reads CANARY_DSH_PORT (see app/api/system.py).

cd /d "%PROJECT_ROOT%"

REM ---------------------------------------------------------------------
REM First run setup (venv + deps + WebUI build - each needed only once):
REM   python -m venv .venv
REM   .venv\Scripts\pip install -r requirements.txt
REM   cd webui && npm install && npm run build && cd ..
REM ---------------------------------------------------------------------
if not exist "%PROJECT_ROOT%\.venv\Scripts\activate.bat" (
    echo [af] .venv not found. Please set it up first:
    echo      python -m venv .venv
    echo      .venv\Scripts\pip install -r requirements.txt
    echo      cd webui ^&^& npm install ^&^& npm run build ^&^& cd ..  optional WebUI
    pause
    exit /b 1
)

REM ---- ensure DSH (DeepSeek Harness, port %GLOBAL_DSH_PORT%) is up -----------------
REM The /ui/console command-input panel embeds DSH; start it together with the
REM master if it is not already listening.
set "DSH_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%GLOBAL_DSH_PORT% .*LISTENING"') do (
    if not defined DSH_PID set "DSH_PID=%%p"
)
if defined DSH_PID (
    echo [af] DSH already running on port %GLOBAL_DSH_PORT% ^(PID %DSH_PID%^)
) else (
    echo [af] DSH not running. Starting it on port %GLOBAL_DSH_PORT% in a new window...
    if exist "%DSH_BIN%" (
        start "CanaryGuard DSH" cmd /k "set DSH_HOME=%DSH_DSH_HOME% && ""%windir%\System32\where.exe"" node >nul 2>&1 && node ""%DSH_BIN%"" web --no-open --trusted-host 127.0.0.1 --port %GLOBAL_DSH_PORT%"
    ) else (
        echo [af] WARNING: DSH binary not found at %DSH_BIN%. Skipping DSH start.
        echo      The /ui/console command-input panel will be unavailable until DSH is installed.
    )
)

REM ---- detect whether the master already listens on PORT --------------
set "FOUND_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    if not defined FOUND_PID set "FOUND_PID=%%p"
)

REM ---- P1-5 single-instance guard: clean up zombie app.run processes --------
REM A zombie is a python process still running "app.run --port 9200" but NOT
REM holding the port (dual-instance race). Kill every one EXCEPT the current
REM listener PID (FOUND_PID) and its PARENT (the venv Scripts\python.exe is a
REM redirector that spawns the real interpreter as a child which binds the port,
REM so both show the app.run cmdline; R1 incident: killing the healthy venv
REM redirector took the master down). CIM unavailable -> skip, never block.
set "LPID=%FOUND_PID%"
powershell -NoProfile -Command "$p=[int]$env:LPID; $pp=0; if($p){ $own=@(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p) -ErrorAction SilentlyContinue); if($own.Count){ $pp=[int]$own[0].ParentProcessId } }; $z=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.ProcessId -ne $p -and $_.ProcessId -ne $pp -and $_.CommandLine -match 'app\.run' -and $_.CommandLine -match '--port 9200' }); foreach($e in $z){ Write-Host ('[af] zombie app.run PID ' + $e.ProcessId + ' not holding port 9200 - killing'); Stop-Process -Id $e.ProcessId -Force -ErrorAction SilentlyContinue }; exit 0"

if defined FOUND_PID goto :ALREADY_RUNNING

:START_MASTER
echo [af] Master is NOT running on port %PORT%. Starting it in a new window...
start "CanaryGuard Master" cmd /k ""%PROJECT_ROOT%\.venv\Scripts\python.exe" -m app.run --host 127.0.0.1 --port %PORT%"

REM ---- P3-5: post-start re-check (close the dual-launch kill window) ----
REM Wait ~2s for our own instance to bind the port, then re-run the zombie
REM cleanup: whoever holds the port (and its venv parent) is protected, the
REM losing instance from a same-second dual launch is cleaned up.
ping -n 3 127.0.0.1 >nul
set "LPID2="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    if not defined LPID2 set "LPID2=%%p"
)
if defined LPID2 (
    set "LPID=%LPID2%"
    powershell -NoProfile -Command "$p=[int]$env:LPID; $pp=0; if($p){ $own=@(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p) -ErrorAction SilentlyContinue); if($own.Count){ $pp=[int]$own[0].ParentProcessId } }; $z=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.ProcessId -ne $p -and $_.ProcessId -ne $pp -and $_.CommandLine -match 'app\.run' -and $_.CommandLine -match '--port 9200' }); foreach($e in $z){ Write-Host ('[af] post-start: zombie app.run PID ' + $e.ProcessId + ' - killing'); Stop-Process -Id $e.ProcessId -Force -ErrorAction SilentlyContinue }; exit 0"
)

REM ---- health probe (up to ~20 seconds; curl -f fails on non-2xx) ----
REM sleep via ping -n 2: works even when stdin is redirected (timeout /t would
REM error out under redirected input, e.g. CI / scheduled tasks)
set /a PROBE=0
:PROBE_LOOP
set /a PROBE+=1
if %PROBE% gtr 20 goto :PROBE_FAILED
ping -n 2 127.0.0.1 >nul
curl -f -s -o nul "%HEALTH_URL%"
if not errorlevel 1 goto :PROBE_OK
goto :PROBE_LOOP

:PROBE_OK
echo [af] Master is healthy: %HEALTH_URL%
echo [af] admin key: %PROJECT_ROOT%\data\bootstrap_admin_key.txt
echo      (type the file to view it; or use the one-click login button in the WebUI)
start "" "%UI_URL%"
echo [af] Browser opened at %UI_URL%
goto :END

:PROBE_FAILED
echo [af] WARNING: master window was opened, but the health probe did not pass
echo      within 20 seconds. Check the "CanaryGuard Master" window for errors
echo      (port conflict? venv problem?).
echo [af] You can still open the UI manually: start %UI_URL%
goto :END

:ALREADY_RUNNING
echo [af] Master already running on port %PORT% (PID %FOUND_PID%).
echo      Not starting a second instance.
start "" "%UI_URL%"
echo [af] Opened a new browser tab at %UI_URL%
goto :END

:END
endlocal
