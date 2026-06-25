# tussi

A [TUS 1.0.0](https://tus.io/protocols/resumable-upload) resumable upload server for Python. ASGI-native, filesystem storage, no framework lock-in.

File uploads break and are a chore to implement. tussi handles the resume. Clients pick up exactly where they left off. Drop it into any ASGI app, point a TUS client at it, done.

**Linux only**. Tussi uses `posix_fallocate` for pre-allocation and `fcntl.flock` for safe worker coordination.

## Install

[![PyPI](https://img.shields.io/pypi/v/tussi)](https://pypi.org/project/tussi/)

```bash
pip install tussi
```

Core dependencies (`anyio`, `pydantic`, `starlette`) are installed automatically. Optional extras:

| Extra | Installs | When you need it |
|---|---|---|
| `tussi[cli]` | `fastapi`, `rich`, `uvicorn`, `requests` | `tussi-server` and `tussi-upload` CLI tools |
| `tussi[test]` | `pytest`, `httpx`, `anyio[trio]` | Running the test suite |

## Quick start

```python
from pathlib import Path
from tussi import TUSApp, FilesystemStorage

storage = FilesystemStorage(directory=Path('./uploads'))
tus = TUSApp(
    storage=storage,
    completed_dir=Path('./completed'),
)
```

`tus` is a standard ASGI callable. Run it with any ASGI server:

```bash
uvicorn myapp:tus
```

## FastAPI integration

tussi does not require FastAPI, but integrates cleanly via `get_response`:

```python
from pathlib import Path
from fastapi import FastAPI, Request
from starlette.responses import Response
from tussi import TUSApp, FilesystemStorage

storage = FilesystemStorage(directory=Path('./uploads'))
tus = TUSApp(storage=storage, completed_dir=Path('./completed'))
app = FastAPI()

@app.api_route(
    '/files/{path:path}',
    methods=['HEAD', 'PATCH', 'POST', 'OPTIONS'],
    include_in_schema=False,
)
async def tus_handler(request: Request) -> Response:
    return await tus.get_response(request.scope, request.receive)
```

See [`tussi/_demo_server.py`](tussi/_demo_server.py) for a full example including auth dependency, lifespan worker, and janitor.

## Processing completed uploads

`wait_for_file` is an async context manager that blocks until a completed upload
is available, claims it with an exclusive lock, and cleans up on exit. Safe to
call from multiple concurrent workers, because each worker claims exactly one file.

```python
# tus = TUSApp(...) - from "Quick start" above
async with tus.wait_for_file(timeout=3600) as upload:
    filename = upload.record.metadata.get('filename', upload.name)
    upload.save(Path('./dest') / filename)
    upload.save_record(Path('./dest') / f'{filename}.meta')
```

- `upload.save(dest)` moves the upload file to `dest`
- `upload.save_record(dest)` moves the `.meta` sidecar file to `dest`; call this if you want to keep the record (fields like `finished_at`, `duration`, `metadata`) alongside the file
- Both raise `RuntimeError` if called more than once
- On context manager exit both files are deleted from `completed_dir`, regardless of whether `save`/`save_record` were called

To read back a saved record later:

```python
from tussi import UploadRecord

record = UploadRecord.from_file(Path('./dest') / f'{filename}.meta')
print(record.duration, record.metadata)
```

Raises `TimeoutError` if no upload is available within `timeout` seconds.

## UploadRecord

`upload.record` inside `wait_for_file` is an `UploadRecord` with these fields:

| Field | Type | Description |
|---|---|---|
| `metadata` | `dict[str, str]` | Key-value pairs decoded from the `Upload-Metadata` header |
| `server_metadata` | `dict[str, str]` | Key-value pairs returned by the `on_create` hook (empty if no hook) |
| `length` | `int \| None` | Declared upload size in bytes |
| `offset` | `int` | Bytes received |
| `created_at` | `float` | Unix timestamp of upload creation |
| `last_write` | `float` | Unix timestamp of last successful write |
| `finished_at` | `datetime \| None` | UTC timestamp set when finalized |
| `duration` | `timedelta \| None` | Time from creation to finalization |

Metadata constraints:

- Keys must match `[a-zA-Z0-9_-]+` (one or more characters). Pairs with invalid keys are silently ignored
- The total header size is limited by `max_metadata_size` (default `4096` bytes)
- The `filename` key, if present, must contain only printable ASCII (`0x20-0x7E`),
  otherwise the upload is rejected with `400 Bad Request`

Tussi never uses `filename` for storage. Uploads are always stored under a UUID. Path traversal via metadata is not possible.

## Event hooks

Pass `on_event` to react to upload lifecycle events:

```python
from tussi import TUSApp, TUSEvent, UploadCompletedEvent

async def on_event(event: TUSEvent) -> None:
    if isinstance(event, UploadCompletedEvent):
        print(f'upload complete: {event.upload_info.upload_id}')

tus = TUSApp(
    storage=storage,
    completed_dir=Path('./completed'),
    on_event=on_event,
)
```

Available events: `UploadCreatedEvent`, `UploadProgressEvent`,
`UploadCompletedEvent`, `UploadFailedEvent`.

## Server-side metadata

Pass `on_create` to inject server-controlled metadata at upload creation time.
The hook is called on every POST before the record is persisted. It receives
the request headers and the client-provided metadata, and returns a dict that
is stored separately as `server_metadata` on the record. The client metadata
is never modified.

```python
async def on_create(
    headers: dict[str, str],   # lowercase header names, decoded values
    metadata: dict[str, str],  # client-provided Upload-Metadata (already decoded)
) -> dict[str, str]:           # stored as record.server_metadata
    token = headers.get('authorization', '').removeprefix('Bearer ')
    user_id = await resolve_user(token)
    return {'uploaded_by': str(user_id)}

tus = TUSApp(
    storage=storage,
    completed_dir=Path('./completed'),
    on_create=on_create,
)
```

After the upload completes:

```python
async with tus.wait_for_file(timeout=3600) as upload:
    user = upload.record.server_metadata.get('uploaded_by')
    client_filename = upload.record.metadata.get('filename')
```

## Janitor

`Janitor` cleans up stale and stuck uploads. Call `janitor.run()` periodically, e.g. from a background worker.

```python
from tussi import Janitor

# storage and completed_dir are the same instances passed to TUSApp
janitor = Janitor(
    storage=storage,
    completed_dir=Path('./completed'),
)
```

| Parameter | Default | Description |
|---|---|---|
| `storage` | required | Same `Storage` instance as `TUSApp` |
| `completed_dir` | required | Same `completed_dir` as `TUSApp` |
| `stale_upload_age` | `86400` | Delete incomplete uploads with no write activity for this many seconds |
| `completed_file_age` | `604800` | Delete finalized files from `completed_dir` older than this many seconds |

Each `run()` handles four cleanup cases:

| Case | Trigger | Action |
|---|---|---|
| Finalize zombie | `offset == length` but `finalize` never ran | Delete from storage |
| Stale upload | No write for `stale_upload_age` seconds | Delete from storage |
| Orphaned meta | `.meta` in staging with no upload data | Remove `.meta` file |
| Old completed file | File in `completed_dir` older than `completed_file_age` | Delete file and `.meta` |

FastAPI lifespan example with file worker and periodic cleanup:

```python
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from tussi import TUSApp, FilesystemStorage, Janitor

log = logging.getLogger(__name__)

storage = FilesystemStorage(directory=Path('./uploads'))
tus = TUSApp(storage=storage, completed_dir=Path('./completed'))
janitor = Janitor(storage=storage, completed_dir=Path('./completed'))

@asynccontextmanager
async def lifespan(app: FastAPI):
    async def file_worker():
        while True:
            try:
                async with tus.wait_for_file(timeout=3600) as upload:
                    filename = upload.record.metadata.get('filename', upload.name)
                    upload.save(Path('./dest') / filename)
            except TimeoutError:
                pass
            except Exception:
                log.exception('file worker error')

    async def cleanup_worker():
        while True:
            await asyncio.sleep(3600)
            await janitor.run()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(file_worker())
        tg.create_task(cleanup_worker())
        yield

app = FastAPI(lifespan=lifespan)
```

## Security

Tussi has no built-in authentication. Protect the upload endpoint by placing auth in front of it either as ASGI middleware wrapping the whole app, or as a FastAPI dependency on the route:

```python
async def require_auth(request: Request) -> None:
    if request.headers.get('Authorization') != f'Bearer {SECRET}':
        raise HTTPException(status_code=401)

@app.api_route('/files/{path:path}', ..., dependencies=[Depends(require_auth)])
async def tus_handler(request: Request) -> Response:
    return await tus.get_response(request.scope, request.receive)
```

Other considerations:

- Set `max_size` and `max_chunk_size` to prevent clients from uploading arbitrarily large files
- Uploads are stored under UUIDs, never under the client-supplied `filename`. Path traversal via metadata is not possible
- The uploads and completed directories should not be served as static files

## Behind a reverse proxy

When tussi runs behind a reverse proxy (nginx, Caddy, Traefik, etc.), the `Location` header in `201 Created` responses must reflect the public URL, not
the internal one. Pass `trusted_proxies` to enable this:

```python
tus = TUSApp(
    storage=storage,
    completed_dir=Path('./completed'),
    trusted_proxies=['127.0.0.1', '10.0.0.0/8'],
)
```

Each entry is an IP address or CIDR range. When the connecting client's IP
matches, tussi reads `X-Forwarded-Proto` to determine the scheme for the
`Location` header. Without `trusted_proxies`, `X-Forwarded-Proto` is always
ignored. A client cannot spoof the scheme by sending the header directly.

**nginx example**

```nginx
location /files/ {
    proxy_pass         http://127.0.0.1:8000;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-Proto $scheme;
}
```

`proxy_set_header Host $host` is required so the `Location` hostname matches
the public domain. `X-Forwarded-Proto` is only honoured when the proxy IP is
listed in `trusted_proxies`.

Only `X-Forwarded-Proto` is used. `X-Forwarded-For` and `X-Forwarded-Host`
are not read.

## Configuration

### `TUSApp`

| Parameter | Default | Description | Type |
|---|---|---|---|
| `storage` | required | `Storage` instance (e.g. `FilesystemStorage`) | `tussi.storage.Storage` |
| `completed_dir` | required | Directory for finalized uploads | `pathlib.Path \| str` |
| `on_event` | `None` | Async callback for lifecycle events | `Callable[[TUSEvent], Awaitable[None]] \| None` |
| `on_create` | `None` | Async hook called on upload creation receiving request headers and client metadata, returns `server_metadata` to persist | `Callable[[dict[str, str], dict[str, str]], Awaitable[dict[str, str]]] \| None` |
| `max_size` | `None` | Max upload size in bytes | `int \| None` |
| `max_chunk_size` | `10485760` | Max PATCH body size in bytes | `int \| None` |
| `max_metadata_size` | `4096` | Max `Upload-Metadata` header size in bytes | `int` |
| `trusted_proxies` | `None` | IPs or CIDR ranges whose `X-Forwarded-Proto` header is trusted for scheme resolution | `list[str] \| None` |

### `FilesystemStorage`

| Parameter | Default | Description | Type |
|---|---|---|---|
| `directory` | required | Upload staging directory | `pathlib.Path \| str` |
| `directory_mode` | `0o755` | Mode for directory creation | `int` |
| `fsync` | `True` | `fsync` data to disk before updating offset in meta file. Disable for higher throughput at the cost of durability | `bool` |

### `Janitor`

| Parameter | Default | Description | Type |
|---|---|---|---|
| `storage` | required | Same `Storage` instance as `TUSApp` | `tussi.storage.Storage` |
| `completed_dir` | required | Same `completed_dir` as `TUSApp` | `pathlib.Path` |
| `stale_upload_age` | `86400` | Seconds of inactivity before an incomplete upload is deleted | `float` |
| `completed_file_age` | `604800` | Seconds before a finalized file is deleted from `completed_dir` | `float` |

## Storage layout

```
uploads/          # Storage directory for in-progress uploads
  {uuid}          # pre-allocated buffer file (posix_fallocate)
  {uuid}.meta     # upload record (JSON)

completed/        # completed_dir for finalized uploads
  {uuid}          # completed file (moved atomically from uploads/)
  {uuid}.meta     # upload record with finished_at and duration (JSON)
```

## Demo server

The `tussi-server` command starts an interactive server with prompts for upload and destination directories. It can be used for testing.

> The optional dependency `cli` is required in order for the `tussi-server` command to be registered.

```bash
pip install 'tussi[cli]'
tussi-server
```

## Demo upload

The `tussi-upload` tool provides a CLI for uploading a file to a TUS 1.0.0 instance. Call it with `--help` for additional params e.g. a file to upload. If no file is submitted, it just creates some random data, stores it in a temp file and uploads it then.

> The optional dependency `cli` is required in order for the `tussi-upload` command to be registered.

```bash
pip install 'tussi[cli]'
tussi-upload
```

## Protocol

Implements TUS 1.0.0 core + `creation` extension.

| Method | Path | Description |
|---|---|---|
| OPTIONS | `/files/` | Server capabilities |
| POST | `/files/` | Create upload |
| HEAD | `/files/{id}` | Query offset |
| PATCH | `/files/{id}` | Send chunk |

## Release

```bash
# 1. bump version in pyproject.toml
# 2. commit and tag
git commit -am 'release: x.y.z'
git tag vx.y.z
git push && git push --tags
# CI runs tests, builds, and publishes to PyPI automatically
```

## License

MIT
