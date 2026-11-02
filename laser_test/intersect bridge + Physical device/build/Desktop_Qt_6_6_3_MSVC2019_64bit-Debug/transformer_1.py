import os
import re
import io
import json
import time
import sys
import random
import math
from typing import List, Tuple

import numpy as np

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader


# =========================================================
# Cell 1/10: config
# =========================================================
DATA_ROOT = r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data"

LIB_FILES = {
    "pyo":     r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data\pyo_1s_50mw_1mM_10x10_portable.txt",
    "adamite": r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data\Admine_1s_50mw_10mM_10x10cycle_portable.txt",
    "si":      r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data\Si_1s_50mw_10x10_2D_Portable.txt",
    "r6g":     r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data\r6g_1s_50mw_1mM_10x10_2D_portable.txt",
}

TEST_FILE = r"C:\Users\Jeremy666\OneDrive\Desktop\text.txt"

SUPPORTED_EXTS = {".txt", ".csv", ".tsv", ".dat", ".asc", ".prn"}

# ----------------- Key parameters -----------------
TARGET_LEN = 2048   # 2045 -> interpolated to 2048 (almost lossless, and a power of 2, which is convenient for patch splitting)
PATCH_SIZE = 64     # each patch covers 64 spectral points; 2048/64 = 32 tokens
                    # optional combinations: PATCH_SIZE=32 -> 64 tokens; PATCH_SIZE=128 -> 16 tokens
# --------------------------------------------

BATCH_SIZE = 32
EPOCHS     = 80     # a Transformer usually needs a few more training epochs than a CNN
LR         = 5e-4   # Transformers are more sensitive to the learning rate, so keep it slightly lower
WEIGHT_DECAY = 1e-4
SEED = 42

TRAIN_RATIO = 0.80
VAL_RATIO   = 0.10
TEST_RATIO  = 0.10

DO_BASELINE = False
NUM_WORKERS = 0

# file-level matching threshold
MATCH_THRESHOLD = 0.90
MATCH_MARGIN    = 0.08

# softmax temperature
INFER_TEMPERATURE = 1.0

# options: "mean_logits", "vote", "max"
INFER_AGGREGATION = "mean_logits"

# training improvements
DROPOUT         = 0.15          # attention/ffn dropout inside the Transformer
DROP_PATH       = 0.10          # Stochastic Depth
LABEL_SMOOTHING = 0.05
GRAD_CLIP_NORM  = 3.0

USE_AUGMENT    = True
AUG_NOISE_STD  = 0.015
AUG_SHIFT_MAX  = 12
AUG_SCALE_LOW  = 0.90
AUG_SCALE_HIGH = 1.10

OUTPUT_DIR = os.path.join(DATA_ROOT, "_ViT1D_4class_r6g_si_adamite_pyo")
MODELS_DIR = os.path.join(OUTPUT_DIR, "models")
META_PATH  = os.path.join(OUTPUT_DIR, "meta.json")
MODEL_PATH = os.path.join(MODELS_DIR, "vit1d_4class.pt")

os.makedirs(MODELS_DIR, exist_ok=True)

DEVICE     = "cuda" if torch.cuda.is_available() else "cpu"
PIN_MEMORY = DEVICE.startswith("cuda")

print("DEVICE     =", DEVICE)
print("MODEL_PATH =", MODEL_PATH)
print(f"TARGET_LEN={TARGET_LEN}  PATCH_SIZE={PATCH_SIZE}  num_tokens={TARGET_LEN // PATCH_SIZE}")


# =========================================================
# Cell 2/10: utils  (identical to the original version)
# =========================================================
def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

set_seed(SEED)


def stratified_split_indices(
    Y: np.ndarray,
    train_ratio=TRAIN_RATIO,
    val_ratio=VAL_RATIO,
    test_ratio=TEST_RATIO,
    seed=SEED
):
    assert abs(train_ratio + val_ratio + test_ratio - 1.0) < 1e-6
    rng = random.Random(seed)

    idx_tr, idx_va, idx_te = [], [], []
    for cls in np.unique(Y):
        idx = np.where(Y == cls)[0].tolist()
        rng.shuffle(idx)
        n = len(idx)

        n_train = int(n * train_ratio)
        n_val   = int(n * val_ratio)

        tr = idx[:n_train]
        va = idx[n_train:n_train + n_val]
        te = idx[n_train + n_val:]

        idx_tr.extend(tr)
        idx_va.extend(va)
        idx_te.extend(te)

    rng.shuffle(idx_tr)
    rng.shuffle(idx_va)
    rng.shuffle(idx_te)
    return idx_tr, idx_va, idx_te


def zscore(x: np.ndarray, eps: float = 1e-8) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    m = float(np.mean(x))
    s = float(np.std(x))
    if (not np.isfinite(m)) or (not np.isfinite(s)) or (s < eps):
        return np.zeros_like(x, dtype=np.float32)
    out = (x - m) / (s + eps)
    out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
    return out.astype(np.float32)


def simple_baseline_remove(y: np.ndarray, poly_order: int = 3) -> np.ndarray:
    n = y.size
    x = np.linspace(0, 1, n)
    coeff = np.polyfit(x, y, deg=poly_order)
    base  = np.polyval(coeff, x)
    return y - base


