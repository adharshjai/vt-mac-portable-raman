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
itself (RAMAN_HANDOFF_DIR at the top of the file), so a fresh clone runs where it is. Set the
RAMAN_HANDOFF_DIR environment variable only to use the data from another folder.

1. Create the environment from the project root (Python 3.13 recommended):
     macOS/Linux:  python3 -m venv .venv
                   .venv/bin/python -m pip install -r Python_dependencies/mac-analysis.requirements.txt
     Windows:      py -3.13 -m venv .venv
                   .venv\Scripts\python.exe -m pip install -r Python_dependencies\mac-analysis.requirements.txt
2. Optional, for notebooks outside VS Code/Antigravity:
     .venv/bin/python -m ipykernel install --user --name portable-raman --display-name "Python (Portable Raman)"
3. Open the project folder in VS Code or Antigravity: .vscode/settings.json selects .venv and runs each
   notebook from its own folder. Install the Python and Jupyter extensions if the editor asks.
4. Run from the hand-off folder, for example:
     pure_only_20260923/shift_robust_v2/CNN3_1_shift_robust.py          (CNN, uses the saved model)
     pure_only_20260923/pure_softmax_v4/Transformer5_1_pure_softmax.py  (Transformer, uses the saved model)
     svm_r6g_update_20260924/SVM4_pure_inference_only.py                (SVM, uses the saved models)
   SVM4_full_PCA_SVM_new_analysis.py RETRAINS on every run and overwrites the saved SVM models in
   _PCA_SVM_r6g20260922_results; the CNN and Transformer scripts retrain only with RETRAIN_MODEL = True.
   Every run rewrites its own result files and plots; git restores the committed versions.

transformer_1.py and Untitled-2.ipynb still point at the author's Desktop data, which is not in this
project. After moving or renaming folders inside laser_test, rerun macos-native/tools/mac_python_paths.py.
