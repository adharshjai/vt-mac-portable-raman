import matplotlib
import sys
if "ipykernel" not in sys.modules:
    matplotlib.use("Agg")

PURE_REPORT_MIN_CONFIDENCE = 0.80
# This inference-only file uses the pure branch. Use the full version for mixture mode.
import os
import sys
import re
import json
import csv
import time
import random
from pathlib import Path
from itertools import combinations

import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

try:
    from sklearn.decomposition import PCA
    from sklearn.svm import SVC
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import classification_report, confusion_matrix, accuracy_score, roc_auc_score
    from sklearn.model_selection import GridSearchCV
    import joblib
except ModuleNotFoundError as e:
    raise ModuleNotFoundError(
        "Missing dependency. Please install: pip install scikit-learn joblib matplotlib numpy"
    ) from e


# =========================================================
# CONFIG
# =========================================================
DATA_ROOT = r"/Users/adhar/Downloads/research/Portable_Raman_full_project/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug"
DATA_DIR = os.path.join(DATA_ROOT, "data_base_code_part")

# ---------------------------------------------------------------------
# Pure multi-class SVM uses MIN-MAX data
# These files are the newly generated *_minmax.npy from your screenshot
# ---------------------------------------------------------------------
TRAINING_MINMAX_NPY_FILES = {
    "r6g": os.path.join(DATA_ROOT, "pure_only_20260923", "data_r6g_20260922", "training_spectra_r6g_minmax.npy"),
    "methylene_blue": os.path.join(DATA_DIR, "training_spectra_methylene_blue_minmax.npy"),
    "adenine": os.path.join(DATA_DIR, "training_spectra_adenine_minmax.npy"),
    "di_water": os.path.join(DATA_DIR, "training_spectra_di_water_minmax.npy"),
}

# ---------------------------------------------------------------------
# Mixture OvR SVM uses BASELINE_RAW data
# These files are the newly generated *_baseline_raw.npy from your screenshot
# ---------------------------------------------------------------------
TRAINING_RAW_NPY_FILES = {
    "r6g": os.path.join(DATA_ROOT, "pure_only_20260923", "data_r6g_20260922", "training_spectra_r6g_baseline_raw.npy"),
    "methylene_blue": os.path.join(DATA_DIR, "training_spectra_methylene_blue_baseline_raw.npy"),
    "adenine": os.path.join(DATA_DIR, "training_spectra_adenine_baseline_raw.npy"),
    "di_water": os.path.join(DATA_DIR, "training_spectra_di_water_baseline_raw.npy"),
}

# TEST_FILE = r"/Users/adhar/Downloads/research/Portable_Raman_full_project/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/portable_raman_test_data/laser_log_20260513_150549.txt"
# TEST_DARK_FILE = r"/Users/adhar/Downloads/research/Portable_Raman_full_project/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/portable_raman_test_data/dark_20260513_150739.txt"
TEST_FILE = r"/Users/adhar/Downloads/research/Portable_Raman_full_project/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/scan_sessions/1d_y_scan_20260921_152840_289/laser_log_combined.txt"

TEST_DARK_FILE = r"/Users/adhar/Downloads/research/Portable_Raman_full_project/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/dark_measurements/dark_20260921_153016_700.txt"

DROP_N = 90
TARGET_LEN = 2048
DO_BASELINE = True
BASELINE_WINDOW = 15
BASELINE_SMOOTH = 15

SEED = 42
TRAIN_RATIO = 0.70
VAL_RATIO = 0.10
TEST_RATIO = 0.20
PCA_PLOT_COMPONENTS = 3

# Each of the two branches has its own PCA
PURE_N_COMPONENTS = 20
MIX_N_COMPONENTS = 20

# ---- Inference thresholds ----
CONFIDENCE_THRESHOLD = 0.80
MIXTURE_THRESHOLD = 0.15

# ---- OvR binary classifier thresholds ----
OVR_THRESHOLD = 0.50
OVR_FINAL_THRESHOLD = 0.40