def softmax_np(logits: np.ndarray) -> np.ndarray:
    z = np.asarray(logits, dtype=np.float64)
    if z.size == 0 or (not np.isfinite(z).all()):
        return np.zeros_like(z, dtype=np.float32)
    z = z - np.max(z)
    e = np.exp(z)
    s = np.sum(e)
    if s <= 0 or (not np.isfinite(s)):
        return np.zeros_like(z, dtype=np.float32)
    out = e / s
    out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
    return out.astype(np.float32)


def pearson_corr_score(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    a = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
    b = np.nan_to_num(b, nan=0.0, posinf=0.0, neginf=0.0)
    a = a - float(np.mean(a))
    b = b - float(np.mean(b))
    da = float(np.linalg.norm(a))
    db = float(np.linalg.norm(b))
    if da < 1e-8 or db < 1e-8:
        return 0.0
    corr = float(np.dot(a, b) / (da * db + 1e-8))
    corr = max(-1.0, min(1.0, corr))
    return (corr + 1.0) * 0.5


def cosine_score(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    a = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
    b = np.nan_to_num(b, nan=0.0, posinf=0.0, neginf=0.0)
    da = float(np.linalg.norm(a))
    db = float(np.linalg.norm(b))
    if da < 1e-8 or db < 1e-8:
        return 0.0
    cs = float(np.dot(a, b) / (da * db + 1e-8))
    cs = max(-1.0, min(1.0, cs))
    return (cs + 1.0) * 0.5


def combined_match_score(a: np.ndarray, b: np.ndarray) -> float:
    p = pearson_corr_score(a, b)
    c = cosine_score(a, b)
    score = 0.7 * p + 0.3 * c
    return float(max(0.0, min(1.0, score)))


# =========================================================
# Cell 3/10: parsing  (identical to the original version)
# =========================================================
_BAD_FILES   = set()
ERROR_PHRASE = "image data error code 0x0001"


def _read_text_any_encoding(path: str) -> Tuple[str, str]:
    b = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "utf-16le", "utf-16be", "gbk", "cp1252", "latin1"):
        try:
            return b.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="ignore"), "utf-8-ignore"


def strip_prefix(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^\[[^\]]+\]\s+\[Backend\]\s*", "", line)
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    return line.strip()


def parse_portable_log_to_spectra(log_file: str) -> Tuple[List[np.ndarray], int]:
    lines = open(log_file, "r", encoding="utf-8", errors="ignore").read().splitlines()

    spectra       = []
    failed_blocks = 0

    i = 0
    while i < len(lines):
        line_raw = lines[i]
        low  = line_raw.lower()
        line = strip_prefix(line_raw)

        if ERROR_PHRASE in low:
            failed_blocks += 1
            i += 1
            continue

        if line.startswith("Image Data"):
            tokens     = []
            first_part = line[len("Image Data"):].strip()
            tokens.extend(re.findall(r"\b[0-9A-Fa-f]{2,4}\b", first_part))

            j = i + 1
            block_has_error = False

            while j < len(lines):
                nxt_raw = lines[j]
                nxt_low = nxt_raw.lower()
                nxt     = strip_prefix(nxt_raw)

                if ERROR_PHRASE in nxt_low:
                    block_has_error = True

                if nxt.startswith("Stop Capture"):
                    break
                if nxt.startswith("Image Data") and j != i:
                    break

                hexes = re.findall(r"\b[0-9A-Fa-f]{2,4}\b", nxt)
                if hexes:
                    tokens.extend(hexes)

                j += 1

            if block_has_error or len(tokens) == 0:
                failed_blocks += 1
            else:
                vals = np.array([int(t, 16) for t in tokens], dtype=np.int64)

                if vals.size % 2 == 0 and vals.max(initial=0) <= 255:
                    le     = vals[0::2] + (vals[1::2] << 8)
                    be     = (vals[0::2] << 8) + vals[1::2]
                    vals16 = le if le.max(initial=0) >= be.max(initial=0) else be
                    if vals16.max(initial=0) > 255:
                        vals = vals16.astype(np.int64)

                spectra.append(vals.astype(np.float32))

            i = j + 1
            continue

        i += 1

    return spectra, failed_blocks


def _parse_hex_tokens_to_vals(tokens: List[str]) -> np.ndarray:
    vals = np.array([int(t, 16) for t in tokens], dtype=np.int64)

    if vals.size % 2 == 0 and vals.max(initial=0) <= 255:
        le     = vals[0::2] + (vals[1::2] << 8)
        be     = (vals[0::2] << 8) + vals[1::2]
        vals16 = le if le.max(initial=0) >= be.max(initial=0) else be
        if vals16.max(initial=0) > 255:
            vals = vals16.astype(np.int64)

    return vals.astype(np.float32)


