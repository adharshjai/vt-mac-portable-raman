# Pure-spectrum control version: 2026-09-23

## Currently enabled version: replaced with the September 22 pure R6G

Both notebooks and the scripts of the same name now use the full `data_r6g_20260922/` dataset. The old `data/` is left untouched.

- R6G: the user-confirmed 124-point scan of pure R6G, processed with the original pipeline after subtracting the corresponding dark. Saturated points 3, 4, 56, 57, 110 were removed, leaving 119 spectra; Z-scan points 1, 2 are included.
- MB 118, Adenine 110, DI water 115 unchanged, 462 spectra in total. New shared split: training 322, validation 44, test 96.
- New outputs are located in `CNN3_1_pure_only_r6g20260922_results/` and `Transformer5_1_pure_only_r6g20260922_results/`, so the first run will retrain and will not load weights trained on the old data.
- Detailed sources and per-point exclusion reasons are in `manifest.json` and `r6g_quality_report.json` in the new data directory.
- All new R6G come from the same scan run; 119 spectra passing basic quality screening does not mean all 119 have been confirmed to have clear diagnostic peaks. A random split cannot demonstrate independent-batch generalization.
- The "Data and outputs" section below describes the retained initial version; the current run follows this section.

## Usage

1. Open `CNN3_1_pure_only.ipynb` in VS Code, select the Python environment that could previously run PyTorch, and Run All.
2. When finished, open `Transformer5_1_pure_only.ipynb` and Run All with the same environment. `.py` files of the same name are also provided.
3. The first time, when there are no new weights, it trains automatically; afterwards `RETRAIN_MODEL=False` loads this version's new weights. Change it to `True` when retraining is needed.
4. To test other data, change only `TEST_FILE` and `TEST_DARK_FILE`, and set `RETRAIN_MODEL=False`.

## Data and outputs

- `data/`: copies of the real pure spectra from the original `data_base_code_part`, R6G 113, MB 118, Adenine 110, DI water 115, 456 spectra in total; no new spectra were additionally acquired or generated.
- `data/manifest.json`: original paths, file checksums, class order, and the fixed split shared by both models: training 318, validation 44, test 94.
- `CNN3_1_pure_only_results/`, `Transformer5_1_pure_only_results/`: each stores the new model, metrics, figures, and per-point analysis, without overwriting the old-version results.
- The default measured-scan input is still `1d_y_scan_20260921_152840_289/laser_log_combined.txt`, and the dark is `dark_20260921_153016_700.txt`, for easy comparison with the old version.

## Changes and limitations in this version

- Synthetic binary spectra, ternary spectra, and low-ratio R6G+MB synthetic spectra are all disabled; online random augmentation is also disabled.
- The original CNN/ViT architecture, four sigmoid outputs, loss function, preprocessing, and main thresholds are retained as a control for the change in training data; each point is not forced to output R6G or MB.
- The lenient R6G 0.25 decision rule is disabled; the normal threshold is used. The thresholds have not been recalibrated yet.
- Fixed the CPU best-weights snapshot to use a true tensor copy.
- Both models use the same stratified spectrum-level split. The original data lacks acquisition-batch/independent-sample grouping, so independent-batch validation cannot be claimed.
- This version has only pure-spectrum test samples, so mixture exact-match has no valid denominator and the related metrics should be NaN; it cannot be used to demonstrate mixture identification capability.
- Do not substitute this version's pure-spectrum metrics directly into the old mixture Figure 4; the two test tasks are different.
- The code has been checked for data loading, split consistency, and a single-batch forward/backward pass; full training and measured-data performance need to be confirmed after running.

Do not add new measured data directly to the training set when there are no per-point ground-truth labels, and do not tune thresholds on the final test set.
