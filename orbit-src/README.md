# Orbit

Orbit is a unified media automation app for Umbrel. It combines manual media
discovery, MDBList and Trakt imports, debrid acquisition, a remote-only mount,
library organization, and Plex refresh into one visible pipeline.

## Features

- TMDb-powered manual movie and TV search
- Existing Plex library inventory with actual resolution, HDR/Dolby Vision,
  codecs, containers, file sizes, and multi-version visibility
- TMDb/IMDb matching that marks Discover results already in Plex
- Explicit 1080p upgrades for lower-quality Plex items
- Persistent SQLite request queue and event history
- Direct MDBList and Trakt list imports that skip titles already in Plex
- Existing TorBox/Real-Debrid acquisition engine reused as an isolated worker
- Existing WebDAV/zurg FUSE mount reused as a separate least-privilege service
- One authenticated Orbit dashboard and one Umbrel app icon
- Remote source media with persistent VFS caching forced off

## Architecture

The Umbrel app contains two internal services:

- `server`: Orbit dashboard, API, database, importer, and manual request worker
- `mount`: rclone WebDAV or zurg FUSE mount supervisor

The split is intentional: only the mount receives FUSE/SYS_ADMIN privileges.
Users interact with Orbit as one application.

## Local development

Orbit's core has no third-party Python dependencies:

```sh
ORBIT_DATA_DIR=./data ORBIT_ACQUIRE_COMMAND='' python3 -m orbit
```

Open `http://localhost:8080`. Manual requests remain queued when the acquisition
command is empty, which is useful for UI and API development without live debrid
credentials.

Run tests with:

```sh
python3 -m unittest discover -s tests -v
```

## Safety

Automatic lists use import-only behavior. Removing a title from MDBList or
Trakt never deletes Plex media, debrid torrents, or library links, and items
already present in Plex are skipped. Plex refresh failures preserve the last
successful inventory. Persistent source-media caching remains disabled; only
Orbit state, logs, and library symlinks are stored locally.
