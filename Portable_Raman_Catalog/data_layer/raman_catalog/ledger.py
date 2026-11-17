"""The local ledger: one row per capture in HOME/data_catalog/ledger.sqlite (PROTOCOL.md 5.4).

The ledger is what makes offline work: a capture is written here as soon as it is found on
disk and moves from state to state as the catalog accepts it. Every change is its own committed
transaction, so a sync that dies leaves each row in the last state it really reached, and the
next sync continues from there.

Several processes may use the ledger at once (a sync started by a timer, one started by a
click, the status poll of the controller). SQLite serialises their writes; `sync_lock` makes
sure only one of them runs a sync.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
import sqlite3
from pathlib import Path

from .config import LEDGER_FILE_NAME, REVIEW_LOCK_FILE_NAME, SYNC_LOCK_FILE_NAME, utc_now

STATES = (
    "captured",
    "registered",
    "uploading",
    "uploaded",
    "classified",
    "needs_review",
    "reviewed",
    "accepted",
    "error",
)
STATE_LABELS = {
    "captured": "Captured",
    "registered": "Registered",
    "uploading": "Uploading",
    "uploaded": "Uploaded",
    "classified": "Classified",
    "needs_review": "Needs review",
    "reviewed": "Reviewed",
    "accepted": "Accepted",
    "error": "Error",
}
ERROR_STAGES = ("metadata", "register", "upload")
# Not safely in the catalog yet.
PENDING_STATES = ("captured", "registered", "uploading", "error")
# States in which a prediction, or a review made elsewhere, may still turn up.
AWAITING_PREDICTION_STATES = ("uploaded", "classified", "needs_review")
REVIEW_DONE_STATUSES = ("confirmed", "relabelled", "rejected")

_SCHEMA_VERSION = 1
_TABLES = (
    """CREATE TABLE IF NOT EXISTS captures (
        capture_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        session_folder TEXT NOT NULL,
        point_file TEXT NOT NULL,
        point_id INTEGER,
        scan_mode TEXT,
        sample_id TEXT,
        operator TEXT,
        location TEXT,
        timestamp_utc TEXT,
        simulated INTEGER,
        x_mm REAL,
        y_mm REAL,
        z_mm REAL,
        state TEXT NOT NULL,
        error_stage TEXT,
        error_message TEXT,
        register_attempts INTEGER NOT NULL DEFAULT 0,
        upload_attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        last_error_utc TEXT,
        record_id TEXT,
        record_alias TEXT,
        raw_sha256 TEXT,
        upload_task_id TEXT,
        upload_status TEXT,
        upload_message TEXT,
        prediction_id TEXT,
        prediction_json TEXT,
        review_json TEXT,
        review_queued INTEGER NOT NULL DEFAULT 0,
        created_utc TEXT NOT NULL,
        updated_utc TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS captures_by_state ON captures (state)",
    "CREATE INDEX IF NOT EXISTS captures_by_file ON captures (session_folder, point_file)",
    "CREATE INDEX IF NOT EXISTS captures_by_prediction ON captures (prediction_id)",
    """CREATE TABLE IF NOT EXISTS darks (
        sha256 TEXT PRIMARY KEY,
        file TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_alias TEXT,
        state TEXT NOT NULL,
        upload_task_id TEXT,
        upload_status TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_utc TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        catalog_key TEXT NOT NULL,
        prediction_id TEXT NOT NULL,
        review_json TEXT NOT NULL,
        queued_utc TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    )""",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)",
)

_SET_VALUE = "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value"
# The captures a sync moves forward: new, registered or uploading, and the failed ones still below
# the attempt limit (:max). Missing fields and errors past the limit wait for a person.
_TO_ADVANCE = ("(state IN ('captured', 'registered', 'uploading')"
               " OR (state = 'error' AND error_stage = 'register' AND register_attempts < :max)"
               " OR (state = 'error' AND error_stage = 'upload' AND upload_attempts < :max))")
