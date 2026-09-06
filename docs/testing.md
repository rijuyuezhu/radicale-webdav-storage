# Testing

## Local test suite

The default suite is self-contained and does not contact a real WebDAV server:

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

It covers:

- WebDAV PROPFIND parsing and URL encoding;
- collection creation and metadata;
- item upload, replacement, discovery, move, and verification;
- safe parsing of the credentials file;
- a real Radicale WSGI request path for `MKCALENDAR`, `PUT`, stale `If-Match`, and `GET`.

The Radicale request test is important because it verifies the concurrency assumption: stale client writes are rejected by Radicale before the plugin sends the backend PUT.

## Real WebDAV integration test

The optional integration tests create isolated temporary collections under a configured WebDAV root and removes it afterwards.

Set:

```bash
export RADICALE_WEBDAV_TEST_CREDENTIALS=/path/to/webdav-test-credentials
```

Then run:

```bash
uv run pytest -m integration -q
```

The credentials file uses:

```text
URL=http://127.0.0.1:2222/
USERNAME=...
PASSWORD=...
```

The test checks the operations this plugin actually depends on: collection creation, metadata/item PUT, listing, byte-preserving GET, MOVE, and DELETE.

Do not point the integration test at a WebDAV root where creating a uniquely named temporary child collection is unsafe or prohibited.
