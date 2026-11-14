"""Inference with the shipped PCA-SVM classifier, and the rule that turns its scores into an outcome.

Nothing here trains a model. `load_dark`, `preprocess` and the prediction of the "pure" branch
are a port of the original project's prediction code (laser_test/intersect_bridge/svm_predictor.py):
the same operations in the same order, so that the same spectrum and dark spectrum give the
same scores. What is added is the outcome policy of PROTOCOL.md section 5.3.

numpy, scikit-learn and joblib are imported inside the functions that need them: the command
line imports this module for every command, and `status` or `sync` must work without them.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path

from .config import CommandError

# The order is part of the model hash (PROTOCOL.md 5.2).
MODEL_FILES = ("pure_scaler_minmax.joblib", "pure_pca_minmax.joblib", "pure_svm_minmax.joblib", "meta.json")

PIXELS = 2048
# The model was trained without the first pixels of the detector, which hold the laser line.
DROPPED_PIXELS = 90
FEATURES = PIXELS - DROPPED_PIXELS
# Width, in pixels, of both the rolling minimum and the moving average of the baseline estimate.
BASELINE_WINDOW = 15
SATURATION_COUNT = 65535
# Below these the dark-corrected spectrum is noise: the laser was off or the sample out of focus.
MIN_PEAK_TO_PEAK = 100
MIN_STANDARD_DEVIATION = 15

# The class the model was given for "nothing but the blank plate".
BACKGROUND_CLASS = "di_water"
LABEL_UNKNOWN = "UNKNOWN"
LABEL_REJECTED = "REJECTED"
NO_DARK = "no dark spectrum"
UNUSABLE_DARK = "A complete finite 2048-pixel dark spectrum is required"
UNUSABLE_SPECTRUM = "Invalid, incomplete or saturated spectrum"


class ModelError(CommandError):
    """The model folder cannot be used: files missing, or saved by another scikit-learn version."""


def model_fingerprint(model_dir):
    """(model hash, {file name: {"sha256", "bytes"}}) of a model folder.

    The model hash is the SHA-256 over the four files in the fixed order of MODEL_FILES.
    """
    root = Path(model_dir)
    overall = hashlib.sha256()
    files = {}
    for name in MODEL_FILES:
        try:
            content = (root / name).read_bytes()
        except OSError as error:
            raise ModelError(
                "The model folder %s has no usable %s" % (root.name, name),
                detail="%s: %s. The folder is set by classifier.model_dir." % (root / name, error.strerror or error),
            ) from error
        overall.update(content)
        files[name] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    return overall.hexdigest(), files


def model_hash(model_dir):
    return model_fingerprint(model_dir)[0]


def read_meta(model_dir):
    """The content of the model's meta.json."""
    path = Path(model_dir) / "meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ModelError("The model's meta.json cannot be read", detail="%s: %s" % (path, error)) from error
    if not isinstance(meta, dict) or not isinstance(meta.get("classes"), list):
        raise ModelError("The model's meta.json does not list the classes", detail=str(path))
    return meta


def library_versions():
    """The versions a prediction was computed with, kept in its record: scores can differ in the
    last digits between numerical libraries."""
    import platform

    import joblib
    import numpy
    import sklearn

    return {"python": platform.python_version(), "numpy": numpy.__version__, "scikit-learn": sklearn.__version__,
            "joblib": joblib.__version__}


def load_dark(path):
    """The 2048 intensities of a dark spectrum file as the controller saves it (third column)."""
    import numpy as np

    values = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        columns = line.split("\t")
        if len(columns) >= 3:
            try:
                values.append(float(columns[2]))
            except ValueError:
                pass
    dark = np.asarray(values, dtype=np.float64)
    if dark.shape != (PIXELS,) or not np.isfinite(dark).all():
        raise ValueError(UNUSABLE_DARK)
    return dark


