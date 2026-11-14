"""The files of the virtual sample without any simulator (data_layer/PROTOCOL.md section 4).

Simulation data only; no program of the instrument chain is started.

- virtual_sample/sample_model.py against numbers worked out by hand from the rules of section 4
  (test_backend_virtual_sample.py then checks the capture simulator against sample_model.py),
- the shipped virtual_sample/reference_spectra.json and sample.json,
- virtual_sample/build_reference_spectra.py on small made-up inputs, and, when the original
  project's hand-off folder is next to this edition, on the measured inputs recorded in the
  shipped file, which must give the shipped file again byte for byte.

Run:  python3 -m unittest macos-native/backend/tests/test_virtual_sample_model.py
"""
import importlib.util
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDITION = HERE.parents[2]
SAMPLE_DIR = EDITION / "virtual_sample"
GENERATOR = SAMPLE_DIR / "build_reference_spectra.py"
# The measured inputs live in the original project; this edition sits inside it.
HANDOFF = Path(os.environ.get("RAMAN_HANDOFF_DIR") or EDITION.parent / "laser_test" / "build" / "Desktop_Qt_6_6_3_MSVC2019_64bit-Debug")
sys.path.insert(0, str(SAMPLE_DIR))
import sample_model  # noqa: E402

PIXELS = 2048
HAVE_NUMPY = importlib.util.find_spec("numpy") is not None


def flat(value: float, first: float = None):
    """A spectrum with the same value in every pixel, except pixel 0 when `first` is given."""
    spectrum = [float(value)] * PIXELS
    if first is not None:
        spectrum[0] = float(first)
    return spectrum


def small_sample(**changes):
    sample = {
        "schema": "raman-virtual-sample/1", "steps_per_mm": 1000,
        "start_position_mm": {"x": 1.0, "y": 2.0, "z": 3.0},
        "focus": {"z_focus_mm": 4.0, "half_width_mm": 0.5},
        "background": "water",
        "regions": [
            {"name": "band", "material": "alpha", "shape": "frame", "inner_mm": 10, "outer_mm": 14},
            {"name": "next band", "material": "beta", "shape": "frame", "inner_mm": 14, "outer_mm": 18},
            {"name": "spot", "material": "alpha", "shape": "circle", "center_mm": [2.0, 2.0], "radius_mm": 1.0},
            {"name": "other spot", "material": "beta", "shape": "circle", "center_mm": [3.0, 2.0], "radius_mm": 1.0},
        ],
        "edge_blend_mm": {"frame": 2.0, "circle": 0.4},
        "power_scale": {"LOW": 0.25, "MEDIUM": 0.5, "HIGH": 1.0},
        "noise_counts_rms": 0,
    }
    sample.update(changes)
    return sample


def small_reference(**signals):
    reference = {"schema": "raman-reference-spectra/1", "source": "made up for this test", "pixel_count": PIXELS,
                 "dark": flat(100), "signals": {"alpha": flat(1000, first=2000), "beta": flat(3000), "water": flat(10)}}
    reference["signals"].update(signals)
    return reference


