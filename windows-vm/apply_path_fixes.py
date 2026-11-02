"""Make laser_test/main.cpp find its helpers next to the executable instead of the
original author's absolute Windows paths. Run from the project root:
    python windows-vm/apply_path_fixes.py
It keeps a copy of the untouched file as main.cpp.orig and refuses to run twice."""
import sys, os, shutil
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
p = os.path.join(root, 'laser_test', 'main.cpp')
s = open(p, encoding='utf-8').read()
edits = [
 # 1) analysis script: cnn_1.py next to the exe (was C:\Users\Jeremy666\...\cnn_1.py)
 ('static const QString kPythonScriptPath = R"(C:\\Users\\Jeremy666\\OneDrive\\Documents\\laser_test\\build\\Desktop_Qt_6_6_3_MSVC2019_64bit-Debug\\cnn_1.py)";',
  'static QString kPythonScriptPathValue() { return QCoreApplication::applicationDirPath() + "/cnn_1.py"; }\n#define kPythonScriptPath kPythonScriptPathValue()'),
 # 2) motor program: handshake_v2/motor next to the exe in ALL builds (was the author's Downloads path)
 ('        const QString ramanDir = QStringLiteral("C:/Users/Jeremy666/Downloads/portableRamanSpec_Software (1)/RamanAutofocus/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/RamanAutofocus");',
  '        const QString ramanDir = QCoreApplication::applicationDirPath() + "/handshake_v2/motor";'),
 # 3) mapping log: the laser_log.txt this program writes next to the exe
 ('        mappingLogPath = R"(C:/Users/Jeremy666/OneDrive/Documents/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/laser_log.txt)";',
  '        mappingLogPath = QCoreApplication::applicationDirPath() + "/laser_log.txt";'),
]
def patch_cnn():
    # 4) cnn_1.py: DATA_ROOT is the folder the script sits in (was the author's absolute build path)
    c = os.path.join(root, 'laser_test', 'build', 'Desktop_Qt_6_6_3_MSVC2019_64bit-Debug', 'cnn_1.py')
    t = open(c, encoding='utf-8').read()
    old = 'DATA_ROOT = r"C:\\Users\\Jeremy666\\OneDrive\\Documents\\laser_test\\build\\Desktop_Qt_6_6_3_MSVC2019_64bit-Debug"'
    new = 'DATA_ROOT = os.path.dirname(os.path.abspath(__file__))  # the folder this script sits in'
    if new in t: print('cnn_1.py already patched'); return
    if t.count(old) != 1: print('ABORT cnn_1.py: expected exactly one DATA_ROOT line'); return
    if not os.path.exists(c + '.orig'): shutil.copyfile(c, c + '.orig')
    open(c, 'w', encoding='utf-8', newline='').write(t.replace(old, new))
    print('patched cnn_1.py DATA_ROOT (backup: cnn_1.py.orig)')

if 'kPythonScriptPathValue' in s:
    print('main.cpp already patched'); patch_cnn(); sys.exit(0)
for old, new in edits:
    if s.count(old) != 1:
        print('ABORT: expected exactly one occurrence of:\n  ' + old[:100]); sys.exit(1)
if not os.path.exists(p + '.orig'): shutil.copyfile(p, p + '.orig')
for old, new in edits: s = s.replace(old, new)
open(p, 'w', encoding='utf-8', newline='').write(s)
print('patched laser_test/main.cpp (backup: main.cpp.orig)')
for old, new in edits: print('  -', new.strip().splitlines()[0][:110])
patch_cnn()
