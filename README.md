# aniani

A floating GUI for searching, watching, and downloading anime -- search →
episode list → play, with continue-watching, resume-from-timestamp,
skip-intro, and always-on Discord Rich Presence.

## Sources

- **anidb.app** (default) -- scraped directly, no login, generally
  reachable.
- **YumaAPI / aniwatchtv.to** -- vendored from
  [pyanimecli](https://github.com/gammadevv/pyanimecli) (MIT, see
  `LICENSE_PYANIMECLI`). Blocked by some ISPs (confirmed: India, via both
  DNS poisoning and SNI-level filtering) -- works fine off that network.
- **nyaa.si** (torrent) -- different flow: search returns torrent
  releases, not a clean episode list. Streams via sequential download
  (qbittorrent-nox) once enough has buffered, or downloads fully for
  offline viewing.

## Players

- **VLC** (default) -- driven over its HTTP control interface.
- **mpv** -- driven over its JSON IPC socket.

Either way, playback is a **real, separate player window**, not embedded
in the Qt panel -- embedding was tried two ways (X11 `--wid` reparenting,
libmpv's OpenGL render API) and abandoned: reparenting doesn't actually
work on wlroots/Hyprland even through XWayland, and the render API
crashed (SIGABRT) on first paint. The panel spawns/drives/closes the
player over IPC/HTTP instead.

## Downloads / offline

Download a single episode, a range, or a whole series via ffmpeg (same
approach `ani-cli` itself uses for `-d`/`--download`). Downloaded
episodes show up in the in-app Offline Library, playable with no source
or network needed at all.

## Discord Rich Presence

Always on -- shows "Browsing" while searching/picking an episode, and
the full watching state (cover art, elapsed time) during playback. While
the panel is open it updates presence itself; closing it hands off to a
standalone daemon (`rpc_daemon.py`) that keeps polling the player and
updating Discord while it plays on its own, so presence doesn't drop
just because you closed the control panel. Reopening the panel kills
that daemon and takes back over.

## Setup

Needs `curl_chrome136` (curl-impersonate, for anidb.app), `vlc` and/or
`mpv`, `qbittorrent-nox` (for the nyaa.si source), `ani-skip` (optional,
for anidb.app's skip-intro), and Python deps: `PyQt6`, `requests`,
`httpx`, `beautifulsoup4`, `tqdm`, `pypresence`.

Create a Discord Application at
<https://discord.com/developers/applications> and write its client ID to
`config.json`:

```json
{ "client_id": "..." }
```