# =========================================================
# Cell 4/10: preprocess  (identical to the original version)
# =========================================================
def preprocess_intensity_to_vec(y: np.ndarray, target_len: int, do_baseline: bool) -> np.ndarray:
    y = np.asarray(y, dtype=np.float32)
    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)

    if y.size < 4:
        out = np.zeros(target_len, dtype=np.float32)
        out[:min(target_len, y.size)] = y[:min(target_len, y.size)]
        return out

    if do_baseline:
        y = simple_baseline_remove(y.astype(np.float64), poly_order=3).astype(np.float32)

    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)

    x     = np.arange(y.size, dtype=np.float64)
    x_new = np.linspace(float(x.min()), float(x.max()), target_len, dtype=np.float64)
    y_new = np.interp(x_new, x, y.astype(np.float64)).astype(np.float32)
    y_new = np.nan_to_num(y_new, nan=0.0, posinf=0.0, neginf=0.0)
    y_new = zscore(y_new).astype(np.float32)
    return y_new


def load_spectrum_text(path: str, target_len: int, do_baseline: bool) -> np.ndarray:
    try:
        text, _ = _read_text_any_encoding(path)
        low     = text.lower()

        if ("image data" in low) or ("stop capture" in low):
            spectra, failed = parse_portable_log_to_spectra(path)
            if len(spectra) == 0:
                raise ValueError(f"portable log parsed 0 spectra (failed_blocks={failed})")
            v = preprocess_intensity_to_vec(spectra[0], target_len, do_baseline)
            return v.astype(np.float32)

        tokens = re.findall(r"\b(?:0x)?[0-9A-Fa-f]{2,4}\b", text)
        tokens = [t[2:] if t.lower().startswith("0x") else t for t in tokens]
        if len(tokens) >= 32:
            vals = _parse_hex_tokens_to_vals(tokens)
            v    = preprocess_intensity_to_vec(vals, target_len, do_baseline)
            return v.astype(np.float32)

        head      = "\n".join(text.splitlines()[:50])
        delimiter = "," if "," in head else ("\t" if "\t" in head else None)

        data = np.genfromtxt(io.StringIO(text), delimiter=delimiter, dtype=np.float64, invalid_raise=False)
        if data is None or (isinstance(data, float) and np.isnan(data)):
            raise ValueError("genfromtxt returned NaN/None")

        if np.isscalar(data):
            y = np.array([float(data)], dtype=np.float64)
        elif data.ndim == 1:
            y = data.astype(np.float64)
        else:
            y = (data[:, 1] if data.shape[1] >= 2 else data[:, 0]).astype(np.float64)

        y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
        v = preprocess_intensity_to_vec(y, target_len, do_baseline)
        return v.astype(np.float32)

    except Exception as e:
        if path not in _BAD_FILES:
            _BAD_FILES.add(path)
            print(f"[WARN] Failed to parse spectrum: {path}\n       Reason: {repr(e)}")
        return np.zeros((target_len,), dtype=np.float32)


# =========================================================
# Cell 5/10: build dataset  (identical to the original version)
# =========================================================
class_names = list(LIB_FILES.keys())
class_to_id = {c: i for i, c in enumerate(class_names)}
id_to_class = {i: c for c, i in class_to_id.items()}

X        = []
Y        = []
RAW_LENS = []

for cls, fp in LIB_FILES.items():
    if not os.path.exists(fp):
        raise FileNotFoundError(fp)

    spectra, failed = parse_portable_log_to_spectra(fp)
    print(f"[LIB] {cls:8s}  spectra={len(spectra)}  failed_blocks={failed}  file={os.path.basename(fp)}")

    for s in spectra:
        v = preprocess_intensity_to_vec(s, TARGET_LEN, DO_BASELINE)
        if np.allclose(v, 0):
            continue
        RAW_LENS.append(int(s.size))
        X.append(v)
        Y.append(class_to_id[cls])

X = np.stack(X, axis=0).astype(np.float32)
Y = np.array(Y, dtype=np.int64)

print("Total samples:", X.shape, "labels:", Y.shape)
print("Class counts:", {id_to_class[i]: int((Y == i).sum()) for i in range(len(class_names))})

RAW_LEN_MIN, RAW_LEN_MAX = 0, 10**9
if len(RAW_LENS) > 0:
    arr         = np.array(RAW_LENS, dtype=np.int64)
    q10, q90    = np.percentile(arr, [10, 90]).astype(np.float64)
    RAW_LEN_MIN = int(max(16, np.floor(q10 * 0.80)))
    RAW_LEN_MAX = int(np.ceil(q90 * 1.20))
print(f"RAW_LEN_RANGE (from LIB): [{RAW_LEN_MIN}, {RAW_LEN_MAX}]  (q10/q90 based)")

idx_tr, idx_va, idx_te = stratified_split_indices(Y, seed=SEED)
print("Split sizes:", len(idx_tr), len(idx_va), len(idx_te))

CLASS_PROTOTYPES = {}
for cls_name, cls_id in class_to_id.items():
    cls_x = X[Y == cls_id]
    if cls_x.shape[0] == 0:
        CLASS_PROTOTYPES[cls_name] = np.zeros((TARGET_LEN,), dtype=np.float32)
    else:
        proto = np.mean(cls_x, axis=0).astype(np.float32)
        CLASS_PROTOTYPES[cls_name] = zscore(proto).astype(np.float32)


def compute_match_rates_from_vec(v: np.ndarray) -> np.ndarray:
    rates = []
    for cls_name in class_names:
        proto = CLASS_PROTOTYPES[cls_name]
        rate  = combined_match_score(v, proto)
        rates.append(rate)
    rates = np.asarray(rates, dtype=np.float32)
    rates = np.nan_to_num(rates, nan=0.0, posinf=0.0, neginf=0.0)
    return rates