_DESCRIPTIVE_COLUMNS = (
    "session_id",
    "session_folder",
    "point_file",
    "point_id",
    "scan_mode",
    "sample_id",
    "operator",
    "location",
    "timestamp_utc",
    "simulated",
    "x_mm",
    "y_mm",
    "z_mm",
)
_CATALOG_KEY = "catalog_key"
# The captures a change of catalog makes "captured" again (bind_catalog): all but those lacking
# fields, which no catalog has taken yet.
_REBOUND = "NOT (state = 'error' AND error_stage = 'metadata')"
_SURROGATE = re.compile("[\ud800-\udfff]")
_SQLITE_INTEGERS = range(-2 ** 63, 2 ** 63)


def _flag(value):
    return None if value is None else int(bool(value))


def _storable(value):
    """A value of a point file as a descriptive column can hold it.

    The columns only describe the capture for the list; the record is made from the file. Text with
    half of a character in it (a lone surrogate, which has no UTF-8 form) is stored with U+FFFD in
    its place, and what SQLite cannot hold at all (an object, a list, a whole number beyond 64 bits)
    as NULL, so that a point whose file is not usable as it is still gets its row in error/metadata.
    """
    if isinstance(value, str):
        return _SURROGATE.sub("\ufffd", value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value in _SQLITE_INTEGERS else None
    if value is None or isinstance(value, (bool, float)):
        return value
    return None


def _loads(text):
    return json.loads(text) if text else None


def review_state(prediction, review):
    """The ledger state that a prediction and its review stand for."""
    if prediction.get("needs_review"):
        return "needs_review"
    if (review or {}).get("status") in REVIEW_DONE_STATUSES:
        return "reviewed"
    return "accepted"


def item_from_row(row):
    """The `list` item of PROTOCOL.md section 6 for one ledger row."""
    position = None
    if row["x_mm"] is not None and row["y_mm"] is not None and row["z_mm"] is not None:
        position = {"x": row["x_mm"], "y": row["y_mm"], "z": row["z_mm"]}
    upload = None
    if row["upload_task_id"]:
        upload = {"task_id": row["upload_task_id"], "status": row["upload_status"]}
    review = _loads(row["review_json"])
    if review is not None:
        review = {key: review.get(key) for key in ("status", "label", "reviewer", "time_utc")}
        if row["review_queued"]:
            # Recorded here while offline; the catalog does not have it yet.
            review["queued"] = True
    error = None
    if row["state"] == "error":
        attempts = {"register": row["register_attempts"], "upload": row["upload_attempts"]}
        error = {"stage": row["error_stage"], "message": row["error_message"],
                 "attempts": attempts.get(row["error_stage"], 0)}
    return {
        "capture_id": row["capture_id"],
        "session_id": row["session_id"],
        "session_folder": row["session_folder"],
        "point_id": row["point_id"],
        "point_file": row["point_file"],
        "scan_mode": row["scan_mode"],
        "sample_id": row["sample_id"],
        "operator": row["operator"],
        "timestamp_utc": row["timestamp_utc"],
        "simulated": None if row["simulated"] is None else bool(row["simulated"]),
        "position_mm": position,
        "state": row["state"],
        "state_label": STATE_LABELS[row["state"]],
        "record_id": row["record_id"],
        "record_alias": row["record_alias"],
        "upload": upload,
        "prediction": _loads(row["prediction_json"]),
        "review": review,
        "error": error,
    }


class Ledger:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / LEDGER_FILE_NAME
        self.lock_path = self.data_dir / SYNC_LOCK_FILE_NAME
        # Autocommit: transactions are opened explicitly, and only where a change needs one.
        self._db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if self._db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
            with self._write() as db:
                for statement in _TABLES:
                    db.execute(statement)
                db.execute("PRAGMA user_version = %d" % _SCHEMA_VERSION)

    def close(self):
        self._db.close()

    @contextlib.contextmanager
    def _write(self):
        # IMMEDIATE takes the write lock up front: two writers then wait for each other instead
        # of failing halfway through with "database is locked".
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _all(self, sql, parameters=()):
        return [dict(row) for row in self._db.execute(sql, parameters).fetchall()]

    def _one(self, sql, parameters=()):
        row = self._db.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ sync lock

    @contextlib.contextmanager
    def sync_lock(self):
        """Hold HOME/data_catalog/sync.lock for a whole sync. Yields False, at once, when another
        process holds it. The lock goes away with the process, so a sync that dies cannot block
        the next one."""
        with open(self.lock_path, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def review_lock(self):
        """Hold HOME/data_catalog/review.lock while one review goes to the catalog and the ledger.

        Waits for another process that holds it. A review made in the window or the Studio and
        the sending of a review queued offline then happen one after the other, never across
        each other, so an older review cannot be written over a newer one.
        """
        with open(self.data_dir / REVIEW_LOCK_FILE_NAME, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ small values

    def get_value(self, key, default=None):
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row is not None else default

    def set_value(self, key, value):
        with self._write() as db:
            db.execute(_SET_VALUE, (key, value))

    # ------------------------------------------------------------------ which catalog the rows belong to

    @property
    def catalog_key(self):
        return self.get_value(_CATALOG_KEY)

    def bind_catalog(self, key):
        """Make the ledger describe the catalog named by `key`. Returns True when rows were reset.

        Record ids, uploads and predictions belong to one catalog. When the user switches catalog
        (another root alias), every capture is "captured" again for
        the new one, so that it gets registered there instead of looking finished.
        """
        with self._write() as db:
            row = db.execute("SELECT value FROM kv WHERE key = ?", (_CATALOG_KEY,)).fetchone()
            current = row[0] if row is not None else None
            if current == key:
                return False
            db.execute(_SET_VALUE, (_CATALOG_KEY, key))
            if current is None:
                # Reviews queued before the ledger knew its catalog belong to this, its first one.
                db.execute("UPDATE review_queue SET catalog_key = ? WHERE catalog_key = ''", (key,))
                return False
            db.execute(
                """UPDATE captures SET state = 'captured', error_stage = NULL, error_message = NULL,
                       register_attempts = 0, upload_attempts = 0, record_id = NULL, record_alias = NULL,
                       raw_sha256 = NULL, upload_task_id = NULL, upload_status = NULL, upload_message = NULL,
                       prediction_id = NULL, prediction_json = NULL, review_json = NULL, review_queued = 0,
                       updated_utc = ?
                   WHERE """ + _REBOUND,
                (utc_now(),),
            )
            db.execute("DELETE FROM darks")
            return True

    def count_rebound(self):
        """How many captures bind_catalog makes "captured" again when the catalog changes. Changes
        nothing: the status asked for before that sync counts with it."""
        return self._db.execute("SELECT COUNT(*) FROM captures WHERE " + _REBOUND).fetchone()[0]

    # ------------------------------------------------------------------ captures

    def known_files(self):
        """{(session_folder, point_file): capture_id} for the rows that need no second look.

        Rows in error/metadata are left out: their file is read again by every sync, because
        the missing fields may have been supplied in the meantime.
        """
        rows = self._db.execute(
            "SELECT session_folder, point_file, capture_id FROM captures "
            "WHERE NOT (state = 'error' AND error_stage = 'metadata')"
        )
        return {(row[0], row[1]): row[2] for row in rows}

    def add_capture(self, entry, error_message=None):
        """Add a point found on disk, or bring a row waiting in error/metadata up to date.

        `entry` holds capture_id and the descriptive columns; `error_message` is set when the
        point file cannot be registered as it is. Returns "added", "fixed" (a row left
        error/metadata), "unchanged", or "duplicate" when another file already has this capture_id.
        """
        now = utc_now()
        state = "error" if error_message else "captured"
        stage = "metadata" if error_message else None
        values = [_storable(entry.get(column)) for column in _DESCRIPTIVE_COLUMNS]
        with self._write() as db:
            row = db.execute(
                "SELECT session_folder, point_file, state, error_stage, error_message FROM captures "
                "WHERE capture_id = ?",
                (entry["capture_id"],),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO captures (capture_id, %s, state, error_stage, error_message, last_error, "
                    "last_error_utc, created_utc, updated_utc) VALUES (?, %s, ?, ?, ?, ?, ?, ?, ?)"
                    % (", ".join(_DESCRIPTIVE_COLUMNS), ", ".join("?" * len(_DESCRIPTIVE_COLUMNS))),
                    [entry["capture_id"], *values, state, stage, error_message, error_message,
                     now if error_message else None, now, now],
                )
                return "added"
            if (row["session_folder"], row["point_file"]) != (entry["session_folder"], entry["point_file"]):
                return "duplicate"
            if not (row["state"] == "error" and row["error_stage"] == "metadata"):
                return "unchanged"
            if error_message and error_message == row["error_message"]:
                return "unchanged"
            db.execute(
                "UPDATE captures SET %s, state = ?, error_stage = ?, error_message = ?, updated_utc = ? "
                "WHERE capture_id = ?" % ", ".join(column + " = ?" for column in _DESCRIPTIVE_COLUMNS),
                [*values, state, stage, error_message, now, entry["capture_id"]],
            )
            return "unchanged" if error_message else "fixed"

    def relocate(self, capture_id, session_folder, point_file):
        """The session folder was renamed or moved: point the row at where the file is now."""
        with self._write() as db:
            db.execute("UPDATE captures SET session_folder = ?, point_file = ?, updated_utc = ? WHERE capture_id = ?",
                       (session_folder, point_file, utc_now(), capture_id))

    def get(self, capture_id):
        return self._one("SELECT * FROM captures WHERE capture_id = ?", (capture_id,))

    def find_by_prediction(self, prediction_id):
        return self._one("SELECT * FROM captures WHERE prediction_id = ?", (prediction_id,))

    @staticmethod
    def _filter(states, session):
        clauses, parameters = [], []
        if states:
            clauses.append("state IN (%s)" % ", ".join("?" * len(states)))
            parameters.extend(states)
        if session:
            clauses.append("(session_id = ? OR session_folder = ?)")
            parameters.extend([session, session])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", parameters

    def rows(self, states=None, session=None, limit=None, newest_first=True):
        where, parameters = self._filter(states, session)
        order = "DESC" if newest_first else "ASC"
        sql = "SELECT * FROM captures%s ORDER BY timestamp_utc %s, session_folder %s, point_id %s" % (
            where, order, order, order)
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return self._all(sql, parameters)

    def count(self, states=None, session=None):
        where, parameters = self._filter(states, session)
        return self._db.execute("SELECT COUNT(*) FROM captures" + where, parameters).fetchone()[0]

    def counts(self):
        """Number of captures per state, every state present."""
        result = dict.fromkeys(STATES, 0)
        for state, number in self._db.execute("SELECT state, COUNT(*) FROM captures GROUP BY state"):
            result[state] = number
        return result

    def error_counts(self, max_attempts):
        """How the rows in `error` split up: lacking fields, to be retried, given up on."""
        row = self._db.execute(
            """SELECT
                   COALESCE(SUM(error_stage = 'metadata'), 0),
                   COALESCE(SUM((error_stage = 'register' AND register_attempts < :max)
                                OR (error_stage = 'upload' AND upload_attempts < :max)), 0),
                   COALESCE(SUM((error_stage = 'register' AND register_attempts >= :max)
                                OR (error_stage = 'upload' AND upload_attempts >= :max)), 0)
               FROM captures WHERE state = 'error'""",
            {"max": max_attempts},
        ).fetchone()
        return {"metadata": row[0], "retrying": row[1], "given_up": row[2]}

    def rows_to_advance(self, max_attempts):
        """Captures a sync still has to move forward, oldest first. Errors past the limit stay put."""
        return self._all(
            "SELECT * FROM captures WHERE " + _TO_ADVANCE + " ORDER BY timestamp_utc, session_folder, point_id",
            {"max": max_attempts},
        )

    def count_to_advance(self, max_attempts):
        """How many rows rows_to_advance would give, without reading them: asked at every status."""
        return self._db.execute("SELECT COUNT(*) FROM captures WHERE " + _TO_ADVANCE,
                                {"max": max_attempts}).fetchone()[0]

    def reset_attempts(self):
        """Give every failed capture, dark upload and queued review its full number of attempts again."""
        with self._write() as db:
            db.execute("UPDATE captures SET register_attempts = 0, upload_attempts = 0 WHERE state = 'error'")
            db.execute("UPDATE darks SET attempts = 0 WHERE state = 'error'")
            db.execute("UPDATE review_queue SET attempts = 0")

    def _update(self, capture_id, assignments, parameters=()):
        with self._write() as db:
            db.execute("UPDATE captures SET %s, updated_utc = ? WHERE capture_id = ?" % assignments,
                       [*parameters, utc_now(), capture_id])

    def mark_registered(self, capture_id, record_id, record_alias, raw_sha256):
        self._update(
            capture_id,
            "state = 'registered', error_stage = NULL, error_message = NULL, register_attempts = 0, "
            "record_id = ?, record_alias = ?, raw_sha256 = ?",
            (record_id, record_alias, raw_sha256),
        )

    def mark_uploading(self, capture_id, task_id, status, message=""):
        self._update(
            capture_id,
            "state = 'uploading', error_stage = NULL, error_message = NULL, upload_task_id = ?, "
            "upload_status = ?, upload_message = ?",
            (task_id, status, message),
        )

    def set_upload_status(self, capture_id, status, message=""):
        self._update(capture_id, "upload_status = ?, upload_message = ?", (status, message))

    def mark_uploaded(self, capture_id):
        self._update(capture_id, "state = 'uploaded', upload_status = 'succeeded', upload_message = '', "
                                 "upload_attempts = 0")

    def mark_error(self, capture_id, stage, message):
        """Put a capture in the `error` side state. Register and upload failures count as an attempt."""
        if stage not in ERROR_STAGES:
            raise ValueError("unknown error stage: %r" % (stage,))
        counting = ""
        if stage != "metadata":
            counting = ", %s_attempts = %s_attempts + 1" % (stage, stage)
        if stage == "upload":
            counting += ", upload_status = 'failed', upload_message = ?"
        now = utc_now()
        parameters = [stage, message, message, now] + ([message] if stage == "upload" else [])
        self._update(
            capture_id,
            "state = 'error', error_stage = ?, error_message = ?, last_error = ?, last_error_utc = ?" + counting,
            parameters,
        )

    def set_prediction(self, capture_id, prediction_id, metadata):
        """Copy a prediction record into the capture's row and set the state it stands for.

        Returns True when the row changed. A review that was recorded here while offline and
        still waits to be sent is kept: the catalog cannot know about it yet.
        """
        prediction = {
            "record_id": prediction_id,
            "label": metadata.get("label"),
            "top_class": metadata.get("top_class"),
            "confidence": metadata.get("confidence"),
            "outcome": metadata.get("outcome"),
            "needs_review": bool(metadata.get("needs_review")),
        }
        review = metadata.get("review") if isinstance(metadata.get("review"), dict) else None
        with self._write() as db:
            row = db.execute(
                "SELECT state, prediction_id, prediction_json, review_json, review_queued FROM captures "
                "WHERE capture_id = ?", (capture_id,)).fetchone()
            if row is None:
                return False
            if row["review_queued"] and row["prediction_id"] == prediction_id:
                return False
            state = review_state(prediction, review)
            before = (row["state"], _loads(row["prediction_json"]), _loads(row["review_json"]))
            if before == (state, prediction, review):
                return False
            db.execute(
                "UPDATE captures SET state = ?, prediction_id = ?, prediction_json = ?, review_json = ?, "
                "review_queued = 0, updated_utc = ? WHERE capture_id = ?",
                (state, prediction_id, json.dumps(prediction), json.dumps(review) if review is not None else None,
                 utc_now(), capture_id),
            )
            return True

    # ------------------------------------------------------------------ reviews

    @staticmethod
    def _store_review(db, prediction_id, review, queued):
        row = db.execute("SELECT capture_id, prediction_json FROM captures WHERE prediction_id = ?",
                         (prediction_id,)).fetchone()
        if row is None:
            return None
        prediction = _loads(row["prediction_json"]) or {"record_id": prediction_id}
        prediction["needs_review"] = False
        db.execute(
            "UPDATE captures SET state = 'reviewed', prediction_json = ?, review_json = ?, review_queued = ?, "
            "updated_utc = ? WHERE capture_id = ?",
            (json.dumps(prediction), json.dumps(review), _flag(queued), utc_now(), row["capture_id"]),
        )
        return row["capture_id"]

    def record_review(self, prediction_id, review):
        """A review that is in the catalog: show it in the capture's row. Returns the capture_id, if any."""
        with self._write() as db:
            return self._store_review(db, prediction_id, review, queued=False)

    def queue_review(self, prediction_id, review):
        """A review made while the catalog is unreachable: keep it for the next sync.

        The capture's row shows the review at once, marked as waiting. A later review of the same
        prediction replaces one that still waits.
        """
        with self._write() as db:
            key = db.execute("SELECT value FROM kv WHERE key = ?", (_CATALOG_KEY,)).fetchone()
            key = key[0] if key is not None else ""
            db.execute("DELETE FROM review_queue WHERE catalog_key = ? AND prediction_id = ?", (key, prediction_id))
            db.execute(
                "INSERT INTO review_queue (catalog_key, prediction_id, review_json, queued_utc) VALUES (?, ?, ?, ?)",
                (key, prediction_id, json.dumps(review), utc_now()),
            )
            return self._store_review(db, prediction_id, review, queued=True)

    def queued_reviews(self, catalog_key, max_attempts=None):
        """Reviews waiting for the catalog named by `catalog_key`, oldest first."""
        sql = "SELECT * FROM review_queue WHERE catalog_key = ?"
        parameters = [catalog_key]
        if max_attempts is not None:
            sql += " AND attempts < ?"
            parameters.append(max_attempts)
        rows = self._all(sql + " ORDER BY id", parameters)
        for row in rows:
            row["review"] = json.loads(row.pop("review_json"))
        return rows

    def queued_review(self, queue_id):
        """One waiting review as `queued_reviews` gives it; None once it was sent or replaced."""
        row = self._one("SELECT * FROM review_queue WHERE id = ?", (queue_id,))
        if row is not None:
            row["review"] = json.loads(row.pop("review_json"))
        return row

    def review_sent(self, queue_id):
        with self._write() as db:
            row = db.execute("SELECT prediction_id FROM review_queue WHERE id = ?", (queue_id,)).fetchone()
            if row is None:
                return
            db.execute("DELETE FROM review_queue WHERE id = ?", (queue_id,))
            db.execute("UPDATE captures SET review_queued = 0, updated_utc = ? WHERE prediction_id = ?",
                       (utc_now(), row["prediction_id"]))

    def review_failed(self, queue_id, message):
        with self._write() as db:
            db.execute("UPDATE review_queue SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                       (message, queue_id))

    # ------------------------------------------------------------------ dark spectra

    def get_dark(self, sha256):
        return self._one("SELECT * FROM darks WHERE sha256 = ?", (sha256,))

    def add_dark(self, sha256, file, record_id, record_alias):
        with self._write() as db:
            db.execute(
                "INSERT OR IGNORE INTO darks (sha256, file, record_id, record_alias, state, updated_utc) "
                "VALUES (?, ?, ?, ?, 'registered', ?)",
                (sha256, file, record_id, record_alias, utc_now()),
            )

    def darks_to_advance(self, max_attempts):
        return self._all(
            "SELECT * FROM darks WHERE state IN ('registered', 'uploading') OR (state = 'error' AND attempts < ?) "
            "ORDER BY file",
            (max_attempts,),
        )

    def failed_darks(self):
        """The dark spectra whose upload failed, the most recent failure first."""
        return self._all("SELECT * FROM darks WHERE state = 'error' ORDER BY updated_utc DESC")

    def _update_dark(self, sha256, assignments, parameters=()):
        with self._write() as db:
            db.execute("UPDATE darks SET %s, updated_utc = ? WHERE sha256 = ?" % assignments,
                       [*parameters, utc_now(), sha256])

    def dark_uploading(self, sha256, task_id, status):
        self._update_dark(sha256, "state = 'uploading', upload_task_id = ?, upload_status = ?", (task_id, status))

    def dark_status(self, sha256, status):
        self._update_dark(sha256, "upload_status = ?", (status,))

    def dark_uploaded(self, sha256):
        self._update_dark(sha256, "state = 'uploaded', upload_status = 'succeeded', attempts = 0, last_error = NULL")

    def dark_failed(self, sha256, message):
        self._update_dark(sha256, "state = 'error', upload_status = 'failed', attempts = attempts + 1, last_error = ?",
                          (message,))
