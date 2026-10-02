@echo off
setlocal
cd /d "%~dp0"

rem ---- find a Python launcher: prefer "py", fall back to "python" ----
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)

if not defined PY (
  echo.
  echo   [X] Python not found.
  echo.
  echo   Install Python 3.10 or newer:  https://www.python.org/downloads/
  echo   During setup, tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo Starting musictag, using: %PY%
echo.

%PY% webui.py %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo   [!] musictag exited with code %CODE%.
  echo       The message above says why.  The usual fix is the missing dependency:
  echo.
  echo           %PY% -m pip install mutagen
  echo.
  pause
)

endlocal
