from abc import (
    ABC,
    abstractmethod
)
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from fcntl import (
    flock,
    LOCK_EX,
    LOCK_SH,
    LOCK_UN
)
from logging import getLogger
from errno import ENOSPC
from os import (
    access,
    fsync as os_fsync,
    posix_fallocate,
    walk,
    R_OK,
    W_OK,
    X_OK
)
from pathlib import Path
from shutil import disk_usage
from time import time
from typing import (
    BinaryIO,
    Generator
)

from anyio import to_thread

from tussi.models import (
    UploadInfo,
    UploadMeta
)


_log = getLogger(__name__)


class StorageException(Exception):
    '''
        All methods of the Storage interface should raise this exception
        in case any error occurs.

        The TUSApp catches this and handles it as an expected storage
        layer exception. It also catches Exception but that indicates a
        missing catch within the storage layer.
    '''


class InsufficientStorageException(StorageException):
    '''
        This is thrown if a create operation fails due to insufficient
        remaining disk space "below" the upload dir
    '''


class UploadNotFoundException(StorageException):
    '''
        Raised when an operation targets an upload_id that does not exist.
        Callers should map this to a 404 response.
    '''


class UploadAlreadyExistsException(StorageException):
    '''
        Raised by create() when the upload_id already exists. The caller
        should retry with a freshly generated ID.
    '''


class UploadSizeExceededException(StorageException):
    '''
        Raised by write() when offset + len(data) would exceed the upload's
        declared Upload-Length. Callers should map this to a 413 response.
    '''


class OffsetMismatchException(StorageException):
    '''
        Raised by write() when the expected offset does not match the
        actual current offset of the upload. Carries the actual offset
        so the caller can return it in a 409 response.
    '''

    def __init__(self, actual: int) -> None:
        super().__init__(f'offset mismatch: actual={actual}')
        self.actual = actual


class Storage(ABC):

    @abstractmethod
    async def create(
        self,
        upload_id: str,
        length: int,
        metadata: dict[str, str],
    ) -> None:
        '''
            Initialize a new upload resource. Must allocate or reserve space
            for the full upload length before returning. Raises
            InsufficientStorageException if the backend cannot accommodate
            length bytes
        '''

    @abstractmethod
    async def write(
        self,
        upload_id: str,
        expected_offset: int,
        data: bytes
    ) -> UploadInfo:
        '''
            Atomically verify the current offset matches expected_offset,
            write data, and update offset/last_write under one exclusive
            lock. Returns UploadInfo reflecting the new state. Raises
            OffsetMismatchException (carrying the actual offset) if the offsets
            do not match. Raises StorageException if the upload does not exist
            or I/O fails.
        '''

    @abstractmethod
    async def finalize(self, upload_id: str, dest: Path) -> None:
        '''
            Move the completed upload to dest and its metadata to dest.parent /
            (dest.name + ".meta"). The meta file is written first; the upload
            rename is the atomic marker. If dest exists, meta is guaranteed to
            be present. dest must be on the same filesystem as the upload dir.
            Raises StorageException on failure.
        '''

    @abstractmethod
    async def info(self, upload_id: str) -> UploadInfo:
        '''
            Return the current state of an upload.
        '''

    @abstractmethod
    async def delete(self, upload_id: str) -> None:
        '''
            Permanently remove an upload and all associated metadata
        '''

    @abstractmethod
    async def free_space(self) -> int:
        '''
            Return the number of bytes available for new uploads
        '''

    @abstractmethod
    async def can_fit(self, size: int) -> bool:
        '''
            Return True if size bytes can currently be accommodated
        '''

    @abstractmethod
    async def list_uploads(self) -> list[UploadInfo]:
        '''
            Return all currently active (in-progress) uploads.
        '''

    async def purge_orphaned_metas(self) -> int:
        '''
            Remove metadata entries that have no corresponding upload data.
            Returns the number of entries removed. Default: no-op (returns 0).
            Override in backends that have a concept of orphaned metadata.
        '''
        return 0


