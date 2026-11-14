#!/usr/bin/env python3
"""Show what the shipped classifier makes of the virtual plate (simulation only).

The numbers in sample.json (blend widths, noise, power scale, focus width) were chosen with this
script; run it again after changing them or after rebuilding reference_spectra.json. It builds
simulated frames with sample_model.py (the capture simulator produces the same frames, see
macos-native/backend/tests/test_backend_virtual_sample.py), takes one simulated dark capture, and
classifies every frame with the original prediction code, which is not part of this folder:

    <interpreter with numpy, scikit-learn, joblib> check_classifier.py \
        --predictor-dir <folder that holds svm_predictor.py and common.py>

It prints the three scan patterns of the controller (1D X at y = 2.5 mm, 1D Y at x = 2.5 mm, the
11 x 11 grid of the 2D scan) at HIGH power, the 1D X scan again at MEDIUM and LOW power, and the
cases that must be rejected. It ends with exit code 1 when one of these does not hold:

  - a point that lies on one material only gets that material as top class with a score of at
    least the model's threshold (the blank plate counts as di_water),
  - no scan point is rejected, some are below the threshold, and at least 80 % are not,
  - laser off and far out of focus are rejected as "Low raw signal".
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

sys.dont_write_bytecode = True      # --predictor-dir usually points into another project: leave no cache files there
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sample_model  # noqa: E402

LETTERS = {"r6g": "R", "methylene_blue": "M", "adenine": "A", "di_water": "W"}
LOW_SIGNAL = "Low raw signal"


class Checker:
    def __init__(self, sample: sample_model.VirtualSample, predictor_dir: str, model_dir: str, seed: int, workdir: str):
        sys.path.insert(0, predictor_dir)
        import svm_predictor              # the original prediction code
        self.sample = sample
        self.rng = random.Random(seed)
        dark_file = os.path.join(workdir, "dark_simulated.txt")
        dark = sample.frame(*sample.start_position_mm, dark_capture=True, rng=self.rng)
        with open(dark_file, "w", encoding="utf-8") as handle:      # the layout of the controller's dark files
            handle.write("# Dark Measurement (simulated)\n# Timestamp: -\n# Points: %d\nindex\traman_shift_cm-1\tintensity\n" % len(dark))
            for index, value in enumerate(dark):
                handle.write("%d\t%.8f\t%d\n" % (index, 0.0, value))
        self.predictor = svm_predictor.Predictor(model_dir, dark_file)
        self.threshold = self.predictor.threshold
        self.failures: List[str] = []

    def classify(self, x: float, y: float, z: float, level: str = "HIGH", laser_on: bool = True) -> Dict:
        frame = self.sample.frame(x, y, z, laser_on=laser_on, level=level, rng=self.rng)
        record = {"session_id": "check", "point_id": 1, "capture_id": "check_1", "controller_x_steps": 0,
                  "controller_y_steps": 0, "controller_z_steps": 0, "status": "OK", "pixel_count": len(frame), "intensity": frame}
        result = self.predictor.predict(record)
        scores = result["scores"]
        top = max(scores, key=scores.get) if scores else None
        score = scores[top] if scores else 0.0
        if result["label"] == "REJECTED":
            outcome = "rejected"
        elif score < self.threshold:
            outcome = "low_confidence"
        else:
            outcome = "background" if top == self.sample.background else "confident"
        return {"top": top, "score": score, "outcome": outcome, "reason": result["reason"]}

    def scan_point(self, x: float, y: float, level: str) -> Tuple[Dict, str]:
        """One point of a scan at the focus height, checked against the rule for single-material points."""
        result = self.classify(x, y, self.sample.z_focus_mm, level)
        weights = {material: weight for material, weight in self.sample.weights(x, y).items() if weight > 0.0005}
        shares = " + ".join("%.0f%% %s" % (100 * weight, material) for material, weight in
                            sorted(weights.items(), key=lambda item: -item[1]))
        if len(weights) == 1:
            material = next(iter(weights))
            if result["top"] != material or result["score"] < self.threshold:
                self.failures.append("(%.1f, %.1f) %s: pure %s classified as %s %.3f (%s)"
                                     % (x, y, level, material, result["top"], result["score"], result["outcome"]))
        return result, shares

    def line(self, axis: str, level: str) -> List[Dict]:
        print("\n1D %s scan, %s = 2.5 mm, z = %.1f mm, %s power" % (axis, "y" if axis == "X" else "x", self.sample.z_focus_mm, level))
        print("  %s mm | on the plate                         | top class      | score | outcome" % axis.lower())
        results = []
        for index in range(13):
            position = 2.0 * index
            x, y = (position, 2.5) if axis == "X" else (2.5, position)
            result, shares = self.scan_point(x, y, level)
            results.append(result)
            print("  %4.0f | %-36s | %-14s | %.3f | %s" % (position, shares, result["top"] or "-", result["score"],
                                                         result["outcome"] + (" (%s)" % result["reason"] if result["outcome"] == "rejected" else "")))
        return results

    def grid(self, level: str) -> List[Dict]:
        print("\n2D scan grid, x and y from 2.0 to 3.0 mm in 0.1 mm steps, z = %.1f mm, %s power" % (self.sample.z_focus_mm, level))
        print("  top class (R r6g, M methylene_blue, A adenine, W di_water) and score; lower case = below %.1f; ! = rejected" % self.threshold)
        results = []
        for row in range(10, -1, -1):
            y = 2.0 + 0.1 * row
            cells = []
            for column in range(11):
                result, _shares = self.scan_point(2.0 + 0.1 * column, y, level)
                results.append(result)
                letter = "!" if result["outcome"] == "rejected" else LETTERS.get(result["top"], "?")
                cells.append("%s.%02d" % (letter.lower() if result["outcome"] == "low_confidence" else letter, int(result["score"] * 100)))
            print("  y %.1f | %s" % (y, " ".join(cells)))
        print("    x   | " + " ".join("%4.1f" % (2.0 + 0.1 * column) for column in range(11)))
        return results

    def summary(self, name: str, results: List[Dict]) -> None:
        counts: Dict[str, int] = {}
        for result in results:
            counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
        print("  %s: %s" % (name, ", ".join("%d %s" % (count, outcome) for outcome, count in sorted(counts.items()))))
        low, rejected = counts.get("low_confidence", 0), counts.get("rejected", 0)
        if rejected:
            self.failures.append("%s: %d scan points rejected" % (name, rejected))
        if low == 0:
            self.failures.append("%s: no point below the threshold" % name)
        if low + rejected > 0.2 * len(results):
            self.failures.append("%s: only %d of %d points at or above the threshold" % (name, len(results) - low - rejected, len(results)))

    def must_reject(self, name: str, x: float, y: float, z: float, laser_on: bool = True, level: str = "HIGH") -> None:
        result = self.classify(x, y, z, level, laser_on)
        print("  %-52s | %-14s | %s" % (name, result["outcome"], result["reason"]))
        if result["outcome"] != "rejected" or result["reason"] != LOW_SIGNAL:
            self.failures.append("%s: %s %s, expected rejected (%s)" % (name, result["outcome"], result["reason"], LOW_SIGNAL))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Classify simulated frames of the virtual plate (simulation only).")
    parser.add_argument("--predictor-dir", required=True, help="folder with svm_predictor.py and common.py (original project: laser_test/intersect_bridge)")
    parser.add_argument("--model-dir", default=os.path.join(HERE, "..", "models", "pca_svm_pure_20260922"))
    parser.add_argument("--sample-dir", default=HERE)
    parser.add_argument("--seed", type=int, default=1, help="seed of the simulated noise")
    args = parser.parse_args(argv)

    sample = sample_model.VirtualSample.load(args.sample_dir)
    with tempfile.TemporaryDirectory(prefix="raman_check_") as workdir:
        checker = Checker(sample, os.path.abspath(args.predictor_dir), os.path.abspath(args.model_dir), args.seed, workdir)
        print("Simulated frames, noise seed %d, threshold %.2f; outcomes as in data_layer/PROTOCOL.md section 5.3" % (args.seed, checker.threshold))
        line_x = checker.line("X", "HIGH")
        line_y = checker.line("Y", "HIGH")
        grid = checker.grid("HIGH")
        line_x_medium = checker.line("X", "MEDIUM")
        line_x_low = checker.line("X", "LOW")
        print("\nSummary")
        checker.summary("1D X, HIGH", line_x)
        checker.summary("1D Y, HIGH", line_y)
        checker.summary("2D grid, HIGH", grid)
        checker.summary("1D X, MEDIUM", line_x_medium)
        checker.summary("1D X, LOW", line_x_low)
        print("\nMust be rejected as \"%s\"" % LOW_SIGNAL)
        start_x, start_y, _start_z = sample.start_position_mm
        checker.must_reject("laser off at the start position", start_x, start_y, sample.z_focus_mm, laser_on=False)
        for name, x in (("blank plate", 0.0), ("R6G band", 8.0), ("methylene blue band", 14.0), ("adenine band", 20.0)):
            checker.must_reject("%s, z = 0 mm (homed, %.0f mm below focus)" % (name, sample.z_focus_mm), x, 2.5, 0.0)
            checker.must_reject("%s, z = 20 mm (2D scan's Z excursion)" % name, x, 2.5, 20.0)
    if checker.failures:
        print("\nNOT AS EXPECTED:")
        for failure in checker.failures:
            print("  " + failure)
        return 1
    print("\nAll expectations hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
