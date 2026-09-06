from __future__ import annotations

from pathlib import Path

import pytest
from radicale import config
from radicale.item import Item

from radicale_webdav_storage.storage import Collection, Storage, _read_credentials_file

from .fakes import MemoryWebDAVClient

EVENT = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//EN",
        "BEGIN:VEVENT",
        "UID:event-1",
        "DTSTAMP:20260906T000000Z",
        "DTSTART:20260906T010000Z",
        "DTEND:20260906T020000Z",
        "SUMMARY:Test event",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)

EVENT_2 = EVENT.replace("UID:event-1", "UID:event-2").replace(
    "SUMMARY:Test event", "SUMMARY:Second"
)


def make_storage() -> tuple[Storage, MemoryWebDAVClient]:
    configuration = config.Configuration(config.DEFAULT_CONFIG_SCHEMA)
    configuration.update(
        {
            "storage": {
                "type": "radicale_webdav_storage",
                "webdav_url": "https://dav.example/root/",
            }
        },
        "test",
    )
    backend = Storage(configuration)
    backend._client.close()
    fake = MemoryWebDAVClient()
    backend._client = fake  # type: ignore[assignment]
    return backend, fake


def create_calendar(backend: Storage) -> Collection:
    backend.create_collection("/alice/")
    collection, _, _ = backend.create_collection(
        "/alice/personal/",
        props={"tag": "VCALENDAR", "D:displayname": "Personal"},
    )
    return collection


def test_create_collection_metadata_and_upload_round_trip() -> None:
    backend, fake = make_storage()
    collection = create_calendar(backend)

    assert collection.tag == "VCALENDAR"
    assert collection.get_meta("D:displayname") == "Personal"
    assert "alice/personal/.Radicale.props" in fake.files

    item = Item(collection=collection, href="event.ics", text=EVENT)
    uploaded, old = collection.upload("event.ics", item)

    assert old is None
    assert uploaded.href == "event.ics"
    assert uploaded.etag == item.etag
    stored = list(collection.get_all())
    assert len(stored) == 1
    assert stored[0].uid == "event-1"
    assert stored[0].serialize() == EVENT


def test_upload_returns_old_item_on_replace() -> None:
    backend, _ = make_storage()
    collection = create_calendar(backend)
    collection.upload("event.ics", Item(collection=collection, href="event.ics", text=EVENT))

    replacement = EVENT.replace("SUMMARY:Test event", "SUMMARY:Changed")
    uploaded, old = collection.upload(
        "event.ics", Item(collection=collection, href="event.ics", text=replacement)
    )

    assert old is not None
    assert old.serialize() == EVENT
    assert uploaded.serialize() == replacement
    assert uploaded.etag != old.etag


def test_discover_lists_principal_calendar_and_item() -> None:
    backend, _ = make_storage()
    collection = create_calendar(backend)
    collection.upload("event.ics", Item(collection=collection, href="event.ics", text=EVENT))

    root = list(backend.discover("/", depth="1"))
    assert [entry.path for entry in root] == ["", "alice"]

    principal = list(backend.discover("/alice/", depth="1"))
    assert [entry.path for entry in principal] == ["alice", "alice/personal"]

    calendar = list(backend.discover("/alice/personal/", depth="1"))
    assert calendar[0].path == "alice/personal"
    assert isinstance(calendar[1], Item)
    assert calendar[1].uid == "event-1"


def test_move_uses_webdav_move_and_preserves_content() -> None:
    backend, fake = make_storage()
    source = create_calendar(backend)
    target, _, _ = backend.create_collection(
        "/alice/research/", props={"tag": "VCALENDAR", "D:displayname": "Research"}
    )
    uploaded, _ = source.upload("event.ics", Item(collection=source, href="event.ics", text=EVENT))

    backend.move(uploaded, target, "moved.ics")

    assert "alice/personal/event.ics" not in fake.files
    assert fake.files["alice/research/moved.ics"].decode() == EVENT


def test_create_collection_with_items_generates_safe_hrefs() -> None:
    backend, _ = make_storage()
    backend.create_collection("/alice/")
    collection, replaced, new_hrefs = backend.create_collection(
        "/alice/imported/",
        items=[
            Item(collection_path="alice/imported", text=EVENT),
            Item(collection_path="alice/imported", text=EVENT_2),
        ],
        props={"tag": "VCALENDAR"},
    )

    assert replaced == {}
    assert sorted(new_hrefs) == ["event-1.ics", "event-2.ics"]
    assert sorted(item.uid for item in collection.get_all()) == ["event-1", "event-2"]


def test_verify_detects_malformed_calendar_resource() -> None:
    backend, fake = make_storage()
    create_calendar(backend)
    assert backend.verify()

    fake.put(
        "alice/personal/broken.ics",
        b"this is not iCalendar",
        content_type="text/calendar",
    )
    assert not backend.verify()


def test_credentials_file_is_parsed_without_shell_execution(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    credentials = tmp_path / "credentials"
    credentials.write_text(
        f"URL='http://127.0.0.1:2222/'\nUSERNAME=alice\nPASSWORD='$(touch {marker})'\n",
        encoding="utf-8",
    )

    parsed = _read_credentials_file(str(credentials))

    assert parsed["URL"] == "http://127.0.0.1:2222/"
    assert parsed["USERNAME"] == "alice"
    assert parsed["PASSWORD"].startswith("$(touch ")
    assert not marker.exists()


def test_invalid_lock_mode_is_rejected() -> None:
    backend, _ = make_storage()
    with pytest.raises(ValueError, match="invalid storage lock mode"), backend.acquire_lock("x"):
        pass
