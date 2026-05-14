from logging import getLogger
from pathlib import Path
from time import time

from anyio import to_thread

from tussi.storage import Storage, StorageException


_log = getLogger(__name__)


class Janitor:
    '''
    Cleans up stale and stuck uploads. Designed to be called externally,
    e.g. as a FastAPI background task or a periodic job.

    Responsibilities:
    - Deletes uploads where offset == length but finalize never ran
      (500 on last PATCH)
    - Deletes stale incomplete uploads whose last_write is older than
      stale_upload_age
    - Removes orphaned .meta files with no corresponding upload data
    - Deletes old files from completed_dir older than completed_file_age
    '''

    def __init__(
        self,
        storage: Storage,
        completed_dir: Path,
        stale_upload_age: float = 86400,
        completed_file_age: float = 86400 * 7,
    ) -> None:
        self._storage = storage
        self._completed_dir = completed_dir
        self._stale_upload_age = stale_upload_age
        self._completed_file_age = completed_file_age

    async def run(self) -> None:
        _log.info('janitor run started')
        now = time()
        await self._cleanup_uploads(now)
        orphans = await self._storage.purge_orphaned_metas()
        if orphans:
            _log.info('purged %d orphaned meta file(s)', orphans)
        await to_thread.run_sync(lambda: self._purge_completed(now))
        _log.info('janitor run finished')

    async def _cleanup_uploads(self, now: float) -> None:
        try:
            uploads = await self._storage.list_uploads()
        except StorageException as exc:
            _log.error('failed to list uploads: %s', exc)
            return
        for upload in uploads:
            is_finalize_zombie = (
                upload.length is not None and
                upload.offset == upload.length
            )
            is_stale = now - upload.last_write > self._stale_upload_age
            if is_finalize_zombie or is_stale:
                reason = 'finalize-zombie' if is_finalize_zombie else 'stale'
                try:
                    await self._storage.delete(upload.upload_id)
                    _log.info(
                        'deleted upload [upload_id=%s reason=%s]',
                        upload.upload_id, reason
                    )
                except StorageException as exc:
                    _log.warning(
                        'failed to delete upload [upload_id=%s reason=%s]: %s',
                        upload.upload_id, reason, exc
                    )

    def _purge_completed(self, now: float) -> None:
        try:
            files = {p for p in self._completed_dir.iterdir() if p.is_file()}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _log.error(
                'failed to iterate completed_dir [path=%s]: %s',
                self._completed_dir, exc
            )
            return

        upload_files = {p for p in files if p.suffix != '.meta'}
        meta_files = {p for p in files if p.suffix == '.meta'}
        meta_stems = {p.stem for p in meta_files}
        upload_stems = {p.name for p in upload_files}

        for path in files:
            try:
                is_orphaned_meta = (
                    path.suffix == '.meta' and
                    path.stem not in upload_stems
                )
                is_orphaned_upload = (
                    path.suffix != '.meta' and
                    path.name not in meta_stems
                )
                is_old = now - path.stat().st_mtime > self._completed_file_age
                if is_orphaned_meta or is_orphaned_upload or is_old:
                    path.unlink(missing_ok=True)
                    _log.info(
                        'deleted completed file [path=%s reason=%s]',
                        path,
                        'orphaned-meta' if is_orphaned_meta
                        else 'orphaned-upload' if is_orphaned_upload
                        else 'old'
                    )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _log.warning(
                    'failed to delete completed file [path=%s]: %s', path, exc
                )
