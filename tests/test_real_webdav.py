from __future__ import annotations

import os
import uuid

import pytest

from radicale_webdav_storage.storage import _read_credentials_file
from radicale_webdav_storage.webdav import WebDAVClient

from .test_storage import EVENT

pytestmark = pytest.mark.integration


def test_real_webdav_round_trip() -> None:
    credentials_path = os.environ.get("RADICALE_WEBDAV_TEST_CREDENTIALS")
    if not credentials_path:
        pytest.skip("RADICALE_WEBDAV_TEST_CREDENTIALS is not set")

    credentials = _read_credentials_file(credentials_path)
    client = WebDAVClient(
        credentials["URL"],
        credentials.get("USERNAME", ""),
        credentials.get("PASSWORD", ""),
    )
    collection = f".radicale-webdav-storage-test-{uuid.uuid4().hex}"
    source = f"{collection}/event.ics"
    destination = f"{collection}/moved.ics"
    payload = EVENT.encode("utf-8")

    try:
        client.mkcol(collection)
        client.put(source, payload, content_type="text/calendar; charset=utf-8")

        entries = client.list(collection)
        assert [entry.path for entry in entries] == [source]
        assert client.get_bytes(source) == payload

        moved = client.move(source, destination)
        if moved:
            assert client.get_bytes(source) is None
            assert client.get_bytes(destination) == payload
        else:
            client.put(destination, payload, content_type="text/calendar; charset=utf-8")
            client.delete(source)
            assert client.get_bytes(destination) == payload
    finally:
        client.delete(destination, missing_ok=True)
        client.delete(source, missing_ok=True)
        client.delete(collection, collection=True, missing_ok=True)
        client.close()
