"""Copy the existing SVM with only two R6G inputs and the output path changed."""
from pathlib import Path
import ast
import hashlib
import json
import numpy as np

root = Path(__file__).resolve().parent
base = root / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug'
source = root / 'model_replacements_20260921/SVM4_component_summary_complete.py'
original = source.read_text(encoding='utf-8-sig')
updated = original
changes = []
for suffix in ('minmax', 'baseline_raw'):
    old = f'"r6g": os.path.join(DATA_DIR, "training_spectra_r6g_{suffix}.npy")'
    new = f'"r6g": os.path.join(DATA_ROOT, "pure_only_20260923", "data_r6g_20260922", "training_spectra_r6g_{suffix}.npy")'
    assert updated.count(old) == 1
    updated = updated.replace(old, new)
    changes.append((old, new))
old = 'OUTPUT_DIR = os.path.join(DATA_ROOT, "_PCA_SVM_v7_target_only_pca3")'
new = 'OUTPUT_DIR = os.path.join(DATA_ROOT, "_PCA_SVM_r6g20260922_results")'
assert updated.count(old) == 1
updated = updated.replace(old, new)
changes.append((old, new))
restored = updated
for old, new in changes:
    restored = restored.replace(new, old)
assert restored == original
compile(updated, 'SVM4_r6g20260922_only.py', 'exec')
old_tree, new_tree = ast.parse(original), ast.parse(updated)
old_functions = [ast.dump(n) for n in old_tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
new_functions = [ast.dump(n) for n in new_tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
assert old_functions == new_functions
for suffix in ('minmax', 'baseline_raw'):
    data = np.load(base / f'pure_only_20260923/data_r6g_20260922/training_spectra_r6g_{suffix}.npy')
    assert data.shape == (119, 1958) and np.isfinite(data).all()
out = base / 'svm_r6g_update_20260924'
out.mkdir(exist_ok=True)
(out / 'SVM4_r6g20260922_only.py').write_text(updated, encoding='utf-8')
nb = {'cells': [{'cell_type': 'code', 'id': 'svm-r6g-update', 'execution_count': None,
                 'metadata': {}, 'outputs': [], 'source': updated.splitlines(keepends=True)}],
      'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'}},
      'nbformat': 4, 'nbformat_minor': 5}
(out / 'SVM4_r6g20260922_only.ipynb').write_text(json.dumps(nb, indent=1), encoding='utf-8')
(out / 'changes.json').write_text(json.dumps(dict(source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
     changes=[dict(before=a, after=b) for a, b in changes],
     validation='Only three path expressions differ; all other source text is identical. Training not run.'), indent=2), encoding='utf-8')
print('PASS: only two R6G input paths and one output path changed; syntax/data checks passed.')
print(out)
