'''
tussi-upload: upload files to any TUS 1.0.0 server with resume support.

Usage:
    tussi-upload [options]
    python3 -m tussi._demo_upload [options]

Re-run with the same --progress file to resume an interrupted upload.
'''
import argparse
import os
import sys
import tempfile
from base64 import b64encode
from pathlib import Path

try:
    import requests
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.prompt import Confirm
except ImportError as exc:
    raise ImportError(
        'rich and requests are required. '
        'Install via: pip install "tussi[cli]"'
    ) from exc


TUS = {'Tus-Resumable': '1.0.0'}
console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Upload a file to a TUS 1.0.0 server'
    )
    parser.add_argument(
        '--url',
        default='http://127.0.0.1:8080/files/',
        help='Server endpoint URL (default: http://127.0.0.1:8080/files/)',
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--file',
        help='Path to an existing file to upload',
    )
    group.add_argument(
        '--size',
        type=int,
        default=1024 * 1024,
        help='Bytes of random test data to upload (default: 1 MiB)',
    )
    parser.add_argument(
        '--chunk-size',
        type=int,
        default=256 * 1024,
        help='Chunk size per request (default: 256 KiB)',
    )
    parser.add_argument(
        '--progress',
        default='tus-progress.txt',
        help='Resume state file (default: tus-progress.txt)',
    )
    args = parser.parse_args()

    if args.file:
        src = Path(args.file)
        if not src.exists():
            console.print(
                f'[red]Error:[/red] File not found: '
                f'[bold red]{src}[/bold red]',
                highlight=False,
            )
            sys.exit(1)
        cleanup = False
    else:
        Path(args.progress).unlink(missing_ok=True)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
        console.print(
            f'[green]Generating {args.size // 1024} KiB'
            f' of random data[/green]',
            highlight=False,
        )
        tmp.write(os.urandom(args.size))
        tmp.close()
        src = Path(tmp.name)
        cleanup = True

    try:
        _run(args, src)
    except KeyboardInterrupt:
        console.print('\n[yellow]Interrupted[/yellow]')
        sys.exit(1)
    except requests.ConnectionError:
        console.print(
            f'[red]Error:[/red] Could not connect to {args.url}',
            highlight=False,
        )
        sys.exit(1)
    except requests.HTTPError as exc:
        has_resp = isinstance(exc.response, requests.models.Response)
        status = exc.response.status_code if has_resp else '?'  # type: ignore
        body = exc.response.text.strip() if has_resp else ''  # type: ignore
        console.print(
            f'[red]Error:[/red] HTTP {status}: {body}',
            highlight=False,
        )
        sys.exit(1)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        console.print(f'[red]Error:[/red] {exc}', highlight=False)
        sys.exit(1)
    finally:
        if cleanup:
            src.unlink(missing_ok=True)


def _run(args: argparse.Namespace, src: Path) -> None:
    total = src.stat().st_size
    progress_path = Path(args.progress)

    with requests.Session() as session:
        location = _resolve_location(
            session=session,
            src=src,
            url=args.url,
            progress_path=progress_path,
            total=total,
        )
        _upload(
            session=session,
            src=src,
            location=location,
            chunk_size=args.chunk_size,
            total=total,
        )

    progress_path.unlink(missing_ok=True)
    console.print(
        f'[bold green]Upload complete.[/bold green] Stored '
        f'[bright_blue]{src.name}[/bright_blue] '
        f'(via [bright_blue]{location}[/bright_blue])',
        highlight=False,
    )


def _resolve_location(
    session: requests.Session,
    src: Path,
    url: str,
    progress_path: Path,
    total: int,
) -> str:
    if progress_path.exists():
        saved = progress_path.read_text(encoding='utf-8').strip()
        if Confirm.ask(
            f'Resume existing upload at [bright_blue]{saved}[/bright_blue]?'
        ):
            return saved
        progress_path.unlink(missing_ok=True)

    metadata = f'filename {b64encode(src.name.encode()).decode()}'
    resp = session.post(
        url,
        headers={
            **TUS,
            'Upload-Length': str(total),
            'Content-Length': '0',
            'Upload-Metadata': metadata,
        },
    )
    resp.raise_for_status()
    location = resp.headers['Location']
    progress_path.write_text(location, encoding='utf-8')
    console.print(
        f'[green]Upload started[/green] for [bright_blue]{src.name}'
        f'[/bright_blue] (at [bright_blue]{location}[/bright_blue])',
        highlight=False,
    )
    return location


def _upload(
    session: requests.Session,
    src: Path,
    location: str,
    chunk_size: int,
    total: int,
) -> None:
    resp = session.head(location, headers=TUS)
    resp.raise_for_status()
    offset = int(resp.headers['Upload-Offset'])

    if offset > 0:
        console.print(
            f'[bold green]Resuming[/bold green] at'
            f' {offset // 1024} / {total // 1024} KiB',
            highlight=False,
        )

    progress = Progress(
        TextColumn('{task.description}'),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    with progress:
        task = progress.add_task(src.name, total=total, completed=offset)
        with open(src, 'rb') as fh:
            fh.seek(offset)
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                resp = session.patch(
                    location,
                    headers={
                        **TUS,
                        'Content-Type': (
                            'application/offset+octet-stream'
                        ),
                        'Content-Length': str(len(chunk)),
                        'Upload-Offset': str(offset),
                    },
                    data=chunk,
                )
                resp.raise_for_status()
                offset = int(resp.headers['Upload-Offset'])
                progress.update(task, completed=offset)


if __name__ == '__main__':
    main()
