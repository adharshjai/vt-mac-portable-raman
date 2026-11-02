"""Read-only spectral comparison; no training or prediction changes."""
from pathlib import Path
import ast, json, re
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

BASE = Path(__file__).resolve().parent / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug'
PROJECT = BASE / 'pure_only_20260923'
DATA = PROJECT / 'data_r6g_20260922'
OUT = PROJECT / 'scan_spectral_review_20260924'
OUT.mkdir(exist_ok=True)
source = (PROJECT / 'CNN3_1_pure_only.py').read_text(encoding='utf-8')
names = {'strip_prefix', 'parse_portable_log_to_spectra', 'moving_average', 'rolling_minimum',
         'baseline_correction_fast', 'normalize_min_max', 'preprocess_two_versions'}
nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
ns = dict(np=np, re=re, TARGET_LEN=2048, DROP_N=90, DO_BASELINE=True,
          BASELINE_WINDOW=15, BASELINE_SMOOTH=15, ERROR_PHRASE='image data error code 0x0001')
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'preprocessing', 'exec'), ns)
scan = BASE / 'scan_sessions/1d_y_scan_20260921_152840_289/laser_log_combined.txt'
darkpath = BASE / 'dark_measurements/dark_20260921_153016_700.txt'
dark = np.loadtxt(darkpath, skiprows=4)
raw, failed = ns['parse_portable_log_to_spectra'](scan)
assert len(raw) == 13 and failed == 0
axis = dark[90:, 1]
mask = (axis >= 300) & (axis <= 1800)
references = {}
for name in ['r6g', 'methylene_blue', 'adenine', 'di_water']:
    a = np.load(DATA / f'raman_shift_axis_{name}.npy')
    spectra = np.load(DATA / f'training_spectra_{name}_minmax.npy')
    references[name] = np.interp(axis, a, np.median(spectra, axis=0))
old_axis = np.load(BASE / 'data_base_code_part/raman_shift_axis_r6g.npy')
references['old_r6g'] = np.interp(axis, old_axis, np.median(np.load(BASE / 'data_base_code_part/training_spectra_r6g_minmax.npy'), axis=0))
plt.rcParams.update({'font.size': 9, 'axes.spines.top': False, 'axes.spines.right': False})
fig_grid, grid = plt.subplots(5, 3, figsize=(15, 17))
report = []
for i, s in enumerate(raw):
    bl, mm, _ = ns['preprocess_two_versions'](s, dark[:, 2])
    meta = json.loads((scan.parent / f'point_{i+1:04d}.json').read_text())
    assert np.array_equal(s, meta['intensity'])
    y = meta['controller_y_steps'] / 34700
    correlations = {name: float(np.corrcoef(mm[mask], ref[mask])[0, 1]) for name, ref in references.items()}
    noise = np.median(np.abs(np.diff(bl[mask]) - np.median(np.diff(bl[mask])))) / (0.67449*np.sqrt(2))
    record = dict(point=i+1, y_mm=y, corrected_max=float(bl[mask].max()),
                  noise_estimate=float(noise), max_to_noise=float(bl[mask].max()/max(noise, 1e-12)),
                  correlations=correlations)
    report.append(record)
    f, axs = plt.subplots(3, 1, figsize=(11, 9))
    axs[0].plot(axis, (s-dark[:,2])[90:], color='#555555', lw=.8)
    axs[0].set_ylabel('Dark-subtracted counts'); axs[0].set_title(f'Point {i+1:02d} | Y={y:.3f} mm | raw minus dark')
    axs[1].plot(axis, bl, color='#146a93', lw=.8)
    axs[1].set_ylabel('Baseline-corrected counts'); axs[1].set_title('Existing baseline processing; no normalization')
    axs[2].plot(axis, mm, color='black', lw=1.1, label='Measured')
    for name, color in [('r6g', '#d95f02'), ('methylene_blue', '#008c85'), ('di_water', '#8d77b3')]:
        axs[2].plot(axis, references[name], color=color, lw=1, alpha=.8, label=f'Database {name}')
    axs[2].set_ylabel('Min-max normalized'); axs[2].legend(ncol=4, fontsize=8)
    for ax in axs:
        ax.set_xlim(300,1800); ax.grid(alpha=.18); ax.set_xlabel('Raman shift (cm$^{-1}$)')
    f.tight_layout(); f.savefig(OUT / f'point_{i+1:02d}.png', dpi=160); plt.close(f)
    g = grid.flat[i]
    g.plot(axis, bl, color='#146a93', lw=.8)
    g.set_xlim(300,1800); g.set_title(f'Point {i+1:02d} | Y={y:.1f} mm')
    g.set_xlabel('Raman shift (cm$^{-1}$)'); g.set_ylabel('Corrected counts'); g.grid(alpha=.15)
for ax in list(grid.flat)[len(raw):]:
    ax.axis('off')
fig_grid.suptitle('All 13 spectra: existing dark + baseline correction\nIndividual y scales; inspect peak shape AND amplitude (no model labels)', fontsize=16)
fig_grid.tight_layout(rect=(0,0,1,.96)); fig_grid.savefig(OUT/'all_13_corrected_spectra.png', dpi=160); plt.close(fig_grid)
f, axs = plt.subplots(3,1, figsize=(12,10))
for ax, points, title in zip(axs, [[1,2,6,7],[4,5,9,10],[3,8,11,12,13]],
                            ['Points 1, 2, 6, 7', 'Points 4, 5, 9, 10', 'Points 3, 8, 11, 12, 13']):
    for point in points:
        bl, mm, _ = ns['preprocess_two_versions'](raw[point-1], dark[:,2])
        ax.plot(axis, bl, lw=.8, label=f'Point {point}')
    ax.set_xlim(300,1800); ax.set_title(title); ax.legend(); ax.set_ylabel('Corrected counts'); ax.grid(alpha=.15)
axs[-1].set_xlabel('Raman shift (cm$^{-1}$)'); f.tight_layout(); f.savefig(OUT/'three_groups_absolute_counts.png', dpi=180); plt.close(f)
(OUT/'spectral_review.json').write_text(json.dumps(dict(scan=str(scan), dark=str(darkpath),
    note='Correlations to database medians, 300-1800 cm-1, no shift fitting; not chemical ground truth. Noise estimate is descriptive.', points=report), indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
print(OUT)
