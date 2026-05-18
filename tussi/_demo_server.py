'''
Demo TUS server. Run with:

    python -m tussi
    tussi-server          # after pip install tussi[cli]

Interactive setup when run in a terminal; pass flags directly to skip.
This file shows how to integrate tussi with FastAPI.
Copy build_app() into your own project and wire up your own auth.
'''
import argparse
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.responses import Response

from tussi import Janitor, TUSApp
from tussi.storage import FilesystemStorage

try:
    import uvicorn  # type: ignore[import]
    from fastapi import Depends, FastAPI, Request
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import IntPrompt, Prompt
except ImportError as exc:
    raise ImportError(
        'fastapi, rich, and uvicorn are required. '
        'Install via: pip install "tussi[cli]"'
    ) from exc


_log = logging.getLogger(__name__)
_console = Console()


async def _janitor_worker(janitor: Janitor, interval: int = 3600) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await janitor.run()
        except Exception:  # pylint: disable=broad-exception-caught
            _log.exception('janitor failed')


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    suffixes = ''.join(path.suffixes)
    stem = path.name[: -len(suffixes)] if suffixes else path.name
    n = 1
    while True:
        candidate = path.parent / f'{stem}_{n}{suffixes}'
        if not candidate.exists():
            return candidate
        n += 1


async def _file_worker(tus: TUSApp, dest: Path, worker_id: int = 0) -> None:
    _log.info('file worker %d started', worker_id)
    while True:
        try:
            async with tus.wait_for_file(timeout=3600) as upload:
                raw = upload.info.metadata.get('filename', upload.name)
                filename = Path(raw).name or upload.name
                dest_path = _unique_path(dest / filename)
                _log.info('worker %d claimed %s', worker_id, upload.name)
                if filename != upload.name:
                    _log.info(
                        'worker %d using filename from metadata: %s',
                        worker_id, filename,
                    )
                if dest_path.name != filename:
                    _log.info(
                        'worker %d renamed to avoid collision: %s',
                        worker_id, dest_path.name,
                    )
                upload.save(dest_path)
                upload.save_meta(
                    dest_path.parent / f'{dest_path.name}.meta'
                )
                _log.info('worker %d saved [dest=%s]', worker_id, dest_path)
        except TimeoutError:
            pass
        except Exception:  # pylint: disable=broad-exception-caught
            _log.exception('file worker %d crashed, restarting', worker_id)


def build_app(
    tus: TUSApp,
    janitor: Janitor,
    dest: Path,
    workers: int = 1,
) -> FastAPI:
    '''
    Wire tussi into a FastAPI application.

    Replace require_auth with your own Depends() for production use.
    '''

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = [
            asyncio.create_task(_file_worker(tus, dest, worker_id=i))
            for i in range(workers)
        ] + [
            asyncio.create_task(_janitor_worker(janitor)),
        ]
        yield
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(lifespan=lifespan)

    async def require_auth() -> None:
        pass  # replace with real auth

    @app.api_route(
        '/files/{path:path}',
        methods=['HEAD', 'PATCH', 'POST', 'OPTIONS'],
        include_in_schema=False,
    )
    async def tus_handler(
        request: Request,
        _: None = Depends(require_auth),
    ) -> Response:
        return await tus.get_response(request.scope, request.receive)

    return app


def _prompt_config(args: argparse.Namespace) -> argparse.Namespace:
    _console.print()
    _console.print(Panel(
        '[bold]Tussi[/bold] / Resumable Upload Server / TUS 1.0.0',
        border_style='cyan',
        padding=(0, 2),
    ))
    _console.print()
    args.upload_dir = Path(Prompt.ask(
        '1/4 Upload dir',
        default=str(args.upload_dir),
        console=_console,
    ))
    args.dest_dir = Path(Prompt.ask(
        '2/4 Dest dir  ',
        default=str(args.dest_dir),
        console=_console,
    ))
    args.host = Prompt.ask(
        '3/4 Host      ',
        default=args.host,
        console=_console,
    )
    args.port = IntPrompt.ask(
        '4/4 Port      ',
        default=args.port,
        console=_console,
    )
    _console.print()
    return args


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )

    parser = argparse.ArgumentParser(
        description='Demo TUS 1.0.0 upload server'
    )
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument(
        '--upload-dir',
        type=Path,
        default=Path('./uploads'),
        metavar='DIR',
    )
    parser.add_argument(
        '--completed-dir',
        type=Path,
        default=None,
        metavar='DIR',
        help=(
            'Staging dir for completed uploads '
            '(default: <upload-dir>/.completed)'
        ),
    )
    parser.add_argument(
        '--dest-dir',
        type=Path,
        default=Path('./dest'),
        metavar='DIR',
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=1,
        metavar='N',
        help='Number of concurrent file workers (default: 1)',
    )
    parser.add_argument(
        '--access-log',
        action='store_true',
        default=False,
        help='Enable uvicorn access log (default: off)',
    )
    args = parser.parse_args()

    if sys.stdin.isatty():
        args = _prompt_config(args)

    if args.completed_dir is None:
        args.completed_dir = args.upload_dir / '.completed'

    args.upload_dir.mkdir(parents=True, exist_ok=True)
    args.completed_dir.mkdir(parents=True, exist_ok=True)
    args.dest_dir.mkdir(parents=True, exist_ok=True)

    storage = FilesystemStorage(directory=args.upload_dir)
    tus = TUSApp(
        storage=storage,
        completed_dir=args.completed_dir,
    )
    janitor = Janitor(
        storage=storage,
        completed_dir=args.completed_dir,
    )

    url = f'http://{args.host}:{args.port}/files/'
    _console.print(Panel(
        f'[cyan]{url}[/cyan]\n\n'
        f'[dim]Uploads: {args.upload_dir}\n'
        f'Dest:    {args.dest_dir}[/dim]',
        border_style='green',
        padding=(1, 2),
    ))
    _console.print()

    uvicorn.run(
        build_app(tus, janitor, args.dest_dir, workers=args.workers),
        host=args.host,
        port=args.port,
        access_log=args.access_log,
        server_header=False,
    )
