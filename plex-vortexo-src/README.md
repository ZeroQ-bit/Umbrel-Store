# Plex Vortexo companion

This image supplies the private Plex Web enhancement used by the
`zeroq-plex` Umbrel package. It does not modify Plex Media Server or Plex's
hashed web bundles.

## Roles

- `VORTEXO_ROLE=gateway` starts the owner-authenticated API on loopback and an
  unprivileged Nginx gateway on port `32401`. Nginx proxies Plex on `32400` and
  injects the local JavaScript and CSS only into `/web/index.html`.
- `VORTEXO_ROLE=mount` starts the privileged rclone supervisor on loopback port
  `32402`. It mounts TorBox WebDAV read-only at
  `/downloads/.vortexo-source` and refuses any mount it did not create.

Settings, signed playback URLs, resume progress, and library jobs live in the
private SQLite data directory. Browser responses never contain the TorBox key,
Plex token, magnet, info hash, manifest request headers, or raw signed URL.

## API

The owner session is established by `PUT /vortexo/api/session`. The gateway
accepts the Plex Web token only when Plex confirms it belongs to the same
account as the owner token in `Preferences.xml`, then sets an HTTP-only cookie.

- `GET /vortexo/api/status`
- `GET|PUT /vortexo/api/settings`
- `GET /vortexo/api/discover/{id}`
- `GET /vortexo/api/discover/{id}/episodes`
- `POST /vortexo/api/streams`
- `POST /vortexo/api/play`
- `POST /vortexo/api/progress`
- `POST /vortexo/api/library-jobs`
- `GET /vortexo/api/library-jobs/{id}`

## Verification

Run:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q vortexo
node --check web/plex-vortexo.js
sh -n entrypoint.sh
```

The image workflow publishes `main` and commit-SHA tags. The Umbrel updater
resolves the published `main` tag to an immutable digest before release.

## Live handoff

Do not overlap this mount with Orbit. Stop Orbit's mount role first, prove
`.vortexo-source` is no longer a mountpoint and is empty, and only then start
Plex Vortexo. The pre-start hook and mount supervisor repeat these checks and
will not detach a foreign mount.

Keep the previous Plex package and the stopped Orbit installation available
until a cached playback and an Add to Plex job both pass through Plex
confirmation. Port `32400` remains the unchanged native-client fallback.
