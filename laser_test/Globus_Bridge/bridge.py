"""Official Globus Transfer SDK adapter. No network call occurs at import/startup."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid

import globus_sdk

ROOT = Path(__file__).resolve().parent
TUTORIAL_CLIENT = "61338d24-54d5-408f-a10d-66c06b59f6d2"
TUTORIAL_SOURCE = "6c54cade-bde5-45c1-bdea-f4bd71dba2cc"
TUTORIAL_DESTINATION = "31ce9ba0-176d-45a5-add3-f37d233ba47d"
TRANSFER_API = "https://transfer.api.globus.org/v0.10"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def uuid_text(value):
    return str(uuid.UUID(str(value).strip()))


def collection_path(value):
    value = str(value).strip()
    if not value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise ValueError("Use a Globus collection path such as /share/godata/file1.txt, not a Windows path")
    if any(part == ".." for part in value.split("/")) or any(ord(ch) < 32 for ch in value):
        raise ValueError("Invalid collection path")
    return value


def tutorial_spec():
    unique = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    return TransferSpec(TUTORIAL_SOURCE, TUTORIAL_DESTINATION,
                        "/share/godata/file1.txt", f"/~/raman-globus-demo-{unique}.txt",
                        label="Raman Globus official tutorial demo")


@dataclass(frozen=True)
class TransferSpec:
    source_collection: str
    destination_collection: str
    source_path: str
    destination_path: str
    recursive: bool = False
    label: str = "Raman Globus data transfer"

    def validated(self):
        if not isinstance(self.recursive, bool):
            raise ValueError("recursive must be true or false")
        if not self.label.strip() or len(self.label) > 128:
            raise ValueError("Transfer label must contain 1 to 128 characters")
        spec = TransferSpec(uuid_text(self.source_collection), uuid_text(self.destination_collection),
                            collection_path(self.source_path), collection_path(self.destination_path),
                            self.recursive, self.label.strip())
        if spec.source_collection == spec.destination_collection and spec.source_path.rstrip("/") == spec.destination_path.rstrip("/"):
            raise ValueError("Source and destination are the same")
        return spec

    @property
    def official_tutorial_only(self):
        return (self.source_collection == TUTORIAL_SOURCE
                and self.destination_collection == TUTORIAL_DESTINATION
                and self.source_path == "/share/godata/file1.txt" and not self.recursive)


def transfer_data(spec, submission_id=None):
    """The official SDK builds the actual Transfer API request body."""
    spec = spec.validated()
    kwargs = dict(label=spec.label, verify_checksum=True, delete_destination_extra=False,
                  notify_on_succeeded=False, notify_on_failed=False, notify_on_inactive=False)
    if submission_id is not None:
        kwargs["submission_id"] = uuid_text(submission_id)
    data = globus_sdk.TransferData(spec.source_collection, spec.destination_collection, **kwargs)
    data.add_item(spec.source_path, spec.destination_path, recursive=spec.recursive)
    return data


def plain_payload(value):
    """Omit the SDK's public MISSING sentinel, as its HTTP serializer does."""
    if isinstance(value, Mapping):
        return {key: plain_payload(item) for key, item in value.items() if item is not globus_sdk.MISSING}
    if isinstance(value, (tuple, list)):
        return [plain_payload(item) for item in value if item is not globus_sdk.MISSING]
    return value


class SubmissionUncertain(RuntimeError):
    def __init__(self, record_path, detail):
        self.record_path = Path(record_path)
        super().__init__(f"Submission is uncertain. Do not create a new transfer. Retry the SAME saved submission or inspect Globus Activity. Record: {record_path}. {detail}")


