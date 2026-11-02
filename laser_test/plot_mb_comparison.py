"""Read-only spectral comparison; no model or source-data changes."""
from pathlib import Path
import ast
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
B = ROOT / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug'
P = B / 'pure_only_20260923'
OUT = P / 'mb_comparison_20260923'
OUT.mkdir(exist_ok=True)
SCAN = B / 'scan_sessions/1d_y_scan_20260921_152840_289'
DARK = B / 'dark_measurements/dark_20260921_153016_700.txt'
data = P / 'data_r6g_20260922'
train = np.load(data / 'training_spectra_methylene_blue_minmax.npy')
train_raw = np.load(data / 'training_spectra_methylene_blue_baseline_raw.npy')
axis = np.load(data / 'raman_shift_axis_methylene_blue.npy')
table = np.loadtxt(DARK, skiprows=4)
x = table[90:, 1]
source = (P / 'CNN3_1_pure_only.py').read_text(encoding='utf-8')
names = {'moving_average', 'rolling_minimum', 'baseline_correction_fast', 'normalize_min_max', 'preprocess_two_versions'}
nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
ns = {'np': np, 'TARGET_LEN': 2048, 'DROP_N': 90, 'DO_BASELINE': True, 'BASELINE_WINDOW': 15, 'BASELINE_SMOOTH': 15}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'preprocessing_only', 'exec'), ns)
raw = np.array([[int(v, 16) for v in line[len('Image Data'):].split()] for line in
                (SCAN / 'laser_log_combined.txt').read_text(encoding='utf-8-sig').splitlines() if line.startswith('Image Data')])
assert raw.shape == (13, 2048)
processed = [ns['preprocess_two_versions'](r, table[:, 2]) for r in raw]
bl = np.array([t[0] for t in processed])
mm = np.array([t[1] for t in processed])
ids = [4, 5, 9, 10]
colors = ['#1676ae', '#dc6b26', '#25926d', '#8951a5']
plt.rcParams.update({'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False, 'figure.dpi': 120})
lo, med, hi = np.percentile(train, [10, 50, 90], axis=0)
def save(fig, name):
    fig.savefig(OUT / name, dpi=180, bbox_inches='tight')
    plt.close(fig)

fig, axs = plt.subplots(2, 2, figsize=(13, 7.6), sharex=True, sharey=True)
for ax, point, color in zip(axs.flat, ids, colors):
    ax.fill_between(axis, lo, hi, color='0.7', alpha=.45, label='Training MB: 10-90% range')
    ax.plot(axis, med, color='black', lw=1.3, label='Training MB: median (118 spectra)')
    ax.plot(x, mm[point-1], color=color, lw=1.1, label=f'Scan point {point} (MB candidate)')
    ax.set_title(f'Point {point} | Y={(point-1)*2} mm')
    ax.set_xlim(307, 1800); ax.set_ylim(-.04, 1.06)
    ax.grid(alpha=.15)
axs[0,0].legend(fontsize=8, loc='upper right')
fig.supxlabel('Raman shift (cm$^{-1}$)'); fig.supylabel('Min-max normalized intensity')
fig.suptitle('Measured candidates vs. training MB | identical existing preprocessing', y=.995)
fig.text(.5, .01, 'Candidates selected from reported model responses, not independent chemical labels. Display range: 307-1800 cm$^{-1}$.', ha='center', fontsize=9)
fig.tight_layout(rect=(.02,.035,1,.965)); save(fig, '01_mb_normalized_comparison.png')

fig, axs = plt.subplots(2, 2, figsize=(13, 7.5))
for ax, point, color in zip(axs.flat, ids, colors):
    rd = (raw[point-1] - table[:,2])[90:]
    ax.plot(x, rd, color='0.35', lw=1, label='After dark subtraction')
    ax.plot(x, rd-bl[point-1], color='#d99320', lw=1, label='Estimated baseline (window 15)')
    ax.plot(x, bl[point-1], color=color, lw=1, label='After baseline subtraction')
    ax.set_title(f'Point {point} | raw max={raw[point-1].max():.0f}; no full-scale clipping')
    ax.set_xlim(307,3235); ax.grid(alpha=.15)
axs[0,0].legend(fontsize=8)
fig.supxlabel('Raman shift (cm$^{-1}$)'); fig.supylabel('Intensity (counts; axes autoscaled per panel)')
fig.suptitle('What the current baseline correction removes', y=.99)
fig.tight_layout(rect=(.02,.02,1,.96)); save(fig, '02_mb_background_correction.png')

fig, axs = plt.subplots(1, 3, figsize=(14,4.5))
windows = [(405,475),(1360,1430),(1580,1660)]
peaks = []
for ax, (left,right) in zip(axs, windows):
    ax.fill_between(axis, lo, hi, color='0.7', alpha=.45)
    ax.plot(axis, med, color='black', lw=2, label='Training MB median')
    mask = (axis>=left)&(axis<=right)
    ref_peak = float(axis[np.flatnonzero(mask)[np.argmax(med[mask])]])
    ax.axvline(ref_peak, color='black', lw=.7, ls=':')
    ax.set_title(f'{left}-{right} cm$^{{-1}}$ | reference max {ref_peak:.1f}')
    row = {'window':[left,right], 'training_median_max_cm1':ref_peak, 'candidates':[]}
    for point,color in zip(ids,colors):
        ax.plot(x,mm[point-1],color=color,lw=1.3,label=f'Point {point}')
        mask2=(x>=left)&(x<=right)
        peak=float(x[np.flatnonzero(mask2)[np.argmax(mm[point-1,mask2])]])
        row['candidates'].append({'point_id':point,'window_max_cm1':peak})
    peaks.append(row)
    ax.set_xlim(left,right); ax.set_ylim(-.03,1.03); ax.grid(alpha=.15)
axs[0].legend(fontsize=8)
fig.supxlabel('Raman shift (cm$^{-1}$)'); fig.supylabel('Global min-max normalized intensity')
fig.suptitle('Peak positions and relative heights | no peak alignment applied',y=1.02)
fig.tight_layout(); save(fig,'03_mb_peak_windows.png')

def unit(y):
    a=y-y.mean(axis=-1,keepdims=True)
    return a/np.maximum(np.linalg.norm(a,axis=-1,keepdims=True),1e-12)
corr=unit(mm)@unit(train).T
report={'source':str(SCAN),'dark':str(DARK),'candidate_point_ids':ids,
        'no_concentrations_provided':True,'peak_windows':peaks,
        'max_training_MB_correlation':{str(i):float(corr[i-1].max()) for i in ids},
        'training_baseline_peak_median':float(np.median(train_raw.max(axis=1))),
        'candidate_baseline_peak_counts':{str(i):float(bl[i-1].max()) for i in ids}}
(OUT/'comparison_notes.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
print('Plots:',OUT)