# =========================================================
# Cell 6/10: dataset loader + augmentation  (identical to the original version)
# =========================================================
def augment_spectrum_1d(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32).copy()

    scale = np.random.uniform(AUG_SCALE_LOW, AUG_SCALE_HIGH)
    x = x * np.float32(scale)

    shift = int(np.random.randint(-AUG_SHIFT_MAX, AUG_SHIFT_MAX + 1))
    if shift != 0:
        x = np.roll(x, shift)

    noise = np.random.normal(0.0, AUG_NOISE_STD, size=x.shape).astype(np.float32)
    x = x + noise

    x = zscore(x).astype(np.float32)
    return x


class RamanMultiClassDataset(Dataset):
    def __init__(self, X: np.ndarray, Y: np.ndarray, indices: List[int], augment: bool = False):
        self.X       = X
        self.Y       = Y
        self.indices = indices
        self.augment = augment

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, k: int):
        i = self.indices[k]
        x = self.X[i].copy()

        if self.augment:
            x = augment_spectrum_1d(x)

        # ViT version: output a 1D vector of shape [TARGET_LEN], without adding a channel dimension
        # the PatchEmbedding layer does the splitting inside the model
        x = torch.from_numpy(x)                            # shape: (TARGET_LEN,)
        y = torch.tensor(int(self.Y[i]), dtype=torch.long)
        return x, y


train_ds = RamanMultiClassDataset(X, Y, idx_tr, augment=USE_AUGMENT)
val_ds   = RamanMultiClassDataset(X, Y, idx_va, augment=False)
test_ds  = RamanMultiClassDataset(X, Y, idx_te, augment=False)

train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True,
                          num_workers=NUM_WORKERS, pin_memory=PIN_MEMORY)
val_loader   = DataLoader(val_ds,   batch_size=BATCH_SIZE, shuffle=False,
                          num_workers=NUM_WORKERS, pin_memory=PIN_MEMORY)
test_loader  = DataLoader(test_ds,  batch_size=BATCH_SIZE, shuffle=False,
                          num_workers=NUM_WORKERS, pin_memory=PIN_MEMORY)


# =========================================================
# Cell 7/10: Patch-based 1D Vision Transformer  <- core change
#
# Overall architecture (using TARGET_LEN=2048, PATCH_SIZE=64 as an example):
#
#   input spectrum  [B, 2048]
#       |
#       v  PatchEmbedding1D
#   32 patches, each patch=64 points -> linearly mapped to d_model=128
#   [B, 32, 128]
#       |
#       v  concatenate learnable [CLS] token
#   [B, 33, 128]
#       |
#       v  add learnable positional encoding + Dropout
#   [B, 33, 128]
#       |
#       v  TransformerBlock x 4 (Pre-LN + MHSA + DropPath + FFN)
#   [B, 33, 128]
#       |
#       v  take CLS token (index=0) + LayerNorm
#   [B, 128]
#       |
#       v  classification head (Linear -> GELU -> Dropout -> Linear)
#   [B, 4]  logits
#
# about 200K parameters, suitable for small datasets of 100-500 samples
# =========================================================

class DropPath(nn.Module):
    """
    Stochastic Depth regularization.
    During training, zeroes the entire residual path with probability drop_prob (equivalent to skipping this layer).
    Automatically disabled at inference; behaves as an identity mapping.
    Difference from Dropout: Dropout randomly zeroes individual neurons, while DropPath randomly zeroes the residual of certain samples in the whole batch.
    """
    def __init__(self, drop_prob: float = 0.0):
        super().__init__()
        self.drop_prob = drop_prob

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.drop_prob == 0.0 or not self.training:
            return x
        keep_prob     = 1.0 - self.drop_prob
        shape         = (x.shape[0],) + (1,) * (x.ndim - 1)  # [B, 1, 1, ...]
        random_tensor = torch.rand(shape, dtype=x.dtype, device=x.device)
        random_tensor = torch.floor(random_tensor + keep_prob)   # 0 or 1
        return x * random_tensor / keep_prob


class PatchEmbedding1D(nn.Module):
    """
    Splits the 1D spectrum into non-overlapping chunks (patches), then linearly projects them to d_model dimensions.

    Example: seq_len=2048, patch_size=64
        -> 2048 / 64 = 32 patches
        each patch is 64 raw spectral intensity values
        a linear layer maps 64 dims to d_model=128 dims

    Why split into patches?
        The self-attention complexity of a Transformer is O(N^2); if every spectral point were treated as a token (N=2048),
        the computation would be too heavy. Packing 64 points into one token brings N down to 32, making computation 64 times faster,
        while each token still retains local spectral shape information.
    """
    def __init__(self, seq_len: int, patch_size: int, d_model: int):
        super().__init__()
        assert seq_len % patch_size == 0, \
            f"seq_len ({seq_len}) must be divisible by patch_size ({patch_size})"
        self.patch_size  = patch_size
        self.num_patches = seq_len // patch_size
        self.proj = nn.Linear(patch_size, d_model, bias=False)
        self.norm = nn.LayerNorm(d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, seq_len]
        B, L = x.shape
        # split into patches: [B, num_patches, patch_size]
        x = x.view(B, self.num_patches, self.patch_size)
        # linear projection: [B, num_patches, d_model]
        x = self.proj(x)
        x = self.norm(x)
        return x


