#!/usr/bin/env python3
"""mac_python_paths.py - make the analysis scripts and notebooks find their data on any computer.

The original author's scripts hard-code paths such as
    r"C:\\Users\\Jeremy666\\OneDrive\\Documents\\laser_test\\build\\Desktop_Qt_6_6_3_MSVC2019_64bit-Debug\\data\\x.npy"
This tool replaces every such quoted path literal with one relative to the hand-off folder,
    f"{RAMAN_HANDOFF_DIR}/data/x.npy"
and adds a short block at the top of the file (or of the notebook cell) that sets RAMAN_HANDOFF_DIR:
it walks up from the script's own folder (a notebook: its working folder) until it finds
laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug. Set the RAMAN_HANDOFF_DIR environment
variable to use another copy. Absolute paths written by an earlier version of this tool
("/Users/.../laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug/...") are converted the same way.

Scope: .py and .ipynb files under laser_test/, code only (notebook outputs and markdown stay as
recorded). Backup copies (folders named backup_*, files named *.before_*) and third_party/ are
skipped. Paths to the author's other folders (Desktop, .codex) have no copy in the project and are
reported, not changed. Line endings and notebook formatting are preserved; git keeps the originals.
Running it again changes nothing.

    python3 macos-native/tools/mac_python_paths.py            # rewrite
    python3 macos-native/tools/mac_python_paths.py --dry-run  # report only
"""
import ast, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LASER_TEST = os.path.join(ROOT, "laser_test")
HANDOFF_REL = "laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug"
SEP = r"(?:\\\\|\\|/)"
PREFIX = r"(?<![\w'\"])(?P<prefix>[rRfFuU]{0,2})(?P<q>['\"])"
WINDOWS_HANDOFF = (r"C:%sUsers%sJeremy666%sOneDrive%sDocuments%slaser_test%sbuild%sDesktop_Qt_6_6_3_MSVC2019_64bit-Debug"
                   % ((SEP,) * 7))
ABSOLUTE_HANDOFF = r"/[^'\"\n]*?/laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug"
LITERAL = re.compile(PREFIX + r"(?:%s|%s)(?P<rest>(?:%s[^'\"\n]*)?)(?P=q)" % (WINDOWS_HANDOFF, ABSOLUTE_HANDOFF, SEP))
OTHER = re.compile(r"C:%sUsers%sJeremy666%s(?!OneDrive%sDocuments%slaser_test)[^'\"\n]*" % ((SEP,) * 5))
MARKER = "RAMAN_HANDOFF_DIR = _raman_handoff_dir()"
BLOCK = '''\
# --- Portable data location (added by macos-native/tools/mac_python_paths.py) ---------------------
# RAMAN_HANDOFF_DIR is the hand-off folder laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug,
# found by walking up from this file (a notebook: from its working folder). To use another copy,
# set the RAMAN_HANDOFF_DIR environment variable to that folder.
import os as _os


def _raman_handoff_dir():
    if _os.environ.get("RAMAN_HANDOFF_DIR"):
        return _os.path.abspath(_os.environ["RAMAN_HANDOFF_DIR"])
    start = _os.path.dirname(_os.path.abspath(globals()["__file__"])) if "__file__" in globals() else _os.getcwd()
    folder = start
    while True:
        candidate = _os.path.join(folder, "laser_test", "build", "Desktop_Qt_6_6_3_MSVC2019_64bit-Debug")
        if _os.path.isdir(candidate):
            return candidate
        parent = _os.path.dirname(folder)
        if parent == folder:
            raise FileNotFoundError("laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug not found above "
                                    + start + "; set the RAMAN_HANDOFF_DIR environment variable")
        folder = parent


%s
# ---------------------------------------------------------------------------------------------------
''' % MARKER


def rewrite_literals(src):
    """Return (new source, number of literals rewritten, other author paths found)."""
    count = 0

    def replace(m):
        nonlocal count
        count += 1
        rest = m.group("rest").replace("\\\\", "/").replace("\\", "/").rstrip("/")
        if not rest:
            return "RAMAN_HANDOFF_DIR"
        if "f" not in m.group("prefix").lower():
            rest = rest.replace("{", "{{").replace("}", "}}")
        q = m.group("q")
        return "f" + q + "{RAMAN_HANDOFF_DIR}" + rest + q

    return LITERAL.sub(replace, src), count, OTHER.findall(src)


