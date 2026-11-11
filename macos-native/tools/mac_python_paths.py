#!/usr/bin/env python3
"""mac_python_paths.py - point the analysis scripts and notebooks at this Mac's copy of the project.

The original author's scripts hard-code paths such as
    r"C:\\Users\\Jeremy666\\OneDrive\\Documents\\laser_test\\build\\Desktop_Qt_6_6_3_MSVC2019_64bit-Debug\\..."
This tool rewrites every quoted path literal that starts with that laser_test folder to the same
place in this checkout (<project>/laser_test/...), with forward slashes. Run it again after moving
the project folder: literals it wrote before are moved to the new location.

Scope: .py and .ipynb files under laser_test/, code only (notebook outputs and markdown are left
as recorded). Backup copies (folders named backup_*, files named *.before_*) and third_party/ are
skipped. Paths to the author's other folders (Desktop, .codex) have no copy here and are reported,
not changed. Line endings and notebook formatting are preserved; git keeps the originals.

    python3 macos-native/tools/mac_python_paths.py            # rewrite
    python3 macos-native/tools/mac_python_paths.py --dry-run  # report only
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LASER_TEST = ROOT + "/laser_test"
SEP = r"(?:\\\\|\\|/)"
WINDOWS = re.compile(r"(?P<q>['\"])C:%sUsers%sJeremy666%sOneDrive%sDocuments%slaser_test(?P<rest>(?:%s[^'\"\n]*)?)(?P=q)"
                     % ((SEP,) * 6))
PREVIOUS = re.compile(r"(?P<q>['\"])(?P<old>/[^'\"\n]*?)/laser_test(?P<rest>/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug(?:/[^'\"\n]*)?)(?P=q)")
OTHER = re.compile(r"C:%sUsers%sJeremy666%s(?!OneDrive%sDocuments%slaser_test)[^'\"\n]*" % ((SEP,) * 5))


def rewrite_code(src):
    count = 0

    def windows(m):
        nonlocal count
        count += 1
        rest = m.group("rest").replace("\\\\", "/").replace("\\", "/")
        return m.group("q") + LASER_TEST + rest + m.group("q")

    def previous(m):
        nonlocal count
        if m.group("old") + "/laser_test" == LASER_TEST:
            return m.group(0)
        count += 1
        return m.group("q") + LASER_TEST + m.group("rest") + m.group("q")

    src = WINDOWS.sub(windows, src)
    src = PREVIOUS.sub(previous, src)
    return src, count, OTHER.findall(src)


JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


def process_notebook_text(path, text, nb, dry):
    """Fallback for notebooks whose JSON layout json.dumps cannot reproduce: rewrite only the JSON
    string tokens that are code-cell source lines containing a path, and leave every other byte."""
    targets, others = {}, []
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source", [])
        for line in (source if isinstance(source, list) else source.splitlines(keepends=True)):
            new, count, found = rewrite_code(line)
            others += found
            if count:
                targets[line] = (new, count)
    total = 0

    def token(m):
        nonlocal total
        raw = m.group(0)
        try:
            value = json.loads(raw)
        except ValueError:
            return raw
        if value not in targets:
            return raw
        new, count = targets[value]
        total += count
        return json.dumps(new, ensure_ascii="\\u" in raw)

    out = JSON_STRING.sub(token, text)
    json.loads(out)  # must still be valid JSON
    if total and not dry:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(out)
    return total, others, None


def skip(path):
    parts = path.split(os.sep)
    return (any(p.startswith("backup_") or p == "third_party" or p in ("__pycache__", ".venv") or p.startswith(".venv")
                for p in parts) or ".before_" in parts[-1])


def process(path, dry):
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    if path.endswith(".ipynb"):
        nb = json.loads(text)
        newline = "\r\n" if "\r\n" in text else "\n"
        canonical = json.dumps(nb, indent=1, ensure_ascii=False).replace("\n", newline) + newline
        if canonical.rstrip("\r\n") != text.rstrip("\r\n"):
            return process_notebook_text(path, text, nb, dry)
        total, others = 0, []
        for cell in nb.get("cells", []):
            if cell.get("cell_type") != "code":
                continue
            source = cell.get("source", [])
            joined = "".join(source) if isinstance(source, list) else source
            new, count, found = rewrite_code(joined)
            others += found
            if count:
                total += count
                cell["source"] = new.splitlines(keepends=True) if isinstance(source, list) else new
        out = json.dumps(nb, indent=1, ensure_ascii=False).replace("\n", newline)
        out += text[len(text.rstrip("\r\n")):]
    else:
        out, total, others = rewrite_code(text)
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
    print("%d file(s) %s; paths now start with %s" % (changed, "to change" if dry else "changed", LASER_TEST))


if __name__ == "__main__":
    main()
