"""Reference implementation of the virtual sample (data_layer/PROTOCOL.md section 4).

Simulation only. The capture simulator (macos-native/backend/SampleApl/VirtualSample.cs) builds
its frames by the same rules; this module states them once more in Python so that the tests can
say what a simulated frame must look like and check_classifier.py can show what the classifier
makes of the virtual plate. Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import random
from typing import Dict, List, Optional, Sequence, Tuple

SAMPLE_SCHEMA = "raman-virtual-sample/1"
REFERENCE_SCHEMA = "raman-reference-spectra/1"
SAMPLE_FILE = "sample.json"
REFERENCE_FILE = "reference_spectra.json"
PIXELS = 2048
MAX_COUNT = 65534          # one below the ADC's full scale, which readers treat as saturation
DARK_CAPTURE_PREFIX = "dark_"
# The LD power values the controller sends with menu item 4, and their names in sample.json.
POWER_LEVELS = {1: "LOW", 2: "MEDIUM", 3: "HIGH"}


def _ramp(inside_mm: float, blend_mm: float) -> float:
    """0 outside an edge, 1 inside, linear across the blend width centred on the edge.
    `inside_mm` is the distance behind the edge (negative in front of it)."""
    if blend_mm <= 0:
        return 1.0 if inside_mm >= 0 else 0.0
    return min(1.0, max(0.0, inside_mm / blend_mm + 0.5))


class VirtualSample:
    """sample.json + reference_spectra.json, validated. Raises ValueError when they do not fit."""

    def __init__(self, sample: dict, reference: dict):
        if sample.get("schema") != SAMPLE_SCHEMA:
            raise ValueError("sample.json: schema is not %s" % SAMPLE_SCHEMA)
        if reference.get("schema") != REFERENCE_SCHEMA:
            raise ValueError("reference_spectra.json: schema is not %s" % REFERENCE_SCHEMA)
        self.dark = [float(value) for value in reference["dark"]]
        self.signals = {name: [float(value) for value in values] for name, values in reference["signals"].items()}
        for name, values in [("dark", self.dark)] + list(self.signals.items()):
            if len(values) != PIXELS:
                raise ValueError("reference_spectra.json: %s has %d values, not %d" % (name, len(values), PIXELS))
        self.background = str(sample["background"])
        self.regions = list(sample["regions"])
        for material in [self.background] + [region["material"] for region in self.regions]:
            if material not in self.signals:
                raise ValueError("reference_spectra.json: no signal for material %s" % material)
        for region in self.regions:
            if region["shape"] not in ("frame", "circle"):
                raise ValueError("sample.json: unknown region shape %s" % region["shape"])
        self.blend = {shape: float(width) for shape, width in sample["edge_blend_mm"].items()}
        self.z_focus_mm = float(sample["focus"]["z_focus_mm"])
        self.half_width_mm = float(sample["focus"]["half_width_mm"])
        if not self.half_width_mm > 0:
            raise ValueError("sample.json: focus.half_width_mm must be positive")
        self.power_scale = {level: float(scale) for level, scale in sample["power_scale"].items()}
        self.noise_counts_rms = float(sample["noise_counts_rms"])
        self.steps_per_mm = float(sample["steps_per_mm"])
        start = sample["start_position_mm"]
        self.start_position_mm = (float(start["x"]), float(start["y"]), float(start["z"]))

    @classmethod
    def load(cls, folder: str) -> "VirtualSample":
        with open(os.path.join(folder, SAMPLE_FILE), "r", encoding="utf-8") as handle:
            sample = json.load(handle)
        with open(os.path.join(folder, REFERENCE_FILE), "r", encoding="utf-8") as handle:
            reference = json.load(handle)
        return cls(sample, reference)

    def position_mm(self, stage: Optional[dict]) -> Tuple[float, float, float]:
        """Stage position from the content of the stage file, or the start position without one."""
        if not stage:
            return self.start_position_mm
        steps_per_mm = float(stage["steps_per_mm"])
        return (stage["x_steps"] / steps_per_mm, stage["y_steps"] / steps_per_mm, stage["z_steps"] / steps_per_mm)

    def region_weight(self, region: dict, x: float, y: float) -> float:
        blend = self.blend.get(region["shape"], 0.0)
        if region["shape"] == "frame":
            d = max(x, y)
            return _ramp(d - float(region["inner_mm"]), blend) * _ramp(float(region["outer_mm"]) - d, blend)
        dx, dy = x - float(region["center_mm"][0]), y - float(region["center_mm"][1])
        return _ramp(float(region["radius_mm"]) - math.sqrt(dx * dx + dy * dy), blend)

    def weights(self, x: float, y: float) -> Dict[str, float]:
        """Share of every material at (x, y); the shares add up to 1."""
        weights: Dict[str, float] = {}
        for region in self.regions:
            material = region["material"]
            weights[material] = max(weights.get(material, 0.0), self.region_weight(region, x, y))
        total = 0.0
        for weight in weights.values():      # a plain running sum, as the capture simulator adds them up
            total += weight
        if total > 1.0:
            weights = {material: weight / total for material, weight in weights.items()}
            total = 1.0
        weights[self.background] = weights.get(self.background, 0.0) + (1.0 - total)
        return weights

    def focus(self, z: float) -> float:
        defocus = (z - self.z_focus_mm) / self.half_width_mm
        return 1.0 / (1.0 + defocus * defocus)

    def expected(self, x: float, y: float, z: float, laser_on: bool = True, level: Optional[str] = "HIGH",
                 dark_capture: bool = False) -> List[float]:
        """The frame before noise, rounding and clipping. `level` is LOW, MEDIUM or HIGH; any
        other value (an LD power the controller never sends) gives no laser signal."""
        gain = 0.0
        if laser_on and not dark_capture:
            gain = self.power_scale.get(level or "", 0.0) * self.focus(z)
        if gain == 0.0:
            return list(self.dark)
        mix = [0.0] * PIXELS
        for material, weight in self.weights(x, y).items():
            if weight > 0.0:
                signal = self.signals[material]
                for index in range(PIXELS):
                    mix[index] += weight * signal[index]
        return [self.dark[index] + gain * mix[index] for index in range(PIXELS)]

    def frame(self, x: float, y: float, z: float, laser_on: bool = True, level: Optional[str] = "HIGH",
              dark_capture: bool = False, rng: Optional[random.Random] = None) -> List[int]:
        """A simulated frame: expected values plus Gaussian noise from `rng` (none without one),
        rounded and clipped to 0..MAX_COUNT."""
        values = self.expected(x, y, z, laser_on, level, dark_capture)
        if rng is not None and self.noise_counts_rms > 0:
            values = [value + rng.gauss(0.0, self.noise_counts_rms) for value in values]
        return clip_counts(values)


def clip_counts(values: Sequence[float]) -> List[int]:
    return [int(min(MAX_COUNT, max(0, round(value)))) for value in values]


def is_dark_capture(capture_id: str) -> bool:
    """The controller's Capture Dark uses ids that start with dark_ (the optical path is blocked)."""
    return capture_id.startswith(DARK_CAPTURE_PREFIX)
