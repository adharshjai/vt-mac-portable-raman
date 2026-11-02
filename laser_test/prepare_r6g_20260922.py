"""Prepare the user-confirmed pure R6G scan using the existing preprocessing."""
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import numpy as np

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug'
PROJECT = BASE / 'pure_only_20260923'
OLD = PROJECT / 'data'
NEW = PROJECT / 'data_r6g_20260922'
SCAN = BASE / 'scan_sessions/2d_scan_20260922_164646_611'
DARK = BASE / 'dark_measurements/dark_20260922_165331_762.txt'
if NEW.exists():
    raise FileExistsError(f'Refusing to overwrite {NEW}')

# Load functions only; do not execute training or inference code.
source_path = PROJECT / 'CNN3_1_pure_only.py'
source = source_path.read_text(encoding='utf-8-sig')
names = {'moving_average', 'rolling_minimum', 'baseline_correction_fast',
         'normalize_min_max', 'preprocess_two_versions'}
nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
assert len(nodes) == len(names)
ns = {'np': np, 'TARGET_LEN': 2048, 'DROP_N': 90, 'DO_BASELINE': True,
      'BASELINE_WINDOW': 15, 'BASELINE_SMOOTH': 15}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'existing_preprocessing', 'exec'), ns)
dark_table = np.loadtxt(DARK, skiprows=4)
assert dark_table.shape == (2048, 3) and np.isfinite(dark_table).all()
dark = dark_table[:, 2]
records = []
baseline_rows, minmax_rows, accepted = [], [], []
combined_lines = [line for line in (SCAN / 'laser_log_combined.txt').read_text().splitlines() if line.startswith('Image Data')]
files = sorted(SCAN.glob('point_*.json'))
assert len(files) == len(combined_lines) == 124
for index, file in enumerate(files):
    record = json.loads(file.read_text())
    raw = np.asarray(record['intensity'], dtype=np.float64)
    assert record['point_id'] == index + 1 and record['status'] == 'OK'
    assert raw.shape == (2048,) and np.isfinite(raw).all()
    txt_raw = np.array([int(v, 16) for v in combined_lines[index][len('Image Data'):].split()])
    assert np.array_equal(raw, txt_raw)
    bl, mm, quality = ns['preprocess_two_versions'](raw, dark)
    reasons = []
    saturated = int(np.count_nonzero(raw >= 65535))
    if saturated:
        reasons.append('ADC saturation before dark subtraction')
    if quality['ptp'] < 100 or quality['std'] < 15:
        reasons.append('Existing raw-dark low-signal gate')
    if not np.isfinite(bl).all() or np.ptp(bl) < 1e-12:
        reasons.append('Invalid or constant processed spectrum')
    info = {'point_id': index + 1, 'phase': 'Z' if index < 4 else 'XY',
            'source': str(file), 'accepted': not reasons, 'reasons': reasons,
            'full_scale_pixels': saturated, 'raw_max': float(raw.max()),
            'dark_subtracted_ptp': quality['ptp'], 'dark_subtracted_std': quality['std'],
            'baseline_corrected_max': float(bl.max()),
            'baseline_corrected_median': float(np.median(bl)),
            'xyz_steps': [record[f'controller_{a}_steps'] for a in 'xyz']}
    records.append(info)
    if not reasons:
        info['training_row'] = len(accepted)
        accepted.append(index + 1)
        baseline_rows.append(bl)
        minmax_rows.append(mm)
baseline_rows = np.asarray(baseline_rows, dtype=np.float32)
minmax_rows = np.asarray(minmax_rows, dtype=np.float32)
assert baseline_rows.shape == minmax_rows.shape == (119, 1958)
assert [r['point_id'] for r in records if not r['accepted']] == [3, 4, 56, 57, 110]

NEW.mkdir()
for f in OLD.glob('*.npy'):
    shutil.copy2(f, NEW / f.name)
np.save(NEW / 'training_spectra_r6g_baseline_raw.npy', baseline_rows)
np.save(NEW / 'training_spectra_r6g_minmax.npy', minmax_rows)
np.save(NEW / 'raman_shift_axis_r6g.npy', dark_table[90:, 1])

