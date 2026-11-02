#!/bin/bash
# build_macos.sh - build every Mac-native piece and assemble deploy-macos/ (see PORT_SPEC.md §1).
#   ./macos-native/build_macos.sh            build + deploy
#   ./macos-native/build_macos.sh --test     also run the unit/protocol tests and the controller test suite
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NATIVE="$ROOT/macos-native"
DEPLOY="$ROOT/deploy-macos"
WINDEPLOY="$ROOT/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug"   # the hand-off folder holding cnn_1.py, models, dark data
QT="${QT_DIR:-$HOME/Qt/6.12.0/macos}"
CMAKE="${CMAKE:-$HOME/Qt/Tools/CMake/CMake.app/Contents/bin/cmake}"
NINJA="${NINJA:-$HOME/Qt/Tools/Ninja/ninja}"
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"; export PATH="$DOTNET_ROOT:$PATH"; export DOTNET_CLI_TELEMETRY_OPTOUT=1
DOTNET="$DOTNET_ROOT/dotnet"
# Python with pyserial, usable from a GUI-launched process (absolute path is baked into the launcher)
PYTHON="${RAMAN_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in /opt/anaconda3/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$cand" ] && "$cand" -c "import serial" 2>/dev/null; then PYTHON="$cand"; break; fi
  done
fi
[ -n "$PYTHON" ] || { echo "ERROR: no python3 with pyserial found; pip install pyserial or set RAMAN_PYTHON"; exit 1; }
[ -x "$DOTNET" ] || { echo "ERROR: .NET SDK not found at $DOTNET (set DOTNET_ROOT)"; exit 1; }
[ -d "$QT" ] || { echo "ERROR: Qt kit not found at $QT (set QT_DIR)"; exit 1; }
echo "== settings: PYTHON=$PYTHON  DOTNET=$DOTNET  QT=$QT"
[ -f "$NATIVE/backend/lib/wrrusb2a.dll" ] || { echo "ERROR: macos-native/backend/lib/wrrusb2a.dll is missing (vendor library, not in git). See macos-native/backend/lib/README.md"; exit 1; }

echo "== 1/5 Windows-API shim (libusb) =="
make -C "$NATIVE/backend/shim" -s
echo "== 2/5 Forms stand-in + capture backend (.NET) =="
( cd "$NATIVE/backend/stubs/System.Windows.Forms" && "$DOTNET" build -c Release -nologo -v q )
( cd "$NATIVE/backend/SampleApl" && "$DOTNET" publish -c Release -o bin/publish -nologo -v q )
echo "== 3/5 Qt controller (flat layout) =="
"$CMAKE" -S "$ROOT/laser_test" -B "$ROOT/build-macos-flat" -G Ninja -DCMAKE_MAKE_PROGRAM="$NINJA" \
   -DCMAKE_PREFIX_PATH="$QT" -DCMAKE_BUILD_TYPE=Release -DRAMAN_FLAT_LAYOUT=ON > /dev/null
"$CMAKE" --build "$ROOT/build-macos-flat" | tail -1

