"""Mechanically derive pure-only controls from the existing model scripts."""
from pathlib import Path
import ast
import hashlib
import json
import shutil
import numpy as np

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug'
DEST = BASE / 'pure_only_20260923'
DATA = DEST / 'data'
DATA.mkdir(parents=True, exist_ok=True)
classes = ['r6g', 'methylene_blue', 'adenine', 'di_water']
manifest = {'classes': classes, 'synthetic_count': 0, 'augmentation': False,
            'split_warning': 'Stratified spectrum-level split; acquisition groups unavailable. Not independent batch validation.',
            'files': [], 'split': {'train': [], 'val': [], 'test': []}}
rng = np.random.RandomState(42)
offset = 0
for name in classes:
    a = np.load(BASE / 'data_base_code_part' / f'training_spectra_{name}_minmax.npy')
    ids = rng.permutation(len(a)) + offset
    ntrain, nval = int(len(a)*.7), int(len(a)*.1)
    for key, values in [('train', ids[:ntrain]), ('val', ids[ntrain:ntrain+nval]), ('test', ids[ntrain+nval:])]:
        manifest['split'][key].extend(values.tolist())
    offset += len(a)
    for fname in [f'training_spectra_{name}_minmax.npy', f'training_spectra_{name}_baseline_raw.npy', f'raman_shift_axis_{name}.npy']:
        source = BASE / 'data_base_code_part' / fname
        shutil.copy2(source, DATA / fname)
        manifest['files'].append({'name': fname, 'source': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
manifest['total_real_spectra'] = offset
(DATA / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')

for source_name, output_name, old_output, old_model, new_model in [
    ('CNN3_1_component_summary_complete.py', 'CNN3_1_pure_only', '_CNN_raman_v3_mixture', 'cnn_mixture.pt', 'cnn_pure_only.pt'),
    ('Transformer5_1_component_summary_complete.py', 'Transformer5_1_pure_only', '_ViT1D_raman_v2_mixture', 'vit1d_mixture.pt', 'vit1d_pure_only.pt'),
]:
    text = (ROOT / 'model_replacements_20260921' / source_name).read_text(encoding='utf-8-sig')
    text = text.replace('DATA_DIR = os.path.join(DATA_ROOT, "data_base_code_part")', f'DATA_DIR = r"{DATA}"')
    text = text.replace('USE_AUGMENT = True', 'USE_AUGMENT = False')
    text = text.replace('N_SYNTHETIC_PER_COMBO = 200; N_SYNTHETIC_TERNARY = 100', 'N_SYNTHETIC_PER_COMBO = 0; N_SYNTHETIC_TERNARY = 0')
    text = text.replace('ENABLE_EXTRA_LOW_R6G_MB = True; N_EXTRA_LOW_R6G_MB = 300', 'ENABLE_EXTRA_LOW_R6G_MB = False; N_EXTRA_LOW_R6G_MB = 0')
    text = text.replace('R6G_WEAK_THRESHOLD_WHEN_MB_DOMINANT = 0.25', 'R6G_WEAK_THRESHOLD_WHEN_MB_DOMINANT = OVR_FINAL_THRESHOLD  # No relaxed R6G threshold')
    text = text.replace(f'OUTPUT_DIR = os.path.join(DATA_ROOT, "{old_output}")', f'OUTPUT_DIR = r"{DEST / (output_name + "_results")}"')
    text = text.replace(old_model, new_model)
    text = text.replace('v.detach().cpu() for k,v', 'v.detach().cpu().clone() for k,v')
    start = text.index('all_idx=list(range(X_all.shape[0]))')
    end = text.index('print(f"  train=', start)
    text = text[:start] + '''assert n_synth == 0 and len(X_all) == len(X_pure_mm), "Pure-only invariant failed"
with open(os.path.join(DATA_DIR, "manifest.json"), encoding="utf-8") as f:
    pure_manifest = json.load(f)
for item in pure_manifest["files"]:
    import hashlib
    with open(os.path.join(DATA_DIR, item["name"]), "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == item["sha256"], "Dataset changed; regenerate split"
idx_tr = pure_manifest["split"]["train"]
idx_va = pure_manifest["split"]["val"]
idx_te = pure_manifest["split"]["test"]
assert sorted(idx_tr + idx_va + idx_te) == list(range(len(X_all)))
print("PURE-ONLY CONTROL: no synthetic mixtures, no augmentation; old weights are not used.")
print("WARNING: spectrum-level split only; no independent acquisition-batch validation.")
print("Mixture detection is NOT validated in this experiment; thresholds are unchanged controls.")
with open(os.path.join(OUTPUT_DIR, "pure_experiment_manifest.json"), "w", encoding="utf-8") as f:
    json.dump(pure_manifest, f, indent=2)
''' + text[end:]
    text = text.replace('Load data + generate synthetic mixtures', 'Load real pure spectra (synthetic mixtures disabled)')
    text = '# Pure-only control derived from the existing model; architecture and sigmoid head retained.\n' + text
    ast.parse(text)
    (DEST / (output_name + '.py')).write_text(text, encoding='utf-8')
    nb = {'cells': [
        {'cell_type': 'markdown', 'metadata': {}, 'source': ['# Pure-only training control\n', '456 real spectra; no synthetic mixtures or augmentation. First run trains a NEW model. Later runs load it unless RETRAIN_MODEL=True.\n', 'This is a spectrum-level split, not independent batch validation. Mixed-sample performance is unvalidated.\n']},
        {'cell_type': 'code', 'metadata': {}, 'execution_count': None, 'outputs': [], 'source': text.splitlines(keepends=True)}
    ], 'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'}}, 'nbformat': 4, 'nbformat_minor': 5}
    (DEST / (output_name + '.ipynb')).write_text(json.dumps(nb, indent=1), encoding='utf-8')
    print('Created:', DEST / (output_name + '.ipynb'))
print('Real samples:', offset, 'Split:', {k: len(v) for k,v in manifest['split'].items()})
