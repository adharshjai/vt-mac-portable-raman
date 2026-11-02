@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 goto usepython
py -3.12 -m venv .venv
if errorlevel 1 goto failed
goto install
:usepython
python -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Double-click Start_Bluesky.cmd.
pause
exit /b 0
:failed
echo Setup failed. Python 3.12 and internet access are required.
pause
exit /b 1