class SampleModelTest(unittest.TestCase):
    def setUp(self):
        self.model = sample_model.VirtualSample(small_sample(), small_reference())

    def assert_weights(self, x, y, **expected):
        weights = self.model.weights(x, y)
        for material in ("alpha", "beta", "water"):
            self.assertAlmostEqual(weights.get(material, 0.0), expected.get(material, 0.0), places=12, msg="%s at (%s, %s)" % (material, x, y))
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=12)

    def test_frame_regions(self):
        # d = max(x, y); clamp((d - inner + b/2) / b) * clamp((outer + b/2 - d) / b) with b = 2
        self.assert_weights(12.0, 0.0, alpha=1.0)
        self.assert_weights(0.0, 12.0, alpha=1.0)
        self.assert_weights(13.5, 3.0, alpha=0.75, beta=0.25)          # (15 - 13.5) / 2 and (13.5 - 13) / 2
        self.assert_weights(14.0, 14.0, alpha=0.5, beta=0.5)
        self.assert_weights(9.5, 3.0, alpha=0.25, water=0.75)          # (9.5 - 9) / 2; the background takes the rest
        self.assert_weights(8.9, 4.0, water=1.0)
        self.assert_weights(18.5, 0.0, beta=0.25, water=0.75)          # (19 - 18.5) / 2
        self.assert_weights(30.0, 30.0, water=1.0)

    def test_circle_regions(self):
        # clamp((radius + b/2 - r) / b) with b = 0.4
        self.assert_weights(2.0, 3.1, alpha=0.25, water=0.75)          # r = 1.1 from the spot
        self.assert_weights(1.0, 2.0, alpha=0.5, water=0.5)            # on the rim: r = radius
        self.assert_weights(0.7, 2.0, water=1.0)                       # r = 1.3: outside the blend
        # In the middle of the spot the other spot still has (1.2 - 1.0) / 0.4 = 0.5: the sum is 1.5,
        # so both are divided by it and nothing is left for the background.
        self.assert_weights(2.0, 2.0, alpha=1.0 / 1.5, beta=0.5 / 1.5)

    def test_largest_weight_of_a_materials_regions(self):
        regions = small_sample()["regions"] + [
            {"name": "second spot", "material": "alpha", "shape": "circle", "center_mm": [2.0, 3.0], "radius_mm": 1.0}]
        model = sample_model.VirtualSample(small_sample(regions=regions), small_reference())
        # (2.0, 3.1): 0.25 from the first spot, 1.0 from the second one, not 1.25.
        weights = model.weights(2.0, 3.1)
        self.assertAlmostEqual(weights["alpha"], 1.0)
        self.assertAlmostEqual(weights["water"], 0.0)

    def test_hard_edges_without_a_blend_width(self):
        model = sample_model.VirtualSample(small_sample(edge_blend_mm={}), small_reference())
        self.assertEqual(model.weights(10.0, 0.0)["alpha"], 1.0)
        self.assertEqual(model.weights(9.999, 0.0)["alpha"], 0.0)
        self.assertEqual(model.weights(14.0, 0.0), {"alpha": 0.5, "beta": 0.5, "water": 0.0})   # the shared edge belongs to both
        self.assertEqual(model.weights(2.0, 3.0)["alpha"], 1.0)
        self.assertEqual(model.weights(2.0, 3.001)["alpha"], 0.0)

    def test_focus(self):
        self.assertEqual(self.model.focus(4.0), 1.0)
        self.assertAlmostEqual(self.model.focus(4.5), 0.5)
        self.assertAlmostEqual(self.model.focus(3.5), 0.5)
        self.assertAlmostEqual(self.model.focus(3.0), 0.2)             # 1 / (1 + 2^2)
        self.assertLess(self.model.focus(20.0), 0.001)

    def test_expected_frame(self):
        m = self.model
        # raw = dark + laser_on * power_scale[level] * focus(z) * sum(w * signal)
        self.assertEqual(m.expected(12.0, 0.0, 4.0)[:2], [100 + 2000, 100 + 1000])
        self.assertEqual(m.expected(12.0, 0.0, 4.0, level="LOW")[1], 100 + 0.25 * 1000)
        self.assertAlmostEqual(m.expected(12.0, 0.0, 4.5, level="MEDIUM")[1], 100 + 0.5 * 0.5 * 1000)
        self.assertAlmostEqual(m.expected(13.5, 0.0, 4.0)[1], 100 + 0.75 * 1000 + 0.25 * 3000)
        self.assertAlmostEqual(m.expected(9.5, 0.0, 4.0)[1], 100 + 0.25 * 1000 + 0.75 * 10)
        self.assertEqual(m.expected(30.0, 0.0, 4.0)[1], 100 + 10)
        dark = flat(100)
        self.assertEqual(m.expected(12.0, 0.0, 4.0, laser_on=False), dark)
        self.assertEqual(m.expected(12.0, 0.0, 4.0, dark_capture=True), dark)
        # Only the three levels the controller sends have a scale.
        self.assertEqual(m.expected(12.0, 0.0, 4.0, level=None), dark)
        self.assertEqual(m.expected(12.0, 0.0, 4.0, level=sample_model.POWER_LEVELS.get(0)), dark)
        self.assertEqual(sample_model.POWER_LEVELS, {1: "LOW", 2: "MEDIUM", 3: "HIGH"})

    def test_frames_are_rounded_and_clipped(self):
        reference = small_reference(alpha=[70000.0, -500.0, 0.5, 1.5] + [0.0] * (PIXELS - 4))
        frame = sample_model.VirtualSample(small_sample(), reference).frame(12.0, 0.0, 4.0)
        self.assertEqual(frame[:4], [65534, 0, 100, 102])              # top of the range, zero, 100.5 and 101.5 to even
        self.assertTrue(all(isinstance(value, int) for value in frame))
        self.assertEqual(sample_model.clip_counts([-1.0, 65534.4, 65535.0, 12.4]), [0, 65534, 65534, 12])

    def test_noise(self):
        model = sample_model.VirtualSample(small_sample(noise_counts_rms=5), small_reference())
        frame = model.frame(30.0, 0.0, 4.0, rng=random.Random(7))
        self.assertEqual(frame, model.frame(30.0, 0.0, 4.0, rng=random.Random(7)), "the same seed gives the same frame")
        self.assertNotEqual(frame, model.frame(30.0, 0.0, 4.0, rng=random.Random(8)))
        residual = [value - 110 for value in frame]
        self.assertLess(abs(statistics.fmean(residual)), 0.5)
        self.assertAlmostEqual(statistics.pstdev(residual), 5.0, delta=0.5)
        self.assertEqual(model.frame(30.0, 0.0, 4.0), [110] * PIXELS, "no generator, no noise")

    def test_stage_position(self):
        self.assertEqual(self.model.position_mm(None), (1.0, 2.0, 3.0))
        stage = {"x_steps": 86750, "y_steps": 0, "z_steps": 138800, "steps_per_mm": 34700, "moving": False, "updated_utc": "-"}
        self.assertEqual(self.model.position_mm(stage), (2.5, 0.0, 4.0))

    def test_dark_capture_ids(self):
        self.assertTrue(sample_model.is_dark_capture("dark_3f2a"))
        for capture_id in ("darkroom", "Dark_1", "x_dark_1", "single_dark_1", ""):
            self.assertFalse(sample_model.is_dark_capture(capture_id), capture_id)

    def test_unusable_files_are_refused(self):
        cases = [
            (small_sample(schema="raman-virtual-sample/2"), small_reference(), "schema"),
            (small_sample(), dict(small_reference(), schema="other"), "schema"),
            (small_sample(background="oil"), small_reference(), "no signal for material oil"),
            (small_sample(focus={"z_focus_mm": 4.0, "half_width_mm": 0}), small_reference(), "half_width_mm"),
            (small_sample(regions=[{"name": "x", "material": "alpha", "shape": "square"}]), small_reference(), "unknown region shape"),
            (small_sample(), small_reference(beta=[1.0] * 2047), "beta has 2047 values"),
        ]
        for sample, reference, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as caught:
                    sample_model.VirtualSample(sample, reference)
                self.assertIn(message, str(caught.exception))


class ShippedFilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (SAMPLE_DIR / "reference_spectra.json").read_text(encoding="utf-8")
        cls.reference = json.loads(cls.text)
        cls.sample = json.loads((SAMPLE_DIR / "sample.json").read_text(encoding="utf-8"))

    def test_reference_spectra(self):
        reference = self.reference
        self.assertEqual(reference["schema"], "raman-reference-spectra/1")
        self.assertEqual(reference["pixel_count"], PIXELS)
        self.assertEqual(sorted(reference["signals"]), ["adenine", "di_water", "methylene_blue", "r6g"])
        for name, values in [("dark", reference["dark"])] + list(reference["signals"].items()):
            with self.subTest(spectrum=name):
                self.assertEqual(len(values), PIXELS)
                self.assertTrue(all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values))
                self.assertTrue(all(round(value, 1) == value for value in values), "one decimal at most")
        self.assertTrue(all(4000 < value < 6000 for value in reference["dark"]), "a dark level of some thousand counts")

    def test_sources_are_recorded_without_machine_paths(self):
        sources = self.reference["sources"]
        self.assertEqual(sorted(sources), sorted(self.reference["signals"]))
        for name, source in sources.items():
            with self.subTest(material=name):
                self.assertEqual(sorted(source), ["dark", "dark_sha256", "skipped_incomplete", "skipped_saturated",
                                                  "spectra", "spectra_sha256", "spectra_used"])
                self.assertGreaterEqual(source["spectra_used"], 100)
                self.assertFalse(os.path.isabs(source["spectra"]) or os.path.isabs(source["dark"]))
        for fragment in ("/Users/", "/home/", ":\\"):
            self.assertNotIn(fragment, self.text)

    def test_sample_and_reference_fit_together(self):
        model = sample_model.VirtualSample.load(str(SAMPLE_DIR))
        self.assertEqual(model.start_position_mm, (2.5, 2.5, 4.0))
        self.assertEqual({region["material"] for region in self.sample["regions"]} | {self.sample["background"]},
                         set(self.reference["signals"]))
        self.assertEqual(sorted(self.sample["power_scale"]), ["HIGH", "LOW", "MEDIUM"])
        # Nothing on the plate saturates at the highest power in focus (65535 is the ADC's full scale).
        top = max(self.sample["power_scale"].values())
        for name, signal in self.reference["signals"].items():
            self.assertLess(max(dark + top * value for dark, value in zip(self.reference["dark"], signal)), 60000, name)
        # The start position is on the R6G droplet.
        self.assertAlmostEqual(model.weights(2.5, 2.5)["r6g"], 1.0)


