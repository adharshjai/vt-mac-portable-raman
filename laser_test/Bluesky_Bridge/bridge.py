"""Bluesky plans for the existing whole-scan Qt interface; no hardware at import."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import math
import os
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from bluesky import RunEngine, plan_stubs as bps, preprocessors as bpp
from bluesky.utils import DuringTask
from ophyd.status import Status

ROOT = Path(__file__).resolve().parent
MODES = ("1d-x", "1d-y", "2d")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=json_default)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(dump(value), encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        temp.unlink(missing_ok=True)


def settings(path=None):
    path = Path(path or ROOT / "config.json").resolve()
    cfg = read_json(path)
    for key in ("intersect_bridge_dir", "output_dir"):
        value = Path(cfg[key])
        cfg[key] = (path.parent / value).resolve() if not value.is_absolute() else value
    if not 0.05 <= float(cfg["poll_seconds"]) <= 5:
        raise ValueError("poll_seconds must be between 0.05 and 5")
    if float(cfg["scan_timeout_seconds"]) <= 0:
        raise ValueError("scan_timeout_seconds must be positive")
    return cfg


def device_settings(cfg):
    """Read only local paths from the old config; never copy broker credentials."""
    folder = cfg["intersect_bridge_dir"]
    original = read_json(folder / "config.json")
    result = {}
    for key in ("ipc_dir", "scan_root", "model_dir", "dark_file"):
        value = Path(original[key])
        result[key] = value.resolve() if value.is_absolute() else (folder / value).resolve()
    if cfg.get("dark_file_override"):
        result["dark_file"] = Path(cfg["dark_file_override"]).resolve()
    return result


def upstream_module(cfg, name):
    folder = cfg["intersect_bridge_dir"]
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        module = importlib.import_module(name)
    finally:
        sys.dont_write_bytecode = previous
    if Path(module.__file__).resolve().parent != folder:
        raise RuntimeError(f"Unexpected module location for {name}")
    return module


def qt_client(cfg):
    return upstream_module(cfg, "qt_client").QtClient(device_settings(cfg)["ipc_dir"])


def predictor(cfg):
    paths = device_settings(cfg)
    return upstream_module(cfg, "svm_predictor").Predictor(paths["model_dir"], paths["dark_file"])


def validate_point(record, sid, expected):
    if record.get("session_id") != sid or record.get("point_id") != expected:
        raise ValueError("Point/session mismatch or missing/out-of-order point")
    raw = np.asarray(record.get("intensity"), dtype=float)
    if raw.shape != (2048,) or not np.isfinite(raw).all():
        raise ValueError("Expected a finite 2048-pixel spectrum")
    for axis in "xyz":
        value = record.get(f"controller_{axis}_steps")
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Missing or invalid reported motor position")
    return record


class ReplayScan:
    name = "raman_scan"
    parent = None

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.files = sorted(self.directory.glob("point_*.json"))
        if not self.files:
            raise ValueError("No point_*.json files in the selected scan folder")
        self.sid = read_json(self.files[0])["session_id"]
        self.mode = read_json(self.files[0]).get("scan_mode", "unknown")
        self.seen = 0
        self.status = None
        self.done = False

    def trigger(self):
        self.status = Status()
        return self.status

    def poll(self):
        record = validate_point(read_json(self.files[self.seen]), self.sid, self.seen + 1)
        self.seen += 1
        if self.seen == len(self.files):
            self.done = True
            self.status.set_finished()
        return [(record, self.files[self.seen - 1])]

    def stop(self, *, success=False):
        if self.status is not None and not self.status.done:
            self.status.set_finished()


class SimulatedScan:
    name = "raman_scan"
    parent = None

    def __init__(self, mode="1d-y", count=13):
        self.sid = "simulation-" + uuid.uuid4().hex
        self.directory = None
        self.mode, self.count = mode, count
        self.seen = 0
        self.done = False
        self.status = None

    def trigger(self):
        self.status = Status()
        return self.status

    def poll(self):
        self.seen += 1
        pixels = np.arange(2048)
        peak = 700 * np.exp(-((pixels - 680) / 35) ** 2)
        envelope = 0.3 + np.exp(-((self.seen - 7) / 3) ** 2)
        raw = 900 + envelope * peak + 12 * np.sin(pixels / 11 + self.seen)
        record = dict(session_id=self.sid, point_id=self.seen,
                      capture_id=f"{self.sid}_{self.seen}", status="OK", pixel_count=2048,
                      intensity=raw.tolist(), scan_mode=self.mode,
                      controller_x_steps=(self.seen-1)*100 if self.mode == "1d-x" else 0,
                      controller_y_steps=(self.seen-1)*100 if self.mode != "1d-x" else 0,
                      controller_z_steps=0)
        if self.seen == self.count:
            self.done = True
            self.status.set_finished()
        return [(record, None)]

    def stop(self, *, success=False):
        if self.status is not None and not self.status.done:
            self.status.set_finished()


class QtScan:
    """One trigger starts ONE existing Qt scan, not individual motor moves."""
    name = "raman_scan"
    parent = None

    def __init__(self, mailbox, scan_root, mode, run_dir):
        self.mailbox, self.scan_root = mailbox, Path(scan_root).resolve()
        self.mode, self.run_dir = mode, Path(run_dir)
        self.request_id = "bs-" + uuid.uuid4().hex
        self.sid, self.directory = None, None
        self.seen = 0
        self.done = False
        self.status = None
        self.finish_wait = None
        self.cancel_reply = None
        self.accepted_utc_ms = 0
        self.boot_id = None

    def trigger(self):
        state = self.mailbox.status()
        self.boot_id = state["boot_id"]
        if not state.get("remote_scan_enabled"):
            raise RuntimeError("Enable remote scans in the LOCAL Qt window first")
        if (not state.get("laser_on") or state.get("laser_state_uncertain")
                or state.get("backend_fault") or state.get("scan_active")
                or state.get("collecting")):
            raise RuntimeError("Qt is not ready. Check the local laser, scan and device state")
        # Durable before submission: uncertain starts are never auto-retried.
        save_json(self.run_dir / "start_request.json", dict(request_id=self.request_id,
                  mode=self.mode, qt_boot_id=state["boot_id"], state="submitting"))
        self.status = Status()
        reply = self.mailbox.request("start_scan", self.request_id, mode=self.mode)
        save_json(self.run_dir / "start_reply.json", reply)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "Qt rejected the scan"))
        self.sid = reply["session_id"]
        self.accepted_utc_ms = reply.get("state", {}).get("utc_ms", 0)
        self.directory = Path(reply["session_dir"]).resolve()
        if self.directory.parent != self.scan_root:
            raise RuntimeError("Qt returned a scan folder outside configured scan_root")
        return self.status

    def poll(self):
        state = self.mailbox.status()
        if state.get("boot_id") != self.boot_id:
            raise RuntimeError("Qt restarted during this scan; inspect local Qt")
        # Qt writes status before processing commands on a timer tick. The start
        # reply can therefore arrive one tick before status.json has the new ID.
        if state.get("utc_ms", 0) < self.accepted_utc_ms:
            return []
        if state.get("session_id") != self.sid:
            if state.get("utc_ms", 0) == self.accepted_utc_ms:
                return []
            raise RuntimeError("Qt session changed; this plan will not read another experiment")
        if state.get("backend_fault") or state.get("laser_state_uncertain"):
            raise RuntimeError("Qt reported a device fault or uncertain laser state")
        phase = state.get("scan_state")
        if phase in ("failed", "cancelled"):
            raise RuntimeError(f"Qt scan {phase}")
        rows = []
        # Read only atomically saved records up to the published completion count.
        published = int(state.get("last_completed_point", 0))
        for index in range(self.seen + 1, min(published, self.seen + 10) + 1):
            path = self.directory / f"point_{index:04d}.json"
            if not path.exists():
                break
            rows.append((validate_point(read_json(path), self.sid, index), path))
            self.seen = index
        if phase == "completed":
            if self.seen == published and published > 0:
                self.done = True
                self.status.set_finished()
            elif not rows:
                self.finish_wait = self.finish_wait or time.monotonic()
                if time.monotonic() - self.finish_wait > 5:
                    raise RuntimeError("Qt completed but one or more point records are missing")
        elif not state.get("scan_active"):
            raise RuntimeError("Qt scan stopped without a completed state")
        return rows

    def stop(self, *, success=False):
        if self.done or not self.sid:
            return
        # Never cancel another client's session, or retry a start with a new ID.
        state = self.mailbox.status()
        deadline = time.monotonic() + 2
        while (state.get("boot_id") == self.boot_id
               and state.get("session_id") != self.sid
               and state.get("utc_ms", 0) <= self.accepted_utc_ms
               and time.monotonic() < deadline):
            time.sleep(0.05)
            state = self.mailbox.status()
        if state.get("boot_id") != self.boot_id:
            raise RuntimeError("Cannot cancel: Qt restarted; inspect local Qt")
        if state.get("session_id") != self.sid:
            raise RuntimeError("Cannot cancel: Qt is now on a different session")
        if state.get("scan_active") or state.get("collecting"):
            reply = self.mailbox.request("cancel_scan", "bs-cancel-" + uuid.uuid4().hex,
                                         session_id=self.sid)
            self.cancel_reply = reply
            save_json(self.run_dir / "cancel_reply.json", reply)
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "Qt did not acknowledge cancellation"))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = self.mailbox.status()
                if current.get("session_id") != self.sid:
                    raise RuntimeError("Session changed while waiting for cancellation")
                if not current.get("scan_active") and not current.get("collecting"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Cancel acknowledged, but Qt is still active; use local Stop")
        if self.status is not None and not self.status.done:
            # The physical action has ended. The plan's original exception records
            # failure/cancellation; a second FailedStatus would hide that cause.
            self.status.set_finished()


class PointReading:
    name = "raman_point"
    parent = None

    def set_point(self, record, source_path, analysis):
        raw = np.asarray(record["intensity"], dtype=float)
        self.values = dict(point_id=record["point_id"], session_id=record["session_id"],
                           capture_id=record["capture_id"], spectrum=raw,
                           raw_max=float(raw.max()), raw_mean=float(raw.mean()),
                           source_file=str(source_path or ""), label=analysis["label"],
                           reason=analysis.get("reason", ""), scores_json=dump(analysis.get("scores", {})),
                           acquisition_status=record["status"],
                           capture_completed_utc=record.get("completed_utc", ""),
                           source_scan_mode=record.get("scan_mode", "unknown"))
        for axis in "xyz":
            self.values[f"controller_{axis}_steps"] = record[f"controller_{axis}_steps"]
        self.timestamp = time.time()

    def trigger(self):
        status = Status()
        status.set_finished()
        return status

    def read(self):
        return {key: dict(value=value, timestamp=self.timestamp) for key, value in self.values.items()}

    def describe(self):
        result = {}
        for key, value in self.values.items():
            dtype = "array" if isinstance(value, np.ndarray) else "string" if isinstance(value, str) else "number"
            result[key] = dict(source=f"raman-qt:{key}", dtype=dtype,
                               shape=list(value.shape) if isinstance(value, np.ndarray) else [])
            if key.endswith("_steps"):
                result[key]["units"] = "controller steps (reported; not calibrated mm)"
        return result


class RunWriter:
    def __init__(self, directory, metadata, on_event):
        self.directory, self.metadata, self.on_event = directory, metadata, on_event
        self.rows, self.run_uid, self.stop_doc = [], None, None
        self.stream = (directory / "documents.jsonl").open("x", encoding="utf-8")

    def __call__(self, name, doc):
        self.stream.write(dump(dict(name=name, doc=doc)) + "\n")
        self.stream.flush()
        if name == "start":
            self.run_uid = doc["uid"]
            self.on_event("log", f"Bluesky run UID: {self.run_uid}")
        elif name == "event":
            data = doc["data"]
            row = {key: value for key, value in data.items() if key != "spectrum"}
            row["scores"] = json.loads(row.pop("scores_json"))
            row.update(bluesky_run_uid=self.run_uid, sample_id=self.metadata["sample_id"])
            save_json(self.directory / f"point_{int(data['point_id']):04d}.json", row)
            self.rows.append(row)
            self.on_event("point", dict(data, seq_num=doc["seq_num"]))
        elif name == "stop":
            self.stop_doc = dict(doc)

    def close(self):
        self.stream.close()


class UserCancelled(RuntimeError):
    pass


def experiment_plan(source, model, writer, cancel, timeout, interval):
    point = PointReading()

    def body():
        deadline = time.monotonic() + timeout
        group = uuid.uuid4().hex
        if cancel.is_set():
            raise UserCancelled("Stopped before submitting a scan")
        yield from bps.trigger(source, group=group)
        writer.on_event("log", f"Session: {source.sid}")
        if source.directory:
            writer.on_event("log", f"Source spectra: {source.directory}")
        while True:
            if cancel.is_set():
                raise UserCancelled("Stop requested by user")
            if time.monotonic() > deadline:
                raise TimeoutError("Scan timeout; cancellation will be attempted")
            for record, path in source.poll():
                if cancel.is_set():
                    raise UserCancelled("Stop requested by user")
                result = model.predict(record) if model else dict(
                    label="SIMULATED" if isinstance(source, SimulatedScan) else "NOT_ANALYZED", scores={})
                point.set_point(record, path, result)
                yield from bps.trigger_and_read([point])
            if source.done:
                break
            yield from bps.sleep(interval)
        yield from bps.wait(group=group)

    def cleanup():
        if not source.done:
            try:
                yield from bps.stop(source)
            except Exception as exc:
                # Preserve the original scan error as well as the stop failure.
                source.cleanup_error = f"{type(exc).__name__}: {exc}"
                writer.on_event("log", "Stop handling issue: " + source.cleanup_error)

    # Cleanup happens before the run's stop document is emitted.
    yield from bpp.run_wrapper(bpp.finalize_wrapper(body(), cleanup()), md=writer.metadata)


def run_experiment(kind="simulation", sample_id="demo-sample", mode="1d-y", session_dir=None,
                   analyze=True, cfg=None, cancel=None, on_event=None, timeout=None):
    if kind not in ("simulation", "replay", "hardware") or mode not in MODES:
        raise ValueError("Unsupported data source or scan mode")
    if not sample_id.strip() or len(sample_id) > 160:
        raise ValueError("Enter a sample ID (1 to 160 characters)")
    cfg = cfg or settings()
    cancel, on_event = cancel or threading.Event(), on_event or (lambda name, value: None)
    timeout = float(timeout if timeout is not None else cfg["scan_timeout_seconds"])
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    # Load trusted local models BEFORE any hardware request.
    model = predictor(cfg) if analyze and kind != "simulation" else None
    token = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    directory = cfg["output_dir"] / (kind + "_" + token)
    directory.mkdir(parents=True, exist_ok=False)
    if kind == "simulation":
        source = SimulatedScan(mode)
    elif kind == "replay":
        source = ReplayScan(session_dir)
    else:
        source = QtScan(qt_client(cfg), device_settings(cfg)["scan_root"], mode, directory)
    metadata = dict(sample_id=sample_id.strip(), data_source=kind, plan_name=f"raman_{kind}_plan",
                    requested_scan_mode=source.mode, scan_execution="existing Qt whole-scan controller" if kind == "hardware" else kind,
                    analysis="PCA-SVM" if model else "disabled",
                    model_sha256=model.version if model else "", dark_sha256=model.dark_hash if model else "",
                    software_versions={p: importlib.metadata.version(p) for p in ("bluesky", "ophyd", "numpy")},
                    note="Motor values are reported controller steps. Class scores are not accuracy.")
    if model:
        paths = device_settings(cfg)
        metadata.update(dark_file=str(paths["dark_file"]), model_dir=str(paths["model_dir"]))
    writer = RunWriter(directory, metadata, on_event)
    save_json(directory / "manifest.json", metadata)
    on_event("output", str(directory))
    error, outcome = "", "failed"
    engine = RunEngine({}, context_managers=[], during_task=DuringTask())
    # RunEngine lives in a worker thread in the GUI; no process-level signal handler.
    engine.subscribe(writer)
    try:
        engine(experiment_plan(source, model, writer, cancel, timeout, float(cfg["poll_seconds"])))
        outcome = "completed"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        outcome = "cancelled" if isinstance(exc, UserCancelled) else "failed"
        on_event("log", error)
    finally:
        writer.close()
        cleanup_error = getattr(source, "cleanup_error", "")
        if cleanup_error:
            outcome = "failed"
        summary = dict(metadata, state=outcome, error=error, bluesky_run_uid=writer.run_uid,
                       cleanup_error=cleanup_error,
                       qt_session_id=source.sid, source_directory=str(source.directory or ""),
                       request_id=getattr(source, "request_id", ""), recorded_points=len(writer.rows),
                       labels=dict(Counter(row["label"] for row in writer.rows)),
                       stop_document=writer.stop_doc, finished_utc=datetime.now(timezone.utc).isoformat(),
                       output_directory=str(directory), laser_power_changed_by_bluesky=False)
        save_json(directory / "summary.json", summary)
        on_event("finished", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("simulation", "replay", "hardware", "status", "check"))
    parser.add_argument("--config")
    parser.add_argument("--sample-id", default="demo-sample")
    parser.add_argument("--mode", choices=MODES, default="1d-y")
    parser.add_argument("--session-dir")
    parser.add_argument("--no-analysis", action="store_true")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--execute-existing-qt-scan", action="store_true",
                        help="Explicitly submit a real Qt scan; local Qt arming is also required")
    args = parser.parse_args()
    cfg = settings(args.config)
    if args.action == "status":
        print(dump(qt_client(cfg).status()))
        return
    if args.action == "check":
        paths = device_settings(cfg)
        model = predictor(cfg)
        print(dump(dict(check="passed", hardware_started=False, model_sha256=model.version,
                        dark_sha256=model.dark_hash, paths=paths)))
        return
    if args.action == "hardware" and not args.execute_existing_qt_scan:
        parser.error("Hardware action requires --execute-existing-qt-scan")
    if args.action == "replay" and not args.session_dir:
        parser.error("replay requires --session-dir")
    def output(name, value):
        if name in ("log", "output"):
            print(value, flush=True)
        elif name == "point":
            print(f"Point {value['point_id']}: {value['label']}", flush=True)
    import signal
    cancel = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        summary = run_experiment(args.action, args.sample_id, args.mode, args.session_dir,
                                 not args.no_analysis, cfg, cancel=cancel,
                                 on_event=output, timeout=args.timeout)
    finally:
        signal.signal(signal.SIGINT, previous)
    print(dump(summary))
    if summary["state"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
