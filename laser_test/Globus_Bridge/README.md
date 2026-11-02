# Globus Raman Data Bridge

This is a separate desktop demo using the **official Globus Python SDK**
(`globus-sdk==4.10.0`) and the **Globus Transfer API**.

Globus transfers files between authorized storage collections. It does not
directly move the Raman motor, turn on the laser, or run PCA-SVM in this demo.
The existing INTERSECT and Bluesky programs are unchanged.

## First demo: official Globus test data

Double-click **GlobusBridge.exe** or **Start_Globus.cmd**.
Keep the EXE, `.venv`, and Python files together in this folder.
Opening the app does not log in or submit a transfer.

1. Leave Demo mode on **Official tutorial data**.
2. Click **Log in with Globus**. Your default browser opens the official
   Globus login page. Choose your university or another supported identity.
3. Review the Globus authorization page and approve it yourself if acceptable.
   The default client is the official SDK tutorial native client. It is not
   a separately registered production Raman application.
4. Return to this window after the browser says login is complete.
5. Click **List source folder** to check access to the official test files.
6. Optionally click **Offline API preview** to inspect the SDK-built request.
   This is not a transfer and does not produce a task ID.
7. Click **Submit REAL transfer**, review the source/destination, then confirm.
8. A real Globus task ID appears. Wait for **GLOBUS SUCCEEDED**.
   Use **Check status** or **Show task events** to inspect the server response.
9. Click **List destination folder** to find the new test file.

The default demo copies only `/share/godata/file1.txt` between these two
Globus-hosted tutorial collections:

| Role | Collection UUID |
|---|---|
| Globus Tutorial Collection 1 | `6c54cade-bde5-45c1-bdea-f4bd71dba2cc` |
| Globus Tutorial Collection 2 | `31ce9ba0-176d-45a5-add3-f37d233ba47d` |

The destination is `/~/raman-globus-demo-<time>_<unique_id>.txt`.
Each new tutorial target uses a unique name. No local Raman data is uploaded.
These defaults come from the official SDK transfer tutorial, not invented IDs.

You can also inspect the destination through
[Globus File Manager](https://app.globus.org/), using the same account and the
destination collection/path recorded in the task.

## What is a successful demo?

- **Offline preview:** the SDK made a valid request document; nothing was sent.
- **SUBMITTED:** the real service accepted a task and returned its task ID.
- **ACTIVE:** the server is still processing it.
- **SUCCEEDED:** the server reports successful completion.
- **FAILED:** inspect task events and permissions.

A successful offline or mocked HTTP test is not proof that a real transfer
completed. Only the live Globus task response can establish that.

## Login and permissions

The GUI uses official `UserApp`, `GlobusAppConfig`, and the native
`LocalServerLoginFlowManager` (selected as `local-server`). Globus handles
OAuth/PKCE and redirects the browser to a loopback callback on this computer.
No password, secret, or authorization code needs to be pasted into this chat.

Credentials stay in memory while this window is open. Refresh tokens and
on-disk token caching are not enabled. You may need to log in after reopening.
The official native client ID is public and is not an API key or password.
For a deployed research application, register your own native client ID using
the official instructions linked below, then replace the client ID in the GUI.

Some collections require more consent, identity linking, recent login, or
project permissions. The SDK's `auto_redrive_gares=True` handles supported
authentication requirements by requesting another login/consent when needed.
It does not grant storage access. This app does not blindly add `data_access`
scopes to every collection; the SDK documentation warns this can be wrong for
guest, GCP, and High Assurance collections.

## Local records and interrupted requests

The **Open local records** button opens `Globus_Bridge/records/`.

- `preview_*.json`: offline request preview; `submitted=false`, `task_id=null`.
- `live_<time>_<id>/record.json`: source, destination, server submission ID,
  request payload, returned task ID, and subsequent status responses.

The server submission ID is saved before submitting the request. If the
connection is lost after submission, use **Retry SAME submission** or load the
saved `record.json`. This reuses exactly the same submission ID and payload.
It does not allocate a new submission ID or silently create another transfer.
Do not edit an uncertain request's payload and reuse its ID.

Closing the GUI does not cancel a server task. Reopen the program, log in,
choose **Load saved task**, and check its status. **Cancel task** requests
remote cancellation; check status again to confirm that the task has ended.
No file deletion API is provided in this demo.

## Later: transfer Raman data

Select **Custom collections** only after confirming the source and target
storage permissions. A Windows folder is not automatically a Globus collection.
Your local storage must be exposed by Globus Connect Personal or another
authorized endpoint. The target may be an institutional collection, another
computer, or an authorized VT ARC project directory.

Use the collection's path as shown in Globus File Manager, not an unmodified
Windows path such as `C:\Users\...`. Confirm the mapping before transferring.
The GUI supports a file or a directory (`recursive`). A destination file that
already exists can be replaced; review it before submission.

The intended larger workflow is:

```
Qt / Bluesky / INTERSECT acquires and analyzes Raman data
                         |
                         v
Saved spectra and experimental records
                         |
                         v
Globus Transfer API -> authorized destination storage
```

Globus Compute and Flows are separate services. They are not implemented in
this transfer demo, and no cloud-side model analysis is claimed.

## Files

| File | Purpose |
|---|---|
| `bridge.py` | Official SDK adapter and optional CLI |
| `app.py` | English desktop interface |
| `launcher.py` / `GlobusBridge.exe` | Small launcher for the local environment |
| `config.json` | Public tutorial defaults and last saved record path; no tokens |
| `requirements.txt` | Tested direct runtime versions |
| `requirements-lock.txt` | Exact environment snapshot, including test/build tools |
| `test_bridge.py` | Real SDK with mocked HTTP contract tests |
| `API_AUDIT.md` | Bluesky and Globus API mapping and limits |
| `VERIFICATION.json` | What was tested; live-transfer status |

## Setup / optional command line

This machine has a separate Python 3.12 environment in `.venv`.
On another computer install Python 3.12, then run `Setup_Globus.cmd`.
The EXE alone is not a portable bundle.

```powershell
.\.venv\Scripts\python.exe bridge.py preview
.\.venv\Scripts\python.exe bridge.py tutorial-transfer --execute-official-test-transfer
.\.venv\Scripts\python.exe bridge.py status --task-id REAL_TASK_UUID
.\.venv\Scripts\python.exe bridge.py events --task-id REAL_TASK_UUID
.\.venv\Scripts\python.exe bridge.py retry --record "PATH_TO_LIVE_RECORD_JSON"
```

The GUI is recommended: it keeps authorization in memory between operations.
Separate CLI invocations may each require login because tokens are not saved.

Tests (no actual login or transfer):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest test_bridge -v
```

## Official references

- [SDK transfer tutorial: collection IDs, source path, and client ID](https://globus-sdk-python.readthedocs.io/en/stable/user_guide/usage_patterns/data_transfer/submit_transfer/index.html)
- [SDK UserApp tutorial](https://globus-sdk-python.readthedocs.io/en/stable/user_guide/getting_started/minimal_script.html)
- [SDK login flow managers](https://globus-sdk-python.readthedocs.io/en/stable/authorization/login_flows.html)
- [SDK TransferClient reference](https://globus-sdk-python.readthedocs.io/en/stable/services/transfer.html)
- [Transfer API task submission](https://docs.globus.org/api/transfer/task_submit/)
- [Register your own native application](https://globus-sdk-python.readthedocs.io/en/stable/user_guide/getting_started/register_app.html)
- [VT ARC Globus setup](https://docs.arc.vt.edu/software/globus.html)
