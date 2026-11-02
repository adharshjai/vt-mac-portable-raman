# Transformer R6G threshold control version

Open `Transformer5_1_r6g_threshold.ipynb` and Run All with the original PyTorch environment. By default it reads the already-trained shift_robust_v2 weights; no retraining is needed, and the original weights are not overwritten.

`R6G_THRESHOLD_OVERRIDE = None` means the R6G threshold is selected using only the validation set, with the objective of maximizing validation F1; the other classes remain at 0.40. A manual threshold can be entered for sensitivity experiments, but do not pick the threshold based on the final test labels. The threshold is not a model confidence calibration, nor can it improve the separability of the features themselves.

If `RETRAIN_MODEL=True`, the new weights are saved only to this version's results/models and do not overwrite v2. After retraining, the threshold is re-selected based on the validation scores.

## Actual results of this run

- The validation set selected approximately 0.53986037; validation-set R6G: TP 10, FP 2, FN 1, TN 31. Original threshold 0.40: TP 10, FP 22, FN 1, TN 11.
- Evaluated on the internal test set after threshold selection: R6G TP 24, FP 4, FN 1, TN 67, F1 0.9057; original threshold F1 0.5208.
- Positive and negative scores are compressed around approximately 0.54, and the closest validation score is only about 0.00000149 from the threshold, lacking a robust margin. This specific value cannot be transferred directly to another set of weights.
- In the 13-point measured scan, R6G positives become points 1, 2, 12; MB remains points 4, 5, 9, 10; Adenine still gives false positives at the same original 9 points. This does not mean the measured-data classification problem has been solved.
- `per_point_scores.csv` gives the difference from the R6G threshold and flags points whose absolute difference is less than 0.001. This flag is only a sensitivity hint, not a statistical confidence interval.
- All new R6G come from the same scan run. Internal random-split metrics do not represent independent-batch performance; the current 13-point scan has also already been used for development observation.

## Output locations

- `results/calibration/r6g_threshold.json`: validation threshold, before/after metrics, threshold sensitivity, weights checksum.
- `results/calibration/validation_predictions.npz`: validation scores and labels.
- `results/benchmark/`: test metrics with the new threshold, ROC, figures, and raw model scores.
- `results/scan_model_scores/per_point_scores.csv`: raw measured-data scores, R6G threshold, and near-threshold flag.
- `results/scan_analysis/`: per-point position and class summary.

AUC measures score ranking and does not depend on the chosen decision threshold, so a threshold change should not change the AUC. This version only adjusts the R6G decision; it does not fix the Adenine classification capability.