class MultiHeadSelfAttention(nn.Module):
    """
    Multi-head self-attention (MHSA).

    Core idea:
        For every token in the sequence, compute its "relevance" scores (attention weights) with all other tokens,
        then aggregate the values (Value) of all tokens by those weights. Multi-head = run several independent attentions at the same time,
        each head attends to a different feature direction, and the results are concatenated at the end.

    Significance for Raman spectra:
        One patch (e.g. the peak region at 700-800 cm-1) can attend to another patch (e.g. 1400-1500 cm-1),
        capturing cross-peak correlations, which a CNN cannot do (a CNN only has a local receptive field).
    """
    def __init__(self, d_model: int, num_heads: int, attn_drop: float = 0.0):
        super().__init__()
        assert d_model % num_heads == 0
        self.num_heads = num_heads
        self.head_dim  = d_model // num_heads
        self.scale     = self.head_dim ** -0.5   # scaling factor, prevents the dot product from growing too large

        # Q/K/V are merged into a single linear layer and split again at inference
        self.qkv       = nn.Linear(d_model, d_model * 3, bias=False)
        self.proj      = nn.Linear(d_model, d_model, bias=False)
        self.attn_drop = nn.Dropout(attn_drop)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, N, C = x.shape
        # qkv: [B, N, 3C] -> [3, B, heads, N, head_dim]
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, self.head_dim)
        qkv = qkv.permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0)   # each [B, heads, N, head_dim]

        # Scaled dot-product attention
        attn = (q @ k.transpose(-2, -1)) * self.scale  # [B, heads, N, N]
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        out = (attn @ v)                               # [B, heads, N, head_dim]
        out = out.transpose(1, 2).reshape(B, N, C)    # [B, N, C]
        out = self.proj(out)
        return out


class TransformerBlock(nn.Module):
    """
    A single Transformer encoder layer, using the Pre-LayerNorm structure (more stable to train than the original Post-LN).

    Structure:
        x -> LayerNorm -> MHSA -> DropPath -> (+residual) -> x
        x -> LayerNorm -> FFN  -> DropPath -> (+residual) -> x

    FFN (feed-forward network) = Linear(d) -> GELU -> Dropout -> Linear(d)
    FFN hidden-layer width = d_model * mlp_ratio (4x by default), used to learn non-linear feature transformations.
    """
    def __init__(self,
                 d_model:   int,
                 num_heads: int,
                 mlp_ratio: float = 4.0,
                 dropout:   float = 0.0,
                 attn_drop: float = 0.0,
                 drop_path: float = 0.0):
        super().__init__()
        self.norm1 = nn.LayerNorm(d_model)
        self.attn  = MultiHeadSelfAttention(d_model, num_heads, attn_drop)
        self.dp1   = DropPath(drop_path)

        self.norm2  = nn.LayerNorm(d_model)
        hidden_dim  = int(d_model * mlp_ratio)
        self.ffn = nn.Sequential(
            nn.Linear(d_model, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, d_model),
            nn.Dropout(dropout),
        )
        self.dp2 = DropPath(drop_path)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.dp1(self.attn(self.norm1(x)))   # attention residual
        x = x + self.dp2(self.ffn(self.norm2(x)))    # FFN residual
        return x


