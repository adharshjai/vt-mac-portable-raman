"""Keep the user's full PCA-SVM training flow and replace only the scan report."""
from pathlib import Path
import json
import ast
import hashlib

root = Path(__file__).resolve().parent
source = Path('C:/Users/Jeremy666/.codex/attachments/6b32857f-87e3-413b-9a1b-aee9a89a782a/pasted-text.txt')
original = source.read_text(encoding='utf-8-sig')
marker = '# STEP 6: Inference on real test file'
prefix = original[:original.index(marker)]
report = (root / 'svm_pure_report_body.txt').read_text(encoding='utf-8')
code = prefix + '\n# STEP 6: Pure-branch scan analysis; training above is unchanged.\n' + \
    'PURE_REPORT_MIN_CONFIDENCE = CONFIDENCE_THRESHOLD\n' + report
assert code[:len(prefix)] == prefix
compile(code, 'SVM4_full_PCA_SVM_new_analysis.py', 'exec')
assert 'pca.transform(scaler.transform(mm.reshape(1, -1)))' in code
out = root / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/svm_r6g_update_20260924'
stem = 'SVM4_full_PCA_SVM_new_analysis'
(out / (stem+'.py')).write_text(code, encoding='utf-8')
nb = dict(cells=[dict(cell_type='code', id='full-pca-svm-new-analysis', execution_count=None,
    metadata={}, outputs=[], source=code.splitlines(keepends=True))],
    metadata={'kernelspec': dict(display_name='Python 3', language='python', name='python3')}, nbformat=4, nbformat_minor=5)
(out / (stem+'.ipynb')).write_text(json.dumps(nb, indent=1), encoding='utf-8')
manifest = dict(source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                preserved='All source text before Step 6, including all training, PCA figures, evaluation and model saving.',
                changed='Step 6 scan analysis uses existing scaler -> PCA -> pure SVM, confidence threshold from original config, background -> UNKNOWN.',
                verification='Syntax checked; inference tested using existing saved models; full training not rerun.')
(out / (stem+'_changes.json')).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print('PASS: Steps 1-5 are identical to the user attachment, including PCA and both SVM branches.')
print(out / (stem+'.ipynb'))

# Test the new report against existing weights without running training.
import matplotlib
matplotlib.use('Agg')
setup = original[:original.index('# STEP 1: Load MINMAX and RAW pure data')]
labels = original[original.index('class_names ='):original.index('print("=" * 70)', original.index('class_names ='))]
ns = {'__name__': '__main__'}
exec(compile(setup + labels + '\nPURE_REPORT_MIN_CONFIDENCE = CONFIDENCE_THRESHOLD\n' + report,
             'inference_check', 'exec'), ns)
rows = ns['spatial_results']
assert len(rows) == 13
print('PASS: new scan report processed all 13 points using saved PCA-SVM models.')
