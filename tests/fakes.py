from __future__ import annotations

import posixpath
from collections.abc import Iterable

from radicale_webdav_storage.webdav import WebDAVEntry


class MemoryWebDAVClient:
    def __init__(self) -> None:
        self.directories = {""}
        self.files: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self._tick = 0
        self.modified: dict[str, str] = {"": self._timestamp()}

    def _timestamp(self) -> str:
        self._tick += 1
        return f"Sun, 06 Sep 2026 00:00:{self._tick % 60:02d} GMT"

    @staticmethod
    def _clean(path: str) -> str:
        return path.strip("/")

    def close(self) -> None:
        pass

    def ensure_collection(self, path: str) -> None:
        current: list[str] = []
        for part in self._clean(path).split("/") if self._clean(path) else []:
            current.append(part)
            value = "/".join(current)
            self.directories.add(value)
            self.modified[value] = self._timestamp()

    def stat(self, path: str, *, collection: bool) -> WebDAVEntry | None:
        del collection
        path = self._clean(path)
        if path in self.directories:
            return WebDAVEntry(path, True, last_modified=self.modified.get(path))
        if path in self.files:
            return WebDAVEntry(
                path,
                False,
                etag=f'"{hash(self.files[path])}"',
                last_modified=self.modified.get(path),
                content_type=self.content_types.get(path),
            )
        return None

    def list(self, path: str) -> list[WebDAVEntry]:
        path = self._clean(path)
        prefix = f"{path}/" if path else ""
        result: list[WebDAVEntry] = []
        for directory in sorted(self.directories):
            if not directory.startswith(prefix) or directory == path:
                continue
            remainder = directory[len(prefix) :]
            if "/" not in remainder:
                result.append(
                    WebDAVEntry(directory, True, last_modified=self.modified.get(directory))
                )
        for filename in sorted(self.files):
            if not filename.startswith(prefix):
                continue
            remainder = filename[len(prefix) :]
            if "/" not in remainder:
                result.append(
                    WebDAVEntry(
                        filename,
                        False,
                        etag=f'"{hash(self.files[filename])}"',
                        last_modified=self.modified.get(filename),
                        content_type=self.content_types.get(filename),
                    )
                )
        return result

    def get(self, path: str) -> str | None:
        content = self.files.get(self._clean(path))
        return None if content is None else content.decode("utf-8")

    def get_bytes(self, path: str) -> bytes | None:
        return self.files.get(self._clean(path))

    def put(self, path: str, content: bytes, *, content_type: str) -> None:
        path = self._clean(path)
        parent = posixpath.dirname(path)
        if parent not in self.directories:
            raise RuntimeError(f"parent collection missing: {parent}")
        self.files[path] = content
        self.content_types[path] = content_type
        self.modified[path] = self._timestamp()
        self.modified[parent] = self._timestamp()

    def delete(self, path: str, *, collection: bool = False, missing_ok: bool = True) -> None:
        path = self._clean(path)
        if collection:
            if path not in self.directories:
                if missing_ok:
                    return
                raise RuntimeError("missing collection")
            prefix = f"{path}/"
            self.files = {
                key: value for key, value in self.files.items() if not key.startswith(prefix)
            }
            self.content_types = {
                key: value
                for key, value in self.content_types.items()
                if not key.startswith(prefix)
            }
            self.directories = {
                directory
                for directory in self.directories
                if directory != path and not directory.startswith(prefix)
            }
            self.modified = {
                key: value
                for key, value in self.modified.items()
                if key != path and not key.startswith(prefix)
            }
            return
        if path not in self.files:
            if missing_ok:
                return
            raise RuntimeError("missing file")
        del self.files[path]
        self.content_types.pop(path, None)
        self.modified.pop(path, None)

    def move(self, source: str, destination: str) -> bool:
        source = self._clean(source)
        destination = self._clean(destination)
        if source not in self.files:
            raise RuntimeError("missing source")
        self.files[destination] = self.files.pop(source)
        self.content_types[destination] = self.content_types.pop(source)
        self.modified.pop(source, None)
        self.modified[destination] = self._timestamp()
        return True

    def iter_tree(self, path: str = "") -> Iterable[WebDAVEntry]:
        for entry in self.list(path):
            yield entry
            if entry.is_collection:
                yield from self.iter_tree(entry.path)