class RamanViT1D(nn.Module):
    """
    Patch-based 1D Vision Transformer, designed specifically for portable Raman/SERS spectrum classification.

    Key hyperparameters:
        seq_len    : TARGET_LEN (number of spectral points, must be an integer multiple of patch_size)
        patch_size : PATCH_SIZE (how many spectral points each token covers)
        d_model    : token embedding dimension (internal width of the Transformer)
        num_heads  : number of self-attention heads (d_model must be divisible by num_heads)
        depth      : number of Transformer encoder layers
        mlp_ratio  : FFN hidden-layer multiplier

    Default configuration (d_model=128, depth=4) has about 200K parameters:
        suitable for 100-500 samples/class.
    With more data (>1000 samples/class) it can be scaled up:
        d_model=192, depth=6, num_heads=6
    """
    def __init__(self,
                 seq_len:     int   = TARGET_LEN,
                 patch_size:  int   = PATCH_SIZE,
                 d_model:     int   = 128,
                 num_heads:   int   = 4,
                 depth:       int   = 4,
                 mlp_ratio:   float = 4.0,
                 num_classes: int   = 4,
                 dropout:     float = 0.15,
                 attn_drop:   float = 0.10,
                 drop_path:   float = DROP_PATH):
        super().__init__()

        # ---- Patch Embedding ----
        self.patch_embed = PatchEmbedding1D(seq_len, patch_size, d_model)
        num_patches      = seq_len // patch_size

        # ---- learnable [CLS] token ----
        # same as in the original ViT paper, used to aggregate global information and finally feed the classification head
        self.cls_token = nn.Parameter(torch.zeros(1, 1, d_model))

        # ---- learnable positional encoding ----
        # lets the model know the relative position of each patch within the spectrum (which Raman shift segment)
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, d_model))
        self.pos_drop  = nn.Dropout(dropout)

        # ---- Transformer encoder stack ----
        # deeper layers get a higher DropPath probability (linearly increasing); the closer to the output, the more regularization is needed
        dpr = [x.item() for x in torch.linspace(0, drop_path, depth)]
        self.blocks = nn.ModuleList([
            TransformerBlock(
                d_model   = d_model,
                num_heads = num_heads,
                mlp_ratio = mlp_ratio,
                dropout   = dropout,
                attn_drop = attn_drop,
                drop_path = dpr[i],
            )
            for i in range(depth)
        ])

        self.norm = nn.LayerNorm(d_model)

        # ---- classification head ----
        self.head = nn.Sequential(
            nn.Linear(d_model, 64),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(64, num_classes),
        )

        self._init_weights()

    def _init_weights(self):
        # truncated-normal initialization of the positional encoding and CLS token (standard ViT practice)
        nn.init.trunc_normal_(self.pos_embed, std=0.02)
        nn.init.trunc_normal_(self.cls_token, std=0.02)
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight, std=0.02)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.LayerNorm):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, TARGET_LEN]
        B = x.shape[0]

        # Step 1: split into patches + linear projection -> [B, num_patches, d_model]
        x = self.patch_embed(x)

        # Step 2: prepend the CLS token -> [B, num_patches+1, d_model]
        cls = self.cls_token.expand(B, -1, -1)   # [B, 1, d_model]
        x   = torch.cat([cls, x], dim=1)

        # Step 3: add positional encoding (lets the model know the position of each patch)
        x = x + self.pos_embed
        x = self.pos_drop(x)

        # Step 4: pass through depth Transformer layers
        for blk in self.blocks:
            x = blk(x)

        x = self.norm(x)

        # Step 5: take the CLS token (index 0) for classification
        cls_out = x[:, 0]         # [B, d_model]
        logits  = self.head(cls_out)  # [B, num_classes]
        return logits


# =========================================================
# Cell 8/10: train  (only model initialization and learning-rate scheduling differ from the original version)
# =========================================================
@torch.no_grad()
def eval_acc(model: nn.Module, loader: DataLoader):
    model.eval()
    total, correct = 0, 0
    for x, y in loader:
        x      = x.to(DEVICE, non_blocking=True)
        y      = y.to(DEVICE, non_blocking=True)
        logits = model(x)
        pred   = torch.argmax(logits, dim=1)
        correct += int((pred == y).sum().item())
        total   += int(y.numel())
    return correct / max(total, 1)


