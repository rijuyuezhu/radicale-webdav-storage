from __future__ import annotations

import contextlib
import hashlib
import json
import posixpath
import shlex
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

import radicale.item as radicale_item
from radicale import config, pathutils, storage, types
from radicale.log import logger

from .webdav import WebDAVClient, WebDAVEntry

_METADATA_FILENAME = ".Radicale.props"

PLUGIN_CONFIG_SCHEMA = {
    "storage": {
        "webdav_url": {"value": "", "type": str},
        "webdav_username": {"value": "", "type": str},
        "webdav_password": {"value": "", "type": str},
        "webdav_credentials_file": {"value": "", "type": str},
        "webdav_timeout": {"value": 30.0, "type": float},
        "webdav_verify_tls": {"value": True, "type": bool},
    }
}


def _join(*parts: str) -> str:
    clean = [part.strip("/") for part in parts if part.strip("/")]
    return posixpath.join(*clean) if clean else ""


def _read_credentials_file(path: str) -> dict[str, str]:
    """Read KEY=value credentials without executing the file as shell code."""
    result: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = shlex.split(line, comments=True, posix=True)
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ValueError(f"invalid credentials line in {path!r}")
        key, value = tokens[0].split("=", 1)
        if key in {"URL", "USERNAME", "PASSWORD"}:
            result[key] = value
    return result


