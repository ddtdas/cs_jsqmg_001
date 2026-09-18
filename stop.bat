@echo off
REM =====================================================================
REM  CanaryGuard AntiFraud v1.0.0 - stop the master server
REM  (Windows cmd)
REM
REM  Finds the process listening on port 9200 (netstat -> PID) and kills
REM  it together with its child process tree (taskkill /T /F). If nothing
REM  is listening, reports that the master is not running.
REM
REM  Pure ASCII + CRLF + %~dp0-free (no path needed): runs under any
REM  Windows code page.
REM =====================================================================

setlocal
set "PORT=9200"
set "GLOBAL_DSH_PORT=3080"

REM ---- find the PID listening on PORT ---------------------------------
set "FOUND_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    if not defined FOUND_PID set "FOUND_PID=%%p"
)

if not defined FOUND_PID (
    echo [af] Master is NOT running on port %PORT%.
    goto :STOP_DSH
)

echo [af] Stopping master: PID %FOUND_PID% (listening on port %PORT%)
taskkill /PID %FOUND_PID% /T /F
if errorlevel 1 (
    echo [af] taskkill failed - the process may have already exited.
    endlocal
    exit /b 1
)
echo [af] Master stopped. You can restart it with start.bat

:STOP_DSH
REM ---- also stop DSH (DeepSeek Harness) if it is listening on GLOBAL_DSH_PORT -----
set "DSH_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%GLOBAL_DSH_PORT% .*LISTENING"') do (
    if not defined DSH_PID set "DSH_PID=%%p"
)
if not defined DSH_PID (
    echo [af] DSH is NOT running on port %GLOBAL_DSH_PORT%.
    endlocal
    exit /b 0
)
echo [af] Stopping DSH: PID %DSH_PID% (listening on port %GLOBAL_DSH_PORT%)
taskkill /PID %DSH_PID% /T /F
echo [af] DSH stopped. start.bat will bring it back up together with the master.
endlocal
