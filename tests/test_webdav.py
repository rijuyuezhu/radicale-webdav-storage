from __future__ import annotations

import httpx
import pytest

from radicale_webdav_storage.webdav import WebDAVClient, WebDAVError


def test_propfind_parses_collection_and_item() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PROPFIND"
        assert request.headers["Depth"] == "1"
        assert str(request.url) == "https://dav.example/root/alice/calendar/"
        return httpx.Response(
            207,
            headers={"Content-Type": "application/xml"},
            content=b"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/root/alice/calendar/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>
      <D:getlastmodified>Sun, 06 Sep 2026 00:00:01 GMT</D:getlastmodified>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
  <D:response>
    <D:href>/root/alice/calendar/event%20one.ics</D:href>
    <D:propstat><D:prop><D:resourcetype/><D:getetag>"abc"</D:getetag>
      <D:getcontenttype>text/calendar</D:getcontenttype>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
</D:multistatus>""",
        )

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    client = WebDAVClient("https://dav.example/root/", client=http)
    entries = client.list("alice/calendar")

    assert len(entries) == 1
    assert entries[0].path == "alice/calendar/event one.ics"
    assert not entries[0].is_collection
    assert entries[0].etag == '"abc"'


def test_webdav_url_percent_encodes_each_segment() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(404)

    client = WebDAVClient(
        "https://dav.example/root/",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.get("用户/有 空格.ics") is None
    assert seen == ["/root/%E7%94%A8%E6%88%B7/%E6%9C%89%20%E7%A9%BA%E6%A0%BC.ics"]


def test_mkcol_405_is_accepted_only_when_collection_exists() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "MKCOL":
            return httpx.Response(405)
        assert request.method == "PROPFIND"
        return httpx.Response(
            207,
            content=b"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/root/alice/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
    <D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
</D:multistatus>""",
        )

    client = WebDAVClient(
        "https://dav.example/root/",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.mkcol("alice")

    assert requests == ["MKCOL", "PROPFIND"]


def test_mkcol_405_fails_when_collection_does_not_exist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "MKCOL":
            return httpx.Response(405)
        assert request.method == "PROPFIND"
        return httpx.Response(404)

    client = WebDAVClient(
        "https://dav.example/root/",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WebDAVError, match="MKCOL 'alice' failed: HTTP 405"):
        client.mkcol("alice")


def test_propfind_combines_properties_from_separate_successful_propstats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            207,
            content=b"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
 <D:response>
  <D:href>/root/alice/</D:href>
  <D:propstat>
   <D:prop><D:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</D:getlastmodified></D:prop>
   <D:status>HTTP/1.1 200 OK</D:status>
  </D:propstat>
  <D:propstat>
   <D:prop><D:getetag>"abc"</D:getetag>
    <D:resourcetype><D:collection/></D:resourcetype></D:prop>
   <D:status>HTTP/1.1 200 OK</D:status>
  </D:propstat>
  <D:propstat>
   <D:prop><D:getcontenttype>text/plain</D:getcontenttype></D:prop>
   <D:status>HTTP/1.1 404 Not Found</D:status>
  </D:propstat>
 </D:response>
</D:multistatus>""",
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        entry = WebDAVClient("https://dav.example/root/", client=http).stat(
            "alice", collection=True
        )
        assert entry is not None
        assert entry.is_collection
        assert entry.etag == '"abc"'
        assert entry.last_modified == "Mon, 01 Jan 2024 00:00:00 GMT"
        assert entry.content_type is None


def test_stat_does_not_accept_wrong_href_in_depth_zero_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            207,
            content=b"""<D:multistatus xmlns:D="DAV:">
 <D:response><D:href>/root/alice/other.ics</D:href>
  <D:propstat><D:prop><D:resourcetype/><D:getetag>"x"</D:getetag></D:prop>
  <D:status>HTTP/1.1 200 OK</D:status></D:propstat>
 </D:response>
</D:multistatus>""",
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = WebDAVClient("https://dav.example/root/", client=http)
        assert client.stat("alice/missing.ics", collection=False) is None
        assert client.stat("alice/other.ics", collection=False) is not None
