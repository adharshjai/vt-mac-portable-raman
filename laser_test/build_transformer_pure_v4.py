"""Build a standalone pure-class Transformer from the existing implementation."""
import ast
import json
from pathlib import Path

base = Path(__file__).resolve().parent / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug'
project = base / 'pure_only_20260923'
source = (project / 'shift_robust_v2/Transformer5_1_shift_robust.py').read_text(encoding='utf-8')
names = {'strip_prefix', 'load_dark_spectrum_from_file', 'parse_portable_log_to_spectra',
         'moving_average', 'rolling_minimum', 'baseline_correction_fast', 'normalize_min_max',
         'pad_or_trim', 'preprocess_two_versions', 'DropPath', 'PatchEmbedding1D',
         'MultiHeadSelfAttention', 'TransformerBlock', 'RamanViT1D'}
parts = [ast.get_source_segment(source, node) for node in ast.parse(source).body
         if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
header = '''"""Pure-class ViT-1D v4. No synthetic mixtures; not a mixture detector."""
import os, sys, re, json, csv, random, hashlib
from pathlib import Path
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import matplotlib
if 'ipykernel' not in sys.modules:
    matplotlib.use('Agg')
import matplotlib.pyplot as plt
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support, roc_auc_score, roc_curve

BASE = Path(__BASE__)
DATA_DIR = BASE / 'pure_only_20260923/data_r6g_20260922'
OUTPUT_DIR = BASE / 'pure_only_20260923/pure_softmax_v4/results'
TEST_FILE = BASE / 'scan_sessions/1d_y_scan_20260921_152840_289/laser_log_combined.txt'
TEST_DARK_FILE = BASE / 'dark_measurements/dark_20260921_153016_700.txt'
# First run trains automatically. Set True only to train again.
RETRAIN_MODEL = False
SEED = 42
EPOCHS = 100
PATIENCE = 25
LR = 0.0002
BATCH_SIZE = 32
# This is a rejection setting, not a calibrated probability guarantee.
MIN_CONFIDENCE = 0.60
MIN_MARGIN = 0.15
CLASS_NAMES = ['r6g', 'methylene_blue', 'adenine', 'di_water']
TARGET_LEN = 2048
TARGET_LEN_RAW = 2048
DROP_N = 90
DO_BASELINE = True
BASELINE_WINDOW = BASELINE_SMOOTH = 15
PATCH_SIZE = 64
D_MODEL = 128
NUM_HEADS = 4
DEPTH = 4
MLP_RATIO = 4.0
NUM_CLASSES = 4
DROPOUT = 0.15
ATTN_DROP = DROP_PATH = 0.10
ERROR_PHRASE = 'image data error code 0x0001'
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
torch.set_num_threads(2)
'''.replace('__BASE__', repr(str(base)))
tail = (Path(__file__).parent / 'transformer_pure_v4_body.txt').read_text(encoding='utf-8')
code = header + '\n\n' + '\n\n'.join(parts) + '\n\n' + tail
compile(code, 'Transformer5_1_pure_softmax.py', 'exec')
dest = project / 'pure_softmax_v4'
dest.mkdir(exist_ok=True)
(dest / 'Transformer5_1_pure_softmax.py').write_text(code, encoding='utf-8')
nb = {'cells': [{'cell_type': 'code', 'execution_count': None, 'metadata': {}, 'outputs': [],
                 'source': code.splitlines(keepends=True)}],
      'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'}},
      'nbformat': 4, 'nbformat_minor': 5}
(dest / 'Transformer5_1_pure_softmax.ipynb').write_text(json.dumps(nb, indent=1), encoding='utf-8')
print(dest)
