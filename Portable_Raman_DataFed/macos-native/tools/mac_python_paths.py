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
skipped. Paths to the author's other folders (such as Desktop) have no copy in the project and are
reported, not changed. Every notebook cell that uses RAMAN_HANDOFF_DIR gets its own block, so cells
can run on their own. Line endings and notebook formatting are preserved; git keeps the originals.
A file whose rewritten code would not compile is left unchanged and reported. Running the tool again
only refreshes the inserted blocks; it does not follow folders renamed inside the hand-off folder.

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
OTHER = re.compile(r"C:%sUsers%sJeremy666(?!%sOneDrive%sDocuments%slaser_test)%s[^'\"\n]*" % ((SEP,) * 6))
BLOCK_RE = re.compile(r"# --- Portable data location \(added by macos-native/tools/mac_python_paths\.py\) -+\n.*?\n# -{20,}\n", re.S)
MARKER = "RAMAN_HANDOFF_DIR = _raman_handoff_dir()"
BLOCK = '''\
# --- Portable data location (added by macos-native/tools/mac_python_paths.py) ---------------------
# RAMAN_HANDOFF_DIR is the hand-off folder laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug,
# found by walking up from this file (a notebook: from its working folder). To use another copy,
# set the RAMAN_HANDOFF_DIR environment variable to that folder.
import os as _os


def _raman_handoff_dir():
    if _os.environ.get("RAMAN_HANDOFF_DIR"):
        chosen = _os.path.abspath(_os.path.expanduser(_os.environ["RAMAN_HANDOFF_DIR"]))
        if not _os.path.isdir(chosen):
            raise FileNotFoundError("RAMAN_HANDOFF_DIR is set to " + chosen + ", which is not a folder")
        return chosen
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
        rest = m.group("rest").replace("\\\\", "/").replace("\\", "/")
        if not rest:
            return "RAMAN_HANDOFF_DIR"
        if rest == "/":
            return 'RAMAN_HANDOFF_DIR + "/"'
        if "f" not in m.group("prefix").lower():
            rest = rest.replace("{", "{{").replace("}", "}}")
        q = m.group("q")
        return "f" + q + "{RAMAN_HANDOFF_DIR}" + rest + q

    new = LITERAL.sub(replace, src)
    return new, count, OTHER.findall(new)


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


def masked(src):
    """Cell source with IPython magics / shell lines blanked, so ast and compile() accept it."""
    return "".join(("\n" if line.endswith("\n") else "") if re.match(r"\s*[%!]", line) else line
                   for line in src.splitlines(keepends=True))


def with_block(src, newline, cell=False):
    block = BLOCK.replace("\n", newline)
    if MARKER in src:
        normalized = src.replace(newline, "\n") if newline != "\n" else src
        refreshed = BLOCK_RE.sub(lambda m: BLOCK, normalized, count=1)
        return refreshed.replace("\n", newline) if newline != "\n" else refreshed
    lines = src.splitlines(keepends=True)
    if cell:
        start = 1 if lines and lines[0].startswith("%%") else 0
        body = "".join(lines[start:])
        index = start + module_insert_line(masked(body))
    else:
        index = module_insert_line(src)
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
        total, others, edited = 0, [], False
        for number, cell in enumerate(nb.get("cells", [])):
            if cell.get("cell_type") != "code":
                continue
            source = cell.get("source", [])
            joined = "".join(source) if isinstance(source, list) else source
            new, count, found = rewrite_literals(joined)
            others += found
            total += count
            if "RAMAN_HANDOFF_DIR" in new:
                new = with_block(new, "\n", cell=True)
            if new == joined:
                continue
            try:
                compile(masked(new), "%s cell %d" % (path, number), "exec")
            except SyntaxError as error:
                return 0, [], "cell %d would not compile after rewriting (%s): left unchanged" % (number, error.msg)
            edited = True
            cell["source"] = new.splitlines(keepends=True) if isinstance(source, list) else new
        if not edited:
            return 0, others, None
        out = json.dumps(nb, indent=1, ensure_ascii=ensure_ascii).replace("\n", json_newline)
        out += text[len(text.rstrip("\r\n")):]
        total = total or 1
    else:
        out, total, others = rewrite_literals(text)
        if "RAMAN_HANDOFF_DIR" in out:
            out = with_block(out, newline)
        if out == text:
            return 0, others, None
        try:
            compile(out, path, "exec")
        except SyntaxError as error:
            return 0, [], "would not compile after rewriting (%s): left unchanged" % error.msg
        total = total or 1
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
                print("%s %s" % ("would update" if dry else "updated     ", rel))
            for other in sorted(set(others)):
                print("  not in this project, left as is: %s  (%s)" % (other.strip()[:110], os.path.basename(rel)))
    print("%d file(s) %s; paths are relative to %s" % (changed, "to update" if dry else "updated", HANDOFF_REL))


if __name__ == "__main__":
    main()