@unittest.skipUnless(HAVE_NUMPY, "build_reference_spectra.py needs numpy")
class GeneratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="raman_reference_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def build(self, root: Path, materials, output: Path):
        command = [sys.executable, "-B", str(GENERATOR), "--root", str(root), "--output", str(output)]
        for material in materials:
            command += ["--material"] + [str(part) for part in material]
        return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)

    @staticmethod
    def hex_row(values) -> str:
        return " ".join("%04X" % value for value in values)

    def write_dark(self, path: Path, values):
        rows = ["# Dark Measurement", "# Timestamp: 2026-01-01 00:00:00.000", "# Points: %d" % len(values), "index\traman_shift_cm-1\tintensity"]
        rows += ["%d\t%.8f\t%d" % (index, 57.4 + index, value) for index, value in enumerate(values)]
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_made_up_inputs(self):
        root = self.tmp / "handoff"
        (root / "db").mkdir(parents=True)
        (root / "sessions" / "scan_a").mkdir(parents=True)
        # Material "one": a controller log. Two good spectra (pixel values 1000/1002 and 2000/2003),
        # an incomplete row, a saturated spectrum and an unreadable row, which must all be left out.
        log = ["Parameter setting...",
               "[2026-02-17 15:32:00.708] [Backend] Image Data " + self.hex_row([1000] * 2047 + [2000]),
               "Stop Capture...",
               "Image Data " + self.hex_row([1002] * 2047 + [2003]),
               "[2026-02-17 15:32:02.000] [Backend] Image Data " + self.hex_row([1500] * 131),
               "Image Data " + self.hex_row([65535] + [1001] * 2047),
               "Image Data Error code 0x0001"]
        (root / "db" / "Database_one.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
        self.write_dark(root / "db" / "dark_one.txt", [100] * 2047 + [101])
        # Material "two": a scan-session folder. Two good points and one that did not succeed.
        for point, (status, value) in enumerate((("OK", 500), ("OK", 503), ("FAIL", 9000)), start=1):
            record = {"point_id": point, "status": status, "pixel_count": PIXELS, "intensity": [value] * PIXELS}
            (root / "sessions" / "scan_a" / ("point_%04d.json" % point)).write_text(json.dumps(record), encoding="utf-8")
        self.write_dark(root / "db" / "dark_two.txt", [200] * 2047 + [204])

        output = self.tmp / "reference_spectra.json"
        materials = [("one", "db/Database_one.txt", "db/dark_one.txt"), ("two", "sessions/scan_a", "db/dark_two.txt")]
        done = self.build(root, materials, output)
        self.assertEqual(done.returncode, 0, done.stderr)
        reference = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(reference["schema"], "raman-reference-spectra/1")
        self.assertEqual(reference["pixel_count"], PIXELS)
        # mean(raw) - own dark: (1000 + 1002) / 2 - 100 and (2000 + 2003) / 2 - 101; (500 + 503) / 2 - 200 and - 204
        self.assertEqual(reference["signals"]["one"], [901.0] * 2047 + [1900.5])
        self.assertEqual(reference["signals"]["two"], [301.5] * 2047 + [297.5])
        # one common dark: the mean of the dark measurements
        self.assertEqual(reference["dark"], [150.0] * 2047 + [152.5])
        self.assertEqual(reference["sources"]["one"]["spectra"], "db/Database_one.txt")
        self.assertEqual(reference["sources"]["one"]["dark"], "db/dark_one.txt")
        self.assertEqual({key: reference["sources"]["one"][key] for key in ("spectra_used", "skipped_incomplete", "skipped_saturated")},
                         {"spectra_used": 2, "skipped_incomplete": 2, "skipped_saturated": 1})
        self.assertEqual({key: reference["sources"]["two"][key] for key in ("spectra_used", "skipped_incomplete", "skipped_saturated")},
                         {"spectra_used": 2, "skipped_incomplete": 1, "skipped_saturated": 0})
        self.assertNotIn(str(self.tmp), output.read_text(encoding="utf-8"), "only paths relative to --root are recorded")
        self.assertFalse(output.with_name(output.name + ".tmp").exists())

        # The same input gives the same bytes; an absolute source path is recorded by file name only.
        again = self.tmp / "again.json"
        absolute = [("one", root / "db" / "Database_one.txt", root / "db" / "dark_one.txt"), materials[1]]
        self.assertEqual(self.build(root, materials, again).returncode, 0)
        self.assertEqual(again.read_bytes(), output.read_bytes())
        self.assertEqual(self.build(root, absolute, again).returncode, 0)
        recorded = json.loads(again.read_text(encoding="utf-8"))
        self.assertEqual(recorded["sources"]["one"]["spectra"], "Database_one.txt")
        self.assertEqual(recorded["signals"], reference["signals"])
        self.assertNotIn(str(self.tmp), again.read_text(encoding="utf-8"))

        # A file in the wrong role is refused: the role comes from the position on the command line.
        swapped = self.build(root, [("one", "db/dark_one.txt", "db/Database_one.txt")], self.tmp / "swapped.json")
        self.assertEqual(swapped.returncode, 1)
        self.assertIn("error: dark_one.txt holds no complete 2048-pixel spectrum", swapped.stderr)
        self.assertFalse((self.tmp / "swapped.json").exists())
        not_dark = self.build(root, [("two", "sessions/scan_a", "db/Database_one.txt")], self.tmp / "swapped.json")
        self.assertEqual(not_dark.returncode, 1)
        self.assertIn("error: Database_one.txt is not a 2048-pixel dark measurement table", not_dark.stderr)

    @unittest.skipUnless(HANDOFF.is_dir(), "the original project's hand-off folder is not next to this edition (RAMAN_HANDOFF_DIR)")
    def test_the_shipped_file_is_what_the_generator_makes_of_its_sources(self):
        shipped = SAMPLE_DIR / "reference_spectra.json"
        sources = json.loads(shipped.read_text(encoding="utf-8"))["sources"]
        missing = [part for source in sources.values() for part in (source["spectra"], source["dark"]) if not (HANDOFF / part).exists()]
        if missing:
            self.skipTest("measured inputs not found in %s: %s" % (HANDOFF, ", ".join(missing)))
        output = self.tmp / "reference_spectra.json"
        done = self.build(HANDOFF, [(name, source["spectra"], source["dark"]) for name, source in sources.items()], output)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(output.read_bytes(), shipped.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