class Collection(storage.BaseCollection):
    def __init__(self, storage_: Storage, path: str) -> None:
        self._storage = storage_
        self._path = pathutils.strip_path(path)

    @property
    def path(self) -> str:
        return self._path

    @property
    def _metadata_path(self) -> str:
        return _join(self.path, _METADATA_FILENAME)

    def _item_href(self, entry: WebDAVEntry) -> str:
        return posixpath.basename(entry.path.rstrip("/"))

    def _is_item_entry(self, entry: WebDAVEntry) -> bool:
        if entry.is_collection:
            return False
        href = self._item_href(entry)
        if href == _METADATA_FILENAME or not pathutils.is_safe_filesystem_path_component(href):
            return False

        tag = self.tag
        if tag == "VCALENDAR":
            extension, other_extension = ".ics", ".vcf"
            media_types = {"text/calendar", "text/x-vcalendar", "application/ics"}
        elif tag == "VADDRESSBOOK":
            extension, other_extension = ".vcf", ".ics"
            media_types = {"text/vcard", "text/x-vcard"}
        else:
            return False

        # CalDAV resource names are not required to have a .ics/.vcf suffix.
        # Prefer a matching content type, but also accept the normal suffix
        # when the backing WebDAV server returns a generic MIME type.
        mime = (entry.content_type or "").split(";", 1)[0].strip().lower()
        if mime in media_types:
            return True
        if href.lower().endswith(other_extension):
            return False
        if href.lower().endswith(extension):
            return True
        # Preserve extensionless resources with unknown MIME types, while
        # ignoring ordinary stray files such as .txt and .bak.
        return "." not in href and mime in {"", "application/octet-stream"}

    def _item_from_entry(self, entry: WebDAVEntry) -> radicale_item.Item | None:
        if not self._is_item_entry(entry):
            return None
        href = self._item_href(entry)
        text = self._storage._client.get(entry.path, etag=entry.etag)
        if text is None:
            return None
        return radicale_item.Item(
            collection=self,
            href=href,
            last_modified=entry.last_modified,
            text=text,
        )

    def get_all(self) -> Iterable[radicale_item.Item]:
        entries = [
            entry for entry in self._storage._client.list(self.path) if self._is_item_entry(entry)
        ]
        self._storage._client.prefetch(entries)
        for entry in entries:
            item = self._item_from_entry(entry)
            if item is not None:
                yield item

    def get_multi(self, hrefs: Iterable[str]) -> Iterable[tuple[str, radicale_item.Item | None]]:
        unique_hrefs = list(dict.fromkeys(hrefs))
        if not unique_hrefs:
            return
        if len(unique_hrefs) == 1:
            href = unique_hrefs[0]
            entries = (
                {href: self._storage._client.stat(_join(self.path, href), collection=False)}
                if pathutils.is_safe_filesystem_path_component(href)
                else {}
            )
        else:
            # A multi-get REPORT should use one directory PROPFIND, not one
            # WebDAV PROPFIND per requested href.
            requested = set(unique_hrefs)
            entries = {
                self._item_href(entry): entry
                for entry in self._storage._client.list(self.path)
                if not entry.is_collection and self._item_href(entry) in requested
            }
        valid = [
            entry for entry in entries.values() if entry is not None and self._is_item_entry(entry)
        ]
        self._storage._client.prefetch(valid)
        for href in unique_hrefs:
            if not pathutils.is_safe_filesystem_path_component(href):
                yield href, None
                continue
            entry = entries.get(href)
            yield href, self._item_from_entry(entry) if entry is not None else None

    def upload(
        self, href: str, item: radicale_item.Item
    ) -> tuple[radicale_item.Item, radicale_item.Item | None]:
        if not pathutils.is_safe_filesystem_path_component(href):
            raise pathutils.UnsafePathError(href)
        old_item = next((value for _, value in self.get_multi([href])), None)
        text = item.serialize()
        tag = self.tag
        if tag == "VCALENDAR":
            content_type = "text/calendar; charset=utf-8"
        elif tag == "VADDRESSBOOK":
            content_type = "text/vcard; charset=utf-8"
        else:
            content_type = "text/plain; charset=utf-8"
        resource_path = _join(self.path, href)
        self._storage._client.put(resource_path, text.encode("utf-8"), content_type=content_type)
        entry = self._storage._client.stat(resource_path, collection=False)
        uploaded = radicale_item.Item(
            collection=self,
            href=href,
            last_modified=entry.last_modified if entry else None,
            text=text,
        )
        return uploaded, old_item

    def delete(self, href: str | None = None) -> None:
        if href is None:
            if not self.path:
                raise ValueError("refusing to delete WebDAV storage root")
            self._storage._client.delete(self.path, collection=True)
            return
        if not pathutils.is_safe_filesystem_path_component(href):
            raise pathutils.UnsafePathError(href)
        self._storage._client.delete(_join(self.path, href))

    def get_meta(self, key: str | None = None):
        raw = self._storage._client.get(self._metadata_path)
        if raw is None:
            props: Mapping[str, str] = {}
        else:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("metadata is not an object")
                props = radicale_item.check_and_sanitize_props(parsed)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError(
                    f"failed to load properties of collection {self.path!r}: {exc}"
                ) from exc
        return props if key is None else props.get(key)

    def set_meta(self, props: Mapping[str, str]) -> None:
        checked = radicale_item.check_and_sanitize_props(dict(props))
        body = json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._storage._client.put(
            self._metadata_path,
            body.encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

    @property
    def last_modified(self) -> str:
        timestamps: list[datetime] = []
        own = self._storage._client.stat(self.path, collection=True)
        entries = self._storage._client.list(self.path)
        for entry in ([own] if own is not None else []) + entries:
            if not entry.last_modified:
                continue
            with contextlib.suppress(ValueError, TypeError):
                parsed = parsedate_to_datetime(entry.last_modified)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamps.append(parsed.astimezone(timezone.utc))
        value = max(timestamps) if timestamps else datetime(1970, 1, 1, tzinfo=timezone.utc)
        return format_datetime(value, usegmt=True)


class Storage(storage.BaseStorage):
    _is_collision_free = True
    _supports_unicode = True
    _supports_trailing_whitespace = False
    _supports_problematic_chars = False

    def __init__(self, configuration: config.Configuration) -> None:
        configuration = configuration.copy(PLUGIN_CONFIG_SCHEMA)
        super().__init__(configuration)
        self._lock = threading.RLock()

        credentials_file = self.configuration.get("storage", "webdav_credentials_file")
        credentials: dict[str, str] = {}
        if credentials_file:
            credentials = _read_credentials_file(credentials_file)

        url = self.configuration.get("storage", "webdav_url") or credentials.get("URL", "")
        username = self.configuration.get("storage", "webdav_username") or credentials.get(
            "USERNAME", ""
        )
        password = self.configuration.get("storage", "webdav_password") or credentials.get(
            "PASSWORD", ""
        )
        self._client = WebDAVClient(
            url,
            username,
            password,
            timeout=self.configuration.get("storage", "webdav_timeout"),
            verify_tls=self.configuration.get("storage", "webdav_verify_tls"),
        )
        logger.info("WebDAV storage initialized")

    @staticmethod
    def _collection(path: str, storage_: Storage) -> Collection:
        return Collection(storage_, pathutils.unstrip_path(path.strip("/"), True))

    def discover(
        self,
        path: str,
        depth: str = "0",
        child_context_manager: Callable[[str, str | None], AbstractContextManager[None]]
        | None = None,
        user_groups: set[str] | None = None,
    ) -> Iterator[types.CollectionOrItem]:
        del user_groups  # Group collection discovery is intentionally not implemented.
        sane_path = pathutils.strip_path(path)
        has_child_context_manager = child_context_manager is not None
        if child_context_manager is None:

            def child_context_manager(
                _path: str, _href: str | None
            ) -> AbstractContextManager[None]:
                return contextlib.nullcontext()

        if not sane_path:
            collection = self._collection("", self)
            yield collection
        else:
            entry = self._client.stat(sane_path, collection=True)
            if entry is None or not entry.is_collection:
                entry = self._client.stat(sane_path, collection=False)
            if entry is None:
                return
            if not entry.is_collection:
                parent, href = posixpath.split(sane_path)
                collection = self._collection(parent, self)
                item = next((value for _, value in collection.get_multi([href])), None)
                if item is not None:
                    yield item
                return
            collection = self._collection(sane_path, self)
            yield collection

        if depth == "0":
            return

        children = [
            entry
            for entry in self._client.list(sane_path)
            if (entry.is_collection or collection._is_item_entry(entry))
        ]
        # A caller-provided context may enforce per-item access. In that
        # case read inside each context, rather than prefetching ahead of it.
        if not has_child_context_manager:
            self._client.prefetch(entry for entry in children if not entry.is_collection)
        for entry in children:
            href = posixpath.basename(entry.path.rstrip("/"))
            with child_context_manager(sane_path, None if entry.is_collection else href):
                if entry.is_collection:
                    yield self._collection(entry.path, self)
                else:
                    item = collection._item_from_entry(entry)
                    if item is not None:
                        yield item

    def move(
        self, item: radicale_item.Item, to_collection: storage.BaseCollection, to_href: str
    ) -> None:
        if not isinstance(to_collection, Collection):
            raise TypeError("target collection belongs to another storage backend")
        if not isinstance(item.collection, Collection) or not item.href:
            raise TypeError("source item belongs to another storage backend")
        if not pathutils.is_safe_filesystem_path_component(to_href):
            raise pathutils.UnsafePathError(to_href)
        source = _join(item.collection.path, item.href)
        destination = _join(to_collection.path, to_href)
        if self._client.move(source, destination):
            return
        content = self._client.get_bytes(source)
        if content is None:
            raise storage.ComponentNotFoundError(source)
        tag = to_collection.tag
        content_type = (
            "text/vcard; charset=utf-8"
            if tag == "VADDRESSBOOK"
            else "text/calendar; charset=utf-8"
            if tag == "VCALENDAR"
            else "text/plain; charset=utf-8"
        )
        self._client.put(destination, content, content_type=content_type)
        self._client.delete(source)

    @staticmethod
    def _free_href(uid: str, suffix: str, used: set[str]) -> str:
        candidates = [uid + suffix, hashlib.sha256(uid.encode("utf-8")).hexdigest() + suffix]
        for candidate in candidates:
            if pathutils.is_safe_filesystem_path_component(candidate) and candidate not in used:
                return candidate
        counter = 0
        while True:
            candidate = hashlib.sha256(f"{uid}:{counter}".encode()).hexdigest() + suffix
            if candidate not in used:
                return candidate
            counter += 1

    def create_collection(
        self,
        href: str,
        items: Iterable[radicale_item.Item] | None = None,
        props: Mapping[str, str] | None = None,
    ) -> tuple[Collection, dict[str, radicale_item.Item], list[str]]:
        sane_path = pathutils.strip_path(href)
        self._client.ensure_collection(sane_path)
        collection = self._collection(sane_path, self)

        if props is None and items is None:
            return collection, {}, []

        if props is not None:
            collection.set_meta(props)

        replaced_items: dict[str, radicale_item.Item] = {}
        new_item_hrefs: list[str] = []
        if items is None:
            return collection, replaced_items, new_item_hrefs

        old_items = {item.href: item for item in collection.get_all() if item.href}
        for old_href in old_items:
            collection.delete(old_href)

        tag = (props or {}).get("tag", collection.tag)
        suffix = ".ics" if tag == "VCALENDAR" else ".vcf" if tag == "VADDRESSBOOK" else ""
        used: set[str] = set()
        for item in items:
            item_href = self._free_href(item.uid, suffix, used)
            used.add(item_href)
            uploaded, _ = collection.upload(item_href, item)
            if item_href in old_items:
                replaced_items[item_href] = old_items[item_href]
            else:
                new_item_hrefs.append(item_href)
            assert uploaded.href == item_href

        return collection, replaced_items, new_item_hrefs

    @contextlib.contextmanager
    def acquire_lock(self, mode: str, user: str = "", *args, **kwargs) -> Iterator[None]:
        del user, args, kwargs
        if mode not in {"r", "w"}:
            raise ValueError(f"invalid storage lock mode: {mode!r}")
        with self._lock:
            yield

    def verify(self) -> bool:
        ok = True
        try:
            for entry in self._client.iter_tree(""):
                if entry.is_collection:
                    metadata = self._client.get(_join(entry.path, _METADATA_FILENAME))
                    if metadata is not None:
                        parsed = json.loads(metadata)
                        if not isinstance(parsed, dict):
                            raise ValueError(f"invalid metadata in {entry.path!r}")
                    continue
                if posixpath.basename(entry.path) == _METADATA_FILENAME:
                    continue
                content = self._client.get(entry.path)
                if content is None:
                    continue
                radicale_item.read_components(content)
        except Exception as exc:
            logger.error("WebDAV storage verification failed: %s", exc)
            ok = False
        return ok
