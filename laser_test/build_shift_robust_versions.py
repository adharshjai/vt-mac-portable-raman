"""Derive new model-native reports and train-only shift robustness controls."""
from pathlib import Path
import ast
import json
import re
import sys

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/pure_only_20260923'
DEST = PROJECT / 'shift_robust_v2'
DEST.mkdir(exist_ok=True)

AUGMENT = '''def augment_spectrum_1d(x):
    # Same symmetric shift policy for ALL classes; train set only.
    # Preserve ViT padding and do not wrap the spectrum around its edges.
    axis = AUGMENT_AXIS
    n = len(axis)
    values = x[:n].copy()
    delta = float(np.random.uniform(-8.0, 8.0))
    values = np.interp(axis + delta, axis, values, left=values[0], right=values[-1])
    values = normalize_min_max(values).astype(np.float32)
    result = np.zeros_like(x, dtype=np.float32)
    result[:n] = values
    return result
'''

REPORT = '''
def export_model_benchmark(model):
    # Evaluation is always run, including when loading an existing checkpoint.
    from sklearn.metrics import precision_recall_fscore_support, confusion_matrix, roc_curve, roc_auc_score
    import matplotlib.pyplot as plt
    model.eval()
    truth, scores = [], []
    with torch.no_grad():
        for x, y in test_loader:
            logits, _ = model(x.to(DEVICE))
            scores.append(torch.sigmoid(logits).cpu().numpy())
            truth.append(y.numpy())
    truth = np.vstack(truth).astype(int)
    scores = np.vstack(scores)
    assert np.isfinite(scores).all()
    predicted = (scores >= BENCHMARK_THRESHOLD).astype(int)
    folder = Path(OUTPUT_DIR) / "benchmark"
    folder.mkdir(exist_ok=True)
    rows = []
    fig, axes = plt.subplots(1, NUM_CLASSES, figsize=(15, 3.8))
    roc_fig, roc_ax = plt.subplots(figsize=(7, 5))
    print("\\n" + "=" * 70)
    print("MODEL-NATIVE HELD-OUT TEST REPORT (spectrum-level split)")
    for cid, name in enumerate(class_names):
        tn, fp, fn, tp = confusion_matrix(truth[:,cid], predicted[:,cid], labels=[0,1]).ravel()
        precision, recall, f1, _ = precision_recall_fscore_support(truth[:,cid], predicted[:,cid], average="binary", zero_division=0)
        auc = float(roc_auc_score(truth[:,cid], scores[:,cid])) if len(np.unique(truth[:,cid])) == 2 else None
        rows.append(dict(target=name, TP=int(tp), FP=int(fp), FN=int(fn), TN=int(tn), precision=float(precision), recall=float(recall), f1=float(f1), roc_auc=auc))
        print(f"[{name:20s}] TP={tp} FP={fp} FN={fn} TN={tn}")
        print(f"  Precision={precision:.4f} Recall={recall:.4f} F1={f1:.4f} ROC-AUC={auc}")
        matrix=np.array([[tn,fp],[fn,tp]])
        axes[cid].imshow(matrix, cmap="Blues")
        for r in range(2):
            for c in range(2): axes[cid].text(c,r,str(matrix[r,c]),ha="center",va="center")
        axes[cid].set(xticks=[0,1], yticks=[0,1], xlabel="Predicted", ylabel="True", title=name)
        if auc is not None:
            fpr,tpr,_=roc_curve(truth[:,cid],scores[:,cid]); roc_ax.plot(fpr,tpr,label=f"{name}: {auc:.3f}")
    fig.tight_layout(); fig.savefig(folder/"per_target_confusion.png",dpi=180); plt.close(fig)
    score_fig, score_ax = plt.subplots(figsize=(8, 4))
    positions=np.arange(len(TARGET_CIDS)); width=.25
    for offset, key in enumerate(["precision","recall","f1"]):
        score_ax.bar(positions+(offset-1)*width,[rows[c][key] for c in TARGET_CIDS],width,label=key)
    score_ax.set(xticks=positions,xticklabels=[class_names[c] for c in TARGET_CIDS],ylim=(0,1.05),ylabel="Score",title="Held-out per-target performance")
    score_ax.legend(); score_fig.tight_layout(); score_fig.savefig(folder/"per_target_metrics.png",dpi=180); plt.close(score_fig)
    roc_ax.plot([0,1],[0,1],"k--",lw=1); roc_ax.set(xlabel="False-positive rate",ylabel="True-positive rate",title="Held-out pure-spectrum ROC (not independent batches)")
    roc_ax.legend(); roc_fig.tight_layout(); roc_fig.savefig(folder/"roc_curves.png",dpi=180); plt.close(roc_fig)
    with (folder/"per_target_metrics.csv").open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    macro=float(np.mean([rows[c]["f1"] for c in TARGET_CIDS]))
    overall=dict(model=MODEL_LABEL,n_test=len(truth),threshold=BENCHMARK_THRESHOLD,target_macro_f1=macro,
                 r6g_recall=rows[class_to_id["r6g"]]["recall"],target_exact_match=float(np.all(predicted[:,TARGET_CIDS]==truth[:,TARGET_CIDS],axis=1).mean()),
                 mixture_exact_match=None,n_mixture=0,warning="Pure-only spectrum split; same-scan dependence; mixed samples unvalidated")
    with (folder/"overall_metrics.csv").open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(overall)); writer.writeheader(); writer.writerow(overall)
    np.savez(folder/"test_predictions.npz",y_true=truth,y_score=scores,y_pred=predicted,test_indices=np.array(idx_te),classes=np.array(class_names))
    print(f"Target Macro-F1={macro:.4f}; mixture exact-match=N/A (no mixture samples)")
    print("F1 = 2 * precision * recall / (precision + recall).")
    print("This 13-point real scan was inspected during development; use a new independent scan for final validation.")
    print("Saved benchmark:", folder)

def export_scan_scores(spec_path, accepted, rejected):
    # No SVM predictions, nearest-template overrides or assumed point labels.
    folder=Path(OUTPUT_DIR)/"scan_model_scores"
    folder.mkdir(exist_ok=True)
    rows=[]
    for block in accepted:
        row=dict(index=block["idx"],point_id=block["idx"]+1,status="ACCEPTED",reason="")
        row.update({name:float(block["probs"][cid]) for cid,name in enumerate(class_names)})
        rows.append(row)
    for block in rejected:
        row=dict(index=block["idx"],point_id=block["idx"]+1,status="REJECTED",reason=block["reason"])
        row.update({name:None for name in class_names}); rows.append(row)
    rows.sort(key=lambda r:r["index"])
    single=re.fullmatch(r"point_(\\d+)",Path(spec_path).stem)
    if single and len(rows)==1: rows[0]["point_id"]=int(single.group(1))
    with (folder/"per_point_scores.csv").open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=["index","point_id","status","reason"]+class_names); writer.writeheader(); writer.writerows(rows)
    (folder/"source.json").write_text(json.dumps(dict(source=str(spec_path),model=MODEL_LABEL,threshold=OVR_FINAL_THRESHOLD,checkpoint=MODEL_PATH),indent=2),encoding="utf-8")
'''

