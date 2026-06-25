from base64 import b64decode, b64encode
from collections.abc import (
    Awaitable,
    Callable,
)
from contextlib import asynccontextmanager
from fcntl import (
    flock,
    LOCK_EX,
    LOCK_NB,
    LOCK_UN,
)
import ipaddress
from logging import getLogger
from http import HTTPMethod
from pathlib import Path
from re import match as re_match
from uuid import UUID, uuid4
from typing import (
    BinaryIO,
    Final,
    Union
)

from anyio import current_time, sleep, to_thread
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import (
    Receive,
    Scope,
    Send,
)

from tussi.enums import (
    CommonHeader,
    TUSHeader
)
from tussi.events import (
    TUSEvent,
    UploadCompletedEvent,
    UploadCreatedEvent,
    UploadFailedEvent,
    UploadProgressEvent,
)
from tussi.models import (
    CompletedUpload,
    UploadInfo,
    UploadRecord,
)
from tussi.storage import (
    InsufficientStorageException,
    OffsetMismatchException,
    Storage,
    StorageException,
    UploadAlreadyExistsException,
    UploadNotFoundException,
    UploadSizeExceededException,
)


_log = getLogger(__name__)

TUS_MAX_RAW_METADATA_SIZE: Final[int] = 4096
TUS_DEFAULT_MAX_CHUNK_SIZE: Final[int] = 10 * 1024 * 1024  # 10 MB
TUS_ACCEPTABLE_PATCH_CONTENT_TYPE: Final[str] = (
    'application/offset+octet-stream'
)

_TUS_PROTOCOL_VERSION: Final[str] = '1.0.0'
_TUS_SUPPORTED_EXTENSIONS: Final[str] = 'creation'


