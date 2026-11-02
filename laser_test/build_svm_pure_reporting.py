"""Preserve SVM training; select pure or legacy mixture reporting explicitly."""
from pathlib import Path
import json, textwrap, ast
root = Path(__file__).resolve().parent
folder = root / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/svm_r6g_update_20260924'
source = folder / 'SVM4_r6g20260922_only.py'
backup = folder / 'backup_before_pure_reporting'
backup.mkdir(exist_ok=True)
for name in ['SVM4_r6g20260922_only.py', 'SVM4_r6g20260922_only.ipynb']:
    dest = backup / name
    if not dest.exists():
        dest.write_bytes((folder/name).read_bytes())
original = (backup/source.name).read_text(encoding='utf-8')
marker = '# STEP 6: Inference on real test file'
start = original.index(marker)
prefix, tail = original[:start], original[start:]
config = '\nSCAN_REPORT_MODE = "pure"  # "pure" or "mixture"\nPURE_REPORT_MIN_CONFIDENCE = 0.80\nassert SCAN_REPORT_MODE in ("pure", "mixture")\n\n'
pure = (root/'svm_pure_report_body.txt').read_text(encoding='utf-8')
updated = config + prefix + '\nif SCAN_REPORT_MODE == "pure":\n' + textwrap.indent(pure, '    ') + '\nelse:\n' + textwrap.indent(tail, '    ')
assert updated[len(config):len(config)+len(prefix)] == prefix
def save(stem, code):
    compile(code, stem, 'exec')
    (folder/(stem+'.py')).write_text(code, encoding='utf-8')
    nb = dict(cells=[dict(cell_type='code', id=stem.lower().replace('_','-'), execution_count=None,
                         metadata={}, outputs=[], source=code.splitlines(keepends=True))],
              metadata={'kernelspec':dict(display_name='Python 3', language='python', name='python3')}, nbformat=4, nbformat_minor=5)
    (folder/(stem+'.ipynb')).write_text(json.dumps(nb, indent=1), encoding='utf-8')
save('SVM4_r6g20260922_only', updated)
# Load existing pure-branch weights without rerunning any training.
setup = original[:original.index('# STEP 1: Load MINMAX and RAW pure data')]
labels = original[original.index('class_names ='):original.index('print("=" * 70)', original.index('class_names ='))]
inference_config = '\nPURE_REPORT_MIN_CONFIDENCE = 0.80\n# This inference-only file uses the pure branch. Use the full version for mixture mode.\n'
inference = 'import matplotlib\nimport sys\nif "ipykernel" not in sys.modules:\n    matplotlib.use("Agg")\n' + inference_config + setup + labels + '\n' + pure
save('SVM4_pure_inference_only', inference)
print('PASS: training prefix unchanged; legacy mixture report retained; full and inference-only notebooks saved.')
