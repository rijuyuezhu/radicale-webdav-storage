# Deployment

The intended deployment is one Radicale process on the same host as, or with network access to, the backing WebDAV service.

## Process model

Run exactly one Radicale process for a given WebDAV root. Do not put Gunicorn/uWSGI multi-worker process management in front of this plugin.

A reverse proxy may handle public TLS, but it must forward requests to the one Radicale instance.

Recommended same-host layout:

```text
Internet
  │
 HTTPS
  │
Caddy/nginx + public authentication
  │ X-Remote-User
127.0.0.1:5232
  │
Radicale + radicale-webdav-storage
  │
http://127.0.0.1:18080/caldav-data/
  │
existing WebDAV origin
```

If the WebDAV origin and Radicale live on the same machine, connect the plugin to the loopback origin directly. Do not route storage traffic through the public reverse proxy unless the origin has no private endpoint.

Keep the public CalDAV path and the private WebDAV storage path conceptually separate. For example, `/caldav/` may be the public CalDAV route while `/caldav-data/` is only a directory in the backing WebDAV service.

## Credentials

If the loopback WebDAV origin requires no authentication, no storage credential file is needed at all. Otherwise, prefer a root-readable or service-readable credentials file rather than putting the WebDAV password directly in `radicale.conf`.

For the supported combined file format:

```text
URL=https://storage.example.com/dav/calendar-data/
USERNAME=...
PASSWORD=...
```

The plugin parses this file; it does not execute it. Use mode `0600` or an equivalently restrictive group-readable mode.

## Reverse proxy authentication

A useful deployment is to let Caddy or nginx authenticate clients and pass only the authenticated identity to Radicale. Radicale has a built-in backend for this:

```ini
[auth]
type = http_x_remote_user
```

The proxy must overwrite `X-Remote-User`; it must never trust a client-provided value. Radicale must also listen only on loopback or another trusted private interface.

When exposing Radicale under a path prefix such as `/caldav/`, pass `X-Script-Name: /caldav` **without stripping the prefix in the proxy first**. Radicale uses that value both to strip the incoming path and to generate correctly prefixed DAV hrefs.

The included [`../examples/Caddyfile`](../examples/Caddyfile) demonstrates this arrangement, including `/.well-known/caldav` discovery. It intentionally returns `404` for non-CalDAV paths so the backing WebDAV tree is not accidentally exposed through the public calendar host.

## systemd outline

A minimal service can look like:

```ini
[Unit]
Description=Radicale CalDAV server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=radicale
Group=radicale
ExecStart=/opt/radicale/bin/radicale --config /etc/radicale/config
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
```

There is deliberately no writable local data directory required by the plugin. The Python environment and configuration are replaceable deployment artifacts; the WebDAV tree is the durable state.

## Backups

Back up the WebDAV storage root. A consistent backup is easiest with the Radicale service stopped, although ordinary object-level backup while running is acceptable if the backup mechanism tolerates files changing during the scan.

The critical files are:

```text
<principal>/<calendar>/.Radicale.props
<principal>/<calendar>/*.ics
```

Authentication files such as `/etc/radicale/users` are separate from calendar data and should be backed up according to the chosen auth backend.

## Upgrade procedure

1. stop Radicale;
2. upgrade Radicale and this plugin in the Python environment;
3. run the local test suite for the pinned versions if this is a major Radicale upgrade;
4. start Radicale;
5. verify one read and one write from a CalDAV client.

Because the plugin has no schema database, there is no database migration step.

## Deployment smoke test

Before configuring a real client, verify at least these protocol boundaries:

1. unauthenticated public CalDAV returns `401`;
2. authenticated `OPTIONS` advertises `calendar-access`;
3. `current-user-principal` and `calendar-home-set` are discoverable;
4. create a calendar and event with `If-None-Match: *`;
5. restart Radicale and confirm the event is still readable;
6. a stale `If-Match` update returns `412`;
7. a `calendar-query` REPORT returns the event;
8. clean up the temporary calendar through CalDAV.

Step 5 is especially important for this backend: it demonstrates that the Radicale process owns no durable calendar state.