class FilesystemStorage(Storage):
    '''
        Linux-only (!) Storage implementation backed by the local filesystem.
        Relies on fcntl.flock for locking and posix_fallocate for space
        reservation. Neither is available on Windows, and posix_fallocate
        is not supported on macOS/APFS.
    '''

    def __init__(
        self,
        directory: Path | str,
        directory_mode: int = 0o755,
        fsync: bool = True,
    ) -> None:
        self._dir = (
            Path(directory) if isinstance(directory, str)
            else directory
        )
        self._dir_mode = directory_mode
        self._fsync = fsync
        try:
            self._dir.mkdir(
                mode=directory_mode,
                parents=False,
                exist_ok=True
            )
        except Exception as exc:
            raise StorageException(
                f'Failed to ensure existence of path "{self._dir}"'
            ) from exc
        self._ensure_directory_access()

    def _ensure_directory_access(self) -> None:
        try:
            for root, _, __ in walk(self._dir):
                if not access(root, R_OK | W_OK | X_OK):
                    raise StorageException(
                        f'Failed to ensure RWX access to path '
                        f'"{root}" within storage directory '
                        f'"{self._dir}"'
                    )
        except StorageException:
            raise
        except Exception as exc:
            raise StorageException(
                f'Failed to check access to path "{self._dir}"'
            ) from exc

    def _upload_path(self, upload_id: str) -> Path:
        return self._dir / upload_id

    def _meta_path(self, upload_id: str) -> Path:
        return self._dir / f'{upload_id}.meta'

    async def free_space(self) -> int:
        try:
            return await to_thread.run_sync(
                lambda: disk_usage(self._dir).free
            )
        except Exception as exc:
            raise StorageException(
                f'Failed to determine disk usage for path "{self._dir}"'
            ) from exc

    async def create(
        self,
        upload_id: str,
        length: int,
        metadata: dict[str, str],
    ) -> None:
        available_free_space = await self.free_space()
        if available_free_space < length:
            raise InsufficientStorageException(
                f'The available free space of "{available_free_space}" '
                f'bytes is too small to hold the "{length}" bytes '
                f'required to hold this file [upload_id={upload_id};'
                f'metadata={metadata}]'
            )
        now = time()
        meta = UploadMeta(
            length=length,
            metadata=metadata,
            last_write=now,
            created_at=now
        )
        upload_path = self._upload_path(upload_id)
        meta_path = self._meta_path(upload_id)

        def _exclusive_create() -> None:
            try:
                f = open(upload_path, 'xb')
            except FileExistsError as exc:
                raise UploadAlreadyExistsException(
                    f'Upload "{upload_id}" already exists'
                ) from exc
            except Exception as exc:
                raise StorageException(
                    f'Failed to create upload file "{upload_path}"'
                ) from exc
            with f:
                flock(f, LOCK_EX)
                try:
                    if length > 0:
                        try:
                            posix_fallocate(f.fileno(), 0, length)
                        except OSError as exc:
                            if exc.errno == ENOSPC:
                                raise InsufficientStorageException(
                                    f'No space left on device for '
                                    f'upload "{upload_id}"'
                                ) from exc
                            raise StorageException(
                                f'Failed to allocate {length} bytes for '
                                f'upload "{upload_id}"'
                            ) from exc
                    meta_path.write_text(meta.model_dump_json())
                except StorageException:
                    try:
                        upload_path.unlink(missing_ok=True)
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                    raise
                finally:
                    flock(f, LOCK_UN)

        try:
            await to_thread.run_sync(_exclusive_create)
        except StorageException:
            raise
        except Exception as exc:
            raise StorageException(
                f'Failed to create upload "{upload_id}"'
            ) from exc
        _log.debug(
            'created upload [upload_id=%s length=%d]',
            upload_id,
            length
        )

    @contextmanager
    def _lock(
        self,
        upload_id: str,
        exclusive: bool = False,
        return_upload_file_handle: bool = False
    ) -> Generator[BinaryIO | None, None, None]:
        upload_path = self._upload_path(upload_id)
        try:
            f = open(upload_path, 'r+b')
        except FileNotFoundError as exc:
            raise UploadNotFoundException(
                f'Upload "{upload_id}" not found'
            ) from exc
        with f:
            flock(f, LOCK_EX if exclusive else LOCK_SH)
            try:
                yield f if return_upload_file_handle else None
            finally:
                flock(f, LOCK_UN)

    async def write(
        self,
        upload_id: str,
        expected_offset: int,
        data: bytes
    ) -> UploadInfo:
        upload_path = self._upload_path(upload_id)
        meta_path = self._meta_path(upload_id)

        def _do_write() -> UploadInfo:
            with self._lock(
                upload_id,
                exclusive=True,
                return_upload_file_handle=True
            ) as f:
                if f is None:
                    raise StorageException(
                        f'Failed to acquire file handle for '
                        f'upload "{upload_id}"'
                    )
                try:
                    meta = UploadMeta.model_validate_json(
                        meta_path.read_text()
                    )
                except StorageException:
                    raise
                except Exception as exc:
                    raise StorageException(
                        f'Failed to read meta file "{meta_path}"'
                    ) from exc
                if expected_offset != meta.offset:
                    raise OffsetMismatchException(meta.offset)
                if (
                    meta.length is not None and
                    expected_offset + len(data) > meta.length
                ):
                    raise UploadSizeExceededException(
                        f'Write of {len(data)} bytes at offset '
                        f'{expected_offset} would exceed declared '
                        f'upload length {meta.length}'
                    )
                f.seek(expected_offset)
                try:
                    f.write(data)
                    if self._fsync:
                        f.flush()
                        os_fsync(f.fileno())
                except OSError as exc:
                    if exc.errno == ENOSPC:
                        raise InsufficientStorageException(
                            f'No space left on device while writing to '
                            f'"{upload_path}"'
                        ) from exc
                    raise StorageException(
                        f'Failed to write data to "{upload_path}"'
                    ) from exc
                new_offset = expected_offset + len(data)
                meta.offset = new_offset
                meta.last_write = time()
                tmp_meta: Path | None = None
                try:
                    with NamedTemporaryFile(
                        mode='w',
                        dir=meta_path.parent,
                        suffix='.meta',
                        delete=False,
                    ) as tf:
                        tmp_meta = Path(tf.name)
                        tf.write(meta.model_dump_json())
                    tmp_meta.rename(meta_path)
                except Exception as exc:
                    if tmp_meta is not None:
                        tmp_meta.unlink(missing_ok=True)
                    raise StorageException(
                        f'Failed to update meta in "{meta_path}"'
                    ) from exc
                return UploadInfo(
                    upload_id=upload_id,
                    length=meta.length,
                    offset=new_offset,
                    metadata=meta.metadata,
                    last_write=meta.last_write,
                    created_at=meta.created_at,
                )

        try:
            return await to_thread.run_sync(_do_write)
        except StorageException:
            raise
        except Exception as exc:
            raise StorageException(
                f'Failed to write data chunk of length "{len(data)}" into '
                f'"{upload_path}" at offset "{expected_offset}"'
            ) from exc

    async def info(self, upload_id: str) -> UploadInfo:
        meta_path = self._meta_path(upload_id)

        def _do_info() -> UploadInfo:
            with self._lock(upload_id):
                try:
                    raw = meta_path.read_text()
                except Exception as exc:
                    raise StorageException(
                        f'Failed to read contents of meta file "{meta_path}"'
                    ) from exc
                try:
                    meta = UploadMeta.model_validate_json(raw)
                except Exception as exc:
                    raise StorageException(
                        f'Failed to validate contents of meta file '
                        f'"{meta_path}" against the UploadMeta model'
                    ) from exc
                return UploadInfo(
                    upload_id=upload_id,
                    length=meta.length,
                    offset=meta.offset,
                    metadata=meta.metadata,
                    last_write=meta.last_write,
                    created_at=meta.created_at,
                )

        try:
            return await to_thread.run_sync(_do_info)
        except StorageException:
            raise
        except Exception as exc:
            raise StorageException(
                f'Failed to retrieve info for upload "{upload_id}"'
            ) from exc

    async def delete(self, upload_id: str) -> None:
        upload_path = self._upload_path(upload_id)
        meta_path = self._meta_path(upload_id)

        def _do_delete() -> None:
            with self._lock(upload_id, exclusive=True):
                try:
                    upload_path.unlink(missing_ok=True)
                except Exception as exc:
                    raise StorageException(
                        f'Failed to remove upload path "{upload_path}"'
                    ) from exc
                try:
                    meta_path.unlink(missing_ok=True)
                except Exception as exc:
                    raise StorageException(
                        f'Failed to remove meta path "{meta_path}"'
                    ) from exc

        try:
            await to_thread.run_sync(_do_delete)
        except StorageException:
            raise
        except Exception as exc:
            raise StorageException(
                f'Failed to delete upload "{upload_id}"'
            ) from exc
        _log.debug('deleted upload [upload_id=%s]', upload_id)

    async def finalize(self, upload_id: str, dest: Path) -> None:
        upload_path = self._upload_path(upload_id)
        meta_path = self._meta_path(upload_id)

        dest_meta = dest.parent / f'{dest.name}.meta'

        def _do_finalize() -> None:
            with self._lock(upload_id, exclusive=True):
                tmp_meta: Path | None = None
                try:
                    with NamedTemporaryFile(
                        mode='w',
                        dir=dest.parent,
                        suffix='.meta',
                        delete=False,
                    ) as tf:
                        tmp_meta = Path(tf.name)
                        tf.write(meta_path.read_text())
                    tmp_meta.rename(dest_meta)
                except Exception as exc:
                    if tmp_meta is not None:
                        tmp_meta.unlink(missing_ok=True)
                    raise StorageException(
                        f'Failed to copy meta file to "{dest_meta}"'
                    ) from exc
                try:
                    upload_path.rename(dest)
                except Exception as exc:
                    dest_meta.unlink(missing_ok=True)
                    raise StorageException(
                        f'Failed to move "{upload_path}" to "{dest}"'
                    ) from exc
                try:
                    meta_path.unlink(missing_ok=True)
                except Exception as exc:
                    raise StorageException(
                        f'Failed to remove meta file "{meta_path}"'
                    ) from exc

        try:
            await to_thread.run_sync(_do_finalize)
        except StorageException:
            raise
        except Exception as exc:
            raise StorageException(
                f'Failed to finalize upload "{upload_id}"'
            ) from exc
        _log.debug('finalized upload [upload_id=%s dest=%s]', upload_id, dest)

    async def can_fit(self, size: int) -> bool:
        return (await self.free_space()) >= size

    async def list_uploads(self) -> list[UploadInfo]:
        def _do_list() -> list[UploadInfo]:
            results = []
            for meta_path in self._dir.glob('*.meta'):
                upload_id = meta_path.stem
                try:
                    with self._lock(upload_id):
                        try:
                            meta = UploadMeta.model_validate_json(
                                meta_path.read_text()
                            )
                        except Exception as exc:
                            raise StorageException(
                                f'Failed to read meta file "{meta_path}"'
                            ) from exc
                        results.append(UploadInfo(
                            upload_id=upload_id,
                            length=meta.length,
                            offset=meta.offset,
                            metadata=meta.metadata,
                            last_write=meta.last_write,
                            created_at=meta.created_at,
                        ))
                except UploadNotFoundException:
                    continue
                except StorageException:
                    continue
            return results

        try:
            return await to_thread.run_sync(_do_list)
        except Exception as exc:
            raise StorageException(
                f'Failed to list uploads in "{self._dir}"'
            ) from exc

    async def purge_orphaned_metas(self) -> int:
        def _do_purge() -> int:
            count = 0
            for meta_path in self._dir.glob('*.meta'):
                upload_path = self._upload_path(meta_path.stem)
                if not upload_path.exists():
                    try:
                        meta_path.unlink(missing_ok=True)
                        count += 1
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
            return count

        try:
            return await to_thread.run_sync(_do_purge)
        except Exception as exc:
            raise StorageException(
                f'Failed to purge orphaned metas in "{self._dir}"'
            ) from exc