# If MB clearly dominates but R6G is weakly positive, this threshold can be used to catch weak R6G
R6G_WEAK_THRESHOLD_WHEN_MB_DOMINANT = 0.25

# ---- Synthetic mixture generation from baseline_raw ----
N_SYNTHETIC_PER_COMBO = 200
N_SYNTHETIC_TERNARY = 100
MIXTURE_RATIO_MIN = 0.10
MIXTURE_RATIO_MAX = 0.90

# Noise uses a ratio of the mixture spectrum's own standard deviation, no longer a fixed 0.02
SYNTH_NOISE_STD_RATIO = 0.01

# For r6g + methylene_blue, additionally add synthetic samples with a low r6g ratio
ENABLE_EXTRA_LOW_R6G_MB = True
N_EXTRA_LOW_R6G_MB = 300
LOW_R6G_RATIOS = [0.01, 0.02, 0.05, 0.10, 0.20]

# ---- Quality / recon filters ----
ENABLE_QUALITY_FILTER = True
RAW_MIN_PTP = 100.0
RAW_MIN_STD = 15.0

ENABLE_RECON_FILTER = True
RECON_PERCENTILE = 99.0
RECON_TOLERANCE = 1.5

MAHA_PERCENTILE = 99.0
MAHA_TOLERANCE = 1.5

OUTPUT_DIR = os.path.join(DATA_ROOT, "_PCA_SVM_r6g20260922_results")
os.makedirs(OUTPUT_DIR, exist_ok=True)

PURE_SCALER_PATH = os.path.join(OUTPUT_DIR, "pure_scaler_minmax.joblib")
PURE_PCA_PATH = os.path.join(OUTPUT_DIR, "pure_pca_minmax.joblib")
PURE_SVM_PATH = os.path.join(OUTPUT_DIR, "pure_svm_minmax.joblib")

MIX_SCALER_PATH = os.path.join(OUTPUT_DIR, "mix_scaler_raw.joblib")
MIX_PCA_PATH = os.path.join(OUTPUT_DIR, "mix_pca_raw.joblib")
OVR_SCALER_PATH = os.path.join(OUTPUT_DIR, "ovr_scaler_raw.joblib")
OVR_SVMS_PATH = os.path.join(OUTPUT_DIR, "ovr_svms_raw.joblib")

META_PATH = os.path.join(OUTPUT_DIR, "meta.json")


# =========================================================
# DIAGNOSTIC PEAK REGIONS
# =========================================================
DIAGNOSTIC_PEAKS = {
    "r6g": [
        (612, 30),
        (774, 30),
        (1360, 30),
        (1510, 30),
    ],
    "methylene_blue": [
        (450, 30),
        (770, 30),
        (1395, 30),
        (1625, 30),
    ],
    "adenine": [
        (730, 30),
        (1330, 30),
        (1460, 30),
    ],
    "di_water": [],
}


def extract_peak_features(spectrum, cm1_min=200, cm1_max=3200):
    """
    Extract diagnostic peak features from baseline_raw or synthetic raw spectra.
    Note: the input is not required to be min-max here.
    """
    spec = np.asarray(spectrum, dtype=np.float64)
    n = spec.size
    features = []

    for cls_name in ["r6g", "methylene_blue", "adenine"]:
        peaks = DIAGNOSTIC_PEAKS[cls_name]

        for center_cm1, half_w in peaks:
            center_px = int((center_cm1 - cm1_min) / (cm1_max - cm1_min) * n)
            half_px = max(3, int(half_w / (cm1_max - cm1_min) * n))

            lo = max(0, center_px - half_px)
            hi = min(n, center_px + half_px + 1)
            region = spec[lo:hi]

            if region.size == 0:
                features.extend([0.0, 0.0, 0.0])
                continue

            peak_height = float(np.max(region))
            peak_area = float(np.sum(region))

            bg_lo_start = max(0, lo - half_px * 2)
            bg_hi_end = min(n, hi + half_px * 2)
            bg_left = spec[bg_lo_start:lo]
            bg_right = spec[hi:bg_hi_end]

            if bg_left.size + bg_right.size > 0:
                bg_vals = np.concatenate([bg_left, bg_right])
            else:
                bg_vals = np.array([0.0])

            bg_mean = float(np.mean(bg_vals))
            peak_mean = float(np.mean(region))
            peak_contrast = peak_mean - bg_mean

            features.extend([peak_height, peak_area, peak_contrast])

    features.append(float(np.max(spec)))
    features.append(float(np.std(spec)))
    features.append(float(np.sum(spec > 0.3)))
    features.append(float(np.percentile(spec, 95) - np.percentile(spec, 5)))

    return np.array(features, dtype=np.float32)


