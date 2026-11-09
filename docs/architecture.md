# Architecture

## Goals

`radicale-webdav-storage` is for a deliberately small deployment:

- one Radicale server process;
- one existing WebDAV endpoint as the durable source of truth;
- no local calendar database;
- no persistent cache required for correctness;
- ordinary CalDAV clients such as DAVx⁵ and Thunderbird;
- AI clients use CalDAV (or a later semantic MCP layer) rather than writing WebDAV directly.

## Responsibility split

Radicale remains responsible for:

- CalDAV/CardDAV request parsing;
- calendar-query and multiget behavior;
- iCalendar validation;
- client-facing ETags and preconditions;
- recurrence filtering;
- authentication and rights.

The plugin is responsible for:

- mapping collections to WebDAV directories;
- mapping resources to WebDAV files;
- storing collection properties;
- serializing storage operations through the Radicale storage lock;
- translating WebDAV transport failures into storage failures.

The backing WebDAV service is responsible only for durable bytes and directory operations.

## Concurrency invariant

All supported calendar writes pass through one Radicale process.

Radicale performs PUT precondition handling in this order:

```text
storage.acquire_lock("w")
  discover current item
  validate If-Match / If-None-Match
  validate calendar object
  collection.upload(...)
release lock
```

The plugin therefore uses one process-wide `threading.RLock` for both read and write storage locks. This is intentionally conservative: it serializes storage sections but makes the correctness argument straightforward.

The backing WebDAV endpoint is not part of the concurrency protocol. In particular, a backend that ignores HTTP conditional PUT can still be used safely under this single-process invariant.

## Statelessness

The process may hold ephemeral state:

- the in-memory lock;
- HTTP connection pools;
- Python objects for the current request.

None of it is durable. Restarting the Radicale process does not lose calendar state.

Persistent data consists only of the WebDAV tree.

## Collection metadata

WebDAV dead properties are not required. Collection properties are stored as JSON in:

```text
<collection>/.Radicale.props
```

This is the same filename and JSON-shaped metadata convention used by Radicale's built-in multifilesystem backend.

## ETags

The plugin does not use backend ETags as the authoritative CalDAV ETag. `radicale.item.Item` derives the item ETag from serialized calendar bytes.

Strong backend ETags validate the in-memory item-content cache, avoiding repeated GETs for unchanged resources after the directory listing TTL expires. Client-facing ETags are still computed by Radicale from calendar bytes. As with the storage lock, this optimization assumes all mutations go through the single Radicale process.

## Sync tokens

The plugin deliberately does not persist sync-token history. It inherits Radicale's basic `BaseCollection.sync()` semantics.

This trades synchronization efficiency for a much simpler stateless server. It is acceptable for a personal calendar with a modest number of objects. Persistent incremental history can be added later without changing the stored iCalendar resources.

## Failure model

A failed single-resource PUT leaves either the previous backend object or the new backend object depending on the WebDAV server's PUT semantics. The plugin does not claim transactional semantics beyond a single backend request.

`MOVE` is preferred when supported. If unavailable, the fallback is:

```text
GET source
PUT destination
DELETE source
```

This sequence is protected from concurrent CalDAV writes by the process-wide lock, but a transport failure between steps may require manual repair. The data remains plain WebDAV resources and can be inspected directly.

Whole-collection replacement is similarly best-effort rather than transactional. Ordinary CalDAV client CRUD does not rely on whole-collection atomic replacement.

## Unsupported architecture

Do not run:

```text
Radicale worker A ─┐
                   ├─ same WebDAV root
Radicale worker B ─┘
```

Process-local locks cannot coordinate two writers. Multi-process or active-active deployment is intentionally outside this product's scope.

## Read caching

The plugin keeps a 15-second in-memory cache of backing WebDAV GET and PROPFIND responses. In addition, calendar item contents are retained in an LRU cache keyed by URL and strong backend ETag, bounded to 16 MiB or 1024 files. When a fresh directory listing reports an unchanged strong ETag, the item bytes are reused without another GET. Cold scans prefetch missing items with up to four concurrent reads.

Write attempts invalidate the short-lived cache before the request. Item PUT, DELETE, and MOVE also invalidate the affected versioned content; collection DELETE invalidates all versioned contents. Cache state is never persisted. No direct WebDAV writes are supported while Radicale is running.

Calendar scans recognize expected resource types and suffixes, ignoring unrelated .txt/.bak files while retaining correctly typed calendar resources with unconventional filenames. A malformed calendar resource with the expected suffix still surfaces a parsing error for diagnosis.