for stem, label in [('CNN3_1_pure_only', 'CNN'), ('Transformer5_1_pure_only', 'ViT-1D')]:
    text=(PROJECT/(stem+'.py')).read_text(encoding='utf-8')
    text=text.replace('import os, sys, re, json, csv, time, math, random', 'import os, sys, re, json, csv, time, math, random\nimport matplotlib\nif "ipykernel" not in sys.modules:\n    matplotlib.use("Agg")')
    text=text.replace('USE_AUGMENT = False', 'USE_AUGMENT = True')
    text=text.replace('BENCHMARK_THRESHOLD = 0.50','BENCHMARK_THRESHOLD = 0.40')
    text=text.replace('thresholds are unchanged controls.', 'benchmark and inference thresholds fixed at 0.40 (not calibrated).')
    text=re.sub(r'^OUTPUT_DIR = .+$', lambda _:f'OUTPUT_DIR = {str(DEST/(stem+"_results"))!r}', text, flags=re.M)
    text=text.replace('cnn_pure_only.pt','cnn_shift_robust.pt').replace('vit1d_pure_only.pt','vit1d_shift_robust.pt')
    text=text.replace('no synthetic mixtures, no augmentation; old weights are not used.', 'no synthetic mixtures; train-only symmetric shift augmentation; new model weights.')
    pos=text.index('def augment_spectrum_1d('); end=text.index('class RamanDatasetML',pos)
    text=text[:pos]+'AUGMENT_AXIS = np.load(os.path.join(DATA_DIR, "raman_shift_axis_methylene_blue.npy"))\n'+AUGMENT+'\n'+text[end:]
    text=text.replace('with open(os.path.join(OUTPUT_DIR, "pure_experiment_manifest.json"),',
        'pure_manifest["augmentation"] = {"kind": "symmetric shift, training only", "max_cm1": 8.0, "all_classes": True}\nwith open(os.path.join(OUTPUT_DIR, "pure_experiment_manifest.json"),')
    # Strict raw quality gates apply before dark subtraction or normalization.
    text=text.replace('    per_block=[]\n', '    if dark_raw.size != 2048 or not np.isfinite(dark_raw).all():\n        raise ValueError("A complete matching 2048-pixel dark file is required")\n    per_block=[]\n')
    text=text.replace('    for idx,raw in enumerate(spectra):\n', '    for idx,raw in enumerate(spectra):\n        if len(raw)!=2048 or not np.isfinite(raw).all() or np.any(raw>=65535):\n            rejected_blocks.append({"idx":idx,"reason":"invalid length/nonfinite/saturated raw spectrum"})\n            continue\n')
    # Replace average-threshold decision with explicit pointwise union.
    start=text.index('    # Detect target analytes') if '    # Detect target analytes' in text else text.index('    detected=set()',text.index('def predict_file'))
    end=text.index('    print("="*70)',start)
    text=text[:start]+'''    print("\\n  Components with at least one above-threshold point (not proof of a mixture):")
    for cid in TARGET_CIDS:
        indices=[b["idx"] for b in per_block if b["probs"][cid]>=OVR_FINAL_THRESHOLD]
        if indices:
            print(f"    {id_to_class[cid]:20s} mean_score={avg_probs[cid]:.4f}; positive_spectrum_indices={indices}")
    print("  Mean scores are descriptive, not scan-wide presence decisions or accuracy.")
'''+text[end:]
    text=text.replace('    n_acc=len(per_block)', '    export_scan_scores(spec_path, per_block, rejected_blocks)\n    n_acc=len(per_block)')
    # Define common report functions before the RUN section.
    pos=text.index('# RUN\n')
    text=text[:pos]+f'MODEL_LABEL = {label!r}\n'+REPORT+'\n'+text[pos:]
    text+='\nexport_model_benchmark(model)\n'
    ast.parse(text)
    target=DEST/(stem.replace('pure_only','shift_robust')+'.py')
    if target.exists() and '--refresh' not in sys.argv: raise FileExistsError(target)
    target.write_text(text,encoding='utf-8')
    nb={'cells':[{'cell_type':'markdown','metadata':{},'source':['# Train-only shift-robust pure-spectrum control\n','No synthetic mixtures. Fresh weights; all reported scores come from this model. Training-only shifts +/-8 cm-1 for all classes. Real scan is development data, not independent validation.\n']},
        {'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':text.splitlines(keepends=True)}],
        'metadata':{'kernelspec':{'name':'python3','display_name':'Python 3','language':'python'}},'nbformat':4,'nbformat_minor':5}
    target.with_suffix('.ipynb').write_text(json.dumps(nb,indent=1),encoding='utf-8')
    print(target.with_suffix('.ipynb'))
