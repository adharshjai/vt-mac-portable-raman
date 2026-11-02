# API check: Bluesky and Globus

Reviewed 2026-10-26. These are two different types of integration:

- **Bluesky:** local Python experiment-execution API and device protocols.
- **Globus:** authenticated remote web service accessed through its Python SDK.

Neither project claims that a manufacturer supplied a ready-made Raman API.
Our adapters connect the official software interfaces to our local workflow.

## Bluesky_Bridge

Installed official packages: `bluesky==1.15.1`, `ophyd==1.11.2`.
The implementation imports the real libraries, not a locally imitated engine.

| Actual call / interface | Purpose in our code |
|---|---|
| `bluesky.RunEngine` | Executes the generator plan and produces run documents |
| `plan_stubs.trigger` | Starts one full existing Qt scan through our adapter |
| `ophyd.status.Status` | Reports completion of the triggered action |
| `read()` and `describe()` | Return point values, timestamps, type, shape, and units |
| `plan_stubs.trigger_and_read` | Records each saved point using the software point reader |
| `plan_stubs.sleep`, `wait`, `stop` | Poll interval, completion, and stop handling |
| `preprocessors.run_wrapper` | Opens/closes the run and reports failure |
| `preprocessors.finalize_wrapper` | Attempts cleanup on interruption/failure |
| `RunEngine.subscribe` | Writes Start, Descriptor, Event, and Stop documents |

Our `QtScan` and `PointReading` classes implement these interfaces. The local
JSON mailbox remains our own Qt transport, not a Bluesky built-in Raman driver.
One trigger starts a complete Qt scan; Qt still controls individual motor moves
and captures. We do not claim an independently implemented Bluesky motor driver,
point-by-point Bluesky hardware scheduling, or automatic laser control.

Raw 2048-pixel spectra are embedded in Event data. `source_file` is additional
provenance text, not an external-asset key replacing the raw data. Therefore
the current demo is not claiming a StreamResource/StreamDatum asset writer.
PCA-SVM inference and preprocessing are our existing model code.

Verification:

- Re-ran the existing 13 no-hardware tests.
- Existing tests validate generated documents against the official Event Model schemas.
- Added a read-only audit of the runtime `Triggerable`, `Readable`, and `Stoppable` protocols.
- Previous GUI and copied SDK-free Qt emulator integration results remain documented
  in `Bluesky_Bridge/VERIFICATION.json`.
- This audit did not run a new physical scan or alter the Bluesky/INTERSECT code.

References:

- [Hardware protocol specification, v1.15.1](https://blueskyproject.io/bluesky/v1.15.1/hardware.html)
- [RunEngine and plans tutorial, v1.15.1](https://blueskyproject.io/bluesky/v1.15.1/tutorial.html)
- [Bluesky Event Model](https://blueskyproject.io/event-model/main/explanations/data-model.html)

## Globus_Bridge

Installed official package: `globus-sdk==4.10.0`.
The production adapter uses `UserApp` and `TransferClient`. The offline preview
also builds its payload with the official `TransferData` class.

| SDK call | Service operation |
|---|---|
| `UserApp.login()` | Official native OAuth/PKCE login via the SDK's local-server flow |
| `TransferClient.get_endpoint()` | `GET /v0.10/endpoint/<collection_id>` |
| `TransferClient.operation_ls()` | `GET /v0.10/operation/endpoint/<collection_id>/ls` |
| `TransferClient.get_submission_id()` | `GET /v0.10/submission_id` |
| `TransferData` and `add_item()` | Build transfer options and source/destination items |
| `TransferClient.submit_transfer()` | `POST /v0.10/transfer` |
| `TransferClient.get_task()` | `GET /v0.10/task/<task_id>` |
| `TransferClient.task_event_list()` | `GET /v0.10/task/<task_id>/event_list` |
| `TransferClient.cancel_task()` | `POST /v0.10/task/<task_id>/cancel` |

The live base URL is `https://transfer.api.globus.org/`. No replacement server,
local file-copy simulation, vendor-control endpoint, or invented Globus API is used.
`TransferData` contains the SDK's public `MISSING` sentinel for optional unset
fields. The saved JSON preview omits those fields, matching the HTTP serializer.

The tutorial native client ID, two collection IDs, and `/share/godata/file1.txt`
come from the official SDK example. The destination filename is unique to our demo.
The tutorial client is for learning; a deployed application should register its own ID.

Verification:

- HTTP contract tests invoke the real SDK while `responses` mocks HTTP only.
- Tests check HTTP methods, paths, query parameters, request body, server submission
  ID reuse, task ID handling, cancellation, and error propagation.
- Memory-token/local-server app construction is tested without network access.
- Offline preview has no task ID and never reports a successful transfer.
- A live transfer can only be verified after the human user logs in and the real
  server returns a task status. See `VERIFICATION.json` for the current status.

References:

- [Official SDK transfer example](https://globus-sdk-python.readthedocs.io/en/stable/user_guide/usage_patterns/data_transfer/submit_transfer/index.html)
- [Official SDK TransferClient API](https://globus-sdk-python.readthedocs.io/en/stable/services/transfer.html)
- [Official SDK login flow API](https://globus-sdk-python.readthedocs.io/en/stable/authorization/login_flows.html)
- [Official Transfer API task submission](https://docs.globus.org/api/transfer/task_submit/)
