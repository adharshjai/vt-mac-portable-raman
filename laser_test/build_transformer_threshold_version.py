"""Create a validation-selected R6G threshold comparison; preserve v2 weights."""
from pathlib import Path
import ast
import json
import re

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / 'build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/pure_only_20260923'
SOURCE = PROJECT / 'shift_robust_v2/Transformer5_1_shift_robust.py'
CHECKPOINT = PROJECT / 'shift_robust_v2/Transformer5_1_pure_only_results/models/vit1d_shift_robust.pt'
DEST = PROJECT / 'r6g_threshold_v3'
DEST.mkdir(exist_ok=True)
target = DEST / 'Transformer5_1_r6g_threshold.py'
if target.exists():
    raise FileExistsError(target)
text = SOURCE.read_text(encoding='utf-8')
text = re.sub(r'^OUTPUT_DIR = .+$', lambda _:f'OUTPUT_DIR = {str(DEST / "results")!r}', text, flags=re.M)
text = text.replace('MODEL_PATH = os.path.join(MODELS_DIR, "vit1d_shift_robust.pt")',
                    'MODEL_PATH = os.path.join(MODELS_DIR, "vit1d_threshold_retrained.pt")')
config = '''
# None: select R6G threshold using validation samples only.
# A manual number is for sensitivity experiments, not test-set tuning.
R6G_THRESHOLD_OVERRIDE = None
R6G_NEAR_THRESHOLD_MARGIN = 0.001
CLASS_THRESHOLDS = {"r6g": 0.40, "methylene_blue": 0.40, "adenine": 0.40, "di_water": 0.40}
def target_threshold(name):
    return CLASS_THRESHOLDS[name]
def threshold_vector():
    return np.array([target_threshold(name) for name in class_names], dtype=np.float64)
'''
text = text.replace('R6G_WEAK_THRESHOLD_WHEN_MB_DOMINANT = OVR_FINAL_THRESHOLD  # No relaxed R6G threshold', config)
text = text.replace('benchmark and inference thresholds fixed at 0.40 (not calibrated).',
                    'R6G decision threshold will be selected on validation data; other classes stay at 0.40.')
text = text.replace('if v >= OVR_FINAL_THRESHOLD', 'if v >= target_threshold(n)')
text = text.replace('avg_probs[k]>=OVR_FINAL_THRESHOLD', 'avg_probs[k]>=target_threshold(id_to_class[k])')
text = text.replace('b["probs"][cid]>=OVR_FINAL_THRESHOLD', 'b["probs"][cid]>=target_threshold(id_to_class[cid])')
text = text.replace('Point threshold: {OVR_FINAL_THRESHOLD:.2f}; weak R6G rule NOT used for these notes.',
                    'Point thresholds: {CLASS_THRESHOLDS}; weak R6G rule disabled.')
text = text.replace('"Note: \'only\' means only one target exceeded the threshold, not proof of chemical purity.",',
                    '"Note: \'only\' means only one target exceeded the threshold, not proof of chemical purity.",\n        f"R6G scores within {R6G_NEAR_THRESHOLD_MARGIN} of its threshold are sensitive to small changes; inspect per_point_scores.csv.",')
text = text.replace('probs[k]:.3f', 'probs[k]:.8f')
text = text.replace('predicted = (scores >= BENCHMARK_THRESHOLD).astype(int)',
                    'predicted = (scores.astype(np.float64) >= threshold_vector()).astype(int)')
text = text.replace('dict(target=name, TP=', 'dict(target=name, threshold=target_threshold(name), TP=')
text = text.replace('threshold=BENCHMARK_THRESHOLD,target_macro_f1=macro',
                    'threshold=json.dumps(CLASS_THRESHOLDS),target_macro_f1=macro')
text = text.replace('threshold=OVR_FINAL_THRESHOLD,checkpoint=MODEL_PATH',
                    'thresholds=CLASS_THRESHOLDS,checkpoint=MODEL_PATH')
text = text.replace('row.update({name:float(block["probs"][cid]) for cid,name in enumerate(class_names)})',
    '''row.update({name:float(block["probs"][cid]) for cid,name in enumerate(class_names)})
        row["r6g_threshold"] = target_threshold("r6g")
        row["r6g_distance_to_threshold"] = row["r6g"] - target_threshold("r6g")
        row["r6g_near_threshold"] = abs(row["r6g_distance_to_threshold"]) < R6G_NEAR_THRESHOLD_MARGIN''')
text = text.replace('row.update({name:None for name in class_names}); rows.append(row)',
    'row.update({name:None for name in class_names}); row.update(r6g_threshold=target_threshold("r6g"),r6g_distance_to_threshold=None,r6g_near_threshold=None); rows.append(row)')
text = text.replace('fieldnames=["index","point_id","status","reason"]+class_names',
    'fieldnames=["index","point_id","status","reason"]+class_names+["r6g_threshold","r6g_distance_to_threshold","r6g_near_threshold"]')

# Display actual class thresholds on both existing scan plots.
text = text.replace('plt.axhline(OVR_FINAL_THRESHOLD,linestyle="--",alpha=0.55,label=f"threshold={OVR_FINAL_THRESHOLD}")',
    'plt.axhline(0.40,linestyle="--",alpha=0.55,label="Other classes: 0.40")')
text = text.replace('plt.axhline(R6G_WEAK_THRESHOLD_WHEN_MB_DOMINANT,linestyle=":",alpha=0.55,label=f"weak r6g={R6G_WEAK_THRESHOLD_WHEN_MB_DOMINANT}")',
    'plt.axhline(target_threshold("r6g"),linestyle=":",alpha=0.8,label=f"R6G: {target_threshold(\'r6g\'):.8f}")')
text = text.replace('plt.axhline(OVR_FINAL_THRESHOLD*100,linestyle="--",alpha=0.55,label=f"threshold={OVR_FINAL_THRESHOLD}")',
    'plt.axhline(40,linestyle="--",alpha=0.55,label="Other classes: 40% (descriptive)")')
text = text.replace('plt.axhline(R6G_WEAK_THRESHOLD_WHEN_MB_DOMINANT*100,linestyle=":",alpha=0.55,label=f"weak r6g={R6G_WEAK_THRESHOLD_WHEN_MB_DOMINANT}")',
    'plt.axhline(target_threshold("r6g")*100,linestyle=":",alpha=0.8,label=f"R6G: {target_threshold(\'r6g\')*100:.6f}% (point threshold)")')

calibration = '''
def select_r6g_threshold(model):
    from sklearn.metrics import f1_score, precision_score, recall_score
    import hashlib
    model.eval()
    labels, probabilities = [], []
    with torch.no_grad():
        for x, y in val_loader:
            logits, _ = model(x.to(DEVICE))
            probabilities.append(torch.sigmoid(logits).cpu().numpy())
            labels.append(y.numpy())
    labels = np.vstack(labels).astype(int)
    probabilities = np.vstack(probabilities).astype(np.float64)
    cid = class_to_id["r6g"]
    y, scores = labels[:,cid], probabilities[:,cid]
    if set(np.unique(y)) != {0, 1} or not np.isfinite(scores).all():
        raise ValueError("R6G threshold requires finite validation scores and both label classes")
    def metrics(t):
        pred = scores >= t
        return dict(threshold=float(t), f1=float(f1_score(y,pred,zero_division=0)),
                    precision=float(precision_score(y,pred,zero_division=0)),
                    recall=float(recall_score(y,pred,zero_division=0)),
                    TP=int(np.sum(pred & (y==1))), FP=int(np.sum(pred & (y==0))),
                    FN=int(np.sum(~pred & (y==1))), TN=int(np.sum(~pred & (y==0))))
    unique = np.unique(scores)
    # Only consider raising R6G from the previous 0.40. Never use test/scan labels.
    candidates = np.unique(np.r_[0.40, (unique[:-1]+unique[1:])/2, 1.0])
    rows = [metrics(t) for t in candidates if t>=0.40]
    best = max(rows,key=lambda row:(row["f1"],row["precision"],row["recall"],-abs(row["threshold"]-.40)))
    threshold = best["threshold"] if R6G_THRESHOLD_OVERRIDE is None else float(R6G_THRESHOLD_OVERRIDE)
    if not 0.40 <= threshold <= 1.0:
        raise ValueError("R6G threshold must be within [0.40, 1.0]")
    CLASS_THRESHOLDS["r6g"] = threshold
    nearest = float(np.min(abs(scores-threshold)))
    report = dict(selection="validation F1; ties precision, recall, then closest to 0.40",
                  checkpoint=MODEL_PATH,checkpoint_sha256=hashlib.sha256(Path(MODEL_PATH).read_bytes()).hexdigest(),
                  validation_indices=idx_va, baseline=metrics(.40), selected=metrics(threshold),
                  override=R6G_THRESHOLD_OVERRIDE, thresholds=CLASS_THRESHOLDS.copy(),
                  nearest_validation_score_distance=nearest,
                  sensitivity=[metrics(float(np.clip(threshold+d,.0,1.0))) for d in [-.001,-.0001,0,.0001,.001]],
                  warning="Threshold changes decisions only. Tiny score differences may not generalize; Adenine decision unchanged.")
    folder=Path(OUTPUT_DIR)/"calibration"; folder.mkdir(exist_ok=True)
    (folder/"r6g_threshold.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    np.savez(folder/"validation_predictions.npz",y_true=labels,y_score=probabilities,validation_indices=np.array(idx_va))
    print("\\nR6G threshold selection using VALIDATION only")
    print("Before:",report["baseline"])
    print("After:",report["selected"])
    print(f"Nearest validation score is {nearest:.10f} away from threshold.")
    if nearest < R6G_NEAR_THRESHOLD_MARGIN:
        print("WARNING: compressed scores; this threshold is fragile. Higher threshold does not improve learned separation.")
    print("Other class thresholds remain 0.40; Adenine false positives are not corrected by raising R6G.")

'''
start = text.index('# RUN\n')
text = text[:start]+calibration+text[start:]
start = text.index('if RETRAIN_MODEL or not os.path.exists(MODEL_PATH):',text.index('# RUN\n'))
end = text.index('print("\\n"+"="*70); print("Inference")', start)
run = f'''SOURCE_CHECKPOINT = {str(CHECKPOINT)!r}
if RETRAIN_MODEL:
    model = train_model()
else:
    if not os.path.exists(MODEL_PATH):
        MODEL_PATH = SOURCE_CHECKPOINT
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError("Train the shift_robust_v2 model first or set RETRAIN_MODEL=True")
    model = RamanViT1D().to(DEVICE)
    model.load_state_dict(torch.load(MODEL_PATH, map_location=DEVICE, weights_only=True))
    model.eval()
    print(f"Loaded: {{MODEL_PATH}}")
select_r6g_threshold(model)

'''
text = text[:start]+run+text[end:]
ast.parse(text)
target.write_text(text,encoding='utf-8')
nb={'cells':[{'cell_type':'markdown','metadata':{},'source':['# Transformer: validation-selected R6G threshold\n','Default: reuse the existing v2 weights; no retraining. Only R6G decision threshold changes. Calibration uses validation samples only; scores near the threshold are flagged. Other false positives can remain.\n']},
    {'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':text.splitlines(keepends=True)}],
    'metadata':{'kernelspec':{'name':'python3','display_name':'Python 3','language':'python'}},'nbformat':4,'nbformat_minor':5}
target.with_suffix('.ipynb').write_text(json.dumps(nb,indent=1),encoding='utf-8')
print(target.with_suffix('.ipynb'))
