@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1

title SafeWorkspace Agent - Launcher
set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"
set "BACKEND_DIR=%ROOT_DIR%\backend"
set "FRONTEND_DIR=%ROOT_DIR%\frontend"

echo ============================================================
echo   SafeWorkspace Agent - One-Click Launcher (Windows)
echo ============================================================
echo.

:: ------------------------------------------------------------
:: 1. Resolve usable Python 3.11+ interpreter (prefer one with deps)
:: ------------------------------------------------------------
set "PY_CMD="
set "PY_FALLBACK="

if not defined PYTHON_EXE if exist "%ROOT_DIR%\.local_python" (
    set /p PYTHON_EXE=<"%ROOT_DIR%\.local_python"
)

if defined PYTHON_EXE call :probe_python "%PYTHON_EXE%"
if not defined PY_CMD if exist "%BACKEND_DIR%\.venv\Scripts\python.exe" call :probe_python "%BACKEND_DIR%\.venv\Scripts\python.exe"
if not defined PY_CMD if exist "%ROOT_DIR%\.venv\Scripts\python.exe" call :probe_python "%ROOT_DIR%\.venv\Scripts\python.exe"

if not defined PY_CMD (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            if not defined PY_CMD call :probe_python "%%P"
        )
    )
)

if not defined PY_CMD (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if not defined PY_CMD call :probe_python "%%P"
    )
)

if not defined PY_CMD if defined PY_FALLBACK (
    set "PY_CMD=%PY_FALLBACK%"
    echo [INFO] Found Python: !PY_CMD!
    echo [INFO] Installing backend dependencies from requirements.txt ...
    "!PY_CMD!" -m pip install -r "%BACKEND_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Failed to install backend Python dependencies.
        pause
        exit /b 1
    )
)

if not defined PY_CMD (
    echo [ERROR] Python 3.11+ was not found on this machine.
    echo         Please install Python 3.11+ or set PYTHON_EXE before running start.bat.
    pause
    exit /b 1
)

echo [OK] Python   : %PY_CMD%

:: ------------------------------------------------------------
:: 2. Check Node.js & frontend dependencies
:: ------------------------------------------------------------
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js / npm not found in PATH. Please install Node.js 20+.
    pause
    exit /b 1
)

for /f "delims=" %%N in ('node -v 2^>nul') do set "NODE_VER=%%N"
echo [OK] Node.js  : %NODE_VER%

if not exist "%FRONTEND_DIR%\node_modules" (
    echo [INFO] Installing frontend dependencies via npm install...
    pushd "%FRONTEND_DIR%"
    call npm install
    if errorlevel 1 (
        popd
        echo [ERROR] npm install failed.
        pause
        exit /b 1
    )
    popd
) else (
    echo [OK] Frontend : node_modules ready
)

:: ------------------------------------------------------------
:: 3. Initialize .env if missing (optional, keeps user config safe)
:: ------------------------------------------------------------
if not exist "%ROOT_DIR%\.env" (
    if exist "%ROOT_DIR%\.env.example" (
        echo [INFO] Tip: You can configure BYOK API Key directly in the Web UI top-right modal,
        echo        or copy .env.example to .env for persistent defaults.
    )
)

if /i "%~1"=="--check" (
    echo.
    echo [CHECK] Dry-run environment check passed!
    echo         Backend dir  : %BACKEND_DIR%
    echo         Frontend dir : %FRONTEND_DIR%
    exit /b 0
)

echo.
echo [1/2] Starting Backend  (FastAPI + LangGraph) on http://127.0.0.1:8000 ...
start "SafeWorkspace Backend (8000)" /D "%BACKEND_DIR%" cmd /k ""%PY_CMD%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

echo [2/2] Starting Frontend (Vue 3 + Vite)        on http://localhost:5173 ...
start "SafeWorkspace Frontend (5173)" /D "%FRONTEND_DIR%" cmd /k "npm run dev"

:: ------------------------------------------------------------
:: 4. Wait briefly for services to become reachable, then open browser
:: ------------------------------------------------------------
echo.
echo [WAIT] Waiting for services to start...
powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(20); while((Get-Date) -lt $deadline){ try { $r1=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 2; $r2=Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:5173' -TimeoutSec 2; if($r1.StatusCode -eq 200 -and $r2.StatusCode -eq 200){ exit 0 } } catch {}; Start-Sleep -Milliseconds 600 }; exit 0" >nul 2>&1

echo.
echo ============================================================
echo   SafeWorkspace Agent is running!
echo   - Web Workbench : http://localhost:5173
echo   - Backend API   : http://127.0.0.1:8000/docs
echo.
echo   Close the Backend and Frontend terminal windows to stop.
echo ============================================================
start "" "http://localhost:5173"
timeout /t 4 >nul
exit /b 0

:: ------------------------------------------------------------
:: Helper: verify Python >= 3.11 and whether core deps exist
:: ------------------------------------------------------------
:probe_python
set "CANDIDATE=%~1"
if "%CANDIDATE%"=="" goto :eof
"%CANDIDATE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
if not defined PY_FALLBACK set "PY_FALLBACK=%CANDIDATE%"
"%CANDIDATE%" -c "import fastapi, uvicorn, langgraph, langchain_openai, sse_starlette, docx" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=%CANDIDATE%"
)
goto :eof