# =========================================================
# UTILS
# =========================================================
def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)


set_seed(SEED)


def _is_jupyter():
    try:
        from IPython import get_ipython
        shell = get_ipython().__class__.__name__
        if shell in ("ZMQInteractiveShell", "TerminalInteractiveShell"):
            return True
    except Exception:
        pass
    return len(sys.argv) > 0 and "jupyter" in sys.argv[0].lower()


def _parse_cli_args():
    if _is_jupyter():
        return TEST_FILE, TEST_DARK_FILE

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    tf = args[0].strip() if len(args) >= 1 and args[0].strip() else TEST_FILE
    td = args[1].strip() if len(args) >= 2 and args[1].strip() else TEST_DARK_FILE

    return tf, td


def stratified_split_indices_3way(Y, train_ratio=TRAIN_RATIO, val_ratio=VAL_RATIO,
                                  test_ratio=TEST_RATIO, seed=SEED):
    assert abs(train_ratio + val_ratio + test_ratio - 1.0) < 1e-6

    rng = random.Random(seed)
    idx_tr, idx_va, idx_te = [], [], []

    for cls in np.unique(Y):
        idx = np.where(Y == cls)[0].tolist()
        rng.shuffle(idx)

        n = len(idx)
        n_tr = max(1, int(round(n * train_ratio)))
        n_va = max(1, int(round(n * val_ratio)))

        if n_tr + n_va >= n:
            n_va = max(1, n - n_tr - 1)

        if n_tr + n_va >= n:
            n_tr = max(1, n - 2)
            n_va = 1

        idx_tr.extend(idx[:n_tr])
        idx_va.extend(idx[n_tr:n_tr + n_va])
        idx_te.extend(idx[n_tr + n_va:])

    rng.shuffle(idx_tr)
    rng.shuffle(idx_va)
    rng.shuffle(idx_te)

    return idx_tr, idx_va, idx_te


ERROR_PHRASE = "image data error code 0x0001"


def strip_prefix(line):
    line = line.strip()
    line = re.sub(r"^\[[^\]]+\]\s+\[Backend\]\s*", "", line)
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    return line.strip()


