# CNN / Transformer: small peak-position shift augmentation version

## Running

Open `CNN3_1_shift_robust.ipynb` or `Transformer5_1_shift_robust.ipynb`, select the original PyTorch environment, and Run All. On the first run there are no weights for this version, so it performs full training; it is not merely a change to the output text. Subsequent runs load this version's weights by default; to retrain, change `RETRAIN_MODEL` to `True`. The old-version code and models are unchanged.

Still uses the 462 real pure spectra from `data_r6g_20260922`: R6G 119, MB 118, Adenine 110, DI water 115. Both models share the 322/44/96 spectrum-level split.

## Changes

- Only the training set applies a symmetric random shift of ±8 cm⁻¹ to all classes, interpolated by wavenumber, with edge extension and no circular wrapping. No synthetic mixtures, and no measured points labeled as MB were added to training.
- Validation/test/measured peak positions are not changed; the ViT padding region is kept at zero.
- The original architecture and four-output sigmoid are retained; output is not forced to a single component. Multiple responses still require manual verification; pure-spectrum training does not demonstrate mixture identification capability.
- Both the benchmark and the measured data use a fixed 0.40 threshold, with the weak-R6G threshold-lowering rule disabled. This is not a calibrated optimal threshold; do not compare it directly with the old-version 0.50 benchmark.
- Measured data must have a complete dark; frames with raw saturation, non-finite values, or mismatched length are excluded.
- The component summary for the whole scan is changed to the union of per-point above-threshold results; the mean score is kept for description only and cannot be interpreted as accuracy, concentration, or evidence of physical mixing.

## Outputs

The two models are saved to `CNN3_1_pure_only_results` and `Transformer5_1_pure_only_results` under this directory, respectively, not to the old-version results folders.

- `benchmark/per_target_metrics.csv`: TP, FP, FN, TN, Precision, Recall, F1, ROC-AUC.
- `benchmark/overall_metrics.csv`: Target Macro-F1, R6G Recall, target-label exact-match; with no mixture samples, mixture exact-match is left blank.
- `benchmark/test_predictions.npz`: model scores, ground truth, predictions, test indices, and class names.
- `benchmark/per_target_confusion.png`, `per_target_metrics.png`, `roc_curves.png`.
- `scan_model_scores/per_point_scores.csv`: per-point model scores on the measured data and rejection reasons.
- The original measured per-point curves, mean-score plots, and `scan_analysis` position summary are retained.

All metrics and scores come from the respective neural networks; they are not replaced with SVM predictions or spectral template-matching results. The PCA variance plot, optimal SVM C/gamma, etc. are SVM-specific content and are not copied into this version.

## Validation limitations

The training-time shift range was set based on problems exposed by the development scan, so that 13-point scan is no longer a blind test set. All new R6G come from the same scan run, and the current scores do not represent cross-batch validation either. Confirm the improvement with a newly acquired independent scan that has clear per-point labels, in particular checking the Adenine false positives on MB and whether R6G has regressed.

The code has passed a single-batch forward/backward and export-pipeline test; full training has not been run yet, and improved identification performance is not guaranteed.
