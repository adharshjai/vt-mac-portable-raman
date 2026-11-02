"""Bluesky -> real Qt mailbox -> SDK-free backend/motor EMULATORS only."""
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from unittest.mock import patch

import bridge


def main():
    cfg = bridge.settings()
    original = bridge.device_settings(cfg)
    source = cfg["intersect_bridge_dir"] / "build_mock/bin"
    dest = bridge.ROOT / "test_outputs" / ("mock_qt_" + uuid.uuid4().hex[:8])
    # Only the known generated MOCK build is allowed here. No device build path.
    for relative in ("raman_intersect.exe", "handshake_v2/backend/SampleApl.exe", "handshake_v2/motor/RamanAutofocus.exe"):
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    bridge.save_json(dest / "TEST_ONLY.json", dict(note="Qt + SDK-free emulators; no real laser/motor", real_hardware=False))
    paths = dict(original, ipc_dir=dest / "ipc", scan_root=dest / "scan_sessions")
    cfg = dict(cfg, output_dir=dest / "bluesky_runs", poll_seconds=0.1)
    mailbox = bridge.upstream_module(cfg, "qt_client").QtClient(paths["ipc_dir"])
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_OPENGL="software",
               QT_QPA_PLATFORM_PLUGIN_PATH="C:/Qt/6.6.3/msvc2019_64/plugins/platforms")
    env["PATH"] = "C:/Qt/6.6.3/msvc2019_64/bin;" + env["PATH"]
    # Constructor starts only our copied SDK-free fake backend.
    with (dest / "qt.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(dest / "raman_intersect.exe")], cwd=str(dest), env=env,
                                   stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    mailbox.status()
                    break
                except (RuntimeError, FileNotFoundError):
                    if time.monotonic() > deadline or process.poll() is not None:
                        raise RuntimeError("Mock Qt did not become ready; inspect qt.log")
                    time.sleep(0.1)
            with patch.object(bridge, "qt_client", return_value=mailbox), patch.object(bridge, "device_settings", return_value=paths):
                report = bridge.run_experiment("hardware", "MOCK-ONLY-NO-DEVICE", "1d-y", cfg=cfg,
                                               timeout=60, on_event=lambda name, value: print(value if name == "log" else "", end="\n" if name == "log" else ""))
                assert report["state"] == "completed", report
                assert report["recorded_points"] == 13, report
                assert not mailbox.status()["scan_active"]
            bridge.save_json(dest / "test_report.json", dict(passed=True, real_hardware=False,
                              test="Bluesky -> Qt -> emulated capture/motor -> PCA-SVM -> Events", summary=report))
            print("PASS: copied MOCK Qt, 13 point Events, model output; no real devices. " + str(dest))
        finally:
            # Stop only the exact process tree started above, never an operator's Qt.
            prefix = str(dest.resolve()) + os.sep
            script = ("$mockPrefix='" + prefix.replace("'", "''") + "'; "
                      "Get-Process -Name raman_intersect,SampleApl,RamanAutofocus -ErrorAction SilentlyContinue | "
                      "Where-Object { $_.Path -and $_.Path.StartsWith($mockPrefix, [System.StringComparison]::OrdinalIgnoreCase) } | "
                      "Stop-Process -Force -ErrorAction Stop; exit 0")
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
