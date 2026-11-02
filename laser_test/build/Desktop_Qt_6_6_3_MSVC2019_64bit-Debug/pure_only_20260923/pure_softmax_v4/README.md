# Transformer 5.1 pure-class v4

Open `Transformer5_1_pure_softmax.ipynb` and Run All, or run the matching `.py` file.
The first run trains if the new checkpoint is missing. Later runs load it with
`RETRAIN_MODEL = False`. Old v2/v3 checkpoints and code are not changed.

## Changes

- Reuses the existing RamanViT1D architecture and preprocessing functions.
- Uses four-class cross-entropy and softmax for the pure-spectrum task.
- Selects weights using minimum validation cross-entropy, not thresholded exact match.
- Keeps all four classes, including adenine and water; no synthetic mixtures.
- Uses the existing fixed train/validation/test split and verifies dataset hashes.
- Applies symmetric wavelength-shift augmentation to training spectra only.
- Reports low-confidence or small-margin predictions as UNKNOWN.

## Inputs and results

Change `TEST_FILE` and `TEST_DARK_FILE` together for a new scan and matching dark.
The data directory remains `../data_r6g_20260922`; the R6G set has 119 retained
spectra from the September 22 scan. Data are not overwritten.

Results are in `results/`: the new weights, training history, best epoch,
test confusion matrix and ROC figure, overall/per-target metrics, per-point scan
scores and spatial summary, and descriptive scan probability figures.

## Limits

This version is a pure-class classifier, NOT a validated physical-mixture detector.
Softmax permits only one selected class; that does not establish chemical purity.
Its scores are not accuracy or calibrated chemical concentrations. The rejection
settings (0.60 confidence, 0.15 margin) are provisional, not tuned on the 13 points.

Database test metrics use a spectrum-level split. Spectra from a common acquisition
can occur in different splits, so these are not independent-session estimates.
The 13-point scan has already been inspected during development and must not be
presented as untouched final validation. Check predictions against recorded sample
positions and validate with a new acquisition before making performance claims.
