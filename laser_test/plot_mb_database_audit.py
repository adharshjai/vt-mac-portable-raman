"""Plot stored MB spectra and measured comparison spectra, without training."""
from pathlib import Path
import ast
import json
import re
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.signal import find_peaks

BASE = Path(__file__).resolve().parent / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug'
PROJECT = BASE / 'pure_only_20260923'
DATA = PROJECT / 'data_r6g_20260922'
OUT = PROJECT / 'mb_database_visual_check'
OUT.mkdir(exist_ok=True)
raw = np.load(DATA / 'training_spectra_methylene_blue_baseline_raw.npy')
mm = np.load(DATA / 'training_spectra_methylene_blue_minmax.npy')
axis = np.load(DATA / 'raman_shift_axis_methylene_blue.npy')
source = (PROJECT / 'CNN3_1_pure_only.py').read_text(encoding='utf-8')
names = {'strip_prefix', 'parse_portable_log_to_spectra', 'moving_average', 'rolling_minimum',
         'baseline_correction_fast', 'normalize_min_max', 'preprocess_two_versions'}
nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
ns = {'np': np, 're': re, 'TARGET_LEN': 2048, 'DROP_N': 90, 'DO_BASELINE': True,
      'BASELINE_WINDOW': 15, 'BASELINE_SMOOTH': 15, 'ERROR_PHRASE': 'image data error code 0x0001'}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'preprocessing_only', 'exec'), ns)
scan = BASE / 'scan_sessions/1d_y_scan_20260921_152840_289/laser_log_combined.txt'
dark_file = BASE / 'dark_measurements/dark_20260921_153016_700.txt'
dark_table = np.loadtxt(dark_file, skiprows=4)
spectra, failed = ns['parse_portable_log_to_spectra'](scan)
assert len(spectra) == 13 and failed == 0
processed = [ns['preprocess_two_versions'](s, dark_table[:,2]) for s in spectra]
measured = np.array([p[1] for p in processed])
measured_axis = dark_table[90:, 1]
median = np.median(mm, axis=0)
lo, hi = np.percentile(mm, [10, 90], axis=0)
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'axes.spines.top': False,
                     'axes.spines.right': False, 'figure.dpi': 130, 'savefig.dpi': 180})

# Deterministic amplitude-spanning examples; amplitude is NOT concentration.
order = np.argsort(raw.max(axis=1))
selected = order[np.rint(np.linspace(0, len(order)-1, 6)).astype(int)]
fig, axes = plt.subplots(2, 3, figsize=(13, 7), sharex=True)
for ax, row in zip(axes.flat, selected):
    ax.plot(axis, raw[row], lw=1, color='#126e82')
    ax.set_title(f'Database row {row+1} | maximum {raw[row].max():.0f}')
    ax.set_xlim(300, 1800)
    ax.grid(alpha=.15)
    ax.set_ylabel('Baseline-corrected intensity (a.u.)')
    ax.set_xlabel('Raman shift (cm$^{-1}$)')
fig.suptitle('MB training database: six measured spectra out of 118', fontsize=15)
fig.text(.5, .012, 'Rows selected across maximum-intensity ranks; not concentration groups. Already dark/baseline corrected; not raw detector counts. View: 300–1800 cm⁻¹.', ha='center', fontsize=9)
fig.tight_layout(rect=(0,.045,1,.95))
fig.savefig(OUT / '01_mb_database_six_spectra.png'); plt.close(fig)

fig, axes = plt.subplots(2, 1, figsize=(12, 8))
for row in selected:
    axes[0].plot(axis, mm[row], lw=.9, alpha=.65, label=f'Row {row+1}')
axes[0].plot(axis, median, color='black', lw=1.5, label='Database median')
axes[0].set_title('Database: min–max normalization removes simple amplitude scaling')
axes[0].legend(ncol=7, fontsize=8)
colors = ['#d95f02', '#7570b3', '#1b9e77', '#e7298a']
axes[1].fill_between(axis,lo,hi,color='#96aeb6',alpha=.35,label='Database 10–90% band')
axes[1].plot(axis,median,color='black',lw=1.3,label='Database median')
for point,color in zip([4,5,9,10],colors):
    axes[1].plot(measured_axis,measured[point-1],lw=1,alpha=.8,color=color,label=f'Scan point {point}')
axes[1].set_title('Measured points 4, 5, 9, 10 vs MB database (not verified point labels)')
axes[1].legend(ncol=3,fontsize=9)
for ax in axes:
    ax.set_xlim(300,1800); ax.set_ylabel('Min–max normalized intensity'); ax.grid(alpha=.15)
    ax.set_xlabel('Raman shift (cm$^{-1}$)')
fig.tight_layout(); fig.savefig(OUT / '02_mb_normalized_comparison.png'); plt.close(fig)

windows = [(400,480),(1350,1450),(1570,1660)]
fig, axes = plt.subplots(1,3,figsize=(13,4.5))
peak_report = []
for ax,(low,high) in zip(axes,windows):
    ax.fill_between(axis,lo,hi,color='#96aeb6',alpha=.35)
    ax.plot(axis,median,color='black',lw=1.6,label='Database median')
    mask=(axis>=low)&(axis<=high)
    peak=float(axis[np.flatnonzero(mask)[np.argmax(median[mask])]])
    report={'window':[low,high],'database_median_max_cm1':peak,'scan_point_max_cm1':{}}
    ax.axvline(peak,color='black',ls=':',lw=.8)
    for point,color in zip([4,5,9,10],colors):
        ax.plot(measured_axis,measured[point-1],color=color,lw=1,label=f'Point {point}')
        m=(measured_axis>=low)&(measured_axis<=high)
        report['scan_point_max_cm1'][str(point)]=float(measured_axis[np.flatnonzero(m)[np.argmax(measured[point-1,m])]])
    peak_report.append(report)
    ax.set_xlim(low,high); ax.set_title(f'Database local maximum: {peak:.1f} cm$^{{-1}}$')
    ax.set_xlabel('Raman shift (cm$^{-1}$)'); ax.grid(alpha=.15)
axes[0].set_ylabel('Min–max normalized intensity')
axes[2].legend(fontsize=8)
fig.suptitle('Peak-window comparison: observed positions, not assigned chemical bands',fontsize=13)
fig.tight_layout(rect=(0,0,1,.91)); fig.savefig(OUT / '03_mb_peak_windows.png'); plt.close(fig)

report={'database':str(DATA),'database_rows':len(raw),'selected_database_rows_1based':(selected+1).tolist(),
        'baseline_corrected_max_range':[float(raw.max(1).min()),float(raw.max(1).max())],
        'scan':str(scan),'dark':str(dark_file),'peak_windows':peak_report,
        'notes':['Concentration labels unavailable in these arrays.', 'Scan points selected for prior MB response, not assumed ground truth.',
                 'Database spectra are preprocessed, not original detector spectra.', 'Plots show 300–1800 cm-1; stored data cover the full retained axis.']}
(OUT/'comparison_details.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
print('Plots:',OUT)
