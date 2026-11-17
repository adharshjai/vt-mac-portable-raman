#!/bin/bash
# build_macos.sh - build every Mac-native piece of the catalog edition and assemble deploy-macos/
# inside this folder (see PORT_SPEC.md §1). Nothing outside this folder is written, except that a
# missing .venv is made by data_layer/setup_env.sh, which downloads packages from PyPI (through pip's
# cache), and that the tests use the temporary folder.
#   ./macos-native/build_macos.sh            build + deploy
#   ./macos-native/build_macos.sh --test     also run every test suite of the edition (about 15 minutes)
# With --test, RAMAN_SKIP_DEVICE_SELFTESTS=1 leaves out the self-tests that open the USB stack and the
# vendor library (they are always left out while the instruments are attached).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NATIVE="$ROOT/macos-native"
DEPLOY="$ROOT/deploy-macos"
# Optional: the original project's hand-off folder (cnn_1.py, CNN models, training database) for the
# "Analyze Latest (CNN)" button. Found automatically when this folder sits inside the original project.
WINDEPLOY="${RAMAN_HANDOFF_DIR:-$ROOT/../laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug}"
# Interpreter for the data layer (catalog + classifier): this folder's .venv, see data_layer/setup_env.sh
DATAPY="${RAMAN_CATALOG_PYTHON:-$ROOT/.venv/bin/python}"
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

# A CMake build directory cannot be reused after the project folder moved: discard its cache.
fresh_if_moved() {
  if [ -f "$1/CMakeCache.txt" ] && ! grep -qF "CMAKE_CACHEFILE_DIR:INTERNAL=$1" "$1/CMakeCache.txt"; then
    echo "   (the project folder moved: discarding the stale CMake cache in $1)"
    rm -rf "$1/CMakeCache.txt" "$1/CMakeFiles"
  fi
}

# True while an upload to ARC runs: its program holds the worker.lock of its upload's folder
# (data_layer/PROTOCOL.md 5.6). A free lock is taken and given back at once, as arc-list does.
arc_upload_running() {
  [ -d "$DEPLOY/data_catalog/arc_uploads" ] || return 1
  "$PYTHON" -I - "$DEPLOY/data_catalog/arc_uploads" <<'PY'
import fcntl, glob, os, sys
for path in glob.glob(os.path.join(sys.argv[1], "*", "worker.lock")):
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        continue
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)
    finally:
        os.close(handle)
sys.exit(1)
PY
}

echo "== 1/6 Windows-API shim (libusb) =="
make -C "$NATIVE/backend/shim" -s
echo "== 2/6 Forms stand-in + capture backend (.NET) =="
( cd "$NATIVE/backend/stubs/System.Windows.Forms" && "$DOTNET" build -c Release -nologo -v q )
( cd "$NATIVE/backend/SampleApl" && "$DOTNET" publish -c Release -o bin/publish -nologo -v q )
echo "== 3/6 Qt controller (flat layout) =="
fresh_if_moved "$ROOT/build-macos-flat"
"$CMAKE" -S "$ROOT/laser_test" -B "$ROOT/build-macos-flat" -G Ninja -DCMAKE_MAKE_PROGRAM="$NINJA" \
   -DCMAKE_PREFIX_PATH="$QT" -DCMAKE_BUILD_TYPE=Release -DRAMAN_FLAT_LAYOUT=ON > /dev/null
"$CMAKE" --build "$ROOT/build-macos-flat" | tail -1
echo "== 4/6 simulated runner (the controller without a window, for the Simulation Studio) =="
fresh_if_moved "$NATIVE/sim_runner/build"
"$CMAKE" -S "$NATIVE/sim_runner" -B "$NATIVE/sim_runner/build" -G Ninja -DCMAKE_MAKE_PROGRAM="$NINJA" \
   -DCMAKE_PREFIX_PATH="$QT" -DCMAKE_BUILD_TYPE=Release > /dev/null
"$CMAKE" --build "$NATIVE/sim_runner/build" | tail -1

echo "== 5/6 assemble $DEPLOY =="
mkdir -p "$DEPLOY/handshake_v2/backend" "$DEPLOY/handshake_v2/motor" "$DEPLOY/scan_sessions" "$DEPLOY/dark_measurements"
cp "$ROOT/build-macos-flat/laser_test" "$DEPLOY/laser_test"
cp "$NATIVE/sim_runner/build/raman_sim_runner" "$DEPLOY/raman_sim_runner"
# backend: published .NET output + shim + launcher with the absolute dotnet path
cp "$NATIVE/backend/SampleApl/bin/publish/"* "$DEPLOY/handshake_v2/backend/"
cp "$NATIVE/backend/shim/libwinusb_shim.dylib" "$DEPLOY/handshake_v2/backend/"
sed "s|@DOTNET@|$DOTNET|g" "$NATIVE/backend/SampleApl/SampleApl" > "$DEPLOY/handshake_v2/backend/SampleApl"
chmod +x "$DEPLOY/handshake_v2/backend/SampleApl"
# motor: python sources + config + launcher with the absolute python path
cp "$NATIVE/motor/"*.py "$NATIVE/motor/motor_config.json" "$DEPLOY/handshake_v2/motor/"
sed "s|@PYTHON@|$PYTHON|g" "$NATIVE/motor/RamanAutofocus" > "$DEPLOY/handshake_v2/motor/RamanAutofocus"
chmod +x "$DEPLOY/handshake_v2/motor/RamanAutofocus"
# analysis script and the model files it loads (optional)
if [ -f "$WINDEPLOY/cnn_1.py" ]; then cp "$WINDEPLOY/cnn_1.py" "$DEPLOY/"; else
  echo "   (note: $WINDEPLOY not found - 'Analyze Latest (CNN)' assets are not deployed; set RAMAN_HANDOFF_DIR to add them)"
fi
# the interpreter with the analysis packages, for launches without RAMAN_PYTHON (Qt Creator, Finder)
printf '%s\n' "$PYTHON" > "$DEPLOY/raman_python.txt"
# first-contact instrument checks (no motion, laser stays off): ./hardware_check.sh all
cp "$NATIVE/hardware_check.py" "$DEPLOY/hardware_check.py"
printf '#!/bin/bash\n# Instrument checks that cannot move a motor or switch the laser on. Usage: ./hardware_check.sh all [--dark]\nexec "%s" "$(cd "$(dirname "$0")" && pwd)/hardware_check.py" "$@"\n' "$PYTHON" > "$DEPLOY/hardware_check.sh"
chmod +x "$DEPLOY/hardware_check.sh"
if [ -d "$WINDEPLOY/ckpts" ]; then rsync -a "$WINDEPLOY/ckpts/" "$DEPLOY/ckpts/"; fi
if [ -f "$WINDEPLOY/best_raman_1dcnn.pth" ]; then cp "$WINDEPLOY/best_raman_1dcnn.pth" "$DEPLOY/"; fi
# Dark spectra: this deploy holds only the darks captured with this edition. The hand-off darks are
# real measurements of the original instrument PC and stay in the original project. session.json names
# the newest dark of the session's kind in dark_measurements/ (a dark without its .json counts as
# real), and the data layer subtracts it from every spectrum of the session, so a months-old hand-off
# dark there could be paired with a real session. A dark here with the name and the content of a
# hand-off dark and without a .json is a copy an earlier build placed here (every dark of this edition
# has its .json, a hand-off dark never) and is removed, also when it is a link to the hand-off dark (rm
# then takes the link, not the file). Never the hand-off file itself: nothing when both dark folders are
# one (RAMAN_HANDOFF_DIR names this deploy folder, or a link leads here), nor a file that a hand-off
# link leads to (-ef: one and the same file).
handoff_copies=0
if ! [ "$WINDEPLOY/dark_measurements" -ef "$DEPLOY/dark_measurements" ]; then
  for dark in "$WINDEPLOY"/dark_measurements/dark_*.txt; do
    copy="$DEPLOY/dark_measurements/$(basename "$dark")"
    described="${copy%.txt}.json"
    if [ -f "$dark" ] && [ -f "$copy" ] && ! [ -e "$described" ] && ! [ -L "$described" ] && cmp -s "$dark" "$copy" &&
       ! { [ -L "$dark" ] && [ "$dark" -ef "$copy" ]; }; then
      rm "$copy"; handoff_copies=$((handoff_copies + 1))
    fi
  done
fi
if [ "$handoff_copies" -gt 0 ]; then echo "   (removed $handoff_copies hand-off darks, copies or links, from dark_measurements/; $WINDEPLOY/dark_measurements keeps them)"; fi
# training database and the CNN model folder the analysis script reads (DATA_ROOT = its own folder)
if [ -d "$WINDEPLOY/Portable_Raman_database" ]; then rsync -a "$WINDEPLOY/Portable_Raman_database/" "$DEPLOY/Portable_Raman_database/"; fi
if [ -d "$WINDEPLOY/_CNN_2class_r6g_mb" ]; then rsync -a "$WINDEPLOY/_CNN_2class_r6g_mb/" "$DEPLOY/_CNN_2class_r6g_mb/"; fi
# data layer (local demo catalog, uploads to ARC with arc_remote.sh, classifier job, the package lists:
# requirements.txt and requirements-mac.txt, which adds the Globus SDK for uploads to ARC) + the
# classifier model + its interpreter
if [ -d "$ROOT/data_layer/raman_catalog" ]; then
  mkdir -p "$DEPLOY/data_layer"
  if arc_upload_running; then
    echo "   (note: an ARC upload is running; its program keeps the code it started with until it ends)"
  fi
  rsync -a --delete --exclude "__pycache__/" "$ROOT/data_layer/raman_catalog/" "$DEPLOY/data_layer/raman_catalog/"
  cp "$ROOT/data_layer/requirements.txt" "$DEPLOY/data_layer/requirements.txt"
  if [ -f "$ROOT/data_layer/requirements-mac.txt" ]; then
    cp "$ROOT/data_layer/requirements-mac.txt" "$DEPLOY/data_layer/requirements-mac.txt"
  fi
fi
if [ -d "$ROOT/models" ]; then rsync -a "$ROOT/models/" "$DEPLOY/models/"; fi
# simulation-only virtual sample and the Simulation Studio page
if [ -d "$ROOT/virtual_sample" ]; then rsync -a --delete "$ROOT/virtual_sample/" "$DEPLOY/virtual_sample/"; fi
if [ -d "$ROOT/studio" ]; then rsync -a --delete "$ROOT/studio/" "$DEPLOY/studio/"; fi
if [ ! -x "$DATAPY" ] && [ -f "$ROOT/data_layer/setup_env.sh" ]; then bash "$ROOT/data_layer/setup_env.sh"; fi
if [ -x "$DATAPY" ]; then printf '%s\n' "$DATAPY" > "$DEPLOY/catalog_python.txt"; else
  echo "   (note: no data-layer interpreter at $DATAPY - run data_layer/setup_env.sh; the Data and Review tabs need it)"
fi
# Only looked for, never imported or downloaded here: the build does not install into an existing .venv.
if [ -x "$DATAPY" ] && ! "$DATAPY" -I -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("globus_sdk") is None)'; then
  echo "   (note: the Globus SDK is not in .venv: live Globus uploads to ARC need it, demo and SSH uploads work without it; run data_layer/setup_env.sh)"
fi
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
cat > "$DEPLOY/studio.sh" <<'RUN'
#!/bin/bash
# Start the Simulation Studio for this folder: a local web page (its address is printed first) that
# shows the scans and the data pipeline of this folder and starts simulated runs (raman_sim_runner).
# Ctrl-C ends it. Options: --port N, --no-open (do not open the browser), --idle-exit SECONDS.
cd "$(dirname "$0")"
PY="${RAMAN_CATALOG_PYTHON:-$(head -n 1 catalog_python.txt 2>/dev/null)}"
command -v "$PY" > /dev/null 2>&1 || { echo "No data-layer interpreter: set RAMAN_CATALOG_PYTHON or name one in catalog_python.txt" >&2; exit 1; }
PYTHONPATH="$PWD/data_layer${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m raman_catalog --home "$PWD" studio "$@"
RUN
chmod +x "$DEPLOY/run_simulated.sh" "$DEPLOY/run_hardware.sh" "$DEPLOY/studio.sh"
echo "deployed: $(ls "$DEPLOY" | tr '\n' ' ')"

if [ "${1:-}" = "--test" ]; then
  echo "== 6/6 tests =="
  # Hardware safety: with the instruments on USB, skip everything that would open them.
  USB_REGISTRY="$(/usr/sbin/ioreg -p IOUSB -l -w0 2>/dev/null || true)"
  DEVICE_SELFTESTS=1
  if grep -qE '"idVendor" = 1633|"idProduct" = 64240' <<<"$USB_REGISTRY"; then
    DEVICE_SELFTESTS=0
    echo "!! Instruments are attached to this Mac: the shim and real-library self-tests are SKIPPED so that"
    echo "   nothing talks to them. Unplug the spectrometer and the motor controllers for the complete suite."
  elif [ "${RAMAN_SKIP_DEVICE_SELFTESTS:-}" = 1 ]; then
    DEVICE_SELFTESTS=0
    echo "!! RAMAN_SKIP_DEVICE_SELFTESTS=1: shim_selftest, SampleApl --selftest and its unit test are NOT run."
  fi
  # Every helper program a suite starts runs its simulator; the device self-tests decide that themselves.
  # RAMAN_ARC_TEST_GUARD=1: the data layer refuses to sign in to Globus, to run ssh, open or caffeinate, and to
  # start any program but its own upload worker, so no test can open a browser, a Terminal or a connection.
  export RAMAN_BACKEND_SIMULATE=1 RAMAN_MOTOR_SIMULATE=1 PYTHONDONTWRITEBYTECODE=1 RAMAN_ARC_TEST_GUARD=1
  NODE="${NODE:-$(command -v node || echo /usr/local/bin/node)}"
  TESTLOGS="$ROOT/.build/test-logs"
  rm -rf "$TESTLOGS"; mkdir -p "$TESTLOGS"
  FAILED=()
  outcome() {   # what a passing log says about itself, in one line
    if grep -qE '^Ran [0-9]+ tests? in' "$1"; then
      printf '%s, %s' "$(grep -E '^Ran [0-9]+ tests? in' "$1" | tail -n 1)" "$(grep -E '^(OK|FAILED)' "$1" | tail -n 1)"
    elif grep -q '^PASS' "$1"; then printf '%s PASS lines' "$(grep -c '^PASS' "$1")"
    else tail -n 1 "$1"; fi
  }
  # One suite, its output in $TESTLOGS/<name>.log. A failing suite is reported and the next one runs.
  suite() {
    local name="$1" started=$SECONDS; shift
    if ( "$@" ) > "$TESTLOGS/$name.log" 2>&1; then
      echo "   PASS  $name ($((SECONDS - started)) s): $(outcome "$TESTLOGS/$name.log")"
    else
      echo "   FAIL  $name ($((SECONDS - started)) s), end of $TESTLOGS/$name.log:"
      tail -n 15 "$TESTLOGS/$name.log" | sed 's/^/         /'
      FAILED+=("$name")
    fi
  }
  backend_test() { cd "$NATIVE/backend/tests" && "$PYTHON" -m unittest -v "$@"; }
  controller_test() { cd "$ROOT/laser_test/tests/build-macos/$1" && "./$2"; }
  suite data_layer      bash -c 'cd "$1/data_layer" && "$2" -m unittest discover -s tests -v' _ "$ROOT" "$DATAPY"
  suite studio_embedded "$PYTHON" "$ROOT/studio/embed_assets.py" --check
  suite studio_engine   "$NODE" "$ROOT/studio/test_engine.mjs"
  suite studio_page     "$NODE" "$ROOT/studio/test_page.mjs"
  suite studio_arc      "$NODE" "$ROOT/studio/test_arc.mjs"
  suite motor           "$PYTHON" -m unittest discover -s "$NATIVE/motor/tests" -v
  if [ "$DEVICE_SELFTESTS" = 1 ]; then
    suite backend_protocol backend_test test_backend_protocol
    suite shim_selftest    bash -c 'cd "$1/backend/shim" && ./shim_selftest' _ "$NATIVE"
    suite backend_selftest bash -c 'cd "$1/handshake_v2/backend" && ./SampleApl --selftest' _ "$DEPLOY"
  else
    suite backend_protocol backend_test test_backend_protocol.BackendProtocolTest test_backend_protocol.StartupInfoTest
  fi
  suite backend_watchdog       backend_test test_backend_watchdog
  suite backend_virtual_sample backend_test test_backend_virtual_sample
  suite virtual_sample_model   backend_test test_virtual_sample_model
  # The check writes its logs and its frame into <deploy>/scan_sessions/_hwcheck_<time>/. It gets a
  # deploy folder of its own here, the helper programs and the virtual sample their simulators read,
  # so that no test run leaves simulated data among the scans of deploy-macos/.
  HWDEPLOY="$TESTLOGS/hardware_check_deploy"
  mkdir -p "$HWDEPLOY"; cp -R "$DEPLOY/handshake_v2" "$HWDEPLOY/handshake_v2"
  if [ -d "$DEPLOY/virtual_sample" ]; then cp -R "$DEPLOY/virtual_sample" "$HWDEPLOY/virtual_sample"; fi
  suite hardware_check_simulated "$PYTHON" "$NATIVE/hardware_check.py" --deploy "$HWDEPLOY" --simulate all --dark
  fresh_if_moved "$ROOT/laser_test/tests/build-macos"
  "$CMAKE" -S "$ROOT/laser_test/tests" -B "$ROOT/laser_test/tests/build-macos" -G Ninja -DCMAKE_MAKE_PROGRAM="$NINJA" -DCMAKE_PREFIX_PATH="$QT" > /dev/null
  "$CMAKE" --build "$ROOT/laser_test/tests/build-macos" | tail -1
  suite handshake_tests controller_test . handshake_tests
  suite settings_tests  controller_test . settings_tests
  suite ui_tests        controller_test ui_home ui_tests
  suite arc_ui_tests    controller_test ui_home arc_ui_tests
  suite sim_runner      bash -c 'cd "$1/sim_runner" && RAMAN_DEPLOY_DIR="$2" "$3" -m unittest -v test_sim_runner' _ "$NATIVE" "$DEPLOY" "$PYTHON"
  fresh_if_moved "$NATIVE/e2e/build"
  "$CMAKE" -S "$NATIVE/e2e" -B "$NATIVE/e2e/build" -G Ninja -DCMAKE_MAKE_PROGRAM="$NINJA" -DCMAKE_PREFIX_PATH="$QT" > /dev/null
  "$CMAKE" --build "$NATIVE/e2e/build" | tail -1
  rm -rf "$NATIVE/e2e/build/handshake_v2"; cp -R "$DEPLOY/handshake_v2" "$NATIVE/e2e/build/handshake_v2"
  suite e2e_simulated   bash -c 'cd "$1/e2e/build" && ./e2e_simulated_test' _ "$NATIVE"
  if [ "${#FAILED[@]}" -gt 0 ]; then
    echo "!! ${#FAILED[@]} suite(s) FAILED: ${FAILED[*]} (logs in $TESTLOGS)"
    exit 1
  fi
  echo "== every suite passed (logs in $TESTLOGS) =="
else
  echo "== done (add --test to run the test suites) =="
fi
