"""Reproducible Figure 4 benchmark for portable Raman mixture detection.

This runner builds one source-disjoint dataset, trains PCA-SVM, SE-ResNet1D,
and ViT-1D models, evaluates every model on the same test samples, and exports
the metrics and figures used by Figure 4(b)/(c).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import time
import warnings
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import Iterable, Sequence

import joblib
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / ".matplotlib"))
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.decomposition import PCA
from sklearn.metrics import precision_recall_fscore_support, roc_auc_score
from sklearn.model_selection import GridSearchCV
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from torch.utils.data import DataLoader, Dataset

torch.set_num_threads(max(1, min(8, os.cpu_count() or 1)))
warnings.filterwarnings("ignore", message="The `probability` parameter was deprecated", category=FutureWarning)


CLASSES = ("r6g", "methylene_blue", "adenine", "di_water")
TARGETS = CLASSES[:3]
TARGET_IDS = np.array([0, 1, 2], dtype=np.int64)
DISPLAY_MODELS = ("PCA-SVM", "1D-CNN", "ViT-1D")
DISPLAY_TARGETS = ("R6G", "Methylene blue", "Adenine")


@dataclass(frozen=True)
class Config:
    seed: int = 42
    train_ratio: float = 0.70
    val_ratio: float = 0.10
    test_ratio: float = 0.20
    n_binary_per_combo: int = 200
    n_ternary_per_combo: int = 100
    n_low_r6g_mb: int = 300
    low_r6g_ratios: tuple[float, ...] = (0.01, 0.02, 0.05, 0.10, 0.20)
    noise_std_ratio: float = 0.01
    threshold: float = 0.50
    pca_components: int = 20
    batch_size: int = 128
    cnn_epochs: int = 80
    vit_epochs: int = 100
    early_stop_patience: int = 12


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    default_root = here.parent / "build" / "Desktop_Qt_6_6_3_MSVC2019_64bit-Debug"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=default_root)
    parser.add_argument("--output-dir", type=Path, default=here / "outputs")
    parser.add_argument("--epochs-cnn", type=int, default=80)
    parser.add_argument("--epochs-vit", type=int, default=100)
    parser.add_argument("--reuse-dataset", action="store_true")
    parser.add_argument("--reuse-models", action="store_true")
    parser.add_argument("--models", nargs="+", choices=("svm", "cnn", "vit"), default=("svm", "cnn", "vit"))
    return parser.parse_args()


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def minmax_rows(x: np.ndarray) -> np.ndarray:
    lo = x.min(axis=1, keepdims=True)
    hi = x.max(axis=1, keepdims=True)
    return ((x - lo) / np.maximum(hi - lo, 1e-8)).astype(np.float32)


def apportion_class_counts(class_sizes: Sequence[int], total: int, ratio: float) -> np.ndarray:
    quotas = np.asarray(class_sizes, dtype=float) * ratio
    counts = np.floor(quotas).astype(int)
    remaining = total - int(counts.sum())
    order = np.argsort(-(quotas - counts), kind="stable")
    counts[order[:remaining]] += 1
    return counts


def source_splits(data_by_class: dict[str, np.ndarray], cfg: Config) -> dict[str, dict[str, np.ndarray]]:
    sizes = [len(data_by_class[name]) for name in CLASSES]
    total = sum(sizes)
    train_total = int(total * cfg.train_ratio)
    val_total = int(total * cfg.val_ratio)
    n_train = apportion_class_counts(sizes, train_total, cfg.train_ratio)
    n_val = apportion_class_counts(sizes, val_total, cfg.val_ratio)
    result = {split: {} for split in ("train", "val", "test")}
    for class_id, (name, size) in enumerate(zip(CLASSES, sizes)):
        rng = np.random.default_rng(cfg.seed + 1009 * (class_id + 1))
        perm = rng.permutation(size)
        a, b = n_train[class_id], n_train[class_id] + n_val[class_id]
        result["train"][name] = perm[:a]
        result["val"][name] = perm[a:b]
        result["test"][name] = perm[b:]
    return result


def split_count(total: int, split: str, cfg: Config) -> int:
    if split == "train":
        return int(total * cfg.train_ratio)
    if split == "val":
        return int(total * cfg.val_ratio)
    return total - int(total * cfg.train_ratio) - int(total * cfg.val_ratio)


def label_vector(class_ids: Iterable[int]) -> np.ndarray:
    label = np.zeros(len(CLASSES), dtype=np.int8)
    for class_id in class_ids:
        label[class_id] = 1
    return label


def build_partition(
    split: str,
    data_by_class: dict[str, np.ndarray],
    indices_by_class: dict[str, np.ndarray],
    cfg: Config,
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    split_offset = {"train": 0, "val": 1, "test": 2}[split]
    rng = np.random.default_rng(cfg.seed + 100_003 * (split_offset + 1))
    spectra: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    records: list[dict] = []

    def append_sample(raw: np.ndarray, ids: Sequence[int], kind: str, sources: Sequence[str], ratios: Sequence[float]) -> None:
        spectra.append(np.asarray(raw, dtype=np.float32))
        labels.append(label_vector(ids))
        records.append(
            {
                "split": split,
                "kind": kind,
                "classes": "+".join(CLASSES[i] for i in ids),
                "source_ids": ";".join(sources),
                "ratios": ";".join(f"{value:.8f}" for value in ratios),
            }
        )

    for class_id, name in enumerate(CLASSES):
        for source_index in indices_by_class[name]:
            append_sample(
                data_by_class[name][source_index],
                [class_id],
                "pure",
                [f"{name}:{int(source_index)}"],
                [1.0],
            )

    def draw(class_id: int) -> tuple[np.ndarray, str]:
        name = CLASSES[class_id]
        allowed = indices_by_class[name]
        source_index = int(allowed[rng.integers(0, len(allowed))])
        return data_by_class[name][source_index], f"{name}:{source_index}"

    def noisy(raw: np.ndarray) -> np.ndarray:
        scale = cfg.noise_std_ratio * max(float(np.std(raw)), 1e-6)
        return (raw + rng.normal(0.0, scale, size=raw.shape)).astype(np.float32)

    n_binary = split_count(cfg.n_binary_per_combo, split, cfg)
    for a, b in combinations(range(len(CLASSES)), 2):
        for _ in range(n_binary):
            x_a, source_a = draw(a)
            x_b, source_b = draw(b)
            ratio_a = float(rng.uniform(0.10, 0.90))
            append_sample(noisy(ratio_a * x_a + (1.0 - ratio_a) * x_b), [a, b], "binary", [source_a, source_b], [ratio_a, 1.0 - ratio_a])

    per_ratio_total = cfg.n_low_r6g_mb // len(cfg.low_r6g_ratios)
    per_ratio_split = split_count(per_ratio_total, split, cfg)
    for ratio_r6g in cfg.low_r6g_ratios:
        for _ in range(per_ratio_split):
            x_r6g, source_r6g = draw(0)
            x_mb, source_mb = draw(1)
            append_sample(
                noisy(ratio_r6g * x_r6g + (1.0 - ratio_r6g) * x_mb),
                [0, 1],
                "low_r6g_mb",
                [source_r6g, source_mb],
                [ratio_r6g, 1.0 - ratio_r6g],
            )

    n_ternary = split_count(cfg.n_ternary_per_combo, split, cfg)
    for combo in combinations(range(len(CLASSES)), 3):
        for _ in range(n_ternary):
            samples = [draw(class_id) for class_id in combo]
            while True:
                ratios = rng.dirichlet(np.ones(len(combo)))
                if np.all(ratios >= 0.10):
                    break
            raw = sum(float(ratio) * sample[0] for ratio, sample in zip(ratios, samples))
            append_sample(noisy(raw), list(combo), "ternary", [sample[1] for sample in samples], ratios.tolist())

    return np.vstack(spectra).astype(np.float32), np.vstack(labels).astype(np.int8), records


def build_shared_dataset(data_dir: Path, output_dir: Path, cfg: Config) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    raw_paths = {name: data_dir / f"training_spectra_{name}_baseline_raw.npy" for name in CLASSES}
    data_by_class = {name: np.load(path).astype(np.float32) for name, path in raw_paths.items()}
    feature_lengths = {array.shape[1] for array in data_by_class.values()}
    if feature_lengths != {1958}:
        raise ValueError(f"Expected 1958-point spectra, found {feature_lengths}")
    splits = source_splits(data_by_class, cfg)

    x_parts, y_parts, records = [], [], []
    for split in ("train", "val", "test"):
        x, y, split_records = build_partition(split, data_by_class, splits[split], cfg)
        x_parts.append(x)
        y_parts.append(y)
        records.extend(split_records)

    x_raw = np.vstack(x_parts)
    y_all = np.vstack(y_parts)
    manifest = pd.DataFrame(records)
    manifest.insert(0, "sample_id", [f"raman_{i:05d}" for i in range(len(manifest))])
    for class_id, name in enumerate(CLASSES):
        manifest[f"label_{name}"] = y_all[:, class_id]

    expected = {"train": 1649, "val": 235, "test": 472}
    actual = manifest.groupby("split").size().to_dict()
    if actual != expected:
        raise AssertionError(f"Unexpected split sizes: {actual}; expected {expected}")
    if len(manifest) != 2356 or int((manifest["kind"] != "pure").sum()) != 1900:
        raise AssertionError("Dataset size no longer matches 456 pure + 1900 synthetic spectra")

    source_sets: dict[str, set[str]] = {}
    for split in expected:
        source_sets[split] = set(
            source
            for value in manifest.loc[manifest["split"] == split, "source_ids"]
            for source in value.split(";")
        )
    if source_sets["train"] & source_sets["val"] or source_sets["train"] & source_sets["test"] or source_sets["val"] & source_sets["test"]:
        raise AssertionError("Source-spectrum leakage detected across splits")

    output_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output_dir / "benchmark_dataset.npz", x_raw=x_raw, y_all=y_all)
    manifest.to_csv(output_dir / "split_manifest.csv", index=False)
    return x_raw, y_all, manifest


def load_shared_dataset(output_dir: Path) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    dataset = np.load(output_dir / "benchmark_dataset.npz")
    return dataset["x_raw"], dataset["y_all"], pd.read_csv(output_dir / "split_manifest.csv")


DIAGNOSTIC_PEAKS = {
    "r6g": ((612, 30), (774, 30), (1360, 30), (1510, 30)),
    "methylene_blue": ((450, 30), (770, 30), (1395, 30), (1625, 30)),
    "adenine": ((730, 30), (1330, 30), (1460, 30)),
}


def extract_peak_features(spectrum: np.ndarray, cm1_min: float = 200.0, cm1_max: float = 3200.0) -> np.ndarray:
    spec = np.asarray(spectrum, dtype=np.float64)
    n = spec.size
    features: list[float] = []
    for target in TARGETS:
        for center, half_width in DIAGNOSTIC_PEAKS[target]:
            center_px = int((center - cm1_min) / (cm1_max - cm1_min) * n)
            half_px = max(3, int(half_width / (cm1_max - cm1_min) * n))
            lo, hi = max(0, center_px - half_px), min(n, center_px + half_px + 1)
            region = spec[lo:hi]
            bg = np.concatenate((spec[max(0, lo - 2 * half_px):lo], spec[hi:min(n, hi + 2 * half_px)]))
            bg_mean = float(bg.mean()) if bg.size else 0.0
            features.extend((float(region.max()), float(region.sum()), float(region.mean()) - bg_mean))
    features.extend((float(spec.max()), float(spec.std()), float(np.sum(spec > 0.3)), float(np.percentile(spec, 95) - np.percentile(spec, 5))))
    return np.asarray(features, dtype=np.float32)


def train_predict_svm(
    x_raw: np.ndarray,
    y_all: np.ndarray,
    train_idx: np.ndarray,
    test_idx: np.ndarray,
    output_dir: Path,
    cfg: Config,
    reuse: bool,
) -> np.ndarray:
    model_path = output_dir / "pca_svm.joblib"
    if reuse and model_path.exists():
        bundle = joblib.load(model_path)
    else:
        scaler = StandardScaler().fit(x_raw[train_idx])
        x_train_scaled = scaler.transform(x_raw[train_idx])
        pca = PCA(n_components=cfg.pca_components, random_state=cfg.seed).fit(x_train_scaled)
        x_train_pca = pca.transform(x_train_scaled)
        peak_train = np.vstack([extract_peak_features(row) for row in x_raw[train_idx]])
        feature_scaler = StandardScaler().fit(np.hstack((x_train_pca, peak_train)))
        features_train = feature_scaler.transform(np.hstack((x_train_pca, peak_train)))
        models = []
        param_grid = {"C": [0.1, 1.0, 10.0, 100.0], "gamma": ["scale", 0.01, 0.1]}
        for target_id in TARGET_IDS:
            search = GridSearchCV(
                SVC(kernel="rbf", probability=True, class_weight="balanced", random_state=cfg.seed),
                param_grid,
                scoring="f1",
                cv=5,
                n_jobs=-1,
            )
            search.fit(features_train, y_all[train_idx, target_id])
            models.append(search.best_estimator_)
            print(f"[PCA-SVM] {TARGETS[target_id]} best={search.best_params_}", flush=True)
        bundle = {"scaler": scaler, "pca": pca, "feature_scaler": feature_scaler, "models": models}
        joblib.dump(bundle, model_path)

    x_test_pca = bundle["pca"].transform(bundle["scaler"].transform(x_raw[test_idx]))
    peak_test = np.vstack([extract_peak_features(row) for row in x_raw[test_idx]])
    features_test = bundle["feature_scaler"].transform(np.hstack((x_test_pca, peak_test)))
    return np.column_stack([model.predict_proba(features_test)[:, 1] for model in bundle["models"]])


def augment_spectrum(x: np.ndarray) -> np.ndarray:
    result = x.copy() * np.float32(np.random.uniform(0.95, 1.05))
    shift = int(np.random.randint(-10, 11))
    if shift:
        result = np.roll(result, shift)
    result += np.random.normal(0.0, 0.02, result.shape).astype(np.float32)
    result += np.float32(np.random.uniform(-0.12, 0.12)) * np.linspace(-1.0, 1.0, result.size, dtype=np.float32)
    lo, hi = float(result.min()), float(result.max())
    return ((result - lo) / max(hi - lo, 1e-8)).astype(np.float32)


class RamanDataset(Dataset):
    def __init__(self, x: np.ndarray, y: np.ndarray, indices: np.ndarray, augment: bool, pad_to: int | None = None):
        self.x, self.y, self.indices, self.augment, self.pad_to = x, y, indices, augment, pad_to

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, item: int) -> tuple[torch.Tensor, torch.Tensor]:
        index = int(self.indices[item])
        x = self.x[index].copy()
        if self.augment:
            x = augment_spectrum(x)
        if self.pad_to is not None:
            padded = np.zeros(self.pad_to, dtype=np.float32)
            padded[: min(len(x), self.pad_to)] = x[: self.pad_to]
            x = padded
        return torch.from_numpy(x), torch.from_numpy(self.y[index].astype(np.float32))


class SEBlock1D(nn.Module):
    def __init__(self, channels: int, reduction: int = 8):
        super().__init__()
        hidden = max(4, channels // reduction)
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.fc = nn.Sequential(nn.Linear(channels, hidden), nn.ReLU(True), nn.Linear(hidden, channels), nn.Sigmoid())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, channels, _ = x.shape
        return x * self.fc(self.pool(x).view(batch, channels)).view(batch, channels, 1)


class ResBlock1D(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel: int, stride: int, dropout: float):
        super().__init__()
        padding = kernel // 2
        self.conv1 = nn.Conv1d(in_channels, out_channels, kernel, stride, padding, bias=False)
        self.bn1 = nn.BatchNorm1d(out_channels)
        self.conv2 = nn.Conv1d(out_channels, out_channels, kernel, 1, padding, bias=False)
        self.bn2 = nn.BatchNorm1d(out_channels)
        self.se = SEBlock1D(out_channels)
        self.dropout = nn.Dropout(dropout)
        self.shortcut = nn.Sequential(nn.Conv1d(in_channels, out_channels, 1, stride, bias=False), nn.BatchNorm1d(out_channels)) if stride != 1 or in_channels != out_channels else nn.Identity()
        self.activation = nn.GELU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        result = self.activation(self.bn1(self.conv1(x)))
        result = self.se(self.dropout(self.bn2(self.conv2(result))))
        return self.activation(result + self.shortcut(x))


class RamanCNN(nn.Module):
    def __init__(self, num_classes: int = 4, base: int = 32, dropout: float = 0.35):
        super().__init__()
        self.stem = nn.Sequential(nn.Conv1d(1, base, 7, padding=3, bias=False), nn.BatchNorm1d(base), nn.GELU(), nn.Conv1d(base, base, 5, padding=2, bias=False), nn.BatchNorm1d(base), nn.GELU())
        self.stage1 = nn.Sequential(ResBlock1D(base, base, 7, 1, dropout * 0.5), nn.MaxPool1d(2))
        self.stage2 = nn.Sequential(ResBlock1D(base, base * 2, 5, 1, dropout), nn.MaxPool1d(2))
        self.stage3 = nn.Sequential(ResBlock1D(base * 2, base * 4, 3, 1, dropout), nn.MaxPool1d(2))
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.head = nn.Sequential(nn.Linear(base * 4, 128), nn.GELU(), nn.Dropout(dropout), nn.Linear(128, num_classes))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.unsqueeze(1)
        return self.head(self.pool(self.stage3(self.stage2(self.stage1(self.stem(x))))).flatten(1))


class DropPath(nn.Module):
    def __init__(self, probability: float = 0.0):
        super().__init__()
        self.probability = probability

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.probability == 0.0 or not self.training:
            return x
        keep = 1.0 - self.probability
        mask = torch.floor(torch.rand((x.shape[0],) + (1,) * (x.ndim - 1), dtype=x.dtype, device=x.device) + keep)
        return x * mask / keep


class Attention(nn.Module):
    def __init__(self, d_model: int, heads: int, dropout: float):
        super().__init__()
        self.heads, self.head_dim = heads, d_model // heads
        self.scale = self.head_dim ** -0.5
        self.qkv = nn.Linear(d_model, 3 * d_model, bias=False)
        self.proj = nn.Linear(d_model, d_model, bias=False)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, tokens, channels = x.shape
        q, k, v = self.qkv(x).reshape(batch, tokens, 3, self.heads, self.head_dim).permute(2, 0, 3, 1, 4).unbind(0)
        weights = self.dropout((q @ k.transpose(-2, -1) * self.scale).softmax(dim=-1))
        return self.proj((weights @ v).transpose(1, 2).reshape(batch, tokens, channels))


class TransformerBlock(nn.Module):
    def __init__(self, d_model: int, heads: int, drop_path: float):
        super().__init__()
        self.norm1, self.norm2 = nn.LayerNorm(d_model), nn.LayerNorm(d_model)
        self.attention = Attention(d_model, heads, 0.10)
        self.path1, self.path2 = DropPath(drop_path), DropPath(drop_path)
        self.ffn = nn.Sequential(nn.Linear(d_model, 4 * d_model), nn.GELU(), nn.Dropout(0.15), nn.Linear(4 * d_model, d_model), nn.Dropout(0.15))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.path1(self.attention(self.norm1(x)))
        return x + self.path2(self.ffn(self.norm2(x)))


class RamanViT(nn.Module):
    def __init__(self, sequence_length: int = 2048, patch_size: int = 64, d_model: int = 128, heads: int = 4, depth: int = 4, num_classes: int = 4):
        super().__init__()
        self.num_patches, self.patch_size = sequence_length // patch_size, patch_size
        self.patch_projection = nn.Linear(patch_size, d_model, bias=False)
        self.patch_norm = nn.LayerNorm(d_model)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, d_model))
        self.pos_embed = nn.Parameter(torch.zeros(1, self.num_patches + 1, d_model))
        self.pos_drop = nn.Dropout(0.15)
        paths = torch.linspace(0.0, 0.10, depth).tolist()
        self.blocks = nn.ModuleList([TransformerBlock(d_model, heads, float(paths[i])) for i in range(depth)])
        self.norm = nn.LayerNorm(d_model)
        self.head = nn.Sequential(nn.Linear(d_model, 64), nn.GELU(), nn.Dropout(0.15), nn.Linear(64, num_classes))
        nn.init.trunc_normal_(self.cls_token, std=0.02)
        nn.init.trunc_normal_(self.pos_embed, std=0.02)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch = x.shape[0]
        x = self.patch_norm(self.patch_projection(x.view(batch, self.num_patches, self.patch_size)))
        x = self.pos_drop(torch.cat((self.cls_token.expand(batch, -1, -1), x), dim=1) + self.pos_embed)
        for block in self.blocks:
            x = block(x)
        return self.head(self.norm(x)[:, 0])


@torch.no_grad()
def predict_loader(model: nn.Module, loader: DataLoader, device: torch.device) -> tuple[np.ndarray, np.ndarray]:
    model.eval()
    probabilities, labels = [], []
    for x, y in loader:
        probabilities.append(torch.sigmoid(model(x.to(device))).cpu().numpy())
        labels.append(y.numpy())
    return np.vstack(probabilities), np.vstack(labels)


def macro_target_f1(y_true: np.ndarray, probabilities: np.ndarray, threshold: float) -> float:
    y_pred = (probabilities[:, :3] >= threshold).astype(int)
    return float(precision_recall_fscore_support(y_true[:, :3], y_pred, average="macro", zero_division=0)[2])


def train_predict_torch(
    model_name: str,
    x_deep: np.ndarray,
    y_all: np.ndarray,
    train_idx: np.ndarray,
    val_idx: np.ndarray,
    test_idx: np.ndarray,
    output_dir: Path,
    cfg: Config,
    reuse: bool,
    device: torch.device,
) -> np.ndarray:
    is_vit = model_name == "vit"
    model = RamanViT() if is_vit else RamanCNN()
    model = model.to(device)
    model_path = output_dir / f"{model_name}.pt"
    pad_to = 2048 if is_vit else None
    train_loader = DataLoader(RamanDataset(x_deep, y_all, train_idx, True, pad_to), batch_size=cfg.batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(RamanDataset(x_deep, y_all, val_idx, False, pad_to), batch_size=cfg.batch_size, shuffle=False, num_workers=0)
    test_loader = DataLoader(RamanDataset(x_deep, y_all, test_idx, False, pad_to), batch_size=cfg.batch_size, shuffle=False, num_workers=0)

    if reuse and model_path.exists():
        model.load_state_dict(torch.load(model_path, map_location=device))
    else:
        positive = y_all[train_idx].sum(axis=0)
        weights = torch.tensor((len(train_idx) - positive) / np.maximum(positive, 1), dtype=torch.float32, device=device)
        criterion = nn.BCEWithLogitsLoss(pos_weight=weights)
        optimizer = torch.optim.AdamW(model.parameters(), lr=5e-4, weight_decay=1e-4 if is_vit else 5e-4)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="max", factor=0.5, patience=5, min_lr=1e-6)
        epochs = cfg.vit_epochs if is_vit else cfg.cnn_epochs
        best_score, best_state, stale = -1.0, None, 0
        for epoch in range(1, epochs + 1):
            model.train()
            losses = []
            for x, y in train_loader:
                x, y = x.to(device), y.to(device)
                optimizer.zero_grad(set_to_none=True)
                loss = criterion(model(x), y)
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 3.0)
                optimizer.step()
                losses.append(float(loss.item()))
            val_probabilities, val_labels = predict_loader(model, val_loader, device)
            score = macro_target_f1(val_labels, val_probabilities, cfg.threshold)
            scheduler.step(score)
            print(f"[{model_name.upper()}] epoch={epoch:03d}/{epochs} loss={np.mean(losses):.4f} val_target_macro_f1={score:.4f}", flush=True)
            if score > best_score + 1e-6:
                best_score = score
                best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
                stale = 0
            else:
                stale += 1
                if stale >= cfg.early_stop_patience:
                    print(f"[{model_name.upper()}] early stopping at epoch {epoch}", flush=True)
                    break
        if best_state is None:
            raise RuntimeError(f"{model_name} training did not produce a checkpoint")
        model.load_state_dict(best_state)
        torch.save(best_state, model_path)

    test_probabilities, _ = predict_loader(model, test_loader, device)
    return test_probabilities[:, :3]


def compute_metrics(
    model_name: str,
    y_true: np.ndarray,
    probabilities: np.ndarray,
    test_manifest: pd.DataFrame,
    threshold: float,
) -> tuple[dict, list[dict], np.ndarray]:
    predictions = (probabilities >= threshold).astype(int)
    precision, recall, f1, support = precision_recall_fscore_support(y_true, predictions, average=None, zero_division=0)
    auc = np.asarray([roc_auc_score(y_true[:, i], probabilities[:, i]) for i in range(3)])
    per_target = []
    for target_id, target in enumerate(TARGETS):
        true = y_true[:, target_id]
        pred = predictions[:, target_id]
        per_target.append(
            {
                "model": model_name,
                "target": target,
                "precision": precision[target_id],
                "recall": recall[target_id],
                "f1": f1[target_id],
                "roc_auc": auc[target_id],
                "support": int(support[target_id]),
                "tp": int(np.sum((true == 1) & (pred == 1))),
                "fp": int(np.sum((true == 0) & (pred == 1))),
                "fn": int(np.sum((true == 1) & (pred == 0))),
                "tn": int(np.sum((true == 0) & (pred == 0))),
            }
        )
    mixture_mask = (y_true.sum(axis=1) >= 2) & (test_manifest["kind"].to_numpy() != "pure")
    overall = {
        "model": model_name,
        "n_test": len(y_true),
        "n_multi_target_mixture": int(mixture_mask.sum()),
        "target_macro_f1": float(f1.mean()),
        "target_macro_roc_auc": float(auc.mean()),
        "r6g_recall": float(recall[0]),
        "mixture_exact_match": float(np.mean(np.all(predictions[mixture_mask] == y_true[mixture_mask], axis=1))),
        "threshold": threshold,
    }
    return overall, per_target, predictions


def annotate_heatmap(ax: plt.Axes, values: np.ndarray, fmt: str = ".1%") -> None:
    midpoint = (float(np.nanmin(values)) + float(np.nanmax(values))) / 2.0
    for row in range(values.shape[0]):
        for col in range(values.shape[1]):
            ax.text(col, row, format(values[row, col], fmt), ha="center", va="center", color="white" if values[row, col] >= midpoint else "black", fontweight="bold")


def plot_figure4b(overall: pd.DataFrame, output_dir: Path) -> None:
    columns = ("target_macro_f1", "target_macro_roc_auc", "r6g_recall", "mixture_exact_match")
    labels = ("Target macro-F1", "Target macro ROC-AUC", "R6G recall", "Mixture exact-match")
    values = overall.set_index("model").loc[list(DISPLAY_MODELS), list(columns)].to_numpy(float)
    fig, ax = plt.subplots(figsize=(10.2, 4.4))
    image = ax.imshow(values, cmap="YlGnBu", vmin=max(0.50, float(values.min()) - 0.05), vmax=1.0, aspect="auto")
    annotate_heatmap(ax, values)
    ax.set_xticks(range(len(labels)), labels=labels, rotation=20, ha="right")
    ax.set_yticks(range(len(DISPLAY_MODELS)), labels=DISPLAY_MODELS)
    ax.set_title("Overall target-analyte performance on the shared test split")
    cbar = fig.colorbar(image, ax=ax, fraction=0.035, pad=0.03)
    cbar.set_label("Score")
    fig.tight_layout()
    for suffix in ("png", "pdf", "svg"):
        fig.savefig(output_dir / f"figure4b_model_comparison.{suffix}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_figure4c(per_target: pd.DataFrame, output_dir: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(12.8, 4.2), sharey=True)
    last_image = None
    for ax, target, display_target in zip(axes, TARGETS, DISPLAY_TARGETS):
        subset = per_target[per_target["target"] == target].set_index("model").loc[list(DISPLAY_MODELS)]
        values = subset[["precision", "recall", "f1"]].to_numpy(float)
        last_image = ax.imshow(values, cmap="viridis", vmin=0.50, vmax=1.0, aspect="auto")
        annotate_heatmap(ax, values)
        ax.set_xticks(range(3), labels=("Precision", "Recall", "F1"), rotation=20, ha="right")
        ax.set_title(display_target)
    axes[0].set_yticks(range(3), labels=DISPLAY_MODELS)
    fig.suptitle("Per-target precision, recall, and F1 score")
    cbar = fig.colorbar(last_image, ax=axes, fraction=0.025, pad=0.03)
    cbar.set_label("Score")
    fig.subplots_adjust(left=0.11, right=0.91, bottom=0.19, top=0.82, wspace=0.22)
    for suffix in ("png", "pdf", "svg"):
        fig.savefig(output_dir / f"figure4c_per_target_metrics.{suffix}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    args = parse_args()
    cfg = Config(cnn_epochs=args.epochs_cnn, vit_epochs=args.epochs_vit)
    output_dir = args.output_dir.resolve()
    models_dir = output_dir / "models"
    output_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)
    set_seed(cfg.seed)
    started = time.time()

    data_dir = args.data_root.resolve() / "data_base_code_part"
    if args.reuse_dataset and (output_dir / "benchmark_dataset.npz").exists():
        x_raw, y_all, manifest = load_shared_dataset(output_dir)
    else:
        x_raw, y_all, manifest = build_shared_dataset(data_dir, output_dir, cfg)
    x_deep = minmax_rows(x_raw)
    train_idx = np.flatnonzero(manifest["split"].to_numpy() == "train")
    val_idx = np.flatnonzero(manifest["split"].to_numpy() == "val")
    test_idx = np.flatnonzero(manifest["split"].to_numpy() == "test")
    y_test = y_all[test_idx][:, :3]
    test_manifest = manifest.iloc[test_idx].reset_index(drop=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device}; split sizes={len(train_idx)}/{len(val_idx)}/{len(test_idx)}", flush=True)
    probability_by_model: dict[str, np.ndarray] = {}
    if "svm" in args.models:
        probability_by_model["PCA-SVM"] = train_predict_svm(x_raw, y_all, train_idx, test_idx, models_dir, cfg, args.reuse_models)
    if "cnn" in args.models:
        probability_by_model["1D-CNN"] = train_predict_torch("cnn", x_deep, y_all, train_idx, val_idx, test_idx, models_dir, cfg, args.reuse_models, device)
    if "vit" in args.models:
        probability_by_model["ViT-1D"] = train_predict_torch("vit", x_deep, y_all, train_idx, val_idx, test_idx, models_dir, cfg, args.reuse_models, device)

    overall_rows, target_rows, prediction_frames = [], [], []
    for model_name, probabilities in probability_by_model.items():
        overall, targets, predictions = compute_metrics(model_name, y_test, probabilities, test_manifest, cfg.threshold)
        overall_rows.append(overall)
        target_rows.extend(targets)
        frame = test_manifest[["sample_id", "split", "kind", "classes"]].copy()
        frame.insert(0, "model", model_name)
        for target_id, target in enumerate(TARGETS):
            frame[f"true_{target}"] = y_test[:, target_id]
            frame[f"prob_{target}"] = probabilities[:, target_id]
            frame[f"pred_{target}"] = predictions[:, target_id]
        prediction_frames.append(frame)

    overall_df = pd.DataFrame(overall_rows)
    per_target_df = pd.DataFrame(target_rows)
    overall_df.to_csv(output_dir / "overall_metrics.csv", index=False)
    per_target_df.to_csv(output_dir / "per_target_metrics.csv", index=False)
    pd.concat(prediction_frames, ignore_index=True).to_csv(output_dir / "test_predictions.csv", index=False)
    if set(overall_df["model"]) == set(DISPLAY_MODELS):
        plot_figure4b(overall_df, output_dir)
        plot_figure4c(per_target_df, output_dir)

    input_paths = [data_dir / f"training_spectra_{name}_baseline_raw.npy" for name in CLASSES]
    run_manifest = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": time.time() - started,
        "config": asdict(cfg),
        "data_files": {path.name: sha256(path) for path in input_paths},
        "dataset_sha256": sha256(output_dir / "benchmark_dataset.npz"),
        "split_manifest_sha256": sha256(output_dir / "split_manifest.csv"),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scikit_learn": sklearn.__version__,
        "torch": torch.__version__,
        "device": str(device),
        "models": list(probability_by_model),
        "benchmark_threshold_note": "All benchmark target probabilities are binarized at 0.50. The 0.40/0.25 real-scan reporting rule is separate.",
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(run_manifest, indent=2), encoding="utf-8")
    print("\nOVERALL METRICS", flush=True)
    print(overall_df.to_string(index=False), flush=True)
    print(f"\nOutputs: {output_dir}", flush=True)


if __name__ == "__main__":
    main()