def train_model():
    model = RamanViT1D(
        seq_len     = TARGET_LEN,
        patch_size  = PATCH_SIZE,
        d_model     = 128,      # token width; can be changed to 192/256 when there are more samples
        num_heads   = 4,        # number of attention heads; d_model must be divisible by num_heads
        depth       = 4,        # number of Transformer layers; can be changed to 6 when there are more samples
        mlp_ratio   = 4.0,
        num_classes = len(class_names),
        dropout     = DROPOUT,
        attn_drop   = 0.10,
        drop_path   = DROP_PATH,
    ).to(DEVICE)

    # print the parameter count, for easy comparison with the CNN version
    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[ViT1D] trainable params: {n_params:,}")
    print(f"[ViT1D] num_patches={TARGET_LEN // PATCH_SIZE}  d_model=128  depth=4  heads=4")

    class_counts  = np.array([(Y == i).sum() for i in range(len(class_names))], dtype=np.float32)
    class_weights = class_counts.mean() / np.maximum(class_counts, 1.0)
    class_weights = torch.tensor(class_weights, dtype=torch.float32, device=DEVICE)

    ce = nn.CrossEntropyLoss(weight=class_weights, label_smoothing=LABEL_SMOOTHING)

    # Standard for Transformers: AdamW (converges faster than SGD and is friendly to positional encodings)
    opt = torch.optim.AdamW(
        model.parameters(),
        lr           = LR,
        weight_decay = WEIGHT_DECAY,
        betas        = (0.9, 0.999),
    )

    # Warmup (5-epoch linear ramp-up) + Cosine annealing
    # gradients are unstable when a Transformer starts cold; warmup is standard practice
    warmup_epochs = 5
    def lr_lambda(ep):
        if ep < warmup_epochs:
            return float(ep + 1) / float(warmup_epochs)
        progress = float(ep - warmup_epochs) / float(max(1, EPOCHS - warmup_epochs))
        return max(0.05, 0.5 * (1.0 + math.cos(math.pi * progress)))

    scheduler = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda)

    best_val   = -1.0
    best_state = None
    patience   = 16   # a Transformer needs more patience than a CNN
    bad        = 0

    for ep in range(1, EPOCHS + 1):
        model.train()
        losses = []

        for x, y in train_loader:
            x = x.to(DEVICE, non_blocking=True)
            y = y.to(DEVICE, non_blocking=True)

            opt.zero_grad(set_to_none=True)
            logits = model(x)
            loss   = ce(logits, y)

            if not torch.isfinite(loss):
                print(f"[WARN] skip non-finite loss at epoch {ep}")
                continue

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP_NORM)
            opt.step()
            losses.append(float(loss.item()))

        scheduler.step()

        tr_loss    = float(np.mean(losses)) if losses else 0.0
        va         = eval_acc(model, val_loader)
        current_lr = opt.param_groups[0]["lr"]
        print(f"epoch {ep:02d}/{EPOCHS}  train_loss={tr_loss:.4f}  val_acc={va:.3f}  lr={current_lr:.2e}")

        if va > best_val + 1e-6:
            best_val   = va
            best_state = {k: v.detach().cpu() for k, v in model.state_dict().items()}
            bad        = 0
        else:
            bad += 1
            if bad >= patience:
                print("early stop")
                break

    if best_state is not None:
        model.load_state_dict(best_state)

    te = eval_acc(model, test_loader)
    print(f"TEST acc = {te:.3f}")

    torch.save(model.state_dict(), MODEL_PATH)

    meta = {
        "classes"          : class_names,
        "target_len"       : TARGET_LEN,
        "patch_size"       : PATCH_SIZE,
        "do_baseline"      : DO_BASELINE,
        "match_threshold"  : MATCH_THRESHOLD,
        "infer_temperature": INFER_TEMPERATURE,
        "infer_aggregation": INFER_AGGREGATION,
        "raw_len_range"    : [RAW_LEN_MIN, RAW_LEN_MAX],
        "timestamp"        : time.strftime("%Y-%m-%d %H:%M:%S"),
        "lib_files"        : LIB_FILES,
        "model_name"       : "RamanViT1D",
        "d_model"          : 128,
        "num_heads"        : 4,
        "depth"            : 4,
        "dropout"          : DROPOUT,
        "drop_path"        : DROP_PATH,
        "label_smoothing"  : LABEL_SMOOTHING,
        "use_augment"      : USE_AUGMENT,
        "weight_decay"     : WEIGHT_DECAY,
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("Saved model:", MODEL_PATH)
    print("Saved meta :", META_PATH)
    return model


# =========================================================
# Cell 9/10: load model + predict helper  (essentially the same as the original version)
# =========================================================
def load_trained_model():
    m = RamanViT1D(
        seq_len     = TARGET_LEN,
        patch_size  = PATCH_SIZE,
        d_model     = 128,
        num_heads   = 4,
        depth       = 4,
        mlp_ratio   = 4.0,
        num_classes = len(class_names),
        dropout     = DROPOUT,
        attn_drop   = 0.10,
        drop_path   = DROP_PATH,
    )
    sd = torch.load(MODEL_PATH, map_location="cpu")
    m.load_state_dict(sd)
    m.eval()
    return m


@torch.no_grad()
def _forward_logits(model: nn.Module, v: np.ndarray) -> np.ndarray:
    if not np.isfinite(v).all():
        v = np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)

    # ViT version: input [1, TARGET_LEN], no extra channel dimension needed
    x      = torch.from_numpy(v).unsqueeze(0).to(DEVICE)   # [1, TARGET_LEN]
    logits = model(x)

    logits = logits.squeeze(0).detach().cpu().numpy().astype(np.float32)
    logits = np.nan_to_num(logits, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
    return logits


def _filter_by_raw_len(spectra: List[np.ndarray], raw_min: int, raw_max: int) -> Tuple[List[np.ndarray], int]:
    if len(spectra) == 0:
        return spectra, 0
    kept    = []
    dropped = 0
    for s in spectra:
        n = int(getattr(s, "size", 0))
        if raw_min <= n <= raw_max:
            kept.append(s)
        else:
            dropped += 1
    if len(kept) == 0:
        return spectra, 0
    return kept, dropped


# =========================================================
# Cell 10/10: file-level predict  (identical to the original version)
# =========================================================
@torch.no_grad()
def predict_single_file(model: nn.Module, spectrum_path: str,
                        scan_all_spectra: bool = True, debug: bool = True):
    if not os.path.exists(spectrum_path):
        raise FileNotFoundError(spectrum_path)

    ext = os.path.splitext(spectrum_path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"Unsupported ext: {ext}")

    model = model.to(DEVICE)
    model.eval()

    spectra, failed      = parse_portable_log_to_spectra(spectrum_path)
    already_preprocessed = False

    if len(spectra) == 0:
        v_single = load_spectrum_text(spectrum_path, TARGET_LEN, DO_BASELINE).astype(np.float32)
        spectra  = [v_single]
        already_preprocessed = True
        dropped_len = 0
    else:
        spectra, dropped_len = _filter_by_raw_len(spectra, RAW_LEN_MIN, RAW_LEN_MAX)

    n_scan      = len(spectra) if scan_all_spectra else 1
    per_block   = []
    vote_counts = {c: 0 for c in class_names}

    for idx in range(n_scan):
        raw = spectra[idx]

        if already_preprocessed:
            v       = raw.astype(np.float32)
            raw_len = int(v.size)
        else:
            raw_len = int(raw.size)
            v = preprocess_intensity_to_vec(raw, TARGET_LEN, DO_BASELINE).astype(np.float32)

        if np.allclose(v, 0):
            continue

        logits = _forward_logits(model, v)
        prob   = softmax_np(logits / float(INFER_TEMPERATURE))

        match_rates = compute_match_rates_from_vec(v)
        top_id   = int(np.argmax(match_rates))
        top_name = id_to_class[top_id]
        top_rate = float(match_rates[top_id])

        vote_counts[top_name] += 1
        per_block.append({
            "idx"        : idx,
            "raw_len"    : raw_len,
            "logits"     : logits,
            "prob"       : prob,
            "match_rates": match_rates,
            "top_id"     : top_id,
            "top_name"   : top_name,
            "top_rate"   : top_rate,
        })

        if debug and match_rates.size >= 2:
            sorted_idx = np.argsort(match_rates)[::-1]
            top1 = id_to_class[int(sorted_idx[0])]
            top2 = id_to_class[int(sorted_idx[1])]
            r1   = float(match_rates[sorted_idx[0]])
            r2   = float(match_rates[sorted_idx[1]])
            print(f"[DEBUG] idx={idx:3d} raw_len={raw_len:4d} "
                  f"top1={top1}({r1:.4f}) top2={top2}({r2:.4f}) margin={(r1-r2):.4f}")

    if len(per_block) == 0:
        return {
            "matched"      : False,
            "label"        : "NOT MATCHED",
            "top_prob"     : 0.0,
            "top2_prob"    : 0.0,
            "margin"       : 0.0,
            "best_idx"     : 0,
            "num_spectra"  : int(len(spectra)),
            "num_used"     : 0,
            "dropped_len"  : int(dropped_len),
            "failed_blocks": int(failed),
            "probs"        : {c: 0.0 for c in class_names},
            "votes"        : vote_counts,
        }

    agg_rates = np.mean(
        np.stack([d["match_rates"] for d in per_block], axis=0), axis=0
    ).astype(np.float32)

    sorted_idx = np.argsort(agg_rates)[::-1]
    top1_id    = int(sorted_idx[0])
    top2_id    = int(sorted_idx[1])
    top1_name  = id_to_class[top1_id]
    top2_name  = id_to_class[top2_id]
    top1_prob  = float(agg_rates[top1_id])
    top2_prob  = float(agg_rates[top2_id])
    margin     = float(top1_prob - top2_prob)

    best_idx = max(per_block, key=lambda d: float(d["match_rates"][top1_id]))["idx"]

    if debug:
        print("\n[DEBUG] ===== FILE-LEVEL MATCH RATE AGGREGATION =====")
        print("[DEBUG] aggregation: mean_match_rates")
        print("[DEBUG] blocks used:", len(per_block), "/", n_scan, f"(dropped_len={dropped_len})")
        print("[DEBUG] votes:", vote_counts)
        print(f"[DEBUG] top1={top1_name}({top1_prob:.4f}) top2={top2_name}({top2_prob:.4f}) margin={margin:.4f}")
        sup = [d for d in per_block if d["idx"] == best_idx][0]
        print(f"[DEBUG] best_idx_for_top1={best_idx}  raw_len={sup['raw_len']}  "
              f"block_top1={sup['top_name']}({sup['top_rate']:.4f})")

    matched = (top1_prob >= MATCH_THRESHOLD) and (margin >= MATCH_MARGIN)

    prob_dict   = {id_to_class[i]: float(agg_rates[i]) for i in range(len(agg_rates))}
    prob_sorted = dict(sorted(prob_dict.items(), key=lambda kv: kv[1], reverse=True))

    return {
        "matched"      : matched,
        "label"        : top1_name if matched else "NOT MATCHED",
        "top_prob"     : top1_prob,
        "top2_prob"    : top2_prob,
        "margin"       : margin,
        "best_idx"     : int(best_idx),
        "num_spectra"  : int(len(spectra)),
        "num_used"     : int(len(per_block)),
        "dropped_len"  : int(dropped_len),
        "failed_blocks": int(failed),
        "probs"        : prob_sorted,
        "votes"        : vote_counts,
    }


# =========================================================
# run
# =========================================================
RUNTIME_TEST_FILE = sys.argv[1] if len(sys.argv) > 1 and str(sys.argv[1]).strip() else TEST_FILE

model  = train_model()
model2 = load_trained_model()
print("Loaded model from:", MODEL_PATH)

res = predict_single_file(model2, RUNTIME_TEST_FILE, scan_all_spectra=True, debug=True)

print("\n" + "=" * 90)
print("TEST FILE:", RUNTIME_TEST_FILE)
print(f"Parsed spectra: {res['num_spectra']}  used={res['num_used']}  best_idx={res['best_idx']}  "
      f"failed_blocks={res.get('failed_blocks', 0)}  dropped_len={res.get('dropped_len', 0)}")
print(f"top1_prob={res['top_prob']:.4f}  top2_prob={res['top2_prob']:.4f}  margin={res['margin']:.4f}")

print("\nProbabilities (Raman ViT-1D, file-level):")
for k, v in res["probs"].items():
    print(f"  - {k:10s}  {v:.4f}")

print("\nFINAL:")
if not res["matched"]:
    print(f"  -> NOT MATCHED (top1={res['top_prob']:.4f}, margin={res['margin']:.4f})")
else:
    print(f"  -> MATCHED: {res['label']}  (score={res['top_prob']:.4f})")