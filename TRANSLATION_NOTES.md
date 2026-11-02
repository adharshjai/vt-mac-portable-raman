# Translation notes (Chinese / Japanese -> English)

Done 2026-10-27 on the unpacked `Portable_Raman_full_project_20261027.zip`. Every line of Chinese or Japanese text in the project was translated to English in place; code, paths, numbers, Markdown and comment markers were kept exactly. Nothing else in the files was changed. The original zip in `~/Downloads` is the untouched reference.

**Scope:** 515 unique lines, 991 replacements in 42 files, plus 4 renamed files. A final scan finds no Chinese or Japanese text left in any text file, notebook, file name or folder name.

## Renamed files

| Original name | New name |
|---|---|
| `安装和运行说明.txt` | `INSTALL_AND_RUN_NOTES.txt` |
| `Python依赖清单` | `Python_dependencies` |
| `文件清单_SHA256.json` | `file_manifest_SHA256.json` |
| `打包排除清单.json` | `packaging_exclusions.json` |

References to these names inside `INSTALL_AND_RUN_NOTES.txt` were updated to match.

## What was translated, by group

| Group | Unique lines | Replacements | Files |
|---|---|---|---|
| svm4-family | 18 | 256 | `Untitled-4_svm.ipynb`, `Untitled-4_svm_executed.ipynb`, `SVM4_full_PCA_SVM_new_analysis.ipynb`, `SVM4_full_PCA_SVM_new_analysis.py` (+11 more) |
| transformer1 | 99 | 198 | `transformer_1.py`, `transformer_1.py` |
| intersect-readme | 89 | 178 | `README.md`, `README.md` |
| untitled-notebooks | 115 | 115 | `Untitled-1.ipynb`, `Untitled-2.ipynb` |
| install-notes | 71 | 71 | `安装和运行说明.txt` |
| pure-only-readmes | 70 | 70 | `README.md`, `README.md`, `README.md` |
| motor-cpp | 8 | 40 | `RamanAutofocusTest.cpp`, `RamanAutofocusTest.cpp`, `RamanAutofocusTest.cpp`, `RamanAutofocusTest.cpp` (+1 more) |
| sampleapl-japanese | 15 | 30 | `AssemblyInfo.cpp`, `app.rc`, `stdafx.cpp`, `stdafx.h` (+4 more) |
| misc | 15 | 15 | `main.cpp.before_laser_scan_split_20260916.bak` |
| python-deps-readme | 14 | 14 | `README.txt` |
| cmake | 1 | 4 | `CMakeLists.txt`, `CMakeLists.txt` |

Identical copies of a file (for example the duplicates under `intersect bridge + Physical device/`) received identical translations.

## Method and checks

- Each group was translated by one agent, then independently reviewed by a meaning/terminology reviewer and a code-safety reviewer; a third agent adjudicated and corrected flagged lines. The reviewers caught and fixed, for example, reordered code tokens, a lowercased identifier, a narrowed hardware warning and a changed tone.
- Mechanical checks after applying: every file keeps its line count; every line that had no Chinese/Japanese is byte-identical to the original; all 11 modified Python files compile; all 8 modified notebooks are valid JSON; the Qt controller still configures and builds; a whole-tree scan finds no CJK text or names.
- Encodings: files that were Shift-JIS (`stdafx.h`, `stdafx.cpp`, `AssemblyInfo.cpp` in both SampleApl copies) are now plain ASCII, which also removes the MSVC C4819 warning. `app.rc` stays UTF-16 LE with BOM and CRLF. UTF-8 BOMs and Windows line endings were preserved everywhere else.
- Notebooks: all 8 modified notebooks keep their original JSON formatting (line endings, indentation, escaping); only the translated lines differ from the originals.

## Code strings that changed (not just comments)

These lines are string literals, so the program text itself changed. They were the original author's Windows paths and messages and must be edited for your machine anyway, but note the Chinese folder name inside the paths was translated too:

- `print(f"    每一行 = 一条完整处理后的谱 = 一个训练样本")`
  -> `print(f"    Each row = one fully processed spectrum = one training sample")`
  in Untitled-1.ipynb
- `print(f"\n  >>> 后续 CNN / Transformer / PCA-SVM 加载方式:")`
  -> `print(f"\n  >>> How to load for subsequent CNN / Transformer / PCA-SVM:")`
  in Untitled-1.ipynb
- `print(f"      每一行就是一个训练样本，不需要用 mean/std/median")`
  -> `print(f"      Each row is one training sample; no need to use mean/std/median")`
  in Untitled-1.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 11_51`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 11_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 11_55`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 11_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 11_56`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 11_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 13_36`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 13_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 13_37`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 13_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 13_38`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 13_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 13_47`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 13_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 13_48`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 13_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group 数据\2025_11_21 13_48`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Tian's group data\2025_11_21 13_`
  in Untitled-2.ipynb
- `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Jia's group 数据\2025_12_09 11_16_`
  -> `# r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Jia's group data\2025_12_09 11_1`
  in Untitled-2.ipynb
- `source_folder = r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman数据\R`
  -> `source_folder = r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman dat`
  in Untitled-2.ipynb
- `print(f"📂 正在扫描: {source_folder}")`
  -> `print(f"📂 Scanning: {source_folder}")`
  in Untitled-2.ipynb
- `print(f"🎯 严格筛选模式: 只匹配 {list(target_mapping.keys())}")`
  -> `print(f"🎯 Strict filter mode: only matching {list(target_mapping.keys())}")`
  in Untitled-2.ipynb
- `print(f"❌ 错误: 找不到路径 {source_folder}")`
  -> `print(f"❌ Error: path not found {source_folder}")`
  in Untitled-2.ipynb
- `print(f"已处理 {count} 个文件...")`
  -> `print(f"Processed {count} files...")`
  in Untitled-2.ipynb
- `print("✅ 分类完成！")`
  -> `print("✅ Sorting complete!")`
  in Untitled-2.ipynb
- `print(f"📁 结果保存在: {output_base_dir}")`
  -> `print(f"📁 Results saved in: {output_base_dir}")`
  in Untitled-2.ipynb
- `print("📊 详细统计:")`
  -> `print("📊 Detailed statistics:")`
  in Untitled-2.ipynb
- `print(f"  - 未匹配文件 (不含上述标识): {val} 个")`
  -> `print(f"  - Unmatched files (not containing any of the above identifiers): {val} files")`
  in Untitled-2.ipynb
- `print(f"  - {key} (存入 {target_mapping[key]}): {val} 个")`
  -> `print(f"  - {key} (stored in {target_mapping[key]}): {val} files")`
  in Untitled-2.ipynb
- `INPUT_PATH = r"C:\Users\Jeremy666\OneDrive\Desktop\周老师文章题目全部.txt"`
  -> `INPUT_PATH = r"C:\Users\Jeremy666\OneDrive\Desktop\Prof. Zhou all paper titles.txt"`
  in Untitled-2.ipynb
- `OUTPUT_PATH = r"C:\Users\Jeremy666\OneDrive\Desktop\周老师文章题目_2020_2025_提取结果.txt"`
  -> `OUTPUT_PATH = r"C:\Users\Jeremy666\OneDrive\Desktop\Prof. Zhou paper titles_2020_2025_extracted results.txt"`
  in Untitled-2.ipynb
- `raise FileNotFoundError(f"找不到 {cls} 的 minmax 数据文件: {minmax_path}")`
  -> `raise FileNotFoundError(f"Cannot find the {cls} minmax data file: {minmax_path}")`
  in SVM4_component_summary_complete.before_spatial_20260921_165717.py, SVM4_component_summary_complete.py, SVM4_component_summary_complete.py, SVM4_full_PCA_SVM_new_analysis.ipynb, SVM4_full_PCA_SVM_new_analysis.py, SVM4_pointwise_complete.py, SVM4_pointwise_complete.py, SVM4_r6g20260922_only.ipynb, SVM4_r6g20260922_only.ipynb, SVM4_r6g20260922_only.py, SVM4_r6g20260922_only.py, Untitled-4_svm.ipynb, Untitled-4_svm_executed.ipynb
- `raise FileNotFoundError(f"找不到 {cls} 的 baseline_raw 数据文件: {raw_path}")`
  -> `raise FileNotFoundError(f"Cannot find the {cls} baseline_raw data file: {raw_path}")`
  in SVM4_component_summary_complete.before_spatial_20260921_165717.py, SVM4_component_summary_complete.py, SVM4_component_summary_complete.py, SVM4_full_PCA_SVM_new_analysis.ipynb, SVM4_full_PCA_SVM_new_analysis.py, SVM4_pointwise_complete.py, SVM4_pointwise_complete.py, SVM4_r6g20260922_only.ipynb, SVM4_r6g20260922_only.ipynb, SVM4_r6g20260922_only.py, SVM4_r6g20260922_only.py, Untitled-4_svm.ipynb, Untitled-4_svm_executed.ipynb
- `f"{cls} 的 minmax 和 raw shape 不一致: minmax={data_minmax.shape}, raw={data_raw.shape}"`
  -> `f"{cls} minmax and raw shape mismatch: minmax={data_minmax.shape}, raw={data_raw.shape}"`
  in SVM4_component_summary_complete.before_spatial_20260921_165717.py, SVM4_component_summary_complete.py, SVM4_component_summary_complete.py, SVM4_full_PCA_SVM_new_analysis.ipynb, SVM4_full_PCA_SVM_new_analysis.py, SVM4_pointwise_complete.py, SVM4_pointwise_complete.py, SVM4_r6g20260922_only.ipynb, SVM4_r6g20260922_only.ipynb, SVM4_r6g20260922_only.py, SVM4_r6g20260922_only.py, Untitled-4_svm.ipynb, Untitled-4_svm_executed.ipynb
- `DATA_ROOT = r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman数据"`
  -> `DATA_ROOT = r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data"`
  in transformer_1.py, transformer_1.py
- `"pyo":     r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman数据\pyo_1s`
  -> `"pyo":     r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data\pyo`
  in transformer_1.py, transformer_1.py
- `"adamite": r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman数据\Admine`
  -> `"adamite": r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data\Adm`
  in transformer_1.py, transformer_1.py
- `"si":      r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman数据\Si_1s_`
  -> `"si":      r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data\Si_`
  in transformer_1.py, transformer_1.py
- `"r6g":     r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman数据\r6g_1s`
  -> `"r6g":     r"C:\Users\Jeremy666\OneDrive\Desktop\Portable_Raman_Paper_Machine_Learning\Portable Raman Data\r6g`
  in transformer_1.py, transformer_1.py
- `f"seq_len ({seq_len}) 必须能被 patch_size ({patch_size}) 整除"`
  -> `f"seq_len ({seq_len}) must be divisible by patch_size ({patch_size})"`
  in transformer_1.py, transformer_1.py
- `$env:RAMAN_BROKER_PASSWORD = '填写与config.json一致的密码'`
  -> `$env:RAMAN_BROKER_PASSWORD = 'enter the password matching config.json'`
  in README.md, README.md

## Not translated

- PNG figures, `.exe`/`.dll` binaries and `.pt`/`.joblib` model files cannot be edited; the binaries are rebuilt from the now-English sources.
- Latin-script content (English, product names, code) was already English and is untouched.

## Full log

`TRANSLATION_LOG.json` lists every replacement with file, line (or notebook cell), original text and English text, so any line can be checked or reverted.
