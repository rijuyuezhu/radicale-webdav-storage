from __future__ import annotations

import os
import uuid

import pytest
from radicale import app, config

from radicale_webdav_storage.storage import Storage

from .test_radicale_app import request
from .test_storage import EVENT

pytestmark = pytest.mark.integration


def test_real_radicale_round_trip() -> None:
    credentials_path = os.environ.get("RADICALE_WEBDAV_TEST_CREDENTIALS")
    if not credentials_path:
        pytest.skip("RADICALE_WEBDAV_TEST_CREDENTIALS is not set")

    configuration = config.Configuration(config.DEFAULT_CONFIG_SCHEMA)
    configuration.update(
        {
            "auth": {"type": "none"},
            "storage": {
                "type": "radicale_webdav_storage",
                "webdav_credentials_file": credentials_path,
                "strict_preconditions": "True",
            },
        },
        "integration test",
    )
    application = app.Application(configuration)
    assert isinstance(application._storage, Storage)

    username = f"radicale-webdav-test-{uuid.uuid4().hex}"
    calendar_path = f"/{username}/calendar/"
    event_path = f"{calendar_path}event.ics"

    try:
        status, _, _ = request(
            application,
            "MKCALENDAR",
            calendar_path,
            username=username,
        )
        assert status == 201

        status, headers, _ = request(
            application,
            "PUT",
            event_path,
            EVENT,
            username=username,
            content_type="text/calendar; charset=utf-8",
            if_none_match="*",
        )
        assert status == 201
        etag = headers["ETag"]

        status, _, body = request(application, "GET", event_path, username=username)
        assert status == 200
        assert "SUMMARY:Test event" in body

        status, _, _ = request(
            application,
            "PUT",
            event_path,
            EVENT.replace("SUMMARY:Test event", "SUMMARY:Updated"),
            username=username,
            content_type="text/calendar; charset=utf-8",
            if_match=etag,
        )
        assert status == 204

        status, _, body = request(application, "GET", event_path, username=username)
        assert status == 200
        assert "SUMMARY:Updated" in body
    finally:
        application._storage._client.delete(username, collection=True, missing_ok=True)
        application._storage._client.close()
