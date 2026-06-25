import asyncio
import os
from base64 import b64encode
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from starlette import status as http_status

from tussi import FilesystemStorage, TUSApp
from tussi.events import (
    TUSEvent,
    UploadCompletedEvent,
    UploadCreatedEvent,
    UploadProgressEvent,
)


TUS = {'Tus-Resumable': '1.0.0'}
PATCH_CT = {'Content-Type': 'application/offset+octet-stream'}


@pytest.fixture
def _app(tmp_path):
    return TUSApp(
        storage=FilesystemStorage(directory=tmp_path / 'uploads'),
        completed_dir=tmp_path / 'completed',
    )


@pytest.fixture
async def _client(_app):
    async with AsyncClient(
        transport=ASGITransport(app=_app),
        base_url='http://test',
    ) as c:
        yield c


async def create(
    _client,
    size: int,
    metadata: dict[str, str] | None = None,
) -> str:
    headers = {**TUS, 'Upload-Length': str(size)}
    if metadata:
        headers['Upload-Metadata'] = ', '.join(
            f'{k} {b64encode(v.encode()).decode()}'
            for k, v in metadata.items()
        )
    resp = await _client.post('/files/', headers=headers)
    assert resp.status_code == http_status.HTTP_201_CREATED
    return resp.headers['Location']


class TestOptions:
    async def test_204_with_capability_headers(self, _client):
        resp = await _client.options('/files/')
        assert resp.status_code == http_status.HTTP_204_NO_CONTENT
        assert 'Tus-Version' in resp.headers
        assert 'Tus-Extension' in resp.headers

    async def test_max_size_header_present_when_configured(self, tmp_path):
        capped = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            max_size=1024 * 1024,
        )
        async with AsyncClient(
            transport=ASGITransport(app=capped),
            base_url='http://test',
        ) as c:
            resp = await c.options('/files/')
        assert resp.headers['Tus-Max-Size'] == str(1024 * 1024)

    async def test_max_size_header_absent_when_unlimited(self, _client):
        resp = await _client.options('/files/')
        assert 'Tus-Max-Size' not in resp.headers

    async def test_no_tus_resumable_on_options(self, _client):
        resp = await _client.options('/files/')
        assert 'Tus-Resumable' not in resp.headers


class TestPost:
    async def test_412_when_tus_resumable_missing(self, _client):
        resp = await _client.post('/files/', headers={'Upload-Length': '100'})
        assert resp.status_code == http_status.HTTP_412_PRECONDITION_FAILED
        assert 'Tus-Version' in resp.headers
        assert resp.headers['Tus-Resumable'] == '1.0.0'

    async def test_400_when_upload_length_missing(self, _client):
        resp = await _client.post('/files/', headers=TUS)
        assert resp.status_code == http_status.HTTP_400_BAD_REQUEST

    async def test_400_when_upload_length_negative(self, _client):
        resp = await _client.post(
            '/files/',
            headers={**TUS, 'Upload-Length': '-1'},
        )
        assert resp.status_code == http_status.HTTP_400_BAD_REQUEST

    async def test_413_when_upload_exceeds_max_size(self, tmp_path):
        small_app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            max_size=1024,
        )
        async with AsyncClient(
            transport=ASGITransport(app=small_app),
            base_url='http://test',
        ) as c:
            resp = await c.post(
                '/files/',
                headers={**TUS, 'Upload-Length': '1025'},
            )
        assert resp.status_code == http_status.HTTP_413_CONTENT_TOO_LARGE

    async def test_201_with_location(self, _client):
        resp = await _client.post(
            '/files/',
            headers={**TUS, 'Upload-Length': '1024'},
        )
        assert resp.status_code == http_status.HTTP_201_CREATED
        assert 'Location' in resp.headers
        assert resp.headers['Tus-Resumable'] == '1.0.0'

    async def test_400_when_filename_has_control_chars(self, _client):
        bad_filename = b64encode(b'file\x00name').decode()
        resp = await _client.post('/files/', headers={
            **TUS,
            'Upload-Length': '1024',
            'Upload-Metadata': f'filename {bad_filename}',
        })
        assert resp.status_code == http_status.HTTP_400_BAD_REQUEST