def preprocess(raw, dark):
    """What the model sees of a raw spectrum.

    Returns (the 1958 min-max normalised values as float32, the baseline-corrected signal, the
    peak-to-peak and the standard deviation of the dark-corrected spectrum). Raises ValueError
    for a spectrum, or a dark spectrum, that cannot be used.
    """
    import numpy as np

    try:
        raw = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ValueError(UNUSABLE_SPECTRUM) from error
    if raw.shape != (PIXELS,) or not np.isfinite(raw).all() or (raw < 0).any() or (raw >= SATURATION_COUNT).any():
        raise ValueError(UNUSABLE_SPECTRUM)
    try:
        dark = np.asarray(dark, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ValueError(UNUSABLE_DARK) from error
    if dark.shape != (PIXELS,) or not np.isfinite(dark).all():
        raise ValueError(UNUSABLE_DARK)
    half = BASELINE_WINDOW // 2
    corrected = (raw - dark)[DROPPED_PIXELS:]
    padded = np.pad(corrected, (half, half), mode="edge")
    minimum = np.array([padded[i:i + BASELINE_WINDOW].min() for i in range(corrected.size)])
    baseline = np.convolve(np.pad(minimum, (half, half), mode="edge"), np.ones(BASELINE_WINDOW) / BASELINE_WINDOW,
                           mode="valid")
    signal = corrected - baseline
    spread = signal.max() - signal.min()
    normalized = np.zeros_like(signal) if spread < 1e-12 else (signal - signal.min()) / spread
    return normalized.astype(np.float32), signal, float(np.ptp(corrected)), float(np.std(corrected))


def decide(scores, threshold):
    """The outcome policy of PROTOCOL.md 5.3 for the scores of a usable spectrum."""
    winner = max(scores, key=scores.get)
    confidence = scores[winner]
    if confidence < threshold:
        label, outcome, reason = LABEL_UNKNOWN, "low_confidence", "Low pure-class confidence"
    elif winner == BACKGROUND_CLASS:
        label, outcome, reason = LABEL_UNKNOWN, "background", "Background-class response; material not identified"
    else:
        label, outcome, reason = winner, "confident", ""
    return {"label": label, "top_class": winner, "confidence": confidence, "scores": dict(scores),
            "outcome": outcome, "reason": reason, "threshold": threshold, "needs_review": outcome == "low_confidence"}


class Predictor:
    """The model of one folder, loaded once. `threshold` None means the model's own."""

    def __init__(self, model_dir, threshold=None):
        root = Path(model_dir)
        self.name = root.name
        self.sha256, _files = model_fingerprint(root)
        self.meta = read_meta(root)
        self.classes = list(self.meta["classes"])
        try:
            self.model_threshold = float(self.meta["pure_branch"]["confidence_threshold"])
            feature_dim = self.meta["feature_dim"]
        except (KeyError, TypeError, ValueError) as error:
            raise ModelError("The model's meta.json lacks the pure-branch settings", detail=repr(error)) from error
        self.threshold = self.model_threshold if threshold is None else float(threshold)
        self.scaler, self.pca, self.svm = self._load([root / name for name in MODEL_FILES[:3]])
        if feature_dim != FEATURES or self.scaler.n_features_in_ != FEATURES:
            raise ModelError("Model preprocessing does not match the %d-feature pure branch" % FEATURES)

    @staticmethod
    def _load(paths):
        try:
            import joblib
            import numpy  # noqa: F401  (a missing numpy is reported here, not at the first spectrum)
            import sklearn  # noqa: F401
        except ImportError as error:
            raise ModelError(
                "The classifier needs numpy, scikit-learn and joblib in this interpreter",
                detail="%s. Create the data layer's environment with data_layer/setup_env.sh." % error,
            ) from error
        failure = None
        # Recorded, not turned into errors: the warning filters are shared by all threads, and a
        # warning in another thread of the Studio server must not become an exception there.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                # joblib files are pickles: only model files the user put in place themselves, or
                # whose hash matches their model record, are ever loaded.
                parts = [joblib.load(path) for path in paths]
            except Exception as error:
                failure = error
        # scikit-learn warns when it unpickles a model that another version saved. Such a model
        # can score differently without any error, so it is refused.
        other_version = [str(warning.message) for warning in caught
                         if warning.category.__module__.split(".")[0] == "sklearn"
                         or "Trying to unpickle estimator" in str(warning.message)]
        if other_version:
            raise ModelError(
                "The model files were saved by another scikit-learn version than this interpreter has",
                detail="%s Install the versions pinned in data_layer/requirements.txt." % other_version[0],
            ) from failure
        if failure is not None:
            raise ModelError("The model files cannot be loaded",
                             detail="%s: %s" % (type(failure).__name__, failure)) from failure
        return parts

    def reject(self, reason):
        """The result for a spectrum that cannot be classified at all."""
        return {"label": LABEL_REJECTED, "top_class": None, "confidence": None, "scores": {}, "outcome": "rejected",
                "reason": reason, "threshold": self.threshold, "needs_review": True}

    def predict(self, intensity, dark):
        """Classify one raw spectrum. `dark` is what `load_dark` returns, or None when there is none.

        Returns label, top_class, confidence (the top score), scores, outcome, reason, threshold
        and needs_review, as stored in a prediction record.
        """
        import numpy as np

        if dark is None:
            return self.reject(NO_DARK)
        try:
            features, signal, peak_to_peak, deviation = preprocess(intensity, dark)
            if np.allclose(signal, 0):
                raise ValueError("Preprocessing failed: zero baseline-corrected signal")
            if peak_to_peak < MIN_PEAK_TO_PEAK or deviation < MIN_STANDARD_DEVIATION:
                raise ValueError("Low raw signal")
        except ValueError as error:
            return self.reject(str(error))
        native = self.svm.predict_proba(self.pca.transform(self.scaler.transform(features.reshape(1, -1))))[0]
        scores = {self.classes[int(index)]: float(value) for index, value in zip(self.svm.classes_, native)}
        return decide(scores, self.threshold)

    def predict_point(self, point, dark):
        """Classify the content of a point_NNNN.json file."""
        if not isinstance(point, dict) or point.get("status") != "OK" or point.get("pixel_count") != PIXELS:
            return self.reject("Point is not a successful 2048-pixel acquisition")
        return self.predict(point.get("intensity"), dark)