echo "== 4/5 assemble $DEPLOY =="
mkdir -p "$DEPLOY/handshake_v2/backend" "$DEPLOY/handshake_v2/motor" "$DEPLOY/scan_sessions" "$DEPLOY/dark_measurements"
cp "$ROOT/build-macos-flat/laser_test" "$DEPLOY/laser_test"
# backend: published .NET output + shim + launcher with the absolute dotnet path
cp "$NATIVE/backend/SampleApl/bin/publish/"* "$DEPLOY/handshake_v2/backend/"
cp "$NATIVE/backend/shim/libwinusb_shim.dylib" "$DEPLOY/handshake_v2/backend/"
sed "s|@DOTNET@|$DOTNET|g" "$NATIVE/backend/SampleApl/SampleApl" > "$DEPLOY/handshake_v2/backend/SampleApl"
chmod +x "$DEPLOY/handshake_v2/backend/SampleApl"
# motor: python sources + config + launcher with the absolute python path
cp "$NATIVE/motor/"*.py "$NATIVE/motor/motor_config.json" "$DEPLOY/handshake_v2/motor/"
sed "s|@PYTHON@|$PYTHON|g" "$NATIVE/motor/RamanAutofocus" > "$DEPLOY/handshake_v2/motor/RamanAutofocus"
chmod +x "$DEPLOY/handshake_v2/motor/RamanAutofocus"
# analysis script and the model files it loads, plus the dark measurements from the hand-off
cp "$WINDEPLOY/cnn_1.py" "$DEPLOY/"
[ -d "$WINDEPLOY/ckpts" ] && rsync -a "$WINDEPLOY/ckpts/" "$DEPLOY/ckpts/"
[ -f "$WINDEPLOY/best_raman_1dcnn.pth" ] && cp "$WINDEPLOY/best_raman_1dcnn.pth" "$DEPLOY/"
rsync -a "$WINDEPLOY/dark_measurements/" "$DEPLOY/dark_measurements/" 2>/dev/null || true
# training database and the CNN model folder the analysis script reads (DATA_ROOT = its own folder)
[ -d "$WINDEPLOY/Portable_Raman_database" ] && rsync -a "$WINDEPLOY/Portable_Raman_database/" "$DEPLOY/Portable_Raman_database/"
[ -d "$WINDEPLOY/_CNN_2class_r6g_mb" ] && rsync -a "$WINDEPLOY/_CNN_2class_r6g_mb/" "$DEPLOY/_CNN_2class_r6g_mb/"
cat > "$DEPLOY/run_simulated.sh" <<'RUN'
#!/bin/bash
# Start the controller with BOTH backends in simulation (no instruments needed).
cd "$(dirname "$0")"
RAMAN_PYTHON="@PYTHON@" RAMAN_BACKEND_SIMULATE=1 RAMAN_MOTOR_SIMULATE=1 exec ./laser_test "$@"
RUN
cat > "$DEPLOY/run_hardware.sh" <<'RUN'
#!/bin/bash
# Start the controller against the real spectrometer (libusb) and KDC101 controllers (serial).
cd "$(dirname "$0")"
unset RAMAN_BACKEND_SIMULATE RAMAN_MOTOR_SIMULATE
RAMAN_PYTHON="@PYTHON@" exec ./laser_test "$@"
RUN
sed -i '' "s|@PYTHON@|$PYTHON|g" "$DEPLOY/run_simulated.sh" "$DEPLOY/run_hardware.sh"
chmod +x "$DEPLOY/run_simulated.sh" "$DEPLOY/run_hardware.sh"
echo "deployed: $(ls "$DEPLOY" | tr '\n' ' ')"

if [ "${1:-}" = "--test" ]; then
  echo "== 5/5 tests =="
  ( cd "$NATIVE/backend/shim" && ./shim_selftest )
  "$PYTHON" -m unittest discover -s "$NATIVE/motor/tests" -v 2>&1 | tail -3
  "$PYTHON" -m unittest "$NATIVE/backend/tests/test_backend_protocol.py" -v 2>&1 | tail -3
  ( cd "$DEPLOY/handshake_v2/backend" && ./SampleApl --selftest | tail -2 )
  "$CMAKE" -S "$ROOT/laser_test/tests" -B "$ROOT/laser_test/tests/build-macos" -G Ninja -DCMAKE_MAKE_PROGRAM="$NINJA" -DCMAKE_PREFIX_PATH="$QT" > /dev/null
  "$CMAKE" --build "$ROOT/laser_test/tests/build-macos" | tail -1
  ( cd "$ROOT/laser_test/tests/build-macos" && ./handshake_tests | tail -4 )
  echo "== e2e simulated pipeline (several minutes) =="
  "$CMAKE" -S "$NATIVE/e2e" -B "$NATIVE/e2e/build" -G Ninja -DCMAKE_MAKE_PROGRAM="$NINJA" -DCMAKE_PREFIX_PATH="$QT" > /dev/null
  "$CMAKE" --build "$NATIVE/e2e/build" | tail -1
  rm -rf "$NATIVE/e2e/build/handshake_v2"; cp -R "$DEPLOY/handshake_v2" "$NATIVE/e2e/build/handshake_v2"
  ( cd "$NATIVE/e2e/build" && ./e2e_simulated_test )
else
  echo "== done (add --test to run the test suites) =="
fi
