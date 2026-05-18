from sys import platform

if platform != 'linux':
    raise ImportError(
        'tussi is Linux-only. It relies on posix_fallocate and fcntl.flock '
        'which are not available on other platforms'
    )

from .app import TUSApp
from .events import (
    TUSEvent,
    UploadCompletedEvent,
    UploadCreatedEvent,
    UploadFailedEvent,
    UploadProgressEvent
)
from .janitor import Janitor
from .models import (
    CompletedUpload,
    UploadInfo,
    UploadRecord
)
from .storage import (
    FilesystemStorage,
    InsufficientStorageException,
    OffsetMismatchException,
    Storage,
    StorageException,
    UploadAlreadyExistsException,
    UploadNotFoundException,
    UploadSizeExceededException
)

__all__ = [
    'CompletedUpload',
    'FilesystemStorage',
    'InsufficientStorageException',
    'Janitor',
    'OffsetMismatchException',
    'Storage',
    'StorageException',
    'TUSApp',
    'TUSEvent',
    'UploadAlreadyExistsException',
    'UploadCompletedEvent',
    'UploadCreatedEvent',
    'UploadFailedEvent',
    'UploadInfo',
    'UploadRecord',
    'UploadNotFoundException',
    'UploadProgressEvent',
    'UploadSizeExceededException'
]
