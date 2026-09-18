@echo off
REM =====================================================================
REM  CanaryGuard AntiFraud v1.0.0 - open the project console
REM  (Windows cmd)
REM
REM  Opens a new cmd window that:
REM    - cd /d to the project root (derived from %~dp0, robust for
REM      non-ASCII folder names)
REM    - activates the venv (.venv\Scripts\activate.bat)
REM    - prints the common commands (master / pytest / MCP / WebUI dev)
REM  The window stays open. Running start.bat again opens a second
REM  master window / browser tab on top of the existing one.
REM
REM  Pure ASCII + CRLF: runs under any Windows code page.
REM =====================================================================

setlocal
set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

start "CanaryGuard Console" cmd /k "cd /d ""%PROJECT_ROOT%"" && call ""%PROJECT_ROOT%\.venv\Scripts\activate.bat"" && title CanaryGuard Console (running start.bat again opens one more browser tab) && echo[ && echo [af] Project console ready - project root + venv activated && echo[ && echo [af] Useful commands: && echo [af]   python -m app.run --host 127.0.0.1 --port 9200   start master (or double-click start.bat) && echo [af]   pytest -q                                          run all tests && echo [af]   .venv\Scripts\python.exe mcp\server.py              MCP stdio && echo [af]   .venv\Scripts\python.exe mcp\server.py --http --port 9201   MCP http && echo [af]   (cd webui) npm run dev                              WebUI dev server"

endlocal
