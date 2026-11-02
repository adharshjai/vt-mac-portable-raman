@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name BlueskyBridge --distpath . --workpath packaging\build --specpath packaging launcher.py
if errorlevel 1 exit /b 1
echo Built BlueskyBridge.exe. Keep it in this folder.
