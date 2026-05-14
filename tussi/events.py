from dataclasses import dataclass

from tussi.models import UploadInfo


@dataclass(frozen=True)
class UploadCreatedEvent:
    upload_id: str
    length: int
    metadata: dict[str, str]


@dataclass(frozen=True)
class UploadProgressEvent:
    upload_info: UploadInfo
    bytes_written: int


@dataclass(frozen=True)
class UploadCompletedEvent:
    upload_info: UploadInfo


@dataclass(frozen=True)
class UploadFailedEvent:
    upload_id: str
    error: Exception


TUSEvent = (
    UploadCreatedEvent |
    UploadProgressEvent |
    UploadCompletedEvent |
    UploadFailedEvent
)
