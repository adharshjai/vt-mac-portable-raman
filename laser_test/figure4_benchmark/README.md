# Figure 4 unified benchmark

This directory contains the reproducible evaluation pipeline for Figure 4.

The runner:

1. splits the 456 measured pure spectra before mixture generation;
2. generates train, validation, and test mixtures only from source spectra in
   the corresponding partition;
3. preserves the manuscript totals of 456 pure + 1900 synthetic spectra and
   split sizes of 1649/235/472;
4. trains PCA-SVM, SE-ResNet1D, and ViT-1D models;
5. evaluates all models on the same 472 test samples at a fixed 0.50 threshold;
6. exports macro-F1, macro ROC-AUC, R6G recall, multi-target mixture exact
   match, and per-target precision/recall/F1/ROC-AUC.

Deep models use a batch size of 128 for practical CPU execution. Checkpoint
selection is based only on validation target macro-F1.

Run from the workspace root:

```powershell
.\.venv_figure4\Scripts\python.exe .\figure4_benchmark\run_benchmark.py
```

To reuse the generated dataset or trained checkpoints:

```powershell
.\.venv_figure4\Scripts\python.exe .\figure4_benchmark\run_benchmark.py --reuse-dataset --reuse-models
```

The benchmark threshold is deliberately separate from the real-scan reporting
rule. Figure 4 uses 0.50 for each target channel. The 0.40 reporting threshold
and conditional 0.25 weak-R6G rule apply only to averaged real-scan results.