class GlobusBridge:
    def __init__(self, client_id=TUTORIAL_CLIENT, record_root=None, client=None):
        self.client_id = uuid_text(client_id)
        self.record_root = Path(record_root or ROOT / "records")
        self.app = None
        self.client = client

    def ensure_client(self):
        if self.client is None:
            # Official SDK native OAuth flow: browser + loopback callback + PKCE.
            # Session-only credentials: no passwords or tokens written to this project.
            config = globus_sdk.GlobusAppConfig(login_flow_manager="local-server",
                         token_storage="memory", request_refresh_tokens=False, auto_redrive_gares=True)
            self.app = globus_sdk.UserApp("Raman Globus Demo", client_id=self.client_id, config=config)
            self.client = globus_sdk.TransferClient(app=self.app)
        return self.client

    def login(self):
        self.ensure_client()
        if self.app is None:
            raise RuntimeError("Interactive login is unavailable for an injected test client")
        self.app.login()
        return dict(logged_in=not self.app.login_required(), token_storage="memory",
                    sdk_version=importlib.metadata.version("globus-sdk"))

    def logout(self):
        if self.app is not None:
            self.app.logout()
        return dict(logged_in=False)

    def preview(self, spec):
        spec = spec.validated()
        result = dict(kind="offline-api-preview", submitted=False, task_id=None,
                      created_utc=now(), sdk_version=importlib.metadata.version("globus-sdk"),
                      api=f"POST {TRANSFER_API}/transfer", specification=asdict(spec),
                      payload=plain_payload(transfer_data(spec)),
                      note="No login, network call, or file transfer. Live submission gets a server submission_id first.")
        path = self.record_root / ("preview_" + uuid.uuid4().hex[:12] + ".json")
        save_json(path, result)
        return path, result

    def list_directory(self, collection, path):
        return dict(self.ensure_client().operation_ls(uuid_text(collection), path=collection_path(path)).data)

    def collection_info(self, collection):
        return dict(self.ensure_client().get_endpoint(uuid_text(collection)).data)

    def submit(self, spec):
        spec = spec.validated()
        client = self.ensure_client()
        directory = self.record_root / ("live_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
        path = directory / "record.json"
        record = dict(kind="globus-transfer-record", state="ALLOCATING_SUBMISSION_ID",
                      created_utc=now(), specification=asdict(spec), task_id=None,
                      official_tutorial_only=spec.official_tutorial_only,
                      sdk_version=importlib.metadata.version("globus-sdk"))
        save_json(path, record)
        try:
            submission = client.get_submission_id()["value"]
        except Exception as exc:
            record.update(state="NOT_SUBMITTED", error=safe_error(exc))
            save_json(path, record)
            raise
        record.update(submission_id=uuid_text(submission), payload=plain_payload(transfer_data(spec, submission)), state="SUBMITTING")
        # Save the server-generated id BEFORE POST. A retry reuses this exact id.
        save_json(path, record)
        return self._send_saved(path, record)

    def _send_saved(self, path, record):
        try:
            response = dict(self.ensure_client().submit_transfer(record["payload"]).data)
        except Exception as exc:
            record.update(state="SUBMISSION_UNCERTAIN", error=safe_error(exc))
            save_json(path, record)
            raise SubmissionUncertain(path, safe_error(exc)) from exc
        record.update(state="SUBMITTED", task_id=uuid_text(response["task_id"]),
                      submitted_utc=now(), submit_response=response)
        record.pop("error", None)
        save_json(path, record)
        return path, record

    def retry_saved(self, path):
        path, record = Path(path), self.load_record(path)
        if record.get("task_id"):
            raise ValueError("This record already has a task ID. Check status instead")
        if record.get("state") not in ("SUBMITTING", "SUBMISSION_UNCERTAIN"):
            raise ValueError("This record has no uncertain submitted request to retry")
        expected = plain_payload(transfer_data(TransferSpec(**record["specification"]), record["submission_id"]))
        if expected != record.get("payload"):
            raise ValueError("Saved payload changed; refusing to reuse its submission ID")
        return self._send_saved(path, record)

    @staticmethod
    def load_record(path):
        record = read_json(path)
        if record.get("kind") != "globus-transfer-record":
            raise ValueError("Choose a live Globus record.json, not an offline preview")
        if record.get("task_id"):
            uuid_text(record["task_id"])
        return record

    def task_status(self, task_id, record_path=None):
        task_id = uuid_text(task_id)
        result = dict(self.ensure_client().get_task(task_id).data)
        if record_path:
            record = self.load_record(record_path)
            if record.get("task_id") != task_id:
                raise ValueError("Task ID does not match the selected record")
            record.update(last_checked_utc=now(), last_status=result, state=result["status"])
            save_json(record_path, record)
        return result

    def events(self, task_id):
        return dict(self.ensure_client().task_event_list(uuid_text(task_id), limit=20).data)

    def cancel(self, task_id, record_path=None):
        task_id = uuid_text(task_id)
        if record_path:
            record = self.load_record(record_path)
            if record.get("task_id") != task_id:
                raise ValueError("Task ID does not match the selected record")
        reply = dict(self.ensure_client().cancel_task(task_id).data)
        if record_path:
            record.update(cancel_requested_utc=now(), cancel_response=reply)
            save_json(record_path, record)
        # A cancel request is not proof that the task has stopped; query it again.
        return reply

    def close(self):
        if self.client is not None:
            self.client.close()
        if self.app is not None:
            self.app.close()


def safe_error(exc):
    # Avoid persisting request headers, token responses, or authorization URLs.
    if isinstance(exc, globus_sdk.GlobusAPIError):
        return f"{type(exc).__name__}: HTTP {exc.http_status}; code={exc.code}; {exc.message}"
    if isinstance(exc, SubmissionUncertain):
        return str(exc)
    if isinstance(exc, (ValueError, FileNotFoundError, PermissionError)):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}. Check network/login and the saved transfer record."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "login", "tutorial-transfer", "status", "events", "cancel", "retry"))
    parser.add_argument("--task-id")
    parser.add_argument("--record")
    parser.add_argument("--execute-official-test-transfer", action="store_true")
    args = parser.parse_args()
    cfg = read_json(ROOT / "config.json")
    service = GlobusBridge(cfg["client_id"])
    try:
        if args.action == "preview":
            path, result = service.preview(tutorial_spec())
            print(path)
        elif args.action == "login":
            result = service.login()
        elif args.action == "tutorial-transfer":
            if not args.execute_official_test_transfer:
                parser.error("Use --execute-official-test-transfer to submit a real tutorial task")
            path, result = service.submit(tutorial_spec())
            print(path)
        elif args.action == "retry":
            if not args.record:
                parser.error("--record is required")
            path, result = service.retry_saved(args.record)
            print(path)
        else:
            if not args.task_id:
                parser.error("--task-id is required")
            result = (service.task_status(args.task_id, args.record) if args.action == "status"
                      else service.events(args.task_id) if args.action == "events"
                      else service.cancel(args.task_id, args.record))
        print(json.dumps(result, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