class TestHead:
    async def test_412_when_tus_resumable_missing(self, _client):
        resp = await _client.head(
            '/files/00000000-0000-0000-0000-000000000000'
        )
        assert resp.status_code == http_status.HTTP_412_PRECONDITION_FAILED

    async def test_404_for_unknown_upload(self, _client):
        resp = await _client.head(
            '/files/00000000-0000-0000-0000-000000000000',
            headers=TUS,
        )
        assert resp.status_code == http_status.HTTP_404_NOT_FOUND

    async def test_204_with_offset_and_length(self, _client):
        loc = await create(_client, 1024)
        resp = await _client.head(loc, headers=TUS)
        assert resp.status_code == http_status.HTTP_204_NO_CONTENT
        assert resp.headers['Upload-Offset'] == '0'
        assert resp.headers['Upload-Length'] == '1024'
        assert resp.headers['Cache-Control'] == 'no-store'

    async def test_upload_metadata_round_trip(self, _client):
        loc = await create(_client, 256, metadata={'filename': 'hello.txt'})
        resp = await _client.head(loc, headers=TUS)
        assert resp.status_code == http_status.HTTP_204_NO_CONTENT
        assert 'filename' in resp.headers.get('Upload-Metadata', '')


class TestPatch:
    async def test_415_for_wrong_content_type(self, _client):
        loc = await create(_client, 64)
        resp = await _client.patch(loc, headers={
            **TUS,
            'Content-Type': 'application/octet-stream',
            'Upload-Offset': '0',
        }, content=b'x' * 64)
        assert resp.status_code == http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE

    async def test_409_on_offset_mismatch(self, _client):
        loc = await create(_client, 64)
        resp = await _client.patch(loc, headers={
            **TUS, **PATCH_CT,
            'Upload-Offset': '32',
        }, content=b'x' * 32)
        assert resp.status_code == http_status.HTTP_409_CONFLICT
        assert resp.headers['Upload-Offset'] == '0'

    async def test_advances_offset(self, _client):
        data = b'x' * 512
        loc = await create(_client, len(data))
        resp = await _client.patch(loc, headers={
            **TUS, **PATCH_CT, 'Upload-Offset': '0',
        }, content=data)
        assert resp.status_code == http_status.HTTP_204_NO_CONTENT
        assert resp.headers['Upload-Offset'] == '512'

    async def test_404_for_unknown_upload(self, _client):
        resp = await _client.patch(
            '/files/00000000-0000-0000-0000-000000000000',
            headers={**TUS, **PATCH_CT, 'Upload-Offset': '0'},
            content=b'hello',
        )
        assert resp.status_code == http_status.HTTP_404_NOT_FOUND

    async def test_400_for_missing_upload_offset_header(self, _client):
        loc = await create(_client, 64)
        resp = await _client.patch(
            loc,
            headers={**TUS, **PATCH_CT},
            content=b'x' * 64,
        )
        assert resp.status_code == http_status.HTTP_400_BAD_REQUEST


