from __future__ import annotations

import base64
import sys
import wsgiref.util
from io import BytesIO
from typing import Any

from radicale import app, config

from radicale_webdav_storage.storage import Storage

from .fakes import MemoryWebDAVClient
from .test_storage import EVENT


def make_application() -> tuple[app.Application, MemoryWebDAVClient]:
    configuration = config.Configuration(config.DEFAULT_CONFIG_SCHEMA)
    configuration.update(
        {
            "auth": {"type": "none"},
            "storage": {
                "type": "radicale_webdav_storage",
                "webdav_url": "https://dav.example/root/",
                "strict_preconditions": "True",
            },
        },
        "test",
    )
    application = app.Application(configuration)
    assert isinstance(application._storage, Storage)
    application._storage._client.close()
    fake = MemoryWebDAVClient()
    application._storage._client = fake  # type: ignore[assignment]
    return application, fake


def request(
    application: app.Application,
    method: str,
    path: str,
    data: str | None = None,
    *,
    username: str = "alice",
    content_type: str | None = None,
    if_match: str | None = None,
    if_none_match: str | None = None,
) -> tuple[int, dict[str, str], str]:
    environ: dict[str, Any] = {}
    wsgiref.util.setup_testing_defaults(environ)
    environ["REQUEST_METHOD"] = method
    environ["PATH_INFO"] = path
    token = base64.b64encode(f"{username}:password".encode()).decode("ascii")
    environ["HTTP_AUTHORIZATION"] = f"Basic {token}"
    environ["wsgi.errors"] = sys.stderr
    if content_type:
        environ["CONTENT_TYPE"] = content_type
    if if_match:
        environ["HTTP_IF_MATCH"] = if_match
    if if_none_match:
        environ["HTTP_IF_NONE_MATCH"] = if_none_match
    if data is not None:
        encoded = data.encode("utf-8")
        environ["wsgi.input"] = BytesIO(encoded)
        environ["CONTENT_LENGTH"] = str(len(encoded))

    status_code: int | None = None
    response_headers: dict[str, str] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        nonlocal status_code, response_headers
        status_code = int(status.split()[0])
        response_headers = dict(headers)

    chunks = list(application(environ, start_response))
    assert status_code is not None
    return status_code, response_headers, b"".join(chunks).decode("utf-8")


def test_radicale_crud_and_preconditions_are_enforced_above_webdav() -> None:
    application, fake = make_application()

    status, _, _ = request(application, "MKCALENDAR", "/alice/calendar/")
    assert status == 201

    status, headers, _ = request(
        application,
        "PUT",
        "/alice/calendar/event.ics",
        EVENT,
        content_type="text/calendar; charset=utf-8",
        if_none_match="*",
    )
    assert status == 201
    assert "ETag" in headers
    original = fake.files["alice/calendar/event.ics"]

    status, _, _ = request(
        application,
        "PUT",
        "/alice/calendar/event.ics",
        EVENT.replace("SUMMARY:Test event", "SUMMARY:Stale overwrite"),
        content_type="text/calendar; charset=utf-8",
        if_match='"definitely-stale"',
    )
    assert status == 412
    assert fake.files["alice/calendar/event.ics"] == original

    status, _, body = request(
        application,
        "REPORT",
        "/alice/calendar/",
        """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:D="DAV:">
  <D:prop><D:getetag/></D:prop>
</C:calendar-query>""",
        content_type="application/xml; charset=utf-8",
    )
    assert status == 207
    assert "/alice/calendar/event.ics" in body
