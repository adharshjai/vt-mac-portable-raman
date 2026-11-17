#!/usr/bin/env python3
"""Build reference_spectra.json for the virtual sample from measured spectra.

Simulation only: the result feeds the two simulators (data_layer/PROTOCOL.md section 4) and is
never used with real instruments.

For every material the script takes the measured raw spectra and the dark spectrum that was
measured with them, and writes

    signal[material] = mean(raw spectra) - dark of that material
    dark             = mean of the dark spectra of all materials

each with 2048 values rounded to one decimal. Spectra that are incomplete (not 2048 values) or
that touch the ADC's full scale (65535) are left out; the classifier's training set was screened
the same way.

Usage (paths are relative to --root; only these relative paths are recorded in the output):

    python build_reference_spectra.py --root <hand-off folder> \
        --material r6g            <spectra> <dark> \
        --material methylene_blue <spectra> <dark> \
        --material adenine        <spectra> <dark> \
        --material di_water       <spectra> <dark> \
        --output reference_spectra.json

<spectra> is either a text log with "Image Data <hex> <hex> ..." rows (Database_<class>.txt,
laser_log_combined.txt) or a scan-session folder with point_NNNN.json files. <dark> is a dark
measurement table (index <tab> raman shift <tab> intensity). The role of a file is given by its
position on the command line, not by its name. README.md lists the sources of the shipped file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

SCHEMA = "raman-reference-spectra/1"
PIXELS = 2048
FULL_SCALE = 65535
IMAGE_DATA = "Image Data"


class SourceError(Exception):
    """A source file does not hold what its position on the command line says."""


def read_log_spectra(path: Path) -> Tuple[List[List[int]], int]:
    """Rows of a controller log: "[time] [Backend] Image Data 1335 152F ..." (hex counts).
    Returns (complete spectra, number of incomplete or unreadable rows)."""
    spectra, incomplete = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        start = line.find(IMAGE_DATA)
        if start < 0:
            continue
        try:
            values = [int(token, 16) for token in line[start + len(IMAGE_DATA):].split()]
        except ValueError:
            incomplete += 1
            continue
        if len(values) == PIXELS:
            spectra.append(values)
        else:
            incomplete += 1
    return spectra, incomplete


def read_session_spectra(folder: Path) -> Tuple[List[List[int]], int]:
    """point_NNNN.json files of a scan session, as written by the controller."""
    spectra, incomplete = [], 0
    for file in sorted(folder.glob("point_*.json")):
        record = json.loads(file.read_text(encoding="utf-8-sig"))
        values = record.get("intensity")
        if record.get("status") == "OK" and isinstance(values, list) and len(values) == PIXELS:
            spectra.append([int(value) for value in values])
        else:
            incomplete += 1
    return spectra, incomplete


def read_dark(path: Path) -> np.ndarray:
    """Dark measurement table: header lines, then index <tab> raman shift <tab> intensity."""
    values = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        columns = line.split("\t")
        if len(columns) >= 3:
            try:
                values.append(float(columns[2]))
            except ValueError:
                pass
    dark = np.asarray(values, dtype=np.float64)
    if dark.shape != (PIXELS,) or not np.isfinite(dark).all():
        raise SourceError("%s is not a %d-pixel dark measurement table" % (path.name, PIXELS))
    return dark


def sha256_of(path: Path) -> str:
    """SHA-256 of a file, or of the point files of a session folder in name order."""
    digest = hashlib.sha256()
    for file in (sorted(path.glob("point_*.json")) if path.is_dir() else [path]):
        digest.update(file.read_bytes())
    return digest.hexdigest()


def recorded_name(argument: str) -> str:
    """What the output says about a source: the path as typed when it is relative to --root,
    otherwise only the file name, so that no machine-specific folder ends up in the file."""
    path = Path(argument)
    return path.name if path.is_absolute() else path.as_posix()


def material_reference(root: Path, spectra_arg: str, dark_arg: str) -> Tuple[np.ndarray, np.ndarray, Dict]:
    spectra_path, dark_path = root / spectra_arg, root / dark_arg
    if spectra_path.is_dir():
        spectra, incomplete = read_session_spectra(spectra_path)
    else:
        spectra, incomplete = read_log_spectra(spectra_path)
    if not spectra:
        raise SourceError("%s holds no complete %d-pixel spectrum" % (spectra_path.name, PIXELS))
    dark = read_dark(dark_path)
    raw = np.asarray(spectra, dtype=np.float64)
    usable = (raw < FULL_SCALE).all(axis=1)
    if not usable.any():
        raise SourceError("every spectrum in %s is saturated" % spectra_path.name)
    signal = raw[usable].mean(axis=0) - dark
    details = {
        "spectra": recorded_name(spectra_arg), "spectra_sha256": sha256_of(spectra_path),
        "dark": recorded_name(dark_arg), "dark_sha256": sha256_of(dark_path),
        "spectra_used": int(usable.sum()),
        "skipped_incomplete": int(incomplete),
        "skipped_saturated": int((~usable).sum()),
    }
    return signal, dark, details


def rounded(values: np.ndarray) -> List[float]:
    return [round(float(value), 1) for value in values]


def build(root: Path, materials: List[Tuple[str, str, str]]) -> Dict:
    signals, darks, sources = {}, [], {}
    for name, spectra_arg, dark_arg in materials:
        if name in signals:
            raise SourceError("material %s is given twice" % name)
        signal, dark, details = material_reference(root, spectra_arg, dark_arg)
        signals[name] = rounded(signal)
        darks.append(dark)
        sources[name] = details
    return {
        "schema": SCHEMA,
        "source": "Measured spectra (simulation reference only). Per material: mean of the complete, "
                  "unsaturated raw spectra minus the dark measured with them. dark: mean of those dark "
                  "measurements. Built by build_reference_spectra.py; inputs are listed under sources.",
        "sources": sources,
        "pixel_count": PIXELS,
        "dark": rounded(np.mean(darks, axis=0)),
        "signals": signals,
    }


def to_text(reference: Dict) -> str:
    """One spectrum per line: the file stays readable in a diff and loads with any JSON parser."""
    def row(values: List[float]) -> str:
        return "[" + ", ".join(repr(value) for value in values) + "]"

    lines = ["{", '  "schema": %s,' % json.dumps(reference["schema"]),
             '  "source": %s,' % json.dumps(reference["source"]), '  "sources": {']
    names = list(reference["sources"])
    for index, name in enumerate(names):
        lines.append("    %s: %s%s" % (json.dumps(name), json.dumps(reference["sources"][name]),
                                      "," if index + 1 < len(names) else ""))
    lines += ["  },", '  "pixel_count": %d,' % reference["pixel_count"],
              '  "dark": %s,' % row(reference["dark"]), '  "signals": {']
    for index, name in enumerate(names):
        lines.append("    %s: %s%s" % (json.dumps(name), row(reference["signals"][name]),
                                      "," if index + 1 < len(names) else ""))
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Build reference_spectra.json for the virtual sample (simulation only).")
    parser.add_argument("--root", default=".", help="folder the source paths are relative to")
    parser.add_argument("--material", nargs=3, action="append", required=True, metavar=("NAME", "SPECTRA", "DARK"),
                        help="material name, its spectra (log file or scan-session folder) and its dark table")
    parser.add_argument("--output", required=True, help="file to write")
    args = parser.parse_args(argv)
    try:
        reference = build(Path(args.root), [tuple(entry) for entry in args.material])
    except (SourceError, OSError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    output = Path(args.output)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(to_text(reference), encoding="utf-8")
    temporary.replace(output)
    for name, details in reference["sources"].items():
        print("%-16s %4d spectra used, %d incomplete and %d saturated left out  (%s - %s)" % (
            name, details["spectra_used"], details["skipped_incomplete"], details["skipped_saturated"],
            details["spectra"], details["dark"]))
    print("wrote %s" % output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