class TestFullFlow:
    async def test_chunked_upload_completes(self, _client):
        data = os.urandom(1024)
        chunk = 256
        loc = await create(_client, len(data))

        for i in range(4):
            offset = i * chunk
            resp = await _client.patch(loc, headers={
                **TUS, **PATCH_CT, 'Upload-Offset': str(offset),
            }, content=data[offset:offset + chunk])
            assert resp.status_code == http_status.HTTP_204_NO_CONTENT
            assert resp.headers['Upload-Offset'] == str(offset + chunk)

        resp = await _client.head(loc, headers=TUS)
        assert resp.status_code == http_status.HTTP_404_NOT_FOUND  # finalized

    async def test_resume_after_partial(self, _client):
        data = os.urandom(1024)
        loc = await create(_client, len(data))

        resp = await _client.patch(loc, headers={
            **TUS, **PATCH_CT, 'Upload-Offset': '0',
        }, content=data[:512])
        assert resp.status_code == http_status.HTTP_204_NO_CONTENT

        resp = await _client.head(loc, headers=TUS)
        assert resp.headers['Upload-Offset'] == '512'

        resp = await _client.patch(loc, headers={
            **TUS, **PATCH_CT, 'Upload-Offset': '512',
        }, content=data[512:])
        assert resp.status_code == http_status.HTTP_204_NO_CONTENT

        resp = await _client.head(loc, headers=TUS)
        assert resp.status_code == http_status.HTTP_404_NOT_FOUND  # finalized

    async def test_completed_file_contains_upload_data(
        self,
        _client,
        _app,
        tmp_path,
    ):
        data = os.urandom(512)
        loc = await create(_client, len(data))
        upload_id = loc.rstrip('/').split('/')[-1]

        await _client.patch(loc, headers={
            **TUS, **PATCH_CT, 'Upload-Offset': '0',
        }, content=data)

        completed = tmp_path / 'completed' / upload_id
        assert completed.exists()
        assert completed.read_bytes() == data


async def _complete_upload(
    client: AsyncClient,
    data: bytes,
    metadata: dict[str, str] | None = None,
) -> str:
    loc = await create(client, len(data), metadata=metadata)
    await client.patch(loc, headers={
        **TUS, **PATCH_CT, 'Upload-Offset': '0',
    }, content=data)
    return loc


class TestWaitForFile:
    async def test_yields_data(self, _app, _client, tmp_path):
        data = os.urandom(256)
        await _complete_upload(_client, data)

        dest = tmp_path / 'out'
        dest.mkdir()
        async with _app.wait_for_file(timeout=5) as upload:
            saved = dest / upload.name
            upload.save(saved)
        assert saved.read_bytes() == data

    async def test_metadata_available(self, _app, _client):
        await _complete_upload(
            _client,
            b'x' * 64,
            metadata={'filename': 'hello.txt'},
        )
        async with _app.wait_for_file(timeout=5) as upload:
            assert upload.record.metadata.get('filename') == 'hello.txt'

    async def test_finished_at_and_duration_set(self, _app, _client):
        await _complete_upload(_client, b'x' * 64)
        async with _app.wait_for_file(timeout=5) as upload:
            assert upload.record.finished_at is not None
            assert isinstance(upload.record.duration, timedelta)

    async def test_timeout_when_no_uploads(self, _app):
        with pytest.raises(TimeoutError):
            async with _app.wait_for_file(timeout=0):
                pass

    async def test_cleanup_on_exit_without_save(self, _app, _client, tmp_path):
        await _complete_upload(_client, b'y' * 64)

        completed_dir = tmp_path / 'completed'
        assert any(completed_dir.iterdir())

        async with _app.wait_for_file(timeout=5):
            pass  # intentionally no save()

        assert not any(
            f for f in completed_dir.iterdir() if f.suffix != '.meta'
        )

    async def test_two_workers_claim_different_files(self, _app, _client):
        for _ in range(2):
            await _complete_upload(_client, b'z' * 64)

        names: list[str] = []

        async def worker():
            async with _app.wait_for_file(
                timeout=5,
                poll_interval=0.05,
            ) as upload:
                names.append(upload.name)

        await asyncio.gather(worker(), worker())
        assert len(names) == 2
        assert names[0] != names[1]


