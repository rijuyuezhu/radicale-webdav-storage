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


def test_strong_etag_cache_survives_ttl_and_only_refreshes_changed_items() -> None:
    files = {"root/one.ics": b"first", "root/two.ics": b"other"}
    calls: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/")
        calls[request.method + ":" + path] += 1
        if request.method == "GET":
            return httpx.Response(200, content=files[path])
        if request.method == "PUT":
            files[path] = request.content
            return httpx.Response(201)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WebDAVClient("https://dav.example/root/", client=http)
        assert client.get("one.ics", etag='"a"') == "first"
        assert client.get("two.ics", etag='"b"') == "other"
        # Expire the unversioned TTL, but the strong ETags still match.
        client._cache.clear()
        assert client.get("one.ics", etag='"a"') == "first"
        assert calls["GET:root/one.ics"] == 1

        # A changed ETag must not reuse even a fresh unversioned GET cache.
        files["root/one.ics"] = b"external edit"
        assert client.get("one.ics", etag='"a2"') == "external edit"
        assert calls["GET:root/one.ics"] == 2

        client.put("one.ics", b"local edit", content_type="text/calendar")
        assert client.get("one.ics", etag='"a3"') == "local edit"
        assert client.get("two.ics", etag='"b"') == "other"
        assert calls["GET:root/one.ics"] == 3
        assert calls["GET:root/two.ics"] == 1


def test_weak_etags_use_short_ttl_only() -> None:
    calls: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        calls["GET"] += 1
        return httpx.Response(200, content=f"version {calls['GET']}".encode())

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WebDAVClient("https://dav.example/root/", client=http)
        assert client.get("test.ics", etag='W/"weak"') == "version 1"
        assert client.get("test.ics", etag='W/"weak"') == "version 1"
        client._cache.clear()
        assert client.get("test.ics", etag='W/"weak"') == "version 2"


def test_prefetch_limits_concurrency_and_skips_unchanged_etags() -> None:
    import threading
    import time

    from radicale_webdav_storage.webdav import WebDAVEntry

    calls: Counter[str] = Counter()
    active = 0
    peak = 0
    lock = threading.Lock()

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls[request.url.path] += 1
        time.sleep(0.02)
        with lock:
            active -= 1
        return httpx.Response(200, content=request.url.path.encode())

    entries = [
        WebDAVEntry(path=f"file-{i}.ics", is_collection=False, etag=f'"{i}"') for i in range(12)
    ]
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WebDAVClient("https://dav.example/root/", client=http)
        client.prefetch(entries)
        assert 1 < peak <= 4
        assert sum(calls.values()) == 12

        client._cache.clear()
        client.prefetch(entries)
        assert sum(calls.values()) == 12
        assert client.get_bytes("file-0.ics", etag='"0"') == b"/root/file-0.ics"
        assert sum(calls.values()) == 12

        changed = [WebDAVEntry("file-0.ics", False, etag='"changed"'), *entries[1:]]
        client.prefetch(changed)
        assert sum(calls.values()) == 13


def test_versioned_content_cache_has_bounded_entry_count() -> None:
    calls: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        calls["GET"] += 1
        return httpx.Response(200, content=b"tiny")

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = WebDAVClient("https://dav.example/root/", client=http)
        for n in range(1030):
            assert client.get_bytes(f"event-{n}.ics", etag=f'"{n}"') == b"tiny"
        assert len(client._versioned_content) == 1024
        assert client._versioned_bytes == 1024 * 4
        # The least recently used entry was evicted.
        assert client.get_bytes("event-0.ics", etag='"0"') == b"tiny"
        assert calls["GET"] == 1031
