"""The catalog interface (PROTOCOL.md 5.5), the alias rules (5.2) and the catalog factory.

One implementation exists: `local_catalog.LocalDemoCatalog` (files on this Mac). Everything
else in the data layer talks to this interface only.
"""

from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass

from .config import ALIAS_PATTERN, MAX_ALIAS_LENGTH, DataLayerError

# Relation of a dependency, read "record <relation> other": is derived from, is a component of,
# is a new version of.
RELATIONS = ("der", "comp", "ver")
# Titles are shortened to 80 characters, counted in UTF-16 code units, so that an emoji counts twice.
MAX_TITLE_LENGTH = 80
# The end of the title of a record about something simulated (simulated_mark).
SIMULATED_MARK = " [simulated]"
# Put where a title was shortened.
_SHORTENED = "..."
# The most metadata one record may carry.
MAX_METADATA_BYTES = 102400

RAW_TYPE = "raman.raw_spectrum"
DARK_TYPE = "raman.dark_spectrum"
PREDICTION_TYPE = "raman.prediction"
# In the metadata of a dark spectrum's record whose file never reached the catalog and will not:
# why, in words. The instrument computer sets it once it gives up on the upload.
UNAVAILABLE_KEY = "unavailable"


class CatalogError(DataLayerError):
    kind = "catalog"


class CatalogOffline(CatalogError):
    """The catalog cannot be reached. Nothing was changed; the same call can be made later."""

    kind = "offline"


class CatalogAuthRequired(CatalogError):
    """The user has to set up credentials themselves before the catalog can be used."""

    kind = "auth_required"


class CatalogConflict(CatalogError):
    """The alias is taken by something with a different identity."""

    kind = "conflict"


class CatalogNotFound(CatalogError):
    kind = "not_found"


@dataclass(frozen=True)
class Dep:
    """One dependency of a record. direction "out": the record <relation> `id`; "in": `id` <relation> the record."""

    relation: str
    id: str
    alias: str | None
    direction: str


@dataclass
class Record:
    id: str
    alias: str | None
    title: str
    metadata: dict
    deps: tuple = ()
    description: str = ""
    tags: tuple = ()
    # Bytes of raw data attached to the record; 0 while nothing has been uploaded.
    data_size: int = 0
    # True only on the Record returned by the create_record call that made the record.
    created: bool = False


@dataclass(frozen=True)
class Collection:
    id: str
    alias: str | None
    title: str
    description: str = ""
    parent: str | None = None


@dataclass(frozen=True)
class RecordSummary:
    """One item of a collection. An id starting with "c/" is a sub-collection."""

    id: str
    alias: str | None
    title: str


@dataclass(frozen=True)
class Transfer:
    task_id: str
    status: str
    message: str = ""


class Catalog(abc.ABC):
    """What the data layer needs from a catalog. Aliases and ids are interchangeable arguments."""

    name = ""
    label = ""

    @abc.abstractmethod
    def whoami(self):
        """{"online", "user", "context", "endpoint", "demo"}, plus "problem" when something is wrong.

        Never raises CatalogOffline or CatalogAuthRequired: it reports them.
        """

    @abc.abstractmethod
    def ensure_collection(self, alias, title, description="", parent=None):
        """Return the collection with this alias, creating it under `parent` (None: the root) if needed."""

    @abc.abstractmethod
    def create_record(self, alias, title, metadata, parent, deps=(), description="", tags=(), identity_key=None):
        """Create a record, or return the one that already has this alias.

        An existing record is returned when its metadata[identity_key] equals the new one (or when
        identity_key is None); otherwise CatalogConflict. `deps` holds (relation, id or alias) pairs.
        """

    @abc.abstractmethod
    def get_record(self, id_or_alias):
        """The record with its dependencies in both directions."""

    @abc.abstractmethod
    def update_record(self, id_or_alias, metadata=None, deps_add=(), title=None):
        """Merge `metadata` into the record's metadata (nested objects are merged, not replaced).

        A null value in `metadata` comes back as a missing key (the local demo drops null-valued
        keys on update). Read updated metadata with .get().
        """

    @abc.abstractmethod
    def link(self, collection, record):
        """Put the record in the collection too. Doing it twice is not an error."""

    @abc.abstractmethod
    def unlink(self, collection, record):
        """Take the record out of the collection. Not being in it is not an error."""

    @abc.abstractmethod
    def list_collection(self, collection):
        """Every item of the collection as RecordSummary, all pages."""

    @abc.abstractmethod
    def put_data(self, record, local_path):
        """Start uploading the file as the record's raw data. Returns at once with a Transfer."""

    @abc.abstractmethod
    def transfer_status(self, task_id):
        """The Transfer now: queued, running, succeeded or failed."""

    @abc.abstractmethod
    def get_data(self, record, dest_dir):
        """Download the record's raw data into dest_dir and return the file's path. Blocks until it is there."""


