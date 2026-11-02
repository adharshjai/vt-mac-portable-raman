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