class TUSApp:

    def __init__(
        self,
        storage: Storage,
        completed_dir: Path | str,
        on_event: Callable[
            [TUSEvent], Awaitable[None]
        ] | None = None,
        on_create: Callable[
            [dict[str, str], dict[str, str]],
            Awaitable[dict[str, str]]
        ] | None = None,
        max_size: int | None = None,
        max_chunk_size: int | None = TUS_DEFAULT_MAX_CHUNK_SIZE,
        max_metadata_size: int = TUS_MAX_RAW_METADATA_SIZE,
        trusted_proxies: list[str] | None = None,
    ) -> None:  # pylint: disable=too-many-instance-attributes
        self._max_size = max_size
        self._max_chunk_size = max_chunk_size
        self._trusted_proxy_networks: list[
            Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
        ] = []
        for entry in (trusted_proxies or []):
            try:
                self._trusted_proxy_networks.append(
                    ipaddress.ip_network(entry, strict=False)
                )
            except ValueError as exc:
                raise ValueError(
                    f'Invalid trusted_proxies entry "{entry}": {exc}'
                ) from exc
        self._capabilities: dict[TUSHeader | CommonHeader, str] = {
            TUSHeader.TUS_EXTENSION: _TUS_SUPPORTED_EXTENSIONS,
            TUSHeader.TUS_RESUMABLE: _TUS_PROTOCOL_VERSION,
            TUSHeader.TUS_VERSION: _TUS_PROTOCOL_VERSION,
        }
        if max_size is not None:
            self._capabilities[TUSHeader.TUS_MAX_SIZE] = str(max_size)
        completed_dir = (
            Path(completed_dir) if isinstance(completed_dir, str)
            else completed_dir
        )
        try:
            completed_dir.mkdir(parents=False, exist_ok=True)
        except Exception as exc:
            raise ValueError(
                f'Failed to ensure existence of completed_dir '
                f'"{completed_dir}"'
            ) from exc
        self._completed_dir = completed_dir
        self._on_event = on_event
        self._on_create = on_create
        self._max_metadata_size = max_metadata_size
        self._methods_to_callbacks: dict[
            HTTPMethod,
            Callable[[Request, Response], Awaitable[Response]]
        ] = {
            HTTPMethod.HEAD: self._handle_head,
            HTTPMethod.PATCH: self._handle_patch,
            HTTPMethod.POST: self._handle_post,
            HTTPMethod.OPTIONS: self._handle_options
        }
        self._storage = storage

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send
    ) -> None:
        '''
            The ASGI entry point
        '''
        if scope['type'] != 'http':
            return
        response = await self.get_response(scope, receive, send)
        await response(scope, receive, send)

    def _fail_500(
        self,
        response: Response,
        msg: str,
        *args,
        critical: bool = False
    ) -> Response:
        log = (
            _log.critical if critical else
            _log.error
        )
        log(
            msg,
            *args,
            exc_info=True
        )
        response.status_code = http_status.HTTP_500_INTERNAL_SERVER_ERROR
        return response

    async def _handle_head(
        self,
        request: Request,
        response: Response
    ) -> Response:
        self._set_header(
            response=response,
            header=CommonHeader.CACHE_CONTROL,
            value='no-store'
        )
        self._set_header(response=response, header=TUSHeader.TUS_RESUMABLE)
        upload_id = self._extract_upload_id(request=request)
        if upload_id is None:
            response.status_code = http_status.HTTP_404_NOT_FOUND
            return response
        try:
            upload_info = await self._storage.info(upload_id=upload_id)
        except UploadNotFoundException:
            response.status_code = http_status.HTTP_404_NOT_FOUND
            return response
        except StorageException:
            return self._fail_500(
                response,
                'info failed [upload_id=%s]',
                upload_id,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            return self._fail_500(
                response,
                'info failed [upload_id=%s]',
                upload_id,
                critical=True,
            )
        response.status_code = http_status.HTTP_204_NO_CONTENT
        if upload_info.length is not None:
            response.headers[TUSHeader.UPLOAD_LENGTH] = str(upload_info.length)
        response.headers[TUSHeader.UPLOAD_OFFSET] = str(upload_info.offset)
        if upload_info.metadata:
            response.headers[TUSHeader.UPLOAD_METADATA] = (
                self._serialize_metadata(upload_info.metadata)
            )
        return response

    async def _handle_options(
        self,
        _: Request,
        response: Response
    ) -> Response:
        for tus_header in (
            TUSHeader.TUS_EXTENSION,
            TUSHeader.TUS_MAX_SIZE,
            TUSHeader.TUS_VERSION
        ):
            if tus_header not in self._capabilities:
                continue
            self._set_header(
                response=response,
                header=tus_header
            )
        response.status_code = http_status.HTTP_204_NO_CONTENT
        return response

    def _validate_patch_headers(
        self,
        request: Request,
        response: Response,
    ) -> tuple[int, int] | Response:
        content_type = request.headers.get(CommonHeader.CONTENT_TYPE)
        if (
            content_type is None or
            content_type.lower() != TUS_ACCEPTABLE_PATCH_CONTENT_TYPE
        ):
            response.status_code = http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            return response

        content_length_raw = request.headers.get(CommonHeader.CONTENT_LENGTH)
        if content_length_raw is None:
            response.status_code = http_status.HTTP_400_BAD_REQUEST
            return response
        try:
            content_length = int(content_length_raw)
            if content_length < 0:
                raise ValueError
        except ValueError:
            response.status_code = http_status.HTTP_400_BAD_REQUEST
            return response
        if (
            self._max_chunk_size is not None and
            content_length > self._max_chunk_size
        ):
            response.status_code = http_status.HTTP_413_CONTENT_TOO_LARGE
            return response

        try:
            offset = int(request.headers[TUSHeader.UPLOAD_OFFSET])
            if offset < 0:
                raise ValueError
        except (KeyError, ValueError):
            response.status_code = http_status.HTTP_400_BAD_REQUEST
            return response

        return content_length, offset

    async def _do_finalize(
        self,
        upload_id: str,
        upload_info: UploadInfo,
        response: Response,
    ) -> Response:
        if (
            upload_info.length is None or
            upload_info.offset != upload_info.length
        ):
            return response
        try:
            await self._storage.finalize(
                upload_id,
                self._completed_dir / upload_id,
            )
        except StorageException as exc:
            await self._emit(
                UploadFailedEvent(
                    upload_id=upload_id,
                    error=exc,
                )
            )
            return self._fail_500(
                response,
                'finalize failed [upload_id=%s]',
                upload_id,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            await self._emit(
                UploadFailedEvent(
                    upload_id=upload_id,
                    error=exc,
                )
            )
            return self._fail_500(
                response,
                'finalize failed [upload_id=%s]',
                upload_id,
                critical=True,
            )
        _log.info('upload completed [upload_id=%s]', upload_id)
        await self._emit(UploadCompletedEvent(upload_info=upload_info))
        return response

    async def _handle_patch(
        self,
        request: Request,
        response: Response
    ) -> Response:
        self._set_header(response=response, header=TUSHeader.TUS_RESUMABLE)
        upload_id = self._extract_upload_id(request=request)
        if upload_id is None:
            response.status_code = http_status.HTTP_400_BAD_REQUEST
            return response

        validated = self._validate_patch_headers(request, response)
        if isinstance(validated, Response):
            return validated
        _, offset = validated

        # The _validate_patch_headers method does check whether
        # Content-Length is equal to or below the max chunk size.
        # The client could still forge a request with a spoofed
        # Content-Length header and send a huge body. The streaming
        # approach mititgates this issue.
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if (
                self._max_chunk_size is not None
                and len(data) > self._max_chunk_size
            ):
                response.status_code = http_status.HTTP_413_CONTENT_TOO_LARGE
                return response

        try:
            upload_info = await self._storage.write(upload_id, offset, data)
        except OffsetMismatchException as exc:
            response.status_code = http_status.HTTP_409_CONFLICT
            self._set_header(
                response,
                TUSHeader.UPLOAD_OFFSET,
                str(exc.actual),
            )
            return response
        except UploadSizeExceededException:
            response.status_code = http_status.HTTP_413_CONTENT_TOO_LARGE
            return response
        except InsufficientStorageException as exc:
            _log.warning(
                'insufficient storage during write [upload_id=%s]',
                upload_id,
            )
            await self._emit(UploadFailedEvent(upload_id=upload_id, error=exc))
            response.status_code = http_status.HTTP_507_INSUFFICIENT_STORAGE
            return response
        except UploadNotFoundException as exc:
            _log.warning(
                'upload not found during write [upload_id=%s]',
                upload_id,
            )
            await self._emit(UploadFailedEvent(upload_id=upload_id, error=exc))
            response.status_code = http_status.HTTP_404_NOT_FOUND
            return response
        except StorageException as exc:
            await self._emit(UploadFailedEvent(upload_id=upload_id, error=exc))
            return self._fail_500(
                response,
                'write failed [upload_id=%s]',
                upload_id,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            await self._emit(UploadFailedEvent(upload_id=upload_id, error=exc))
            return self._fail_500(
                response,
                'write failed [upload_id=%s]',
                upload_id,
                critical=True,
            )

        _log.debug(
            'patch [upload_id=%s offset=%d/%s]',
            upload_id,
            upload_info.offset,
            upload_info.length,
        )
        await self._emit(
            UploadProgressEvent(
                upload_info=upload_info,
                bytes_written=len(data),
            )
        )
        self._set_header(
            response,
            TUSHeader.UPLOAD_OFFSET,
            str(upload_info.offset),
        )
        response.status_code = http_status.HTTP_204_NO_CONTENT
        return await self._do_finalize(upload_id, upload_info, response)

    def _validate_post_headers(
        self,
        request: Request,
        response: Response,
    ) -> tuple[int, dict[str, str]] | Response:
        upload_length_raw = request.headers.get(TUSHeader.UPLOAD_LENGTH)
        if upload_length_raw is None:
            response.status_code = http_status.HTTP_400_BAD_REQUEST
            return response
        try:
            upload_length = int(upload_length_raw)
            if upload_length < 0:
                raise ValueError
        except ValueError:
            response.status_code = http_status.HTTP_400_BAD_REQUEST
            return response
        if self._max_size is not None and upload_length > self._max_size:
            response.status_code = http_status.HTTP_413_CONTENT_TOO_LARGE
            return response

        raw_metadata = request.headers.get(TUSHeader.UPLOAD_METADATA, '')
        if len(raw_metadata) > self._max_metadata_size:
            response.status_code = http_status.HTTP_400_BAD_REQUEST
            return response
        metadata = self._parse_metadata(raw_metadata)
        filename = metadata.get('filename')
        if filename is not None and not re_match(r'^[\x20-\x7E]+$', filename):
            response.status_code = http_status.HTTP_400_BAD_REQUEST
            return response

        return upload_length, metadata

    async def _create_upload(
        self,
        upload_length: int,
        metadata: dict[str, str],
        server_metadata: dict[str, str],
        response: Response,
    ) -> str | Response:
        try:
            if not await self._storage.can_fit(size=upload_length):
                response.status_code = (
                    http_status.HTTP_507_INSUFFICIENT_STORAGE
                )
                return response
        except StorageException:
            return self._fail_500(
                response,
                'can_fit failed [length=%s]',
                upload_length,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            return self._fail_500(
                response,
                'can_fit failed [length=%s]',
                upload_length,
                critical=True,
            )

        while True:
            upload_id = str(uuid4())
            dest = self._completed_dir / upload_id
            if await to_thread.run_sync(dest.exists):
                continue
            try:
                await self._storage.create(
                    upload_id=upload_id,
                    length=upload_length,
                    metadata=metadata,
                    server_metadata=server_metadata,
                )
                return upload_id
            except UploadAlreadyExistsException:
                continue
            except InsufficientStorageException:
                response.status_code = (
                    http_status.HTTP_507_INSUFFICIENT_STORAGE
                )
                return response
            except StorageException:
                return self._fail_500(
                    response,
                    'create failed [upload_id=%s]',
                    upload_id,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                return self._fail_500(
                    response,
                    'create failed [upload_id=%s]',
                    upload_id,
                    critical=True,
                )

    async def _handle_post(
        self,
        request: Request,
        response: Response,
    ) -> Response:
        self._set_header(response=response, header=TUSHeader.TUS_RESUMABLE)

        validated = self._validate_post_headers(request, response)
        if isinstance(validated, Response):
            return validated
        upload_length, metadata = validated

        if self._on_create is not None:
            server_metadata = await self._on_create(
                dict(request.headers),
                metadata,
            )
        else:
            server_metadata = {}

        result = await self._create_upload(
            upload_length,
            metadata,
            server_metadata,
            response,
        )
        if isinstance(result, Response):
            return result
        upload_id = result

        _log.info(
            'upload created [upload_id=%s length=%d]',
            upload_id,
            upload_length,
        )
        await self._emit(
            UploadCreatedEvent(
                upload_id=upload_id,
                length=upload_length,
                metadata=metadata,
            )
        )
        response.headers[CommonHeader.LOCATION] = (
            self._request_url_to_location_header(
                request=request,
                upload_id=upload_id,
            )
        )
        response.status_code = http_status.HTTP_201_CREATED
        return response

    async def _emit(self, event: TUSEvent) -> None:
        if self._on_event is not None:
            await self._on_event(event)

    def _parse_metadata(self, raw: str) -> dict[str, str]:
        result: dict[str, str] = {}
        if not raw:
            return result
        for pair in raw.split(','):
            parts = pair.strip().split(' ', 1)
            if len(parts) != 2:
                continue
            key, encoded = parts
            if not re_match(r'^[a-zA-Z0-9_\-]+$', key):
                continue
            try:
                result[key] = b64decode(encoded, validate=True).decode()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        return result

    def _serialize_metadata(self, metadata: dict[str, str]) -> str:
        return ', '.join(
            f'{key} {b64encode(value.encode()).decode()}'
            for key, value in metadata.items()
        )

    def _extract_upload_id(self, request: Request) -> str | None:
        parts = request.url.path.strip('/').split('/')
        if not parts or not parts[-1]:
            return None
        try:
            return str(UUID(parts[-1]))
        except ValueError:
            return None

    def _is_trusted_proxy(self, host: str) -> bool:
        '''
            Returns True if *host* matches any entry in trusted_proxies
        '''
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(addr in net for net in self._trusted_proxy_networks)

    def _resolve_scheme(self, request: Request) -> str:
        '''
            Returns the effective scheme for building Location headers.

            Uses X-Forwarded-Proto only when the connecting client is in the
            trusted_proxies list. Falls back to the raw request scheme so
            that TUSApp works correctly without any proxy configuration.
        '''
        client = request.client
        if (
            client is not None
            and self._trusted_proxy_networks
            and self._is_trusted_proxy(client.host)
        ):
            forwarded = request.headers.get('x-forwarded-proto', '').strip()
            if forwarded:
                return forwarded.split(',')[0].strip().lower()
        return request.url.scheme

    def _request_url_to_location_header(
        self,
        request: Request,
        upload_id: str
    ) -> str:
        scheme = self._resolve_scheme(request)
        url = request.url
        return f'{scheme}://{url.netloc}{url.path.rstrip("/")}/{upload_id}'

    def _set_header(
        self,
        response: Response,
        header: TUSHeader | CommonHeader,
        value: str | None = None
    ) -> None:
        response.headers[header] = (
            self._capabilities[header] if value is None else value
        )

    async def get_response(
        self,
        scope: Scope,
        receive: Receive,
        send: Send | None = None,
    ) -> Response:
        request = Request(scope, receive, send)  # type: ignore[arg-type]
        response = Response()
        response.body = b''
        response.status_code = http_status.HTTP_405_METHOD_NOT_ALLOWED

        try:
            method = HTTPMethod(request.method)
        except ValueError:
            return response
        method_override_header = request.headers.get(
            CommonHeader.X_HTTP_METHOD_OVERRIDE,
            None
        )
        if method_override_header is not None:
            try:
                method = HTTPMethod(method_override_header.upper())
            except ValueError:
                pass

        if method is not HTTPMethod.OPTIONS:
            client_version = request.headers.get(TUSHeader.TUS_RESUMABLE)
            if client_version not in (
                self._capabilities[TUSHeader.TUS_VERSION].split(',')
            ):
                self._set_header(response, TUSHeader.TUS_RESUMABLE)
                self._set_header(response, TUSHeader.TUS_VERSION)
                response.status_code = http_status.HTTP_412_PRECONDITION_FAILED
                return response

        if method not in self._methods_to_callbacks:
            self._set_header(response=response, header=TUSHeader.TUS_RESUMABLE)
            return response

        response = await self._methods_to_callbacks[method](
            request,
            response
        )
        return response

    @asynccontextmanager
    async def wait_for_file(
        self,
        timeout: float = 3600,
        poll_interval: float = 1.0,
    ):
        '''
            Async context manager that polls completed_dir until any unclaimed
            upload file appears, claims it with an exclusive non-blocking lock,
            and yields a CompletedUpload. Safe to call from multiple workers
            concurrently. Each worker claims exactly one file. On exit, the
            upload file and its .meta are removed and the lock is released.
            Raises TimeoutError if timeout elapses with no file claimable.
        '''
        deadline = current_time() + timeout

        def _try_claim() -> tuple[Path, BinaryIO, UploadRecord] | None:
            try:
                entries = list(self._completed_dir.iterdir())
            except OSError as exc:
                _log.error(
                    'failed to scan completed_dir [path=%s]: %s',
                    self._completed_dir, exc
                )
                return None
            for candidate in entries:
                if not candidate.is_file() or candidate.suffix == '.meta':
                    continue
                try:
                    f = open(candidate, 'rb')
                except FileNotFoundError:
                    continue
                try:
                    flock(f, LOCK_EX | LOCK_NB)
                except OSError:
                    f.close()
                    continue
                meta_path = candidate.parent / f'{candidate.name}.meta'
                try:
                    record = UploadRecord.model_validate_json(
                        meta_path.read_text()
                    )
                except Exception as exc:  # noqa: E501 # pylint: disable=broad-exception-caught
                    _log.error(
                        'discarding corrupt upload [path=%s]: %s',
                        candidate, exc
                    )
                    candidate.unlink(missing_ok=True)
                    meta_path.unlink(missing_ok=True)
                    flock(f, LOCK_UN)
                    f.close()
                    continue
                return candidate, f, record
            return None

        claimed = None
        while claimed is None:
            claimed = await to_thread.run_sync(_try_claim)
            if claimed is None:
                remaining = deadline - current_time()
                if remaining <= 0:
                    raise TimeoutError(
                        f'no completed upload available within {timeout}s'
                    )
                await sleep(min(poll_interval, remaining))

        dest, fh, record = claimed
        meta_path = dest.parent / f'{dest.name}.meta'
        try:
            yield CompletedUpload(path=dest, record=record)
        finally:
            def _release() -> None:
                dest.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                flock(fh, LOCK_UN)
                fh.close()
            await to_thread.run_sync(_release)

    async def list_uploads(self) -> list[UploadInfo]:
        try:
            return await self._storage.list_uploads()
        except StorageException:
            _log.error(
                'listing uploads failed',
                exc_info=True
            )
            raise
        except Exception:  # pylint: disable=broad-exception-caught
            _log.critical(
                'listing uploads failed due to unexpected error',
                exc_info=True
            )
            raise
