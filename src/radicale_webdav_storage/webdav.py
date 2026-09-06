from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

_DAV = "DAV:"


class WebDAVError(RuntimeError):
    """Raised when the backing WebDAV server returns an unexpected response."""


@dataclass(frozen=True)
class WebDAVEntry:
    path: str
    is_collection: bool
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None


class WebDAVClient:
    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        *,
        timeout: float = 30.0,
        verify_tls: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("webdav_url must not be empty")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"invalid WebDAV URL: {base_url!r}")
        self.base_url = base_url.rstrip("/") + "/"
        self._base_path = urllib.parse.unquote(urllib.parse.urlsplit(self.base_url).path).rstrip(
            "/"
        )
        self._owns_client = client is None
        self._client = client or httpx.Client(
            auth=(username, password) if username or password else None,
            timeout=timeout,
            verify=verify_tls,
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _clean_path(path: str) -> str:
        stripped = path.strip("/")
        if not stripped:
            return ""
        parts = stripped.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"unsafe WebDAV path: {path!r}")
        return "/".join(parts)

    def _url(self, path: str, *, collection: bool = False) -> str:
        clean = self._clean_path(path)
        if not clean:
            return self.base_url
        encoded = "/".join(urllib.parse.quote(part, safe="") for part in clean.split("/"))
        url = self.base_url + encoded
        if collection:
            url += "/"
        return url

    def _relative_path_from_href(self, href: str) -> str:
        absolute = urllib.parse.urljoin(self.base_url, href)
        href_path = urllib.parse.unquote(urllib.parse.urlsplit(absolute).path).rstrip("/")
        base_path = self._base_path
        if base_path:
            if href_path == base_path:
                return ""
            prefix = base_path + "/"
            if not href_path.startswith(prefix):
                raise WebDAVError(f"PROPFIND returned href outside configured root: {href!r}")
            href_path = href_path[len(prefix) :]
        else:
            href_path = href_path.lstrip("/")
        return self._clean_path(href_path) if href_path else ""

    @staticmethod
    def _property(prop: ET.Element, name: str) -> str | None:
        element = prop.find(f"{{{_DAV}}}{name}")
        if element is None or element.text is None:
            return None
        return element.text

    def propfind(self, path: str, *, depth: int, collection: bool = True) -> list[WebDAVEntry]:
        body = b"""<?xml version=\"1.0\" encoding=\"utf-8\"?>
<D:propfind xmlns:D=\"DAV:\"><D:prop>
<D:resourcetype/><D:getetag/><D:getlastmodified/><D:getcontenttype/>
</D:prop></D:propfind>"""
        response = self._client.request(
            "PROPFIND",
            self._url(path, collection=collection),
            headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
            content=body,
        )
        if response.status_code == 404:
            return []
        if response.status_code != 207:
            raise WebDAVError(
                f"PROPFIND {path!r} failed: HTTP {response.status_code} {response.reason_phrase}"
            )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise WebDAVError(f"invalid PROPFIND XML for {path!r}: {exc}") from exc

        result: list[WebDAVEntry] = []
        for dav_response in root.findall(f"{{{_DAV}}}response"):
            href_element = dav_response.find(f"{{{_DAV}}}href")
            if href_element is None or not href_element.text:
                continue
            relative_path = self._relative_path_from_href(href_element.text)
            selected_prop: ET.Element | None = None
            for propstat in dav_response.findall(f"{{{_DAV}}}propstat"):
                status = propstat.find(f"{{{_DAV}}}status")
                if status is None or not status.text or " 200 " not in status.text:
                    continue
                selected_prop = propstat.find(f"{{{_DAV}}}prop")
                if selected_prop is not None:
                    break
            if selected_prop is None:
                continue
            resource_type = selected_prop.find(f"{{{_DAV}}}resourcetype")
            is_collection = (
                resource_type is not None
                and resource_type.find(f"{{{_DAV}}}collection") is not None
            )
            result.append(
                WebDAVEntry(
                    path=relative_path,
                    is_collection=is_collection,
                    etag=self._property(selected_prop, "getetag"),
                    last_modified=self._property(selected_prop, "getlastmodified"),
                    content_type=self._property(selected_prop, "getcontenttype"),
                )
            )
        return result

    def stat(self, path: str, *, collection: bool) -> WebDAVEntry | None:
        entries = self.propfind(path, depth=0, collection=collection)
        if not entries:
            return None
        target = self._clean_path(path)
        for entry in entries:
            if entry.path == target:
                return entry
        return entries[0]

    def list(self, path: str) -> list[WebDAVEntry]:
        clean = self._clean_path(path)
        entries = self.propfind(clean, depth=1, collection=True)
        return [entry for entry in entries if entry.path != clean]

    def get(self, path: str) -> str | None:
        response = self._client.get(self._url(path))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise WebDAVError(
                f"GET {path!r} failed: HTTP {response.status_code} {response.reason_phrase}"
            )
        return response.content.decode("utf-8")

    def get_bytes(self, path: str) -> bytes | None:
        response = self._client.get(self._url(path))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise WebDAVError(
                f"GET {path!r} failed: HTTP {response.status_code} {response.reason_phrase}"
            )
        return response.content

    def put(self, path: str, content: bytes, *, content_type: str) -> None:
        response = self._client.put(
            self._url(path),
            headers={"Content-Type": content_type},
            content=content,
        )
        if response.status_code not in {200, 201, 204}:
            raise WebDAVError(
                f"PUT {path!r} failed: HTTP {response.status_code} {response.reason_phrase}"
            )

    def mkcol(self, path: str) -> None:
        response = self._client.request("MKCOL", self._url(path, collection=True))
        if response.status_code in {200, 201, 204}:
            return
        if response.status_code == 405:
            existing = self.stat(path, collection=True)
            if existing is not None and existing.is_collection:
                return
        raise WebDAVError(
            f"MKCOL {path!r} failed: HTTP {response.status_code} {response.reason_phrase}"
        )

    def ensure_collection(self, path: str) -> None:
        clean = self._clean_path(path)
        if not clean:
            return
        current: list[str] = []
        for component in clean.split("/"):
            current.append(component)
            current_path = "/".join(current)
            entry = self.stat(current_path, collection=True)
            if entry is None:
                self.mkcol(current_path)
            elif not entry.is_collection:
                raise WebDAVError(f"expected collection at {current_path!r}")

    def delete(self, path: str, *, collection: bool = False, missing_ok: bool = True) -> None:
        response = self._client.request("DELETE", self._url(path, collection=collection))
        if response.status_code in {200, 202, 204}:
            return
        if response.status_code == 404 and missing_ok:
            return
        raise WebDAVError(
            f"DELETE {path!r} failed: HTTP {response.status_code} {response.reason_phrase}"
        )

    def move(self, source: str, destination: str) -> bool:
        response = self._client.request(
            "MOVE",
            self._url(source),
            headers={"Destination": self._url(destination), "Overwrite": "T"},
        )
        if response.status_code in {200, 201, 204}:
            return True
        if response.status_code in {405, 501}:
            return False
        raise WebDAVError(
            f"MOVE {source!r} -> {destination!r} failed: "
            f"HTTP {response.status_code} {response.reason_phrase}"
        )

    def iter_tree(self, path: str = "") -> Iterable[WebDAVEntry]:
        for entry in self.list(path):
            yield entry
            if entry.is_collection:
                yield from self.iter_tree(entry.path)
