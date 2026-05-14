# tussi

A [TUS 1.0.0](https://tus.io/protocols/resumable-upload) resumable upload server for Python. ASGI-native, filesystem storage, no framework lock-in.

**Linux only**. Tussi uses `posix_fallocate` for pre-allocation and `fcntl.flock` for safe worker coordination.

## Install

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

tus = TUSApp(
    storage=FilesystemStorage(directory=Path('./uploads')),
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

tus = TUSApp(
    storage=FilesystemStorage(directory=Path('./uploads')),
    completed_dir=Path('./completed'),
)
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
    filename = upload.meta.get('filename', upload.name)
    upload.save(Path('./dest') / filename)
    upload.save_meta(Path('./dest') / f'{filename}.meta')
```

Raises `TimeoutError` if no upload is available within `timeout` seconds.

## Metadata

Clients pass metadata via the `Upload-Metadata` header as a comma-separated list of
`key base64(value)` pairs per the TUS spec. Tussi decodes this into a `dict[str, str]`
available as `upload.meta` inside `wait_for_file`.

Constraints:

- Keys must match `[a-zA-Z0-9_-]+` (one or more characters). Pairs with invalid keys are silently ignored
- The total header size is limited by `max_metadata_size` (default `4096` bytes)
- The `filename` key, if present, must contain only printable ASCII (`0x20-0x7E`),
  otherwise the upload is rejected with `400 Bad Request`

How you use the metadata afterwards is up to your application. The snippet above uses
`filename` as the destination filename.

Tussi never uses `filename` for storage. Uploads are always stored under a UUID. Therefore path traversal via metadata is not possible.

## Event hooks

Pass `on_event` to react to upload lifecycle events:

```python
from tussi import TUSApp, TUSEvent, UploadCompletedEvent

async def on_event(event: TUSEvent) -> None:
    if isinstance(event, UploadCompletedEvent):
        print(f'upload complete: {event.upload_info.upload_id}')

tus = TUSApp(
    storage=FilesystemStorage(directory=Path('./uploads')),
    completed_dir=Path('./completed'),
    on_event=on_event,
)
```

Available events: `UploadCreatedEvent`, `UploadProgressEvent`,
`UploadCompletedEvent`, `UploadFailedEvent`.

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

## Configuration

| Parameter | Default | Description | Type
|---|---|---|---|
| `storage` | required | `Storage` instance (e.g. `FilesystemStorage`) | `tussi.storage.Storage`
| `completed_dir` | required | Directory for finalized uploads | `pathlib.Path \| str`
| `on_event` | `None` | Async callback for lifecycle events | `Callable[[TUSEvent], Awaitable[None]] \| None`
| `max_size` | `None` | Max upload size in bytes | `int \| None`
| `max_chunk_size` | `10485760` | Max PATCH body size in bytes | `int \| None`
| `max_metadata_size` | `4096` | Max `Upload-Metadata` header size in bytes | `int`

### `FilesystemStorage`

| Parameter | Default | Description | Type |
|---|---|---|---|
| `directory` | required | Upload staging directory | `pathlib.Path \| str` |
| `directory_mode` | `0o755` | Mode for directory creation | `int` |
| `fsync` | `True` | `fsync` data to disk before updating offset in meta file. Disable for higher throughput at the cost of durability | `bool` |

## Storage layout

```
uploads/          # Storage directory for in-progress uploads
  {uuid}          # pre-allocated buffer file (posix_fallocate)
  {uuid}.meta     # upload metadata (JSON)

completed/        # completed_dir for finalized uploads
  {uuid}          # completed file (moved atomically from uploads/)
  {uuid}.meta     # upload metadata (JSON)
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
git commit -am 'bump version to 0.x.y'
git tag v0.x.y
git push && git push --tags
# CI runs tests, builds, and publishes to PyPI automatically
```

## License

MIT
