# Set-RamanDeviceInterfaceGuid.ps1
# Run inside the Windows VM, in PowerShell started "as Administrator", AFTER the
# spectrometer has been bound to the inbox "WinUsb Device" driver (see README step 5).
#
# RamSpecView's USB library (wrrusb2a.dll) finds the module through the device
# interface GUID below. The generic WinUsb driver does not register that GUID, so
# this script writes it into the device's registry node and restarts the device.

$guid = '{88DC0D16-F5F0-4836-B6D0-89FD7C3495F3}'

$devs = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_0661&PID_294C*' }
if (-not $devs) {
    Write-Error "Raman module (USB\VID_0661&PID_294C) not found. Plug it in and pass it through to this VM, then run again."
    exit 1
}

foreach ($d in $devs) {
    $service = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue).Data
    Write-Host "Found $($d.InstanceId)  [$($d.FriendlyName)]  driver service: $service"
    if ($service -ne 'WinUSB') {
        Write-Warning "This device is not using the WinUSB driver yet. Do README step 5 (Update driver -> WinUsb Device) first, then re-run."
    }

    $key = "HKLM:\SYSTEM\CurrentControlSet\Enum\$($d.InstanceId)\Device Parameters"
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name 'DeviceInterfaceGUIDs' -PropertyType MultiString -Value @($guid) -Force | Out-Null
    Write-Host "  DeviceInterfaceGUIDs = $guid"

    pnputil /restart-device "$($d.InstanceId)" | Out-Null
    Write-Host "  Device restarted."
}

Write-Host ""
Write-Host "Done. Start RamSpecView.exe. If it still reports no device, unplug the module, plug it back in, and send it to the VM again."
