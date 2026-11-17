"""The snapshot of a session that an upload sends ("the upload copy on this Mac", PROTOCOL.md 5.6).

The worker makes it in state `preparing`: every file that goes is copied (links are never
followed) into <record>/payload.tmp-<random>/<session folder>/, hashed while it is copied
(SHA-256 and MD5), checked for change, completed with the manifest raman_upload_manifest.json
and SHA256SUMS, made read-only (files 0400; folders 0700, so that a kept copy can be deleted) and
renamed to <record>/payload/. The upload then sends exactly that folder, and Retry same request
sends it again. It is deleted once the upload is verified.

Every file of the session is opened through the session folder's own descriptor, one name at a time
and never through a link, and must be the very file the listing found (device, inode, size, time).
The snapshot's session.json of an imported session carries the source folder's name only, not this
Mac's path to it (the manifest's "adjusted" says so).

Copies of the manifest and SHA256SUMS stay in the record folder, and payload_hashes.json there
keeps every file's size, SHA-256 and MD5 (Globus reports MD5 checksums; design 10.6).
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path

from . import __version__, arc_records, arc_session
from .arc_records import MANIFEST_NAME, PAYLOAD_FOLDER, SUMS_NAME, write_private_file
from .config import DataLayerError, utc_now

MANIFEST_SCHEMA = "raman-arc-upload-manifest/1"
HASHES_FILE = "payload_hashes.json"
TEMPORARY_PREFIX = "payload.tmp-"
BLOCK = 1 << 20
SPARE_BYTES = 50 * 1000 * 1000
SNAPSHOT_TRIES = 3
RETRY_PAUSE_SECONDS = 2.0
DATA_LAYER_NAME = "raman_catalog"
FILE_MODE, FOLDER_MODE = 0o400, 0o700
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
SESSION_FILE = "session.json"
# The manifest's "adjusted": what the snapshot changed in a file it sends.
SOURCE_FOLDER_SHORTENED = "imported.source_folder shortened to the folder name"
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class PayloadError(DataLayerError):
    """The snapshot cannot be made or used; `reason` is the Appendix A.2 reason, `values` its placeholders."""

    kind = "internal"

    def __init__(self, reason, message, values=None, detail=""):
        super().__init__(message, detail)
        self.reason = reason
        self.values = values or {}


class Changed(Exception):
    """A file changed while it was copied (or a link was swapped in): the snapshot starts again."""


def payload_folder(record_dir):
    return Path(record_dir) / PAYLOAD_FOLDER


def sort_key(path):
    """Bytewise order of a relative path, as `LC_ALL=C sort` orders it."""
    return path.encode("utf-8", "surrogateescape")


def free_space_needed(session_bytes):
    return 2 * session_bytes + SPARE_BYTES


def check_free_space(home, session_bytes, disk_usage=shutil.disk_usage):
    needed = free_space_needed(session_bytes)
    if disk_usage(str(home)).free < needed:
        megabytes = -(-needed // 1000000)
        raise PayloadError("no_space", "Not enough free disk space", {"mb": megabytes})


def _identity(info):
    """What makes a file the very file a listing found: device, inode, size and modification time."""
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _write_all(handle, data):
    view = memoryview(data)
    while view:
        view = view[os.write(handle, view):]


def _copy_file(source, target, hooks=None, opener=None, expected=None, adjust=None):
    """Copy one file without following a link; returns (size, sha256, md5, source stat) of what was written.

    `opener()` opens the source (default: its path, with O_NOFOLLOW for the last name); `expected` is
    the source's lstat as the listing found it, and the opened file must be that very file (_identity),
    so that a folder swapped for a link on the way to it is noticed. The source is also checked three
    times: fstat before reading, fstat after, and stat of the path; a different size, modification time
    or inode means it changed while it was copied. `adjust(data)` gives the bytes to write instead of
    the file's own (the snapshot's session.json). `hooks` ("before_open", "after_read": functions of the
    source path) exist for the tests.
    """
    if hooks and hooks.get("before_open"):
        hooks["before_open"](source)
    try:
        handle = opener() if opener is not None else os.open(str(source), os.O_RDONLY | O_NOFOLLOW)
    except OSError as error:
        # ELOOP or ENOTDIR: a link where a file or a folder was listed.
        raise Changed(str(source)) from error
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or (expected is not None and _identity(before) != _identity(expected)):
            raise Changed(str(source))
        sha256, md5, size, read = hashlib.sha256(), hashlib.md5(), 0, 0
        out = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            blocks = []
            while True:
                block = os.read(handle, BLOCK)
                if not block:
                    break
                read += len(block)
                if adjust is not None:
                    blocks.append(block)
                    continue
                sha256.update(block)
                md5.update(block)
                size += len(block)
                _write_all(out, block)
            if adjust is not None:
                data = adjust(b"".join(blocks))
                sha256.update(data)
                md5.update(data)
                size = len(data)
                _write_all(out, data)
            os.fsync(out)
        finally:
            os.close(out)
        if hooks and hooks.get("after_read"):
            hooks["after_read"](source)
        after = os.fstat(handle)
    finally:
        os.close(handle)
    try:
        now = os.stat(str(source), follow_symlinks=False)
    except OSError as error:
        raise Changed(str(source)) from error
    signature = (before.st_size, before.st_mtime_ns, before.st_ino)
    if (after.st_size, after.st_mtime_ns, after.st_ino) != signature or \
            (now.st_size, now.st_mtime_ns, now.st_ino) != signature or read != before.st_size or \
            not stat.S_ISREG(now.st_mode):
        raise Changed(str(source))
    os.utime(str(target), ns=(before.st_atime_ns, before.st_mtime_ns))
    return size, sha256.hexdigest(), md5.hexdigest(), before


def _open_folder(folder, expected):
    """The session folder's own descriptor, never through a link; it must be the folder the listing found
    (`expected`: its device and inode)."""
    try:
        handle = os.open(str(folder), os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    except OSError as error:
        raise Changed(str(folder)) from error
    info = os.fstat(handle)
    if (info.st_dev, info.st_ino) != expected or not stat.S_ISDIR(info.st_mode):
        os.close(handle)
        raise Changed(str(folder))
    return handle


def _open_below(root, relative):
    """Open a file of the session through the session folder's descriptor `root`, one name at a time;
    a link anywhere on the way (a folder swapped for one included) is never followed (ELOOP or ENOTDIR)."""
    parts = relative.split("/")
    opened = []
    try:
        folder = root
        for part in parts[:-1]:
            folder = os.open(part, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=folder)
            opened.append(folder)
        return os.open(parts[-1], os.O_RDONLY | O_NOFOLLOW, dir_fd=folder)
    finally:
        for handle in opened:
            os.close(handle)


def _short_folder(value):
    """The last name of an absolute path (this Mac's, a ~ path, or a Windows one), or None for anything else."""
    if not isinstance(value, str) or not (value.startswith(("/", "~", "\\")) or _WINDOWS_PATH.match(value)):
        return None
    parts = [part for part in re.split(r"[\\/]+", value) if part]
    return parts[-1] if parts else ""


def adjusted_session_json(data):
    """(bytes, adjusted) of session.json as the snapshot sends it. An imported session's
    imported.source_folder is the absolute path it was imported from, with this Mac's account name in
    it: the copy keeps its last name only. `adjusted` is the manifest's list ([] when nothing changed);
    the file is written as import-session writes it."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return data, []
    imported = value.get("imported") if isinstance(value, dict) else None
    short = _short_folder(imported.get("source_folder")) if isinstance(imported, dict) else None
    if short is None:
        return data, []
    imported["source_folder"] = short
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8"), [{"path": SESSION_FILE, "what": SOURCE_FOLDER_SHORTENED}]


def _read_file(path):
    """The bytes of a file, without following a link."""
    handle = os.open(str(path), os.O_RDONLY | O_NOFOLLOW)
    try:
        blocks = []
        while True:
            block = os.read(handle, BLOCK)
            if not block:
                return b"".join(blocks)
            blocks.append(block)
    finally:
        os.close(handle)


def hash_file(path):
    """(size, sha256, md5) of a file, without following a link."""
    handle = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        sha256, md5, size = hashlib.sha256(), hashlib.md5(), 0
        while True:
            block = os.read(handle, BLOCK)
            if not block:
                break
            sha256.update(block)
            md5.update(block)
            size += len(block)
    finally:
        os.close(handle)
    return size, sha256.hexdigest(), md5.hexdigest()


def manifest_bytes(manifest):
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sums_bytes(entries):
    """SHA256SUMS: `<hex>  <path>` per file, bytewise order; `entries` [(path, sha256)]."""
    lines = ["%s  %s\n" % (digest, path) for path, digest in sorted(entries, key=lambda item: sort_key(item[0]))]
    return "".join(lines).encode("utf-8")


def build_manifest(upload_id, transport, demo, session, simulated, software, darks, files, excluded, destination,
                   adjusted=None):
    """The manifest of 4.7, its totals made to count itself and SHA256SUMS.

    `files` [{"path", "bytes", "sha256"}] of every file sent except the manifest and SHA256SUMS;
    `adjusted` [{"path", "what"}]: what the snapshot changed in a file it sends (session.json, I11).
    """
    files = sorted(files, key=lambda entry: sort_key(entry["path"]))
    sums_size = sum(64 + 2 + len(path.encode("utf-8")) + 1 for path in [entry["path"] for entry in files] +
                    [MANIFEST_NAME])
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "upload_id": upload_id,
        "made_utc": utc_now(),
        "transport": transport,
        "demo": bool(demo),
        "session": dict(session, simulated=simulated),
        "darks": darks,
        "files": files,
        "totals": {"files": len(files) + 2, "bytes": 0},
        "excluded": excluded,
        "adjusted": list(adjusted or []),
        "destination": destination,
        "software": {"controller": software, "data_layer": {"name": DATA_LAYER_NAME, "version": __version__}},
    }
    base = sum(entry["bytes"] for entry in files) + sums_size
    # The manifest holds its own size in totals.bytes: settle the number of digits.
    for _round in range(5):
        total = base + len(manifest_bytes(manifest))
        if manifest["totals"]["bytes"] == total:
            break
        manifest["totals"]["bytes"] = total
    return manifest


def _dark_entries(darks):
    entries = []
    for entry in darks:
        description = entry["description"].name if entry.get("description") is not None else None
        entries.append({"file": entry["file"], "kind": entry["kind"], "sent": entry["sent"], "reason": entry["reason"],
                        "path": "%s/%s" % (arc_session.DARK_FOLDER, entry["file"]) if entry["sent"] else None,
                        "description": "%s/%s" % (arc_session.DARK_FOLDER, description) if description else None})
    return entries


def _dark_paths(darks):
    """[(relative path in the upload, source path)] of the dark spectra that go."""
    sources = []
    for entry in darks:
        if entry["sent"]:
            sources.append(("%s/%s" % (arc_session.DARK_FOLDER, entry["file"]), Path(entry["txt"])))
            if entry.get("description") is not None:
                sources.append(("%s/%s" % (arc_session.DARK_FOLDER, entry["description"].name),
                                Path(entry["description"])))
    return sources


def _lstat_darks(darks):
    """{relative path: lstat} of the dark spectra that go; a dark that vanished is a change (Changed)."""
    found = {}
    for relative, path in _dark_paths(darks):
        try:
            found[relative] = os.lstat(path)
        except OSError as error:
            raise Changed(str(path)) from error
    return found


def _sources(session_folder, listing, darks, root, adjust):
    """[(relative path in the upload, source path, opener, expected lstat, adjust)] of every file that goes.

    The session's files are opened through the folder's descriptor `root`; the darks by their path, each
    checked against the lstat taken now (and again at the end of the copy)."""
    sources = []
    for relative, info in listing.files:
        sources.append((relative, Path(session_folder) / relative, functools.partial(_open_below, root, relative),
                        info, adjust if relative == SESSION_FILE else None))
    seen = _lstat_darks(darks)
    for relative, path in _dark_paths(darks):
        sources.append((relative, path, None, seen[relative], None))
    return sources, seen


def _identities(stats):
    return {relative: _identity(info) for relative, info in stats.items()}


def _make_read_only(top):
    """Files read-only (0400), folders 0700: the copy cannot be changed by mistake, and can be deleted."""
    for directory, folders, files in os.walk(top, topdown=False):
        for name in files:
            os.chmod(os.path.join(directory, name), FILE_MODE)
        for name in folders:
            os.chmod(os.path.join(directory, name), FOLDER_MODE)
        os.chmod(directory, FOLDER_MODE)


def _fsync_tree(top):
    for directory, _folders, _files in os.walk(top):
        arc_records.fsync_folder(directory)


def remove_tree(path):
    """Delete a snapshot tree. Links are never followed: a link in the place of the tree is removed itself,
    a link inside it is removed as a link, and what a link points to is never changed. Folders of a
    read-only snapshot made by an earlier version (0555) get mode 0700 first."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    # os.walk does not enter a link to a folder; such a link is listed in `folders` and left alone here.
    for directory, folders, _files in os.walk(path):
        with contextlib.suppress(OSError):
            os.chmod(directory, FOLDER_MODE)
        for name in folders:
            child = os.path.join(directory, name)
            if not os.path.islink(child):
                with contextlib.suppress(OSError):
                    os.chmod(child, FOLDER_MODE)
    shutil.rmtree(path)


def remove_temporaries(record_dir):
    """Remove payload.tmp-* folders left by a worker that stopped while copying."""
    for entry in Path(record_dir).iterdir():
        if entry.name.startswith(TEMPORARY_PREFIX):
            remove_tree(entry)


def snapshot(record_dir, home, check, record, fault=None, sleep=time.sleep, hooks=None,
             disk_usage=shutil.disk_usage):
    """Make <record>/payload/ (4.6) for a session that `check` (arc_session.check) found ready.

    `record` gives upload_id, transport, demo, the session summary and the destination. Returns
    the "payload" summary of the record. Raises PayloadError (no_space, changed_during_copy) or
    arc_records.ArcError when the session cannot go any more.
    """
    record_dir = Path(record_dir)
    session_folder = check.folder
    check_free_space(home, check.byte_count(), disk_usage)
    remove_temporaries(record_dir)
    for attempt in range(1, SNAPSHOT_TRIES + 1):
        temporary = record_dir / (TEMPORARY_PREFIX + secrets.token_hex(4))
        target = temporary / session_folder.name
        root = None
        adjusted = []

        def adjust(data):
            content, changes = adjusted_session_json(data)
            adjusted[:] = changes
            return content

        try:
            listing = arc_session.list_folder(session_folder)
            if listing.problem is not None:
                reason, name = listing.problem
                if reason == "name_clash":
                    raise arc_records.refusal("name_clash", folder=session_folder.name)
                raise arc_records.refusal(reason, name=name)
            # Every file is read through the folder that was listed, whatever its name points to meanwhile.
            root = _open_folder(session_folder, listing.folder_id)
            sources, darks_before = _sources(session_folder, listing, check.darks, root, adjust)
            os.mkdir(temporary, 0o700)
            os.mkdir(target, 0o700)
            copied = []
            half = len(sources) // 2
            for index, (relative, source, opener, expected, rewrite) in enumerate(sources):
                if index == half and fault is not None:
                    fault("snapshot_mid_copy")
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                size, sha256, md5, _info = _copy_file(source, destination, hooks, opener=opener, expected=expected,
                                                      adjust=rewrite)
                copied.append({"path": relative, "bytes": size, "sha256": sha256, "md5": md5})
            # Nothing changed meanwhile: the folder (its name still leads to the folder that was read), its
            # files, and the darks.
            if arc_session.list_folder(session_folder).signature() != listing.signature() or \
                    _identities(_lstat_darks(check.darks)) != _identities(darks_before):
                raise Changed("the session's listing changed")
        except Changed:
            remove_tree(temporary)
            if attempt == SNAPSHOT_TRIES:
                raise PayloadError("changed_during_copy", "The session kept changing while it was copied") from None
            sleep(RETRY_PAUSE_SECONDS)
            continue
        except BaseException:
            remove_tree(temporary)
            raise
        finally:
            if root is not None:
                os.close(root)
        break
    session = dict(record["session"])
    simulated = check.session.get("simulated") if isinstance(check.session.get("simulated"), dict) else None
    software = check.session.get("software") if isinstance(check.session.get("software"), dict) else None
    destination = record["destination"]
    manifest = build_manifest(
        record["upload_id"], record["transport"], record["demo"], session, simulated, software,
        _dark_entries(check.darks), [{key: entry[key] for key in ("path", "bytes", "sha256")} for entry in copied],
        listing.excluded, {"arc_folder": destination["arc_folder"], "area": destination["area"]}, adjusted)
    manifest_data = manifest_bytes(manifest)
    manifest_sha256 = hashlib.sha256(manifest_data).hexdigest()
    sums_data = sums_bytes([(entry["path"], entry["sha256"]) for entry in copied] + [(MANIFEST_NAME, manifest_sha256)])
    try:
        arc_records.write_new_file(target / MANIFEST_NAME, manifest_data, 0o600)
        arc_records.write_new_file(target / SUMS_NAME, sums_data, 0o600)
        _make_read_only(temporary)
        _fsync_tree(temporary)
        os.rename(temporary, payload_folder(record_dir))
        arc_records.fsync_folder(record_dir)
    except BaseException:
        remove_tree(temporary)
        raise
    if fault is not None:
        fault("snapshot_before_record")
    return keep(record_dir, session_folder.name)


def _expected(sums_text):
    """{path: sha256} of a SHA256SUMS text; raises ValueError for a line that is not one."""
    expected = {}
    for line in sums_text.splitlines():
        digest, separator, path = line.partition("  ")
        if not separator or len(digest) != 64 or not path or path in expected:
            raise ValueError(line)
        expected[path] = digest
    return expected


def check_payload(record_dir, folder_name):
    """Hash the snapshot again: every file must be what SHA256SUMS lists, the manifest included,
    and nothing more. Returns {path: {"bytes", "sha256", "md5"}} of every file (SHA256SUMS too).
    Raises PayloadError("payload_changed") on any difference (design 4.6, 10.6 step 1)."""
    top = payload_folder(record_dir) / folder_name

    def changed(why):
        return PayloadError("payload_changed", "The files kept for this upload were changed", detail=why)

    try:
        sums_text = (top / SUMS_NAME).read_bytes().decode("utf-8")
        expected = _expected(sums_text)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise changed("SHA256SUMS cannot be read: %s" % error) from None
    found = {}
    for directory, folders, files in os.walk(top):
        for name in folders:
            if os.path.islink(os.path.join(directory, name)):
                raise changed("a link in the snapshot")
        for name in files:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, top).replace(os.sep, "/")
            try:
                size, sha256, md5 = hash_file(path)
            except OSError as error:
                raise changed("%s cannot be read: %s" % (relative, error)) from None
            found[relative] = {"bytes": size, "sha256": sha256, "md5": md5}
    listed = dict(expected)
    listed[SUMS_NAME] = None
    if set(found) != set(listed):
        extra = sorted(set(found) - set(listed))
        missing = sorted(set(listed) - set(found))
        raise changed("extra: %s; missing: %s" % (", ".join(extra[:3]) or "none", ", ".join(missing[:3]) or "none"))
    for path, digest in expected.items():
        if found[path]["sha256"] != digest:
            raise changed("%s differs from SHA256SUMS" % path)
    hashes = _read_hashes(record_dir)
    if hashes is not None:
        for path, entry in found.items():
            known = hashes.get(path)
            if known is not None and (known.get("sha256") != entry["sha256"] or known.get("bytes") != entry["bytes"]):
                raise changed("%s differs from the snapshot" % path)
    return found


