from __future__ import annotations

from collections import Counter

import httpx
import pytest

from radicale_webdav_storage.webdav import WebDAVClient, WebDAVError


def test_cached_get_is_shared_with_get_bytes_and_invalidated_on_writes() -> None:
    files = {"root/test.ics": b"initial"}
    calls: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/")
        calls[request.method] += 1
        if request.method == "GET":
            return (
                httpx.Response(200, content=files[path]) if path in files else httpx.Response(404)
            )
        if request.method == "PUT":
            files[path] = request.content
            return httpx.Response(201)
        if request.method == "MOVE":
            destination = httpx.URL(request.headers["Destination"]).path.removeprefix("/")
            files[destination] = files.pop(path)
            return httpx.Response(201)
        if request.method == "DELETE":
            files.pop(path, None)
            return httpx.Response(204)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WebDAVClient("https://dav.example/root/", client=http)
        assert client.get("test.ics") == "initial"
        assert client.get_bytes("test.ics") == b"initial"
        assert calls["GET"] == 1

        client.put("test.ics", b"updated", content_type="text/calendar")
        assert client.get("test.ics") == "updated"
        assert calls["GET"] == 2

        assert client.move("test.ics", "moved.ics")
        assert client.get("moved.ics") == "updated"
        assert client.get("test.ics") is None
        assert calls["GET"] == 4

        client.delete("moved.ics")
        assert client.get("moved.ics") is None
        assert calls["GET"] == 5


def test_propfind_cache_is_shared_with_list_and_stat_and_expires() -> None:
    calls = Counter()
    body = b"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
<D:response>
 <D:href>/root/alice/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
 <D:status>HTTP/1.1 200 OK</D:status></D:propstat>
</D:response>
</D:multistatus>"""

    def handle(request: httpx.Request) -> httpx.Response:
        calls[request.method] += 1
        assert request.method == "PROPFIND"
        return httpx.Response(207, content=body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WebDAVClient("https://dav.example/root/", client=http)
        assert client.list("") == client.list("")
        assert calls["PROPFIND"] == 1

        assert client.stat("alice", collection=True).is_collection
        assert client.stat("alice", collection=True).is_collection
        assert calls["PROPFIND"] == 2

        # Expiration re-fetches from the durable backing store.
        for key, (_, value) in list(client._cache.items()):
            client._cache[key] = (-1, value)
        assert client.list("")
        assert calls["PROPFIND"] == 3


def test_http_failure_does_not_poison_cache() -> None:
    calls = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        calls[request.method] += 1
        return httpx.Response(503 if calls[request.method] == 1 else 200, content=b"OK")

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WebDAVClient("https://dav.example/root/", client=http)
        with pytest.raises(WebDAVError, match="HTTP 503"):
            client.get("test.ics")
        assert client.get("test.ics") == "OK"
        assert client.get("test.ics") == "OK"
        assert calls["GET"] == 2