def check_alias(alias):
    if not isinstance(alias, str) or not ALIAS_PATTERN.match(alias) or len(alias) > MAX_ALIAS_LENGTH:
        raise CatalogError(
            "Not a valid alias: %r" % (alias,),
            detail="An alias is 1 to %d characters of a-z, 0-9, '.', '_' or '-'." % MAX_ALIAS_LENGTH,
        )
    return alias


def title_length(text):
    """The length of a text in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _start_within(text, units):
    """The longest start of `text` that takes at most `units` UTF-16 code units."""
    used = 0
    for index, character in enumerate(text):
        used += 2 if ord(character) > 0xFFFF else 1
        if used > units:
            return text[:index]
    return text


def fit_title(head, tail=""):
    """A title no longer than the catalog allows. The free text in front is shortened and the tail
    kept whole; a tail that leaves no room for that (a long session folder name) is shortened together
    with the text in front. A "[simulated]" marker at the end always stays, also when a whole title
    is fitted again."""
    head = " ".join(str(head).split())
    if not tail and head.endswith(SIMULATED_MARK):
        head, tail = head[:-len(SIMULATED_MARK)], SIMULATED_MARK
    room = MAX_TITLE_LENGTH - title_length(tail)
    if title_length(head) <= room:
        return head + tail
    if room >= len(_SHORTENED):
        return _start_within(head, room - len(_SHORTENED)) + _SHORTENED + tail
    mark = SIMULATED_MARK if tail.endswith(SIMULATED_MARK) else ""
    text = head + tail[:len(tail) - len(mark)]
    return _start_within(text, MAX_TITLE_LENGTH - len(mark) - len(_SHORTENED)) + _SHORTENED + mark


def simulated_mark(metadata):
    """The end of a title that says the record is about something simulated."""
    return SIMULATED_MARK if metadata.get("simulated") else ""


def dependency_pairs(deps):
    """Normalise `deps` arguments to (relation, id or alias) pairs."""
    pairs = []
    for dep in deps or ():
        relation, target = (dep.relation, dep.id) if isinstance(dep, Dep) else dep
        if relation not in RELATIONS:
            raise CatalogError("Unknown dependency relation: %r" % (relation,))
        pairs.append((relation, target))
    return pairs


def same_identity(existing_metadata, new_metadata, identity_key):
    """The rule of create_record for an alias that already exists."""
    if identity_key is None:
        return True
    if identity_key not in new_metadata:
        raise CatalogError("The metadata has no %r to identify the record by" % identity_key)
    return existing_metadata.get(identity_key) == new_metadata[identity_key]


def h12(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def session_alias(session_id):
    return "rsess-" + h12(session_id)


def raw_alias(capture_id):
    return "rs-" + h12(capture_id)


def dark_alias(dark_sha256):
    return "rdark-" + h12(dark_sha256)


def model_alias(model_hash):
    return "rmodel-" + model_hash[:12]


def prediction_alias(capture_id, model_hash):
    return "rp-" + h12(capture_id + ":" + model_hash)


def session_predictions_alias(session_id):
    return "rpred-" + h12(session_id)


@dataclass(frozen=True)
class Layout:
    """Aliases of the fixed collections under one root alias."""

    root: str
    sessions: str
    darks: str
    models: str
    predictions: str
    to_classify: str
    needs_review: str


_FIXED_COLLECTIONS = (
    ("sessions", "-sessions", "Scan sessions", "One collection per scan session."),
    ("darks", "-darks", "Dark spectra", "Dark spectra referenced by the raw spectra."),
    ("models", "-models", "Classifier models", "One record per classifier model version."),
    ("predictions", "-predictions", "Predictions",
     "One collection per scan session, holding one record per classified spectrum."),
    ("to_classify", "-to-classify", "Queue: waiting for the classifier", "Raw spectra not classified yet."),
    ("needs_review", "-needs-review", "Queue: waiting for a person", "Predictions a person has to check."),
)


def layout_for(root_alias):
    names = {field: root_alias + suffix for field, suffix, _title, _description in _FIXED_COLLECTIONS}
    return Layout(root=root_alias, **names)


def ensure_layout(catalog, root_alias):
    """Create the fixed collections that do not exist yet and return their aliases."""
    names = layout_for(root_alias)
    catalog.ensure_collection(
        names.root,
        "Portable Raman",
        "Spectra captured with the portable Raman controller, with their dark spectra, classifier models "
        "and predictions.",
    )
    for field, _suffix, title, description in _FIXED_COLLECTIONS:
        catalog.ensure_collection(getattr(names, field), title, description, parent=names.root)
    return names


def open_catalog(settings, paths):
    """The catalog of this edition. Nothing is read here; the catalog opens its files lazily."""
    from .local_catalog import LocalDemoCatalog

    return LocalDemoCatalog(paths.local_demo)