class TestEvents:
    @pytest.fixture
    def _events(self) -> list[TUSEvent]:
        return []

    @pytest.fixture
    def _app_with_events(self, tmp_path, _events):
        async def on_event(event: TUSEvent) -> None:
            _events.append(event)
        return TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            on_event=on_event,
        )

    @pytest.fixture
    async def _ec(self, _app_with_events):
        async with AsyncClient(
            transport=ASGITransport(app=_app_with_events),
            base_url='http://test',
        ) as c:
            yield c

    async def test_created_event_on_post(self, _ec, _events):
        await create(_ec, 64)
        assert any(isinstance(e, UploadCreatedEvent) for e in _events)

    async def test_created_event_carries_metadata(self, _ec, _events):
        await create(_ec, 64, metadata={'filename': 'test.txt'})
        created = next(
            e for e in _events if isinstance(e, UploadCreatedEvent)
        )
        assert created.metadata.get('filename') == 'test.txt'

    async def test_progress_event_on_patch(self, _ec, _events):
        loc = await create(_ec, 64)
        await _ec.patch(loc, headers={
            **TUS, **PATCH_CT, 'Upload-Offset': '0',
        }, content=b'x' * 32)
        assert any(isinstance(e, UploadProgressEvent) for e in _events)

    async def test_completed_event_on_final_patch(self, _ec, _events):
        await _complete_upload(_ec, b'x' * 64)
        assert any(isinstance(e, UploadCompletedEvent) for e in _events)


class TestMaxChunkSize:
    async def test_413_when_body_exceeds_max_chunk_size(self, tmp_path):
        app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            max_chunk_size=128,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as c:
            loc = await create(c, 512)
            resp = await c.patch(loc, headers={
                **TUS, **PATCH_CT, 'Upload-Offset': '0',
            }, content=b'x' * 256)
        assert resp.status_code == http_status.HTTP_413_CONTENT_TOO_LARGE


class TestOnCreate:
    @pytest.fixture
    def _captured(self) -> dict:
        return {}

    @pytest.fixture
    def _app_with_hook(self, tmp_path, _captured):
        async def on_create(
            headers: dict[str, str],
            metadata: dict[str, str],
        ) -> dict[str, str]:
            _captured['headers'] = headers
            _captured['metadata'] = metadata
            return {'uploaded_by': 'test-user'}
        return TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            on_create=on_create,
        )

    @pytest.fixture
    async def _hc(self, _app_with_hook):
        async with AsyncClient(
            transport=ASGITransport(app=_app_with_hook),
            base_url='http://test',
        ) as c:
            yield c

    async def test_hook_called_on_post(self, _hc, _captured):
        await create(_hc, 64)
        assert 'headers' in _captured

    async def test_hook_receives_request_headers(self, _hc, _captured):
        await create(_hc, 64)
        assert _captured['headers'].get('tus-resumable') == '1.0.0'

    async def test_hook_receives_client_metadata(self, _hc, _captured):
        await create(_hc, 64, metadata={'filename': 'data.bin'})
        assert _captured['metadata'].get('filename') == 'data.bin'

    async def test_server_metadata_persisted_in_record(
        self,
        _app_with_hook,
        _hc,
    ):
        await _complete_upload(_hc, b'x' * 64)
        async with _app_with_hook.wait_for_file(timeout=5) as upload:
            assert upload.record.server_metadata.get('uploaded_by') == 'test-user'  # noqa: E501

    async def test_client_metadata_not_in_server_metadata(
        self,
        _app_with_hook,
        _hc,
    ):
        await _complete_upload(_hc, b'x' * 64, metadata={'filename': 'f.txt'})
        async with _app_with_hook.wait_for_file(timeout=5) as upload:
            assert upload.record.metadata.get('filename') == 'f.txt'
            assert 'filename' not in upload.record.server_metadata

    async def test_no_hook_yields_empty_server_metadata(self, _app, _client):
        await _complete_upload(_client, b'x' * 64)
        async with _app.wait_for_file(timeout=5) as upload:
            assert upload.record.server_metadata == {}


class TestMetadataValidation:
    async def test_400_when_metadata_exceeds_max_size(self, tmp_path):
        long_value = b64encode(b'this-is-way-too-long').decode()
        app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            max_metadata_size=16,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
        ) as c:
            resp = await c.post('/files/', headers={
                **TUS,
                'Upload-Length': '64',
                'Upload-Metadata': f'filename {long_value}',
            })
        assert resp.status_code == http_status.HTTP_400_BAD_REQUEST

    async def test_invalid_key_silently_ignored(self, _client):
        bad_val = b64encode(b'val').decode()
        ok_val = b64encode(b'ok.txt').decode()
        resp = await _client.post('/files/', headers={
            **TUS,
            'Upload-Length': '64',
            'Upload-Metadata': (
                f'invalid key!! {bad_val}, filename {ok_val}'
            ),
        })
        assert resp.status_code == http_status.HTTP_201_CREATED
        head = await _client.head(resp.headers['Location'], headers=TUS)
        raw = head.headers.get('Upload-Metadata', '')
        assert 'filename' in raw
        assert 'invalid key!!' not in raw


