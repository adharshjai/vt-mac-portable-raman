@echo off
setlocal EnableExtensions
rem ====================================================================
rem build_all.cmd - build and deploy the whole Portable Raman project on
rem Windows as x64 binaries (works on Windows 11 Arm through emulation).
rem Run from a plain Command Prompt in the project root:
rem     windows-vm\build_all.cmd
rem Override locations by setting these before running, for example:
rem     set QT_DIR=C:\Qt\6.9.0\msvc2022_64
rem ====================================================================
if not defined QT_DIR      set "QT_DIR=C:\Qt\6.10.0\msvc2022_64"
if not defined KINESIS_DIR set "KINESIS_DIR=C:\Program Files\Thorlabs\Kinesis"
if not defined CONFIG      set "CONFIG=Release"

pushd "%~dp0.." || exit /b 1
set "ROOT=%CD%"
set "DEPLOY=%ROOT%\laser_test\build\Desktop_Qt_6_6_3_MSVC2019_64bit-Debug"
set "QT_BUILD=%ROOT%\laser_test\build-win-x64"
set "MOTOR_BUILD=%ROOT%\RamanAutofocus\build-win-x64"
set "SAMPLEAPL=%ROOT%\laser_test\hardware\SampleApl\SampleApl.vcxproj"

echo === Settings ===
echo QT_DIR      = %QT_DIR%
echo KINESIS_DIR = %KINESIS_DIR%
echo CONFIG      = %CONFIG%
echo DEPLOY      = %DEPLOY%
if not exist "%QT_DIR%\bin\qmake.exe"        (echo ERROR: Qt kit not found at %QT_DIR% - set QT_DIR & exit /b 1)
if not exist "%QT_DIR%\bin\Qt6Charts.dll"    (echo ERROR: Qt Charts is not installed in this kit - add it with the Qt Maintenance Tool & exit /b 1)
if not exist "%KINESIS_DIR%\Thorlabs.MotionControl.KCube.DCServo.lib" (echo ERROR: Kinesis SDK not found at %KINESIS_DIR% - set KINESIS_DIR & exit /b 1)
if not exist "%DEPLOY%\cnn_1.py"             (echo ERROR: deploy folder missing cnn_1.py - is the project copied completely? & exit /b 1)

rem --- Visual Studio 2022 developer environment, x64 target
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (echo ERROR: Visual Studio 2022 is not installed & exit /b 1)
set "VSDIR="
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
if not defined VSDIR (echo ERROR: no Visual Studio with the "C++ x64/x86 build tools" component & exit /b 1)
call "%VSDIR%\Common7\Tools\VsDevCmd.bat" -arch=x64 -no_logo
if errorlevel 1 exit /b 1
where cl.exe >nul 2>nul || (echo ERROR: no x64 C++ compiler in this environment - install "MSVC v143 - VS 2022 C++ x64/x86 build tools" & exit /b 1)

rem --- CMake / Ninja: use Qt's copies unless already on PATH
where cmake >nul 2>nul || set "PATH=C:\Qt\Tools\CMake_64\bin;%PATH%"
where ninja >nul 2>nul || set "PATH=C:\Qt\Tools\Ninja;%PATH%"
where cmake >nul 2>nul || (echo ERROR: cmake not found - install it from the Qt installer, Build Tools & exit /b 1)
where ninja >nul 2>nul || (echo ERROR: ninja not found - install it from the Qt installer, Build Tools & exit /b 1)

set "KINESIS_FWD=%KINESIS_DIR:\=/%"

echo.
echo === 1/4  Qt controller (laser_test) ===
cmake -S "%ROOT%\laser_test" -B "%QT_BUILD%" -G Ninja -DCMAKE_BUILD_TYPE=%CONFIG% -DCMAKE_PREFIX_PATH="%QT_DIR%"
if errorlevel 1 exit /b 1
cmake --build "%QT_BUILD%"
if errorlevel 1 exit /b 1

echo.
echo === 2/4  Motor program (RamanAutofocus target only) ===
cmake -S "%ROOT%\RamanAutofocus" -B "%MOTOR_BUILD%" -G Ninja -DCMAKE_BUILD_TYPE=%CONFIG% -DTHORLABS_SDK_PATH="%KINESIS_FWD%"
if errorlevel 1 exit /b 1
cmake --build "%MOTOR_BUILD%" --target RamanAutofocus
if errorlevel 1 exit /b 1

echo.
echo === 3/4  Hamamatsu backend (SampleApl, C++/CLI, x64) ===
MSBuild.exe "%SAMPLEAPL%" /p:Configuration=%CONFIG% /p:Platform=x64 /p:PlatformToolset=v143 /p:PreferredToolArchitecture=x64 /p:WindowsTargetPlatformVersion=10.0 /p:OutDir="%DEPLOY%\handshake_v2\backend\\" /p:IntDir="%ROOT%\laser_test\hardware\SampleApl\obj_%CONFIG%\\" /m /v:minimal
if errorlevel 1 exit /b 1

echo.
echo === 4/4  Deploy next to laser_test.exe ===
if not exist "%DEPLOY%\handshake_v2\motor" mkdir "%DEPLOY%\handshake_v2\motor"
copy /y "%QT_BUILD%\laser_test.exe" "%DEPLOY%\laser_test.exe" >nul || exit /b 1
copy /y "%MOTOR_BUILD%\RamanAutofocus\RamanAutofocus.exe" "%DEPLOY%\handshake_v2\motor\" >nul || exit /b 1
copy /y "%KINESIS_DIR%\Thorlabs.MotionControl.KCube.DCServo.dll" "%DEPLOY%\handshake_v2\motor\" >nul || exit /b 1
copy /y "%KINESIS_DIR%\Thorlabs.MotionControl.DeviceManager.dll" "%DEPLOY%\handshake_v2\motor\" >nul || exit /b 1
if not exist "%DEPLOY%\handshake_v2\backend\wrrusb2a.dll" copy /y "%ROOT%\laser_test\hardware\DLL\wrrusb2a.dll" "%DEPLOY%\handshake_v2\backend\" >nul
"%QT_DIR%\bin\windeployqt.exe" --no-translations "%DEPLOY%\laser_test.exe" >nul
if errorlevel 1 exit /b 1

echo.
echo === Done. Deployed layout ===
dir /b "%DEPLOY%\laser_test.exe" "%DEPLOY%\handshake_v2\backend" "%DEPLOY%\handshake_v2\motor"
echo.
echo Start "%DEPLOY%\laser_test.exe" after both instruments are attached to this VM.
echo Read INSTALL_AND_RUN_NOTES.txt section 6 before any hardware run.
popd
exit /b 0
