"""Path rules shared by uploads to ARC and by the ARC classifier job (PROTOCOL.md 5.6).

ARC's Projects collection in Globus shows one cluster folder, the collection root (``/projects``
by default), as its top ``/``: the folder ``/projects/<dir>/x`` that ``ls`` shows on ARC is
``/<dir>/x`` in Globus. Every Globus path of an upload and every download folder of the ARC job is
computed with the functions below, so both transports land in the same folder by construction.

Pure functions, standard library only (the ARC job imports this module on the cluster).
"""

from __future__ import annotations

import os
import pathlib
import re

# One part of a folder path: starts with a letter or digit, then letters, digits, '.', '_', '-'.
# So no '.', '..', no leading '-' or '.', and no empty part. Use it with fullmatch(): in Python '$'
# also matches before a newline at the end, so match() would take "folder\n".
FOLDER_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# The longest ARC folder setting accepted.
MAX_FOLDER_CHARS = 200


def _example(root: str) -> str:
    top = "" if root == "/" else root.rstrip("/")
    return f"{top}/<project-directory>/<folder>"


def check_cluster_folder(path: str, root: str) -> list[str]:
    """Problems with an ARC folder (a cluster path such as /projects/<dir>/<folder>), as sentences.

    Returns [] when the folder is valid: absolute, every part matching FOLDER_PART (so no '.',
    '..', no leading '-' or '.', no empty part, no '/' at the end), at most MAX_FOLDER_CHARS
    characters, inside ``root`` with at least two parts below it. ``root`` itself and
    ``root/<dir>`` alone are refused, so an upload never writes straight into a project directory.
    """
    example = _example(root)
    if not isinstance(path, str) or not path:
        return [f"The ARC folder is empty: it must be a folder on ARC such as {example}."]
    if not path.startswith("/"):
        return [f"The ARC folder {path!r} must start with / (a folder on ARC such as {example})."]
    problems = []
    if len(path) > MAX_FOLDER_CHARS:
        problems.append(f"The ARC folder is longer than {MAX_FOLDER_CHARS} characters.")
    bad = [part for part in path.split("/")[1:] if not FOLDER_PART.fullmatch(part)]
    if bad:
        shown = ", ".join(repr(part) for part in bad[:3])
        problems.append(
            f"The ARC folder {path!r} has a part that is not allowed ({shown}): each part must start "
            "with a letter or digit and use only letters, digits, '.', '_' and '-' (at most 64 "
            "characters), with no empty part and no / at the end."
        )
    try:
        below = collection_path(path, root)
    except ValueError:
        problems.append(
            f"The ARC folder {path} is not inside {root}, the part of ARC that its Globus collection shows."
        )
    else:
        if len([part for part in below.split("/") if part]) < 2:
            problems.append(
                f"The ARC folder must be a folder inside a project directory, such as {example}; "
                f"{path} alone is not allowed."
            )
    return problems


def collection_path(cluster_path: str, root: str) -> str:
    """The Globus path, inside ARC's collection, of a folder that ``ls`` on ARC shows as ``cluster_path``.

    root == "/"                       -> cluster_path unchanged
    cluster_path == root              -> "/"
    cluster_path starts with root+"/" -> "/" + the rest   (/projects/pcl/x -> /pcl/x)
    otherwise                         -> ValueError
    """
    if root == "/":
        return cluster_path
    if cluster_path == root:
        return "/"
    if cluster_path.startswith(root + "/"):
        return "/" + cluster_path[len(root) + 1:]
    raise ValueError(f"{cluster_path} is not inside {root}")


def relative_below(local_path, root) -> str:
    """The POSIX path of ``local_path`` below ``root``, both resolved first (links followed).

    Returns "" when they are the same folder; raises ValueError when ``local_path`` is not inside
    ``root``. Resolving both sides keeps the answer right when the work folder or the root is reached
    through a link (review F-04).
    """
    local = pathlib.Path(os.fspath(local_path)).resolve()
    top = pathlib.Path(os.fspath(root)).resolve()
    try:
        relative = local.relative_to(top)
    except ValueError:
        raise ValueError(f"{local_path} is not inside {root}") from None
    text = relative.as_posix()
    return "" if text == "." else text


def demo_local_path(demo_root, cluster_path: str) -> pathlib.Path:
    """The folder on this Mac that stands in for ``cluster_path`` in demo mode: demo_root/<path>.

    Raises ValueError for a path with a '..' part, so nothing outside the demo root is reached.
    """
    relative = cluster_path.lstrip("/")
    if any(part == ".." for part in relative.split("/")):
        raise ValueError(f"{cluster_path} has a '..' part")
    return pathlib.Path(demo_root) / relative
