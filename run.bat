@echo off
title Pete AI (Purdue GenAI Studio)

rem Enter the project folder, whatever it is.
rem
rem This MUST be `pushd`, not `cd /d`. This project lives on a UNC network share
rem (\\server\share\...), and cmd.exe cannot hold a UNC path as its current
rem directory. `cd /d` on a UNC path fails, cmd falls back to C:\Windows, and
rem %CD% then silently points at the Windows directory -- so ".venv\Scripts" and
rem "requirements.txt" are both looked up in C:\Windows and reported missing,
rem even though both exist right here. That produced the confusing
rem   "Virtual environment not found" / "Could not open requirements file"
rem pair, which looks like a broken install but is purely a path bug.
rem
rem `pushd` maps the UNC path to a temporary drive letter (Z:, Y:, ...) for the
rem life of the script, which cmd *can* use as a current directory.
rem `popd` at the end (via goto :done) releases that mapping.
pushd "%~dp0"
if errorlevel 1 (
    echo.
    echo ERROR: Could not open the project folder:
    echo    "%~dp0"
    echo Check the network share is reachable and try again.
    pause
    exit /b 1
)

rem Switch the console to UTF-8 (code page 65001). Without this the window sits on
rem the OEM code page (cp437/cp1252), and any message containing an em dash, a
rem curly quote or a non-Latin character raises:
rem     UnicodeEncodeError: 'charmap' codec can't encode character ...
rem app\console.py also forces UTF-8 inside Python, because this line only covers
rem the app when launched from this file -- `uvicorn app.api:app` and the test
rem scripts would otherwise still inherit the OEM code page. Both are needed:
rem this one makes the bytes render correctly, the Python one makes them safe.
chcp 65001 >nul 2>&1

echo ========================================================
echo   Starting Pete AI...
echo ========================================================

rem NOTE: we deliberately do not "call .venv\Scripts\activate.bat" here. That script
rem hardcodes an absolute VIRTUAL_ENV path, so moving or renaming the project folder
rem leaves it pointing at a directory that no longer exists -- it then prepends a
rem dead path to PATH, `python` falls through to the system install, and startup
rem dies with a confusing "No module named 'uvicorn'" even though the venv has it.
rem Addressing the interpreter directly is immune to both problems.
rem
rem Two batch-file traps are avoided below, both of which bite when the project
rem folder has a space in its name (like "Purdue Pete AI"):
rem   * every use of %PYTHON% is wrapped in quotes, or the path is split at the space
rem   * the variable is set OUTSIDE any parenthesised block, because cmd expands
rem     variables when it reads the block, not when it runs it
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

if "%PYTHON%"=="python" goto no_venv
echo Using virtual environment: "%PYTHON%"
goto deps

:no_venv
echo Virtual environment not found at "%CD%\.venv".
echo Using system Python instead: "%PYTHON%"
echo.
echo If Pete has worked before, the .venv folder may have been moved or deleted.
echo Recreate it with:  python -m venv .venv
echo.

rem Fail loudly and usefully if dependencies are missing, rather than crashing
rem with a bare ImportError on the first line of main.py.
:deps
"%PYTHON%" -c "import uvicorn, fastapi, playwright" 2>nul
if not errorlevel 1 goto run_app

echo.
echo Missing dependencies. Installing from requirements.txt...
echo Using: "%PYTHON%"
echo.
rem Say WHERE we are looking. If this ever says C:\Windows again, the share
rem mapping failed and the install below would write to the wrong place.
echo Working directory: %CD%
echo.
if not exist "requirements.txt" (
    echo ERROR: requirements.txt is not present in the folder above.
    echo The project folder looks incomplete.
    goto install_failed
)
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto install_failed
echo Dependencies installed.

:run_app
"%PYTHON%" main.py
set "APP_RC=%errorlevel%"
goto :done

rem Common exit path: release the pushd drive mapping before we leave.
:done
popd
if not "%APP_RC%"=="0" goto app_failed
endlocal & exit /b 0

:install_failed
echo.
echo ERROR: Could not install dependencies. Check your internet connection
echo and that this interpreter can reach PyPI, then try again.
popd
pause
exit /b 1

:app_failed
echo.
echo ERROR: Pete AI exited with an error. See the traceback above.
popd
pause
exit /b 1