def load_dark_spectrum_from_file(dark_path):
    if not dark_path or not Path(dark_path).exists():
        return np.array([], dtype=np.float64)

    intensities = []

    with open(dark_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#") or line.startswith("index"):
                continue

            parts = line.split("\t")

            if len(parts) >= 3:
                try:
                    intensities.append(float(parts[2]))
                except ValueError:
                    continue

    return np.array(intensities, dtype=np.float64)

def parse_portable_log_to_spectra(log_file):
    with open(
        log_file,
        "r",
        encoding="utf-8-sig",
        errors="ignore"
    ) as f:
        lines = f.read().splitlines()

    spectra = []
    failed = 0
    i = 0

    error_phrase = str(ERROR_PHRASE).lower()

    # Match lines containing only hex values.
    hex_line_pattern = re.compile(
        r"[0-9A-Fa-f]{2,4}(?:\s+[0-9A-Fa-f]{2,4})*"
    )

    while i < len(lines):
        line_raw = lines[i]
        line = strip_prefix(line_raw).strip()

        # Skip blank lines and comments.
        if not line or line.startswith("#"):
            i += 1
            continue

        if error_phrase and error_phrase in line_raw.lower():
            failed += 1
            i += 1
            continue

        # Find the next spectrum.
        if not line.startswith("Image Data"):
            i += 1
            continue

        payload = line[len("Image Data"):].strip()
        tokens = []
        block_err = False

        if payload:
            if hex_line_pattern.fullmatch(payload):
                tokens.extend(payload.split())
            else:
                block_err = True

        j = i + 1

        # Read any continuation lines.
        while j < len(lines):
            nxt_raw = lines[j]
            nxt = strip_prefix(nxt_raw).strip()

            # Keep the next spectrum for the outer loop.
            if nxt.startswith("Image Data"):
                break

            if nxt.startswith("Stop Capture"):
                break

            if not nxt or nxt.startswith("#"):
                j += 1
                continue

            if error_phrase and error_phrase in nxt_raw.lower():
                block_err = True

            # Ignore non-data lines.
            if hex_line_pattern.fullmatch(nxt):
                tokens.extend(nxt.split())

            j += 1

        if block_err or not tokens:
            failed += 1
        else:
            vals = np.array(
                [int(token, 16) for token in tokens],
                dtype=np.int64
            )

            # Keep legacy byte-pair conversion.
            if vals.size % 2 == 0 and vals.max(initial=0) <= 255:
                le = vals[0::2] + (vals[1::2] << 8)
                be = (vals[0::2] << 8) + vals[1::2]

                if le.max(initial=0) >= be.max(initial=0):
                    v16 = le
                else:
                    v16 = be

                if v16.max(initial=0) > 255:
                    vals = v16.astype(np.int64)

            spectra.append(vals.astype(np.float64))

        # Advance without skipping the next spectrum.
        i = j

    return spectra, failed


def moving_average(y, window):
    window = int(max(3, window))
    if window % 2 == 0:
        window += 1

    pad = window // 2
    ypad = np.pad(y, (pad, pad), mode="edge")
    kernel = np.ones(window, dtype=np.float64) / window

    return np.convolve(ypad, kernel, mode="valid")


def rolling_minimum(y, window):
    window = int(max(3, window))
    if window % 2 == 0:
        window += 1

    pad = window // 2
    ypad = np.pad(y, (pad, pad), mode="edge")
    out = np.empty_like(y, dtype=np.float64)

    for i in range(y.size):
        out[i] = np.min(ypad[i:i + window])

    return out


def baseline_correction_fast(y, window=15, smooth=15):
    y = np.asarray(y, dtype=np.float64)

    if y.size < 5:
        return y.copy()

    baseline = rolling_minimum(y, window=window)
    baseline = moving_average(baseline, window=smooth)

    return y - baseline


def normalize_min_max(y):
    y = np.asarray(y, dtype=np.float64)
    ymin, ymax = np.min(y), np.max(y)
    denom = ymax - ymin

    if denom < 1e-12:
        return np.zeros_like(y)

    return (y - ymin) / denom


def preprocess_two_versions(raw_spectrum, dark_raw):
    """
    Return two versions of the same test spectrum:
    baseline_raw: raw-dark -> drop -> baseline correction, no min-max
    minmax:       baseline_raw, then min-max
    """
    s = np.asarray(raw_spectrum, np.float64)

    if s.size < TARGET_LEN:
        return None, None, None

    s2048 = s[:TARGET_LEN]
    d2048 = dark_raw[:TARGET_LEN] if dark_raw.size >= TARGET_LEN else dark_raw

    if d2048.size >= TARGET_LEN:
        raw_dark_full = s2048 - d2048
    else:
        raw_dark_full = s2048.copy()

    if DROP_N >= raw_dark_full.size:
        return None, None, None

    raw_dark = raw_dark_full[DROP_N:]

    quality = {
        "ptp": float(np.ptp(raw_dark)),
        "std": float(np.std(raw_dark)),
    }

    if DO_BASELINE:
        baseline_raw = baseline_correction_fast(
            raw_dark,
            window=BASELINE_WINDOW,
            smooth=BASELINE_SMOOTH,
        )
    else:
        baseline_raw = raw_dark.copy()

    minmax = normalize_min_max(baseline_raw)

    return baseline_raw.astype(np.float32), minmax.astype(np.float32), quality


# =========================================================
class_names = list(TRAINING_MINMAX_NPY_FILES.keys())
class_to_id = {c: i for i, c in enumerate(class_names)}
id_to_class = {i: c for c, i in class_to_id.items()}
NUM_CLASSES = len(class_names)

# Final report only uses target analytes.
# di_water is kept in the training data as blank/background control,
# but it is NOT trained or reported as a mixture component.
TARGET_ANALYTES = ["r6g", "methylene_blue", "adenine"]
BACKGROUND_CLASSES = ["di_water"]
TARGET_CIDS = [class_to_id[c] for c in TARGET_ANALYTES]
BACKGROUND_CIDS = [class_to_id[c] for c in BACKGROUND_CLASSES if c in class_to_id]


def run_pure_scan_report():
    spec_path, dark_path = _parse_cli_args()
    print('\n' + '=' * 70)
    print('Step 6: Inference on real test file (PURE mode)')
    print('Mixture branch is NOT used for this scan report.')
    print(f'Test file: {spec_path}\nDark file: {dark_path}')
    scaler = joblib.load(PURE_SCALER_PATH)
    pca = joblib.load(PURE_PCA_PATH)
    model = joblib.load(PURE_SVM_PATH)
    print('Loaded:', PURE_SVM_PATH)
    raw, failed = parse_portable_log_to_spectra(spec_path)
    dark = load_dark_spectrum_from_file(dark_path)
    if dark.size != TARGET_LEN or not np.isfinite(dark).all():
        raise ValueError('A complete finite matching dark spectrum is required.')
    rows = []
    all_prob, accepted_indices = [], []
    for index, spectrum in enumerate(raw):
        single = re.fullmatch(r'point_(\d+)', Path(spec_path).stem)
        point_id = int(single.group(1)) if single else index + 1
        row = dict(index=index, point_id=point_id, y_mm=None, label='REJECTED', reason='', scores=None)
        sidecar = Path(spec_path).parent / f'point_{point_id:04d}.json'
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding='utf-8-sig'))
                if np.array_equal(spectrum, meta.get('intensity', [])) and meta.get('scan_mode') == '1d-y':
                    row['y_mm'] = meta['controller_y_steps'] / 34700.0
            except (ValueError, KeyError, OSError, TypeError):
                pass
        if len(spectrum) != TARGET_LEN or not np.isfinite(spectrum).all() or np.any(spectrum >= 65535):
            row['reason'] = 'Invalid, incomplete or saturated spectrum'
        else:
            baseline, mm, quality = preprocess_two_versions(spectrum, dark)
            if mm is None or np.allclose(baseline, 0):
                row['reason'] = 'Preprocessing failed'
            elif ENABLE_QUALITY_FILTER and (quality['ptp'] < RAW_MIN_PTP or quality['std'] < RAW_MIN_STD):
                row['reason'] = 'Low raw signal'
            else:
                native = model.predict_proba(pca.transform(scaler.transform(mm.reshape(1, -1))))[0]
                prob = np.zeros(NUM_CLASSES)
                for col, cid in enumerate(model.classes_):
                    prob[int(cid)] = native[col]
                pred = int(np.argmax(prob)); confidence = float(prob[pred])
                row['scores'] = {id_to_class[c]: float(prob[c]) for c in range(NUM_CLASSES)}
                if pred in BACKGROUND_CIDS:
                    row['label'], row['reason'] = 'UNKNOWN', 'Background-class response; material not identified'
                elif confidence < PURE_REPORT_MIN_CONFIDENCE:
                    row['label'], row['reason'] = 'UNKNOWN', 'Low pure-class confidence'
                else:
                    row['label'] = id_to_class[pred]
                accepted_indices.append(index); all_prob.append(prob)
                scores_text = '  '.join(f'{("background_score" if c in BACKGROUND_CIDS else id_to_class[c])}={prob[c]:.3f}' for c in range(NUM_CLASSES))
                print(f'[SPEC {index:3d}] [{scores_text}] -> {row["label"]}')
        rows.append(row)
    report_dir = Path(OUTPUT_DIR) / 'pure_scan_report'
    report_dir.mkdir(exist_ok=True)
    print('\n' + '=' * 70 + '\nSCAN COMPONENT SUMMARY (pure branch)')
    print(f'Parsed: {len(raw)}; parser failures: {failed}; accepted: {len(all_prob)}')
    if all_prob:
        means = np.mean(all_prob, axis=0)
        for cid in TARGET_CIDS:
            points = [r['point_id'] for r in rows if r['label'] == id_to_class[cid]]
            print(f'{id_to_class[cid]:20s} mean_score={means[cid]:.4f}; assigned point IDs={points}')
        print('Mean scores are descriptive, not accuracy or scan-wide presence thresholds.')
        display = ['background_score' if c in BACKGROUND_CIDS else id_to_class[c] for c in range(NUM_CLASSES)]
        plt.figure(figsize=(12, 5))
        for cid in range(NUM_CLASSES):
            plt.plot(accepted_indices, np.asarray(all_prob)[:, cid], marker='o', label=display[cid])
        plt.xlabel('Spectrum index (0-based)'); plt.ylabel('Pure-branch probability')
        plt.title('SVM pure-class scan scores'); plt.ylim(0, 1.05); plt.legend(); plt.tight_layout()
        path = report_dir / 'svm_real_file_probabilities.png'
        plt.savefig(path, dpi=180); print('Saved plot:', path)
        if _is_jupyter(): plt.show()
        plt.close()
        plt.figure(figsize=(8, 5)); bars = plt.bar(display, means)
        for bar, value in zip(bars, means):
            plt.text(bar.get_x()+bar.get_width()/2, value+.02, f'{value:.3f}', ha='center')
        plt.ylabel('Mean pure-branch score (not accuracy)'); plt.ylim(0, 1.05); plt.tight_layout()
        path = report_dir / 'svm_real_file_avg_probabilities.png'
        plt.savefig(path, dpi=180); print('Saved plot:', path)
        if _is_jupyter(): plt.show()
        plt.close()
    lines = ['=' * 70, 'SCAN DISTRIBUTION SUMMARY', 'Report mode: PURE',
             f'Top-1 confidence threshold: {PURE_REPORT_MIN_CONFIDENCE:.2f}; background -> UNKNOWN.',
             'Position notes: spectrum index starts at 0; point ID starts at 1.']
    for label in dict.fromkeys(r['label'] for r in rows):
        group = [r for r in rows if r['label'] == label]
        lines.append(f"{label}: indices={[r['index'] for r in group]}; point IDs={[r['point_id'] for r in group]}")
    lines.append('Point details:')
    for r in rows:
        pos = f"Y={r['y_mm']:.3f} mm" if r['y_mm'] is not None else 'Position not verified'
        lines.append(f"Index {r['index']:02d} / point {r['point_id']:02d}: {pos}; {r['label']}; {r['reason']}")
    lines += ['UNKNOWN does not identify foil or water, or prove absence of analyte.',
              'Pure-class assignments do not prove chemical purity or validate mixtures.',
              'background_score is the trained di_water channel, not a calibrated unknown probability.',
              'Mixture benchmark metrics from training do not evaluate this pure reporting rule.', '=' * 70]
    text = '\n'.join(lines)
    print(text)
    (report_dir / 'spatial_summary.txt').write_text(text, encoding='utf-8')
    (report_dir / 'scan_point_predictions.json').write_text(json.dumps(dict(mode='pure', source=str(spec_path), dark=str(dark_path),
        confidence_threshold=PURE_REPORT_MIN_CONFIDENCE, points=rows), indent=2), encoding='utf-8')
    flat = [dict(index=r['index'], point_id=r['point_id'], y_mm=r['y_mm'], label=r['label'], reason=r['reason'],
                 **{name: (r['scores'] or {}).get(name, '') for name in class_names}) for r in rows]
    if flat:
        with open(report_dir / 'scan_point_predictions.csv', 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=list(flat[0])); writer.writeheader(); writer.writerows(flat)
    print('Saved position notes:', report_dir / 'spatial_summary.txt')
    return rows


spatial_results = run_pure_scan_report()