manifest = json.loads((OLD / 'manifest.json').read_text())
manifest['files'] = []
manifest['split'] = {'train': [], 'val': [], 'test': []}
manifest['split_warning'] = 'Spectrum-level split only. New R6G rows share ONE scan; no independent scan/batch validation.'
manifest['r6g_source'] = {'scan': str(SCAN), 'dark': str(DARK), 'label_basis': 'User confirmed entire scan is pure R6G',
                        'accepted_point_ids': accepted, 'excluded_point_ids': [3, 4, 56, 57, 110],
                        'quality_scope': 'Integrity, saturation, existing low-signal checks only; not confirmed diagnostic peak quality.'}
rng = np.random.RandomState(42)
offset = 0
counts = {}
for name in manifest['classes']:
    a = np.load(NEW / f'training_spectra_{name}_minmax.npy')
    counts[name] = len(a)
    ids = rng.permutation(len(a)) + offset
    ntrain, nval = int(.7 * len(a)), int(.1 * len(a))
    for key, values in [('train', ids[:ntrain]), ('val', ids[ntrain:ntrain+nval]), ('test', ids[ntrain+nval:])]:
        manifest['split'][key].extend(values.tolist())
    offset += len(a)
    for fname in [f'training_spectra_{name}_minmax.npy', f'training_spectra_{name}_baseline_raw.npy', f'raman_shift_axis_{name}.npy']:
        f = NEW / fname
        manifest['files'].append({'name': fname, 'source': str(SCAN if name == 'r6g' else OLD / fname),
                                  'sha256': hashlib.sha256(f.read_bytes()).hexdigest()})
manifest['total_real_spectra'] = offset
manifest['class_counts'] = counts
(NEW / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')

# Characterize peak suppression on noiseless isolated test peaks, not real labels.
attenuation = {}
x = np.arange(501) - 250
for fwhm in [5, 10, 15, 30]:
    peak = 1000 * np.exp(-.5 * (x / (fwhm / 2.35482)) ** 2)
    corrected = ns['baseline_correction_fast'](peak + 4000, 15, 15)
    attenuation[str(fwhm)] = float(corrected[250] / 1000)
report = {'scan': str(SCAN), 'dark': str(DARK), 'dark_sha256': hashlib.sha256(DARK.read_bytes()).hexdigest(),
          'preprocessing': 'Existing CNN functions: raw-dark, drop 90, rolling minimum 15, average baseline 15, min-max',
          'preprocessing_source_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
          'accepted_count': len(accepted), 'points': records,
          'isolated_gaussian_peak_retention_by_fwhm_pixels': attenuation,
          'warning': '119 quality-screened candidates, not independently verified peak-quality labels. Do not claim independent test accuracy.'}
(NEW / 'r6g_quality_report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')

# Preserve each existing script/notebook independently, including user edits.
backup = PROJECT / ('backup_before_r6g_update_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
backup.mkdir()
for stem in ['CNN3_1_pure_only', 'Transformer5_1_pure_only']:
    output = PROJECT / (stem + '_r6g20260922_results')
    for suffix in ['.py', '.ipynb']:
        f = PROJECT / (stem + suffix)
        shutil.copy2(f, backup / f.name)
        def update_code(text):
            text, n = re.subn(r'^DATA_DIR = .+$', lambda _: f'DATA_DIR = {str(NEW)!r}', text, flags=re.M)
            assert n == 1
            text, n = re.subn(r'^OUTPUT_DIR = .+$', lambda _: f'OUTPUT_DIR = {str(output)!r}', text, flags=re.M)
            assert n == 1
            return text
        if suffix == '.py':
            changed = update_code(f.read_text(encoding='utf-8-sig'))
            ast.parse(changed)
            f.write_text(changed, encoding='utf-8')
        else:
            nb = json.loads(f.read_text(encoding='utf-8-sig'))
            for cell in nb['cells']:
                text = ''.join(cell['source'])
                if cell['cell_type'] == 'code' and re.search(r'^DATA_DIR = ', text, re.M):
                    cell['source'] = update_code(text).splitlines(keepends=True)
                    cell['outputs'] = []
                    cell['execution_count'] = None
                elif cell['cell_type'] == 'markdown':
                    cell['source'] = text.replace('456 real spectra', '462 real spectra (119 new R6G)').splitlines(keepends=True)
            f.write_text(json.dumps(nb, indent=1), encoding='utf-8')
print('Dataset:', NEW)
print('Accepted R6G:', len(accepted), 'Rejected:', manifest['r6g_source']['excluded_point_ids'])
print('Class counts:', counts, 'Split:', {k: len(v) for k,v in manifest['split'].items()})
print('Gaussian peak retention:', attenuation)
print('Old code backup:', backup)