class TestTrustedProxy:
    def test_invalid_entry_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError):
            TUSApp(
                storage=FilesystemStorage(
                    directory=tmp_path / 'uploads',
                ),
                completed_dir=tmp_path / 'completed',
                trusted_proxies=['not-an-ip'],
            )

    def test_valid_single_ip_accepted(self, tmp_path):
        TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            trusted_proxies=['10.0.0.1'],
        )

    def test_valid_cidr_accepted(self, tmp_path):
        TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            trusted_proxies=['192.168.0.0/24'],
        )

    async def test_forwarded_proto_ignored_without_trusted_proxies(
        self,
        tmp_path,
    ):
        app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=('127.0.0.1', 12345),
            ),
            base_url='http://test',
        ) as c:
            resp = await c.post('/files/', headers={
                **TUS,
                'Upload-Length': '64',
                'X-Forwarded-Proto': 'https',
            })
        assert resp.status_code == http_status.HTTP_201_CREATED
        assert resp.headers['Location'].startswith('http://')

    async def test_forwarded_proto_used_from_trusted_proxy(self, tmp_path):
        app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            trusted_proxies=['127.0.0.1'],
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=('127.0.0.1', 12345),
            ),
            base_url='http://test',
        ) as c:
            resp = await c.post('/files/', headers={
                **TUS,
                'Upload-Length': '64',
                'X-Forwarded-Proto': 'https',
            })
        assert resp.status_code == http_status.HTTP_201_CREATED
        assert resp.headers['Location'].startswith('https://')

    async def test_forwarded_proto_ignored_from_untrusted_client(
        self,
        tmp_path,
    ):
        app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            trusted_proxies=['10.0.0.1'],
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=('192.168.1.99', 12345),
            ),
            base_url='http://test',
        ) as c:
            resp = await c.post('/files/', headers={
                **TUS,
                'Upload-Length': '64',
                'X-Forwarded-Proto': 'https',
            })
        assert resp.status_code == http_status.HTTP_201_CREATED
        assert resp.headers['Location'].startswith('http://')

    async def test_fallback_to_raw_scheme_without_forwarded_header(
        self,
        tmp_path,
    ):
        app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            trusted_proxies=['127.0.0.1'],
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=('127.0.0.1', 12345),
            ),
            base_url='http://test',
        ) as c:
            resp = await c.post('/files/', headers={
                **TUS,
                'Upload-Length': '64',
            })
        assert resp.status_code == http_status.HTTP_201_CREATED
        assert resp.headers['Location'].startswith('http://')

    async def test_first_forwarded_proto_value_used(self, tmp_path):
        app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            trusted_proxies=['127.0.0.1'],
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=('127.0.0.1', 12345),
            ),
            base_url='http://test',
        ) as c:
            resp = await c.post('/files/', headers={
                **TUS,
                'Upload-Length': '64',
                'X-Forwarded-Proto': 'https, http',
            })
        assert resp.status_code == http_status.HTTP_201_CREATED
        assert resp.headers['Location'].startswith('https://')

    async def test_cidr_range_trusted_proxy(self, tmp_path):
        app = TUSApp(
            storage=FilesystemStorage(directory=tmp_path / 'uploads'),
            completed_dir=tmp_path / 'completed',
            trusted_proxies=['10.0.0.0/8'],
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=('10.1.2.3', 12345),
            ),
            base_url='http://test',
        ) as c:
            resp = await c.post('/files/', headers={
                **TUS,
                'Upload-Length': '64',
                'X-Forwarded-Proto': 'https',
            })
        assert resp.status_code == http_status.HTTP_201_CREATED
        assert resp.headers['Location'].startswith('https://')
