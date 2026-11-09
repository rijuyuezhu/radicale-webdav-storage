# radicale-webdav-storage

A small, stateless [Radicale](https://radicale.org/) storage plugin that keeps calendar data in an existing WebDAV service.

The design is intentionally simple:

```text
DAVx⁵ / Thunderbird / AI
          │
        CalDAV
          │
       Radicale
     single process
     in-memory lock
          │
        WebDAV
          │
       durable data
```

Radicale owns the CalDAV semantics. This plugin only maps Radicale collections and items to a WebDAV tree. There is no local calendar database, sync-history database, Redis, or persistent plugin state.

## Status

Alpha. The core storage API and Radicale request path are covered by tests, but real client compatibility testing is still in progress.

Current target: Radicale 3.8.x, Python 3.10+. The plugin is also checked against current Radicale 3.8.1.dev source during development.

## Storage layout

The configured WebDAV URL is the storage root. Radicale paths map directly below it:

```text
<webdav root>/
└── alice/
    ├── personal/
    │   ├── .Radicale.props
    │   ├── event-a.ics
    │   └── event-b.ics
    └── research/
        ├── .Radicale.props
        └── meeting.ics
```

Each calendar object is one iCalendar resource. Collection metadata is JSON in `.Radicale.props`, matching Radicale's normal multifilesystem metadata format.

The WebDAV endpoint should be treated as a storage backend, not as a second supported calendar write interface. While Radicale is running, calendar mutations should go through CalDAV. Read-only backup and inspection of the WebDAV tree are fine.

## Why a single process?

The deployment is deliberately single-instance. Radicale evaluates client `If-Match` and `If-None-Match` preconditions while holding `storage.acquire_lock("w")`. This plugin implements that lock in memory.

As a result, the backing WebDAV server does **not** need to implement conditional PUT correctly. It only needs ordinary WebDAV storage operations. This keeps the CalDAV service stateless without introducing a database or distributed lock service.

Do not run multiple Radicale worker processes with this plugin.

## WebDAV requirements

Required:

- `PROPFIND` with Depth 0 and 1
- `GET`
- `PUT`
- `DELETE`
- `MKCOL`

Recommended:

- stable per-resource ETags
- `MOVE`

`MOVE` is used when available; the plugin falls back to GET → PUT → DELETE under the process-wide write lock.

WebDAV dead properties are not required. Calendar metadata is stored in `.Radicale.props` instead.

## Installation

For development:

```bash
uv sync --extra dev
```

Once the package is published, a Radicale environment can install it with `pip`. Until then, install directly from a checkout:

```bash
python -m pip install /path/to/radicale-webdav-storage
```

## Configuration

Minimal Radicale storage configuration:

```ini
[storage]
type = radicale_webdav_storage
webdav_url = http://127.0.0.1:8080/caldav-data/
strict_preconditions = true
```

When the WebDAV origin is on the same host and only exposed on loopback, prefer connecting to that origin directly. There is no benefit in sending storage traffic back through the public TLS/authentication reverse proxy.

If the WebDAV origin itself requires credentials, avoid putting the password directly in the Radicale config. The plugin can consume a credentials file:

```ini
[storage]
type = radicale_webdav_storage
webdav_credentials_file = /etc/radicale/webdav-credentials
```

The file format is deliberately small and is parsed as data, **not sourced as shell code**:

```text
URL=https://storage.example.com/dav/calendar-data/
USERNAME=calendar-storage
PASSWORD=secret
```

Shell-style quoting is accepted for values. Unknown keys are ignored. An explicit `webdav_url`, `webdav_username`, or `webdav_password` takes precedence over the corresponding value in the credentials file.

Additional options:

```ini
[storage]
webdav_timeout = 30
webdav_verify_tls = true
```

## Authentication and rights

This plugin only implements storage. Configure Radicale authentication and rights independently.

For a public Radicale instance behind an authenticating reverse proxy, Radicale's built-in `http_x_remote_user` backend is a small and useful setup:

```ini
[auth]
type = http_x_remote_user

[rights]
type = owner_only
```

The reverse proxy must overwrite `X-Remote-User` with the authenticated identity, and Radicale must not be directly reachable by untrusted clients. See [`docs/deployment.md`](docs/deployment.md) and the Caddy example in [`examples/Caddyfile`](examples/Caddyfile).

Using Radicale's own `htpasswd` backend is also supported when authentication should live in Radicale instead of the reverse proxy.

## Running

Radicale loads external storage plugins by Python module name:

```bash
radicale --config /etc/radicale/config
```

For the intended deployment, run exactly one Radicale process. A systemd service or a single container is fine.

## Development

Run all local checks:

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

There is also an opt-in real-WebDAV integration test; see [`docs/testing.md`](docs/testing.md).

## Design notes

- Client-facing item ETags are derived by Radicale from the actual calendar bytes, not trusted from the backend WebDAV ETag.
- The default `BaseCollection.sync()` behavior is retained. There is no persistent sync-token history; clients can fall back to normal CalDAV listing/query synchronization.
- The plugin keeps only bounded in-memory caches: short-lived directory listings and ETag-validated calendar bytes (up to 16 MiB or 1024 items). Cold calendar scans fetch missing files with at most four concurrent WebDAV reads. No local files or persistent cache are created.
- WebDAV credentials are never intentionally logged.
- Group collection discovery and Radicale sharing backends are not implemented by this storage plugin.

See [`docs/architecture.md`](docs/architecture.md) for invariants and limitations, and [`docs/deployment.md`](docs/deployment.md) for the intended single-process deployment.

## License

GPL-3.0-or-later. Radicale itself is GPL-3.0-or-later, and this plugin directly implements its storage interfaces.
