#!/usr/bin/env python3
"""Rebuild the embedded data block of the Simulation Studio page.

studio/index.html carries its own copy of the virtual sample and of the reference spectra, so
that the page works when it is opened as a file or published on its own. This script rewrites
the text between the /*EMBED:START*/ and /*EMBED:END*/ comments from

    virtual_sample/sample.json             the virtual plate (data_layer/PROTOCOL.md section 4)
    virtual_sample/reference_spectra.json  one dark and one signal spectrum per material, 2048 values each
    laser_test/main.cpp                    the controller's Raman shift table (kRamanShift), for the axis

Every curve is reduced to 256 values by block maximum (the largest of each 8 neighbouring
pixels), the same reduction the Studio server applies in GET /api/sample. The Raman shift axis is
reduced by block mean, so that each embedded value sits in the middle of its block.

    python3 studio/embed_assets.py                 rewrite the block from the files above
    python3 studio/embed_assets.py --check         exit 1 when the block is out of date, write nothing
    python3 studio/embed_assets.py --placeholder   use synthetic curves when no reference file exists yet

Standard library only. Simulation data only: nothing here touches an instrument.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

START_MARK = "/*EMBED:START*/"
END_MARK = "/*EMBED:END*/"
EMBED_SCHEMA = "raman-studio-embed/1"
SAMPLE_SCHEMA = "raman-virtual-sample/1"
REFERENCE_SCHEMA = "raman-reference-spectra/1"
PIXELS = 2048
REDUCED = 256

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class EmbedError(Exception):
    """The inputs cannot be turned into an embedded block."""


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise EmbedError("%s does not exist" % path)
    except (OSError, ValueError) as error:
        raise EmbedError("%s cannot be read as JSON: %s" % (path, error))


def read_shift_table(path):
    """The 2048 Raman shift values (cm-1) of the controller's calibration table."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
    except OSError as error:
        raise EmbedError("%s cannot be read: %s" % (path, error))
    match = re.search(r"kRamanShift\s*\[\s*kNumPixels\s*\]\s*=\s*\{([^}]*)\}", source)
    if not match:
        raise EmbedError("%s holds no kRamanShift table" % path)
    values = [float(token) for token in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", match.group(1))]
    if len(values) != PIXELS:
        raise EmbedError("kRamanShift has %d values, not %d" % (len(values), PIXELS))
    return values


def block_max(values):
    size = len(values) // REDUCED
    return [max(values[index * size:(index + 1) * size]) for index in range(REDUCED)]


def block_mean(values):
    size = len(values) // REDUCED
    return [sum(values[index * size:(index + 1) * size]) / size for index in range(REDUCED)]


def check_sample(sample):
    if sample.get("schema") != SAMPLE_SCHEMA:
        raise EmbedError("sample.json: schema is not %s" % SAMPLE_SCHEMA)
    for key in ("travel_mm", "steps_per_mm", "start_position_mm", "focus", "background", "materials", "regions",
                "edge_blend_mm", "power_scale", "noise_counts_rms"):
        if key not in sample:
            raise EmbedError("sample.json: %s is missing" % key)
    materials = sample["materials"]
    for material in [sample["background"]] + [region.get("material") for region in sample["regions"]]:
        if material not in materials:
            raise EmbedError("sample.json: material %s has no entry under materials" % material)


def check_reference(reference, sample):
    if reference.get("schema") != REFERENCE_SCHEMA:
        raise EmbedError("reference_spectra.json: schema is not %s" % REFERENCE_SCHEMA)
    curves = [("dark", reference.get("dark"))]
    signals = reference.get("signals")
    if not isinstance(signals, dict):
        raise EmbedError("reference_spectra.json: signals is missing")
    for material in sample["materials"]:
        if material not in signals:
            raise EmbedError("reference_spectra.json: no signal for material %s" % material)
        curves.append((material, signals[material]))
    for name, values in curves:
        if not isinstance(values, list) or len(values) != PIXELS:
            raise EmbedError("reference_spectra.json: %s does not hold %d values" % (name, PIXELS))


def lorentz(shift, centre, half_width, height):
    return height / (1.0 + ((shift - centre) / half_width) ** 2)


def hump(shift, centre, width, height):
    return height * math.exp(-(((shift - centre) / width) ** 2))


# Band positions (cm-1) are the commonly reported strong Raman bands of each material; widths,
# heights and backgrounds are made up. Used only until measured reference spectra exist.
PLACEHOLDER_BANDS = {
    "r6g": {"bands": [(612, 6, 9000), (773, 6, 5200), (1127, 7, 2600), (1185, 7, 4300), (1311, 8, 7200),
                      (1363, 8, 11000), (1509, 8, 10000), (1575, 8, 5200), (1650, 8, 8200)],
            "humps": [(1700, 900, 5000)]},
    "methylene_blue": {"bands": [(449, 6, 6200), (502, 6, 3100), (772, 6, 2300), (1035, 7, 1800), (1154, 7, 2300),
                                 (1302, 8, 2700), (1394, 8, 6100), (1624, 8, 11500)],
                       "humps": [(1500, 800, 2500)]},
    "adenine": {"bands": [(623, 6, 1800), (723, 6, 9000), (1248, 8, 2200), (1333, 8, 5200), (1482, 8, 2700)],
                "humps": [(1200, 900, 800)]},
    "di_water": {"bands": [(1640, 60, 450)], "humps": [(3350, 220, 2600), (450, 250, 500)]},
}


def placeholder_reference(sample, shift):
    """Synthetic curves in the shape of reference_spectra.json, for use before the real file exists."""
    signals = {}
    for material in sample["materials"]:
        recipe = PLACEHOLDER_BANDS.get(material, {"bands": [(1000, 8, 3000)], "humps": [(1500, 900, 600)]})
        signals[material] = [
            sum(lorentz(value, *band) for band in recipe["bands"]) + sum(hump(value, *item) for item in recipe["humps"])
            for value in shift
        ]
    dark = [880.0 + 25.0 * math.sin(index / 310.0) + 6.0 * math.sin(index / 37.0) for index in range(PIXELS)]
    return {
        "schema": REFERENCE_SCHEMA,
        "source": "Placeholder curves synthesised by studio/embed_assets.py --placeholder. Not measured data.",
        "pixel_count": PIXELS,
        "dark": dark,
        "signals": signals,
    }


def build_embed(sample, reference, shift, shift_source, placeholder):
    return {
        "schema": EMBED_SCHEMA,
        "placeholder": bool(placeholder),
        "sample": sample,
        "reference": {
            "source": reference.get("source", ""),
            "pixel_count": PIXELS,
            "reduced_to": REDUCED,
            "reduction": "block maximum of %d neighbouring pixels" % (PIXELS // REDUCED),
            "raman_shift_source": shift_source,
            "raman_shift": [round(value, 1) for value in block_mean(shift)],
            "dark": [int(round(value)) for value in block_max(reference["dark"])],
            "signals": {
                material: [int(round(value)) for value in block_max(reference["signals"][material])]
                for material in sample["materials"]
            },
        },
    }


def script_safe(text):
    """JSON text that can sit inside a <script> element: no "</" and no raw line separators."""
    return text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def dump(value):
    return script_safe(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def render_block(embed):
    """One top-level key per line, so that a change to one curve is one changed line."""
    lines = ["globalThis.STUDIO_EMBED = {"]
    keys = list(embed)
    for position, key in enumerate(keys):
        comma = "," if position < len(keys) - 1 else ""
        if key == "reference":
            reference = embed[key]
            lines.append('"reference":{')
            inner = [name for name in reference if name != "signals"]
            for name in inner:
                lines.append("%s:%s," % (json.dumps(name), dump(reference[name])))
            lines.append('"signals":{')
            materials = list(reference["signals"])
            for index, material in enumerate(materials):
                tail = "," if index < len(materials) - 1 else ""
                lines.append("%s:%s%s" % (json.dumps(material), dump(reference["signals"][material]), tail))
            lines.append("}}" + comma)
        else:
            lines.append("%s:%s%s" % (json.dumps(key), dump(embed[key]), comma))
    lines.append("};")
    return "\n".join(lines)


def replace_block(page, block):
    if page.count(START_MARK) != 1 or page.count(END_MARK) != 1:
        raise EmbedError("the page must hold exactly one %s and one %s" % (START_MARK, END_MARK))
    start = page.index(START_MARK) + len(START_MARK)
    end = page.index(END_MARK)
    if end < start:
        raise EmbedError("%s comes before %s" % (END_MARK, START_MARK))
    return page[:start] + "\n" + block + "\n" + page[end:]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rebuild the embedded data block of studio/index.html.")
    parser.add_argument("--page", default=os.path.join(HERE, "index.html"))
    parser.add_argument("--sample", default=os.path.join(ROOT, "virtual_sample", "sample.json"))
    parser.add_argument("--reference", default=os.path.join(ROOT, "virtual_sample", "reference_spectra.json"))
    parser.add_argument("--shift-source", default=os.path.join(ROOT, "laser_test", "main.cpp"),
                        help="C++ source holding the kRamanShift table")
    parser.add_argument("--placeholder", action="store_true",
                        help="embed synthetic curves instead of reference_spectra.json; the page says so")
    parser.add_argument("--check", action="store_true", help="only report whether the block is up to date")
    options = parser.parse_args(argv)

    try:
        sample = read_json(options.sample)
        check_sample(sample)
        shift = read_shift_table(options.shift_source)
        if options.placeholder:
            reference = placeholder_reference(sample, shift)
        else:
            if not os.path.exists(options.reference):
                raise EmbedError("%s does not exist yet. Run again when it does, or pass --placeholder."
                                 % options.reference)
            reference = read_json(options.reference)
        check_reference(reference, sample)
        embed = build_embed(sample, reference, shift, "kRamanShift in laser_test/main.cpp", options.placeholder)
        block = render_block(embed)
        with open(options.page, "r", encoding="utf-8") as handle:
            page = handle.read()
        updated = replace_block(page, block)
    except EmbedError as error:
        print("embed_assets: %s" % error, file=sys.stderr)
        return 2
    except OSError as error:
        print("embed_assets: %s" % error, file=sys.stderr)
        return 2

    kind = "placeholder curves" if options.placeholder else "reference_spectra.json"
    summary = "%d materials, %d values per curve, %d bytes, from %s" % (
        len(sample["materials"]), REDUCED, len(block.encode("utf-8")), kind)
    if options.check:
        if updated == page:
            print("embed_assets: the block is up to date (%s)" % summary)
            return 0
        print("embed_assets: the block is out of date; run without --check to rewrite it", file=sys.stderr)
        return 1
    if updated == page:
        print("embed_assets: nothing to change (%s)" % summary)
        return 0
    temporary = options.page + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(updated)
    os.replace(temporary, options.page)
    print("embed_assets: block rewritten (%s)" % summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
