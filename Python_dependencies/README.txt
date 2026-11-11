Python dependencies / Python dependency list
=======================================
Do not merge all requirements into a single environment. The versions of the various historical projects may conflict.
The accompanying core-analysis.requirements.txt is a list of the main package names compiled from the imports in the source code, not a verified locked environment.
If you reuse an existing model, refer first to the versions of the corresponding environment on this machine in environment_inventory.json; that inventory is only
a snapshot of installed dist-info metadata; it does not mean the packages can be installed on or are compatible with a new computer, and the programs of the original environment were not executed.

Basic analysis example (in a Python you have chosen and verified to be compatible):
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r core-analysis.requirements.txt
  .\.venv\Scripts\python.exe -m pip check
How PyTorch is installed depends on the recipient's CPU/GPU and Python version; the latest version is not guaranteed to be compatible with all old code.
When using Jupyter/VS Code, select the kernel of the environment you just created; do not mistakenly use another global Python.

Locations of the project's original dependency files (all inside the ZIP):
- laser_test/figure4_benchmark/requirements.txt
- laser_test/Bluesky_Bridge/requirements.txt and requirements-lock.txt
- laser_test/Globus_Bridge/requirements.txt, requirements-lock.txt, requirements-dev.txt
- laser_test/intersect_bridge/requirements.txt and requirements-launcher.txt
- laser_test/intersect bridge + Physical device/intersect_bridge/requirements.txt
These files are kept as they are and contain the projects' own version constraints. In the old README, the statement ".venv already installed" refers only to the sender's computer,
the recipient must rebuild the environment. The Qt C++ controller requires Qt Widgets/Charts, MSVC, Hamamatsu and Kinesis, which are not installed by pip.

Quick start: CNN / Transformer / SVM analysis (any computer)
=============================================================
The analysis scripts and notebooks no longer contain the original author's C:\Users\Jeremy666\...
paths. Each one finds the hand-off folder laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug by
itself (RAMAN_HANDOFF_DIR at the top of the file), so a fresh clone runs wherever it is. To use the
data from another copy, set the RAMAN_HANDOFF_DIR environment variable to that copy's hand-off folder.

Requirements: Python 3.11 to 3.14 (3.13 recommended) on Apple Silicon Mac, Windows (64-bit) or Linux (x86-64).
The macOS built-in python3 is 3.9 and will NOT work: install Python 3.13 from python.org, with
"brew install python@3.13", or use Anaconda. Intel Macs are not supported: PyTorch 2.9.1, which the saved
CNN/Transformer models need, has no Intel-Mac build.
Only Apple Silicon macOS with Python 3.13 has been tested; the Windows and Linux steps are untested.

1. Create the environment in the project folder (the folder that contains laser_test/):
     macOS/Linux:  python3.13 -m venv .venv
                   .venv/bin/python -m pip install -r Python_dependencies/mac-analysis.requirements.txt
     Windows:      py -3.13 -m venv .venv
                   .venv\Scripts\python.exe -m pip install -r Python_dependencies\mac-analysis.requirements.txt
2. Optional, only for Jupyter outside VS Code/Antigravity. This creates a user-wide kernel named
   "portable-raman" and replaces any existing kernel of that name (use another --name for a second copy):
     .venv/bin/python -m ipykernel install --user --name portable-raman --display-name "Python (Portable Raman)"
3. Open the project folder in VS Code or Antigravity and install the Python and Jupyter extensions if asked.
   .vscode/settings.json selects .venv on macOS/Linux and runs each notebook from its own folder.
   On Windows, choose .venv\Scripts\python.exe once with "Python: Select Interpreter".
4. Run a script with the environment's Python, for example on macOS/Linux:
     source .venv/bin/activate
     cd laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug
     python pure_only_20260923/shift_robust_v2/CNN3_1_shift_robust.py          # CNN, saved model
     python pure_only_20260923/pure_softmax_v4/Transformer5_1_pure_softmax.py  # Transformer, saved model
     python svm_r6g_update_20260924/SVM4_pure_inference_only.py                # SVM, saved models
   (Windows: .venv\Scripts\activate, then the same commands.) The .ipynb next to each script does the same.

Which scripts retrain:
- SVM4_full_PCA_SVM_new_analysis.py RETRAINS on every run and overwrites the saved SVM models in
  _PCA_SVM_r6g20260922_results.
- cnn_1.py (the script behind the controller's "Analyze Latest" button) retrains its 2-class CNN on every
  run and overwrites _CNN_2class_r6g_mb/models/cnn_2class.pt. The copy under
  "laser_test/intersect bridge + Physical device" writes to the same model in the main hand-off folder.
- The CNN and Transformer scripts retrain only with RETRAIN_MODEL = True, or when their saved model file is
  missing. pure_only_20260923/Transformer5_1_pure_only.py has no saved model in the package, so it trains.
- Every run rewrites its own result files and plots (and the CNN adds a scan_analysis/ folder). Do not
  commit those run outputs; "git restore" puts back the committed versions.

Not portable (they point at the author's own folders, which are not in this package): the backup copies
in backup_* folders and *.before_* files, transformer_1.py, Untitled-2.ipynb and
laser_test/build_svm_full_pure_report.py. macos-native/tools/mac_python_paths.py converts any remaining
C:\Users\Jeremy666\...\Desktop_Qt_6_6_3_MSVC2019_64bit-Debug paths; it does not follow folders renamed
inside the hand-off folder, so edit those paths by hand.
