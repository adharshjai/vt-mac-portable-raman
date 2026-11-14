# wrrusb2a.dll goes here (not in the repository)

The Mac-native backend runs Hamamatsu's own USB library, `wrrusb2a.dll`, which is proprietary
and therefore not committed. To restore it:

1. Take `wrrusb2a.dll` from the module's software CD (`RamanSpectrumViewer_x64/wrrusb2a.dll`,
   version 1.0.0.3) or from the Windows deploy folder of the hand-off, and copy it anywhere, for
   example into a new folder `laser_test/hardware/DLL/` (where the Windows backend of the original
   project expects it; this edition has no such folder until you make it).
2. Create the arm64-loadable copy used on macOS from that copy:

   ```bash
   python3 macos-native/tools/retag_assembly.py laser_test/hardware/DLL/wrrusb2a.dll macos-native/backend/lib/wrrusb2a.dll
   ```

`macos-native/build_macos.sh` checks for the file and stops with this message if it is missing.
