# Changelog

## 0.1.0 - unreleased

Initial implementation:

- stateless Radicale storage backed by WebDAV;
- `.Radicale.props` collection metadata;
- process-local storage lock for single-instance deployment;
- WebDAV MOVE with GET/PUT/DELETE fallback;
- combined `URL`/`USERNAME`/`PASSWORD` credentials-file support;
- defensive MKCOL handling that verifies an HTTP 405 really means the collection exists;
- public reverse-proxy example that keeps the backing WebDAV tree private;
- unit tests, Radicale WSGI CRUD/precondition coverage, and a minimal Python 3.10/3.14 CI matrix.
