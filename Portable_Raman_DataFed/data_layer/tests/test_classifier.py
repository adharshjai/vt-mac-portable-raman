"""The classifier: the answers of the original project's prediction code on real spectra, the
outcome policy of PROTOCOL.md 5.3 on top of them, and what is refused.

The expected scores in fixtures/__init__.py were produced by the original svm_predictor.py for the
same three points, the same dark spectrum and the same model files.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
import warnings
from unittest import mock

import fixtures
import support
from raman_datafed import classifier

try:
    import numpy as np
except ImportError:
    np = None


needs_model = fixtures.needs_model
needs_numpy = unittest.skipIf(np is None, "numpy is not installed in this interpreter")


class ModelFiles(unittest.TestCase):
    def test_model_hash_is_the_sha256_over_the_four_files_in_the_fixed_order(self):
        names = ("pure_scaler_minmax.joblib", "pure_pca_minmax.joblib", "pure_svm_minmax.joblib", "meta.json")
        self.assertEqual(classifier.MODEL_FILES, names)
        content = [(fixtures.MODEL_DIR / name).read_bytes() for name in names]
        expected = hashlib.sha256(b"".join(content)).hexdigest()
        self.assertEqual(classifier.model_hash(fixtures.MODEL_DIR), expected)
        self.assertEqual(expected, fixtures.MODEL_HASH)
        digest, files = classifier.model_fingerprint(fixtures.MODEL_DIR)
        self.assertEqual(digest, expected)
        self.assertEqual(list(files), list(names))
        for name, data in zip(names, content):
            self.assertEqual(files[name], {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})

    def test_another_order_or_another_byte_is_another_model(self):
        home = support.make_home(self)
        copy = home / "model"
        shutil.copytree(fixtures.MODEL_DIR, copy)
        self.assertEqual(classifier.model_hash(copy), fixtures.MODEL_HASH)
        with open(copy / "meta.json", "ab") as stream:
            stream.write(b"\n")
        self.assertNotEqual(classifier.model_hash(copy), fixtures.MODEL_HASH)

    def test_missing_file_is_named(self):
        home = support.make_home(self)
        copy = home / "model"
        shutil.copytree(fixtures.MODEL_DIR, copy)
        (copy / "pure_pca_minmax.joblib").unlink()
        with self.assertRaises(classifier.ModelError) as raised:
            classifier.model_hash(copy)
        self.assertIn("pure_pca_minmax.joblib", str(raised.exception))
        self.assertIn("classifier.model_dir", raised.exception.detail)
        with self.assertRaises(classifier.ModelError):
            classifier.Predictor(copy)

    def test_meta_json_must_list_the_classes(self):
        home = support.make_home(self)
        copy = home / "model"
        shutil.copytree(fixtures.MODEL_DIR, copy)
        self.assertEqual(classifier.read_meta(copy)["classes"], fixtures.CLASSES)
        (copy / "meta.json").write_text('{"feature_dim": 1958}', encoding="utf-8")
        with self.assertRaisesRegex(classifier.ModelError, "does not list the classes"):
            classifier.read_meta(copy)
        (copy / "meta.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(classifier.ModelError, "cannot be read"):
            classifier.read_meta(copy)

    def test_hashing_and_the_command_modules_need_no_numerical_library(self):
        # `status` and `sync` import these modules and publish the model record; an interpreter
        # without numpy, scikit-learn and joblib must still run them.
        code = (
            "import sys\n"
            "for name in ('numpy', 'sklearn', 'joblib'):\n"
            "    sys.modules[name] = None\n"
            "from raman_datafed import arc_job, classifier, job, review\n"
            "print(classifier.model_hash(sys.argv[1]))\n"
            "print(job.model_metadata(sys.argv[1])['name'])\n"
            "try:\n"
            "    classifier.Predictor(sys.argv[1])\n"
            "except classifier.ModelError as error:\n"
            "    print(error)\n"
        )
        done = subprocess.run([sys.executable, "-c", code, str(fixtures.MODEL_DIR)], capture_output=True, text=True,
                              timeout=60, env=support.cli_environment(support.make_home(self)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), [
            fixtures.MODEL_HASH, "pca_svm_pure_20260922",
            "The classifier needs numpy, scikit-learn and joblib in this interpreter"])


@needs_numpy
class DarkAndPreprocessing(unittest.TestCase):
    def setUp(self):
        self.dark = classifier.load_dark(fixtures.FIXTURES / fixtures.DARK_FILE)

    def test_load_dark_reads_the_third_column_of_the_real_file(self):
        self.assertEqual(self.dark.shape, (2048,))
        self.assertEqual(self.dark.dtype, np.float64)
        self.assertEqual(self.dark[:3].tolist(), [4605.0, 4856.0, 4724.0])
        self.assertEqual((self.dark[-1], self.dark.sum()), (4496.0, 8886640.0))

    def test_load_dark_copes_with_a_byte_order_mark_and_either_line_ending(self):
        home = support.make_home(self)
        rows = ["# Dark Measurement", "index\traman_shift_cm-1\tintensity"]
        rows.extend("%d\t%.2f\t%d" % (index, index * 1.5, 4000 + index % 9) for index in range(2048))
        for name, text in (("unix.txt", "\n".join(rows) + "\n"), ("windows.txt", "﻿" + "\r\n".join(rows) + "\r\n")):
            (home / name).write_bytes(text.encode("utf-8"))
            dark = classifier.load_dark(home / name)
            self.assertEqual((dark.shape, dark[0], dark[10]), ((2048,), 4000.0, 4001.0))

    def test_incomplete_or_non_finite_dark_is_refused(self):
        home = support.make_home(self)
        rows = ["%d\t0.0\t%d" % (index, 4000) for index in range(2047)]
        (home / "short.txt").write_text("\n".join(rows), encoding="utf-8")
        (home / "nan.txt").write_text("\n".join(rows + ["2047\t0.0\tnan"]), encoding="utf-8")
        for name in ("short.txt", "nan.txt"):
            with self.assertRaisesRegex(ValueError, "complete finite 2048-pixel dark spectrum"):
                classifier.load_dark(home / name)

    def test_preprocessing_of_a_real_point(self):
        point = fixtures.load_point("point_0001.json")
        features, signal, peak_to_peak, deviation = classifier.preprocess(point["intensity"], self.dark)
        self.assertEqual((features.shape, features.dtype), ((1958,), np.float32))
        self.assertEqual(signal.shape, (1958,))
        self.assertEqual((float(features.min()), float(features.max()), int(features.argmax())), (0.0, 1.0, 30))
        self.assertEqual(peak_to_peak, 42865.0)
        self.assertAlmostEqual(deviation, 13778.381924959165, places=6)
        self.assertAlmostEqual(float(features[1000]), 0.09433332085609436, places=6)
        self.assertAlmostEqual(float(features.astype(np.float64).sum()), 272.85253955287044, places=4)

    def test_what_preprocessing_refuses(self):
        good = fixtures.load_point("point_0001.json")["intensity"]
        unusable = {
            "one value short": good[:-1],
            "saturated": good[:100] + [65535] + good[101:],
            "negative": [-1] + good[1:],
            "not a number": [float("nan")] + good[1:],
            "text": ["x"] * 2048,
            "nothing": None,
            "an object": {"intensity": good},
        }
        for name, raw in unusable.items():
            with self.assertRaisesRegex(ValueError, "Invalid, incomplete or saturated spectrum", msg=name):
                classifier.preprocess(raw, self.dark)
        for dark in (self.dark[:-1], [float("inf")] * 2048, "dark"):
            with self.assertRaisesRegex(ValueError, "complete finite 2048-pixel dark spectrum"):
                classifier.preprocess(good, dark)
        # The highest count the detector reports without being saturated is still accepted.
        classifier.preprocess(good[:100] + [65534] + good[101:], self.dark)


class OutcomePolicy(unittest.TestCase):
    """PROTOCOL.md 5.3, for scores as the model gives them."""

    def decide(self, threshold=0.8, **scores):
        return classifier.decide(scores, threshold)

    def test_confident(self):
        result = self.decide(r6g=0.93, methylene_blue=0.03, adenine=0.02, di_water=0.02)
        self.assertEqual(result, {
            "label": "r6g", "top_class": "r6g", "confidence": 0.93, "outcome": "confident", "reason": "",
            "threshold": 0.8, "needs_review": False,
            "scores": {"r6g": 0.93, "methylene_blue": 0.03, "adenine": 0.02, "di_water": 0.02}})

    def test_background(self):
        result = self.decide(r6g=0.02, di_water=0.95, adenine=0.03)
        self.assertEqual((result["label"], result["top_class"], result["outcome"], result["needs_review"]),
                         ("UNKNOWN", "di_water", "background", False))
        self.assertIn("Background-class response", result["reason"])

    def test_low_confidence_whatever_the_top_class(self):
        for top in ("adenine", "di_water"):
            scores = {"r6g": 0.2, top: 0.61, "methylene_blue": 0.19}
            result = classifier.decide(scores, 0.8)
            self.assertEqual((result["label"], result["top_class"], result["confidence"], result["outcome"],
                              result["needs_review"], result["reason"]),
                             ("UNKNOWN", top, 0.61, "low_confidence", True, "Low pure-class confidence"))

    def test_a_score_equal_to_the_threshold_is_enough(self):
        self.assertEqual(self.decide(threshold=0.8, r6g=0.8, di_water=0.2)["outcome"], "confident")
        self.assertEqual(self.decide(threshold=0.8, r6g=0.2, di_water=0.8)["outcome"], "background")
        self.assertEqual(self.decide(threshold=0.8, r6g=0.7999999, di_water=0.2000001)["outcome"], "low_confidence")

    def test_the_scores_are_copied_not_shared(self):
        scores = {"r6g": 0.9, "di_water": 0.1}
        result = classifier.decide(scores, 0.8)
        result["scores"]["r6g"] = 0.0
        self.assertEqual(scores["r6g"], 0.9)


@needs_model
class RealSpectra(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.predictor = classifier.Predictor(fixtures.MODEL_DIR)
        cls.dark = classifier.load_dark(fixtures.FIXTURES / fixtures.DARK_FILE)
        cls.points = {name: fixtures.load_point(name) for name in fixtures.POINT_FILES}

    def assert_scores(self, result, name):
        expected = fixtures.EXPECTED_SCORES[name]
        self.assertEqual(list(result["scores"]), fixtures.CLASSES)
        for material, score in expected.items():
            self.assertAlmostEqual(result["scores"][material], score, delta=fixtures.SCORE_TOLERANCE,
                                   msg="%s %s" % (name, material))
        top = max(expected, key=expected.get)
        self.assertEqual(result["top_class"], top)
        self.assertEqual(result["confidence"], result["scores"][top])

    def test_model_identity(self):
        self.assertEqual(self.predictor.sha256, fixtures.MODEL_HASH)
        self.assertEqual(self.predictor.name, "pca_svm_pure_20260922")
        self.assertEqual(self.predictor.classes, fixtures.CLASSES)
        self.assertEqual((self.predictor.threshold, self.predictor.model_threshold), (0.8, 0.8))

    def test_the_three_real_points_get_the_answers_of_the_original(self):
        outcomes = {}
        for name, point in self.points.items():
            result = self.predictor.predict_point(point, self.dark)
            self.assert_scores(result, name)
            self.assertEqual(result["label"], fixtures.EXPECTED_LABELS[name])
            self.assertEqual(result["threshold"], 0.8)
            self.assertEqual(set(result), {"label", "top_class", "confidence", "scores", "outcome", "reason",
                                           "threshold", "needs_review"})
            json.dumps(result, allow_nan=False)
            outcomes[name] = (result["outcome"], result["needs_review"], result["reason"])
        self.assertEqual(outcomes, {
            "point_0001.json": ("confident", False, ""),
            "point_0004.json": ("confident", False, ""),
            "point_0008.json": ("background", False, "Background-class response; material not identified"),
        })

    def test_predict_takes_the_bare_intensity_too(self):
        point = self.points["point_0004.json"]
        self.assertEqual(self.predictor.predict(point["intensity"], self.dark),
                         self.predictor.predict_point(point, self.dark))

    def test_threshold_from_the_settings_changes_the_outcome_not_the_scores(self):
        stricter = classifier.Predictor(fixtures.MODEL_DIR, threshold=0.9)
        self.assertEqual((stricter.threshold, stricter.model_threshold), (0.9, 0.8))
        outcomes = {}
        for name, point in self.points.items():
            result = stricter.predict_point(point, self.dark)
            self.assert_scores(result, name)
            self.assertEqual(result["threshold"], 0.9)
            outcomes[name] = (result["outcome"], result["label"], result["needs_review"])
        self.assertEqual(outcomes, {
            "point_0001.json": ("low_confidence", "UNKNOWN", True),
            "point_0004.json": ("confident", "methylene_blue", False),
            "point_0008.json": ("background", "UNKNOWN", False),
        })
        # A background answer below the threshold is as unsure as any other.
        result = classifier.Predictor(fixtures.MODEL_DIR, threshold=0.93).predict_point(
            self.points["point_0008.json"], self.dark)
        self.assertEqual((result["outcome"], result["top_class"], result["needs_review"]),
                         ("low_confidence", "di_water", True))

    def assert_rejected(self, result, reason):
        self.assertEqual(result, {"label": "REJECTED", "top_class": None, "confidence": None, "scores": {},
                                  "outcome": "rejected", "reason": reason, "threshold": 0.8, "needs_review": True})

    def test_no_dark_spectrum_is_rejected(self):
        point = self.points["point_0001.json"]
        self.assert_rejected(self.predictor.predict(point["intensity"], None), "no dark spectrum")
        self.assert_rejected(self.predictor.predict_point(point, None), "no dark spectrum")

    def test_unusable_spectra_are_rejected_with_the_reasons_of_the_original(self):
        good = self.points["point_0001.json"]["intensity"]
        dark = self.dark.tolist()
        cases = {
            "Invalid, incomplete or saturated spectrum": [good[:2047], [65535] * 2048, [float("nan")] * 2048,
                                                          good[:5] + [65535] + good[6:], None, "spectrum"],
            "Preprocessing failed: zero baseline-corrected signal": [dark, [value + 3 for value in dark]],
            "Low raw signal": [[value + 10 * (index % 2) for index, value in enumerate(dark)]],
        }
        for reason, spectra in cases.items():
            for raw in spectra:
                self.assert_rejected(self.predictor.predict(raw, self.dark), reason)
        self.assert_rejected(self.predictor.predict(good, self.dark[:100]),
                             "A complete finite 2048-pixel dark spectrum is required")

    def test_low_raw_signal_is_a_small_range_or_a_small_spread(self):
        dark = self.dark.tolist()

        def above_dark(pattern):
            return [value + pattern(index) for index, value in enumerate(dark)]

        # Range below 100 counts although the spread (30) is well above 15.
        self.assert_rejected(self.predictor.predict(above_dark(lambda index: 60 * (index % 2)), self.dark),
                             "Low raw signal")
        # Spread below 15 although one pixel stands 150 counts out.
        self.assert_rejected(self.predictor.predict(above_dark(lambda index: 150 if index == 1000 else 0), self.dark),
                             "Low raw signal")
        # A range of exactly 100 with a spread of 50 is enough to be classified.
        result = self.predictor.predict(above_dark(lambda index: 100 * (index % 2)), self.dark)
        self.assertNotEqual(result["outcome"], "rejected")

    def test_a_point_that_is_not_a_successful_acquisition_is_rejected(self):
        point = self.points["point_0001.json"]
        without_count = {key: value for key, value in point.items() if key != "pixel_count"}
        for broken in (dict(point, status="FAIL"), dict(point, pixel_count=2047), without_count, None, []):
            self.assert_rejected(self.predictor.predict_point(broken, self.dark),
                                 "Point is not a successful 2048-pixel acquisition")

    def test_library_versions_name_what_computed_the_scores(self):
        versions = classifier.library_versions()
        self.assertEqual(set(versions), {"python", "numpy", "scikit-learn", "joblib"})
        self.assertEqual(versions["numpy"], np.__version__)


@needs_model
class ModelLoading(unittest.TestCase):
    def test_model_saved_by_another_scikit_learn_version_is_refused(self):
        def load_with_the_warning_scikit_learn_gives(_path):
            warnings.warn("Trying to unpickle estimator SVC from version 1.0.0 when using version 9.9.9.",
                          UserWarning)

        with mock.patch("joblib.load", load_with_the_warning_scikit_learn_gives):
            with self.assertRaises(classifier.ModelError) as raised:
                classifier.Predictor(fixtures.MODEL_DIR)
        self.assertIn("another scikit-learn version", str(raised.exception))
        self.assertIn("from version 1.0.0", raised.exception.detail)
        self.assertIn("requirements.txt", raised.exception.detail)

    def test_model_files_that_are_not_models_are_refused(self):
        home = support.make_home(self)
        copy = home / "model"
        shutil.copytree(fixtures.MODEL_DIR, copy)
        (copy / "pure_svm_minmax.joblib").write_bytes(b"not a joblib file")
        with self.assertRaisesRegex(classifier.ModelError, "cannot be loaded"):
            classifier.Predictor(copy)

    def test_model_for_another_number_of_features_is_refused(self):
        home = support.make_home(self)
        copy = home / "model"
        shutil.copytree(fixtures.MODEL_DIR, copy)
        meta = json.loads((copy / "meta.json").read_text(encoding="utf-8"))
        meta["feature_dim"] = 2048
        (copy / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with self.assertRaisesRegex(classifier.ModelError, "1958-feature pure branch"):
            classifier.Predictor(copy)
        del meta["pure_branch"]
        (copy / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with self.assertRaisesRegex(classifier.ModelError, "lacks the pure-branch settings"):
            classifier.Predictor(copy)


if __name__ == "__main__":
    unittest.main()
