# check_prereqs.ps1 - run inside the Windows VM:
#   powershell -ExecutionPolicy Bypass -File windows-vm\check_prereqs.ps1
# Reports the toolchain and instrument status for building and running the
# Portable Raman project. Read-only; it changes nothing.
$ErrorActionPreference = 'SilentlyContinue'
function Row($name, $ok, $detail) {
    $tag = if ($ok) { '[ OK ]' } else { '[MISS]' }
    Write-Host ('{0} {1,-40} {2}' -f $tag, $name, $detail)
}

Write-Host '== System =='
$os = Get-CimInstance Win32_OperatingSystem
Row 'Windows' $true ('{0} build {1}, {2}' -f $os.Caption, $os.BuildNumber, $env:PROCESSOR_ARCHITECTURE)
if ($env:PROCESSOR_ARCHITECTURE -ne 'ARM64') { Write-Host '       (ARM64 expected inside a VM on Apple Silicon; x64 programs run through emulation)' }

Write-Host '== Visual Studio 2022 =='
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = $null
if (Test-Path $vswhere) { $vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath }
Row 'VS 2022 with C++ x64/x86 build tools' ([bool]$vs) $vs
if ($vs) {
    $cl = Get-ChildItem (Join-Path $vs 'VC\Tools\MSVC\*\bin\Host*\x64\cl.exe') | Select-Object -First 1
    Row 'MSVC x64 compiler (cl.exe)' ([bool]$cl) $cl.FullName
    $cli = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.CLI.Support -property installationPath
    Row 'C++/CLI support for v143' ([bool]$cli) 'needed for SampleApl (Hamamatsu backend)'
    $msb = Get-ChildItem (Join-Path $vs 'MSBuild\Current\Bin\MSBuild.exe')
    Row 'MSBuild' ([bool]$msb) $msb.FullName
}
$tp = Test-Path (Join-Path ${env:ProgramFiles(x86)} 'Reference Assemblies\Microsoft\Framework\.NETFramework\v4.7.2')
Row '.NET Framework 4.7.2 targeting pack' $tp 'needed for SampleApl'
$rel = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full').Release
Row '.NET Framework 4.7.2+ runtime' ($rel -ge 461808) ("release $rel")
$sdk = Get-ChildItem (Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Include') -Directory | Sort-Object Name | Select-Object -Last 1
Row 'Windows 10/11 SDK' ([bool]$sdk) $sdk.Name

Write-Host '== Qt / CMake / Ninja =='
$qt = $env:QT_DIR
if (-not $qt) { $qt = Get-ChildItem 'C:\Qt\6.*\msvc2022_64', 'C:\Qt\6.*\msvc2019_64' -Directory | Sort-Object FullName | Select-Object -Last 1 | ForEach-Object { $_.FullName } }
Row 'Qt 6 MSVC 64-bit kit' ([bool]$qt -and (Test-Path "$qt\bin\qmake.exe")) $qt
if ($qt) {
    Row 'Qt Charts module' (Test-Path "$qt\bin\Qt6Charts.dll") 'tick "Qt Charts" under Additional Libraries in the Qt installer'
    Row 'windeployqt' (Test-Path "$qt\bin\windeployqt.exe") ''
}
$cmake = Get-Command cmake; $cmakePath = if ($cmake) { $cmake.Source } elseif (Test-Path 'C:\Qt\Tools\CMake_64\bin\cmake.exe') { 'C:\Qt\Tools\CMake_64\bin\cmake.exe' } else { $null }
Row 'CMake' ([bool]$cmakePath) $cmakePath
$ninja = Get-Command ninja; $ninjaPath = if ($ninja) { $ninja.Source } elseif (Test-Path 'C:\Qt\Tools\Ninja\ninja.exe') { 'C:\Qt\Tools\Ninja\ninja.exe' } else { $null }
Row 'Ninja' ([bool]$ninjaPath) $ninjaPath

Write-Host '== Python =='
$py = Get-Command python
$pyver = if ($py) { (& python --version 2>&1) } else { '' }
Row 'python on PATH (controller runs "python cnn_1.py")' ([bool]$py) ("$($py.Source) $pyver")
if ($py) {
    $mods = & python -c "import importlib.util;print(' '.join(m for m in ['numpy','scipy','sklearn','torch','matplotlib','joblib'] if importlib.util.find_spec(m) is None))" 2>$null
    Row 'analysis packages' (-not $mods) $(if ($mods) { "missing: $mods" } else { 'numpy scipy sklearn torch matplotlib joblib' })
}

Write-Host '== Thorlabs Kinesis =='
$kin = $env:KINESIS_DIR; if (-not $kin) { $kin = 'C:\Program Files\Thorlabs\Kinesis' }
Row 'Kinesis SDK (DCServo .lib/.dll/.h)' ((Test-Path "$kin\Thorlabs.MotionControl.KCube.DCServo.lib") -and (Test-Path "$kin\Thorlabs.MotionControl.KCube.DCServo.h")) $kin
$ftdiDrv = Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName -like '*USB Serial Converter*' -or $_.DriverProviderName -like 'FTDI*' } | Select-Object -First 1
Row 'FTDI driver installed' ([bool]$ftdiDrv) $(if ($ftdiDrv) { "$($ftdiDrv.DriverProviderName) $($ftdiDrv.DriverVersion)" } else { 'install the ARM64 CDM driver from FTDI before plugging in the KDC101s' })

Write-Host '== Instruments (plug in and pass through to this VM first) =='
$ram = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_0661&PID_294C*' }
Row 'Hamamatsu Raman module present' ([bool]$ram) $(if ($ram) { ($ram | ForEach-Object { "$($_.FriendlyName) [$($_.Status)]" }) -join '; ' } else { 'USB\VID_0661&PID_294C not seen' })
foreach ($d in @($ram)) {
    $svc = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_Service').Data
    $key = "HKLM:\SYSTEM\CurrentControlSet\Enum\$($d.InstanceId)\Device Parameters"
    $guid = (Get-ItemProperty $key -Name DeviceInterfaceGUIDs).DeviceInterfaceGUIDs
    Row '  WinUSB driver bound' ($svc -eq 'WinUSB') "service: $svc"
    Row '  interface GUID registered' ($guid -contains '{88DC0D16-F5F0-4836-B6D0-89FD7C3495F3}') "$guid"
}
$ftdi = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_0403*' }
Row 'Thorlabs KDC101 (FTDI) controllers' (@($ftdi).Count -ge 3) ('{0} seen (3 expected): {1}' -f @($ftdi).Count, (($ftdi | ForEach-Object { "$($_.FriendlyName) [$($_.Status)]" }) -join '; '))
Write-Host '       Expected serial numbers (RamanAutofocusTest.cpp): X=27266528  Y=27266445  Z=27266446 - confirm in the Kinesis app, then close it.'

Write-Host ''
Write-Host 'Next: windows-vm\build_all.cmd   (set QT_DIR / KINESIS_DIR first if they are not at the default locations)'