def _read_hashes(record_dir):
    try:
        value = json.loads((Path(record_dir) / HASHES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value.get("files") if isinstance(value, dict) and isinstance(value.get("files"), dict) else None


def keep(record_dir, folder_name):
    """Check an existing snapshot against its own lists, write the copies kept in the record folder
    (manifest, SHA256SUMS, payload_hashes.json), and return the record's "payload" summary."""
    record_dir = Path(record_dir)
    found = check_payload(record_dir, folder_name)
    top = payload_folder(record_dir) / folder_name
    write_private_file(record_dir / MANIFEST_NAME, (top / MANIFEST_NAME).read_bytes())
    write_private_file(record_dir / SUMS_NAME, (top / SUMS_NAME).read_bytes())
    write_private_file(record_dir / HASHES_FILE, (json.dumps({"folder": folder_name, "files": found}, indent=1,
                                                             sort_keys=True) + "\n").encode("utf-8"))
    return {"files": len(found), "bytes": sum(entry["bytes"] for entry in found.values()),
            "manifest_sha256": found[MANIFEST_NAME]["sha256"], "kept": True}


def digests(record_dir):
    """{path: {"bytes", "sha256", "md5"}} of every file of the snapshot, from payload_hashes.json."""
    return _read_hashes(record_dir)


def read_manifest(record_dir):
    """The manifest copy kept in the record folder, or None."""
    try:
        value = json.loads((Path(record_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def delete(record_dir):
    """Delete the snapshot of a verified upload; the copies in the record folder stay."""
    remove_tree(payload_folder(record_dir))
    arc_records.fsync_folder(record_dir)


def after_check(session_folder, manifest, demo=False):
    """Compare the session folder with what was sent, once the upload is verified (4.6).

    Returns (notes, warnings): sentences for the record. A demo upload's copy is in the demo folder on this
    Mac, and its sentences say so (review COR-05).
    """
    where = "The demo folder" if demo else "ARC"
    sent = {entry["path"]: entry for entry in manifest.get("files") or []
            if not entry["path"].startswith(arc_session.DARK_FOLDER + "/")}
    # Files the snapshot changed on their way (session.json of an imported session): compared as sent.
    adjusted = {entry.get("path") for entry in manifest.get("adjusted") or [] if isinstance(entry, dict)}
    try:
        listing = arc_session.list_folder(session_folder)
    except OSError:
        return [], ["The session folder %s is no longer on this Mac. %s has the copy from %s."
                    % (Path(session_folder).name, where, manifest.get("made_utc"))]
    now = {}
    for relative, info in listing.files:
        now[relative] = info
    differences = []
    added = sorted(set(now) - set(sent), key=sort_key)
    removed = sorted(set(sent) - set(now), key=sort_key)
    for path in sorted(set(now) & set(sent), key=sort_key):
        try:
            if path in adjusted and path == SESSION_FILE:
                data = adjusted_session_json(_read_file(Path(session_folder) / path))[0]
                size, sha256 = len(data), hashlib.sha256(data).hexdigest()
            else:
                size, sha256, _md5 = hash_file(Path(session_folder) / path)
        except OSError:
            differences.append(path)
            continue
        if size != sent[path]["bytes"] or sha256 != sent[path]["sha256"]:
            differences.append(path)
    if not added and not removed and not differences:
        return [], []
    if added == ["cancel.txt"] and not removed and not differences:
        return ["cancel.txt was written after the upload (closing the window or pressing Stop writes it); the scan "
                "data %s is complete." % ("in the demo folder" if demo else "on ARC")], []
    changed = (differences + added + removed)
    shown = ", ".join(changed[:3]) + (" and %d more" % (len(changed) - 3) if len(changed) > 3 else "")
    return [], ["The session folder on this Mac changed after it was copied for this upload (%s). %s has the copy "
                "from %s." % (shown, where, manifest.get("made_utc"))]