def module_insert_line(src):
    """Line index after the shebang/encoding comments, module docstring and __future__ imports."""
    lines = src.splitlines(keepends=True)
    index = 0
    try:
        tree = ast.parse(src)
        for node in tree.body:
            is_doc = (node is tree.body[0] and isinstance(node, ast.Expr)
                      and isinstance(getattr(node, "value", None), ast.Constant) and isinstance(node.value.value, str))
            if is_doc or (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
                index = node.end_lineno
            else:
                break
    except SyntaxError:
        pass
    if index == 0:
        while index < len(lines) and (lines[index].startswith("#!") or re.match(r"#.*coding[:=]", lines[index])):
            index += 1
    return index


def with_block(src, newline, cell=False):
    if MARKER in src:
        return src
    lines = src.splitlines(keepends=True)
    if cell:
        index = 0
        while index < len(lines) and (lines[index].startswith("%%") or lines[index].startswith("from __future__")):
            index += 1
    else:
        index = module_insert_line(src)
    block = BLOCK.replace("\n", newline)
    if index and not lines[index - 1].endswith(("\n", "\r")):
        lines[index - 1] += newline
    return "".join(lines[:index]) + block + "".join(lines[index:])


def skip(path):
    parts = path.split(os.sep)
    return (any(p.startswith("backup_") or p == "third_party" or p == "__pycache__" or p.startswith(".venv") for p in parts)
            or ".before_" in parts[-1])


def notebook_layout(text, nb):
    for ensure_ascii in (False, True):
        newline = "\r\n" if "\r\n" in text else "\n"
        dumped = json.dumps(nb, indent=1, ensure_ascii=ensure_ascii).replace("\n", newline)
        if dumped.rstrip("\r\n") == text.rstrip("\r\n"):
            return ensure_ascii, newline
    return None


def process(path, dry):
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    newline = "\r\n" if "\r\n" in text else "\n"
    if path.endswith(".ipynb"):
        nb = json.loads(text)
        layout = notebook_layout(text, nb)
        if layout is None:
            return 0, [], "notebook JSON layout not reproducible: left unchanged"
        ensure_ascii, json_newline = layout
        total, others, block_added = 0, [], False
        for cell in nb.get("cells", []):
            if cell.get("cell_type") != "code":
                continue
            source = cell.get("source", [])
            joined = "".join(source) if isinstance(source, list) else source
            new, count, found = rewrite_literals(joined)
            others += found
            if not count:
                continue
            total += count
            if not block_added and MARKER not in "".join(
                    "".join(c.get("source", [])) for c in nb["cells"] if c.get("cell_type") == "code"):
                new = with_block(new, "\n", cell=True)
            block_added = True
            cell["source"] = new.splitlines(keepends=True) if isinstance(source, list) else new
        out = json.dumps(nb, indent=1, ensure_ascii=ensure_ascii).replace("\n", json_newline)
        out += text[len(text.rstrip("\r\n")):]
    else:
        out, total, others = rewrite_literals(text)
        if total:
            out = with_block(out, newline)
            compile(out, path, "exec")  # never write a file Python cannot parse
    if total and not dry:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(out)
    return total, others, None


def main():
    dry = "--dry-run" in sys.argv
    changed = 0
    for folder, dirs, files in os.walk(LASER_TEST):
        dirs[:] = sorted(d for d in dirs if not skip(os.path.join(folder, d)))
        for name in sorted(files):
            path = os.path.join(folder, name)
            if not name.endswith((".py", ".ipynb")) or skip(path):
                continue
            total, others, problem = process(path, dry)
            rel = os.path.relpath(path, ROOT)
            if problem:
                print("SKIPPED  %s: %s" % (rel, problem))
            if total:
                changed += 1
                print("%s %3d path(s)  %s" % ("would fix" if dry else "fixed    ", total, rel))
            for other in sorted(set(others)):
                print("  not in this project, left as is: %s  (%s)" % (other.strip()[:110], os.path.basename(rel)))
    print("%d file(s) %s; paths are now relative to %s" % (changed, "to change" if dry else "changed", HANDOFF_REL))


if __name__ == "__main__":
    main()
