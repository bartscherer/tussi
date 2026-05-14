from pathlib import Path
from shutil import move

from pydantic import BaseModel


class UploadMeta(BaseModel):
    '''
        Persisted to .meta file alongside the upload
    '''
    length: int | None
    offset: int = 0
    metadata: dict[str, str]
    last_write: float
    created_at: float


class UploadInfo(BaseModel):
    '''
        Returned by Storage.info. Includes runtime fields like offset
    '''
    upload_id: str
    length: int | None
    offset: int
    metadata: dict[str, str]
    last_write: float
    created_at: float


class CompletedUpload:
    '''
        Yielded by TUSApp.wait_for_file. Use as an async context manager.
        On exit both the upload file and its .meta are removed from
        completed_dir regardless of whether save/save_meta were called.

        save() and save_meta() each raise RuntimeError if called more than
        once.
    '''

    def __init__(self, path: Path, meta: dict[str, str]) -> None:
        self._path = path
        self._meta_path = path.parent / f'{path.name}.meta'
        self.meta = meta
        self.name = path.name
        self._file_moved = False
        self._meta_moved = False

    def save(self, dest: Path) -> None:
        if self._file_moved:
            raise RuntimeError('upload file already saved')
        move(str(self._path), dest)
        self._file_moved = True

    def save_meta(self, dest: Path) -> None:
        if self._meta_moved:
            raise RuntimeError('meta file already saved')
        move(str(self._meta_path), dest)
        self._meta_moved = True
