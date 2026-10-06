@echo off
title Building PeteAI.exe
rem ============================================================================
rem Builds PeteAI.exe and arranges the finished layout:
rem
rem     dist\PeteAI\
rem         PeteAI.exe    the app
rem         Engine\       app files + the runtime the exe needs (do not rename)
rem
rem That is the whole folder. There is no _internal\ beside it any more: the
rem spec sets COLLECT(contents_directory="Engine"), so PyInstaller creates the
rem support folder under that name and bakes the name into the exe. See
rem PETE_UX_NOTES.md 8.6 for why renaming the folder afterwards is fatal.
rem
rem Double-click this to rebuild. Takes several minutes on a network share.
rem ============================================================================

rem pushd, not cd /d: this project is on a UNC share and cmd cannot hold a UNC
rem path as its current directory. See run.bat and PETE_UX_NOTES.md 8.2.
pushd "%~dp0"
if errorlevel 1 (
    echo ERROR: could not open "%~dp0"
    pause
    exit /b 1
)

chcp 65001 >nul 2>&1

echo ========================================================
echo   Building PeteAI.exe...
echo ========================================================

rem --clean is not used on purpose: the cached analysis in build\ is what makes a
rem rebuild fast. Pass --clean as an argument if a full rebuild is needed.
".venv\Scripts\python.exe" -m PyInstaller PeteAI.spec --noconfirm %1
if errorlevel 1 goto build_failed

if not exist "dist\PeteAI\PeteAI.exe" (
    echo ERROR: PyInstaller reported success but dist\PeteAI\PeteAI.exe is missing.
    goto build_failed
)

rem The exe's support folder is named Engine, per contents_directory in the
rem spec. This check is what catches the 8.6 failure: if the support folder is
rem present but incomplete, the exe dies at launch with
rem     Failed to load Python DLL '...\Engine\python312.dll'
rem and nothing else explains it.
if not exist "dist\PeteAI\Engine\python312.dll" (
    echo ERROR: dist\PeteAI\Engine is incomplete -- python312.dll is missing.
    goto build_failed
)

rem data\ holds chats, workspaces and the saved API key. It must live in Engine
rem so those survive a restart (config.py points BASE_DIR at Engine when frozen),
rem and must NOT be copied from the development tree, or a colleague's build
rem would ship with my chat history and settings baked in.
if not exist "dist\PeteAI\Engine\data" mkdir "dist\PeteAI\Engine\data"

rem An _internal\ here means an older layout survived a rebuild, or a stale one
rem was never cleaned up. The exe ignores it, but it is ~300 MB of confusing
rem dead weight next to a folder that is supposed to be tidy.
if exist "dist\PeteAI\_internal" (
    echo.
    echo   Removing leftover _internal\ from an older build...
    rmdir /s /q "dist\PeteAI\_internal"
)

echo.
echo ========================================================
echo   Build complete.
echo ========================================================
echo.
echo   dist\PeteAI\PeteAI.exe    ^<- run this
echo   dist\PeteAI\Engine\        ^<- app files (do not rename)
echo.
echo First run: Playwright needs its browser once per user:
echo     .venv\Scripts\python -m playwright install chromium
echo.
popd
pause
exit /b 0

:build_failed
echo.
echo ERROR: the build failed. See the output above.
popd
pause
exit /b 1