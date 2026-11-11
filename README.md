# Portable Raman spectrometer: controller and analysis

Hand-off snapshot of a portable Raman spectrometer project: the Qt controller that drives the laser,
spectrometer and motor stages, its macOS port, and the Python analysis (1D CNN, Transformer and PCA-SVM)
with the trained models and the reference spectra database.

## Start here

| You want to | Read |
|---|---|
| Run the CNN, Transformer or SVM analysis on saved spectra (any computer) | [Python_dependencies/README.txt](Python_dependencies/README.txt), section "Quick start" |
| Build and run the controller on a Mac, simulated or with the instruments | [MAC_BUILD_NOTES.md](MAC_BUILD_NOTES.md), then [macos-native/README.md](macos-native/README.md) |
| Build and run everything on Windows | [WINDOWS_VM_GUIDE.md](WINDOWS_VM_GUIDE.md) and [INSTALL_AND_RUN_NOTES.txt](INSTALL_AND_RUN_NOTES.txt) |

## Analysis in four commands (macOS, Apple Silicon)

Needs Python 3.11 to 3.14; the macOS built-in `python3` (3.9) is too old.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r Python_dependencies/mac-analysis.requirements.txt
cd laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug
../../../.venv/bin/python pure_only_20260923/shift_robust_v2/CNN3_1_shift_robust.py
```

The scripts find their data relative to this checkout, so no paths need editing. Windows steps, the
Transformer and SVM entry points, and which scripts retrain are in the Quick start.

## Layout

| Folder | Contents |
|---|---|
| `laser_test/` | Qt controller source; `build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/` holds the analysis scripts, notebooks, trained models, spectra database and recorded scans from the original lab PC |
| `RamanAutofocus/` | Original Windows motor program (Thorlabs KDC101) |
| `macos-native/` | Mac replacements for the capture backend and motor program, with simulators and tests |
| `windows-vm/` | Helpers for building on Windows |
| `Python_dependencies/` | Python requirements and setup notes |

## Safety

The controller switches a laser on and moves motor stages. Read the safety notes in
[INSTALL_AND_RUN_NOTES.txt](INSTALL_AND_RUN_NOTES.txt) before connecting instruments, and use the
simulated mode described in [macos-native/README.md](macos-native/README.md) to try the controller without them.
The saved models are pickle, joblib and PyTorch files: load them only from a copy you trust.
