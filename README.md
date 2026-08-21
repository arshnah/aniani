# aniani

A desktop GUI for searching, watching, and downloading anime -- a
Netflix-style Discover dashboard (hero banner, Continue Watching /
Trending / Recommended cover-art rows) → episode list → play, with
resume-from-timestamp, skip-intro/outro, AniList tracker sync, and
always-on Discord Rich Presence. Styled in Catppuccin Mocha with a
pink/mauve accent pair. Defaults to a real 1280x800 window; a sidebar
toggle switches to a compact 420x680 floating-panel mode that strips
the dashboard back down to just search + results.

## Playback controls

Playback speed (0.5x-2x) and, on mpv, subtitle/audio track cycling --
VLC's HTTP interface only exposes id-based track *set* commands with no
way to list what's available, so track cycling is mpv-only (the button
hides itself when VLC is the active backend). Keyboard shortcuts while
the player page is focused: `Space` pause, `←`/`→` seek 10s, `↑`/`↓`
volume, `[`/`]` speed.

## Browse / discover

A "Browse" tab pulls trending and popular anime straight from
AniList's public GraphQL API (no account/API key needed) -- selecting a
title searches it on whichever source is currently active.

## AniList tracker sync

Optional, off by default. When enabled (Account tab), finishing an
episode pushes your progress to your AniList list -- so aniani doesn't
become a second, disconnected watch-history silo from whatever you
already track on. One-time setup:

1. Create a free client at
   [anilist.co/settings/developer](https://anilist.co/settings/developer)
   with redirect URL `https://anilist.co/api/v2/oauth/pin`.
2. Paste that client id into the Account tab and click "Open AniList
   authorization page" -- authorize in your browser, AniList shows a
   token as plain text.
3. Paste that token back into the Account tab and hit Connect.

No local webserver or redirect handler needed -- this is the same
token-via-copy-paste flow other unofficial AniList desktop clients use.
Anime titles are matched to AniList entries by title search (best
effort, same limitation any client without a shared cross-reference id
has); episode numbers that aren't a plain integer (specials, etc.) are
skipped rather than guessed at.

## Windows, no Python needed

Grab `aniani.exe` from the [Releases page](../../releases) -- built
automatically by GitHub Actions, no installation, nothing else to set
up. Discord Rich Presence works out of the box (no Discord Developer
account needed, it uses aniani's own shared app id). You'll still want
**VLC** (recommended) or **mpv** installed for actual playback, and
optionally **ffmpeg** for downloads -- aniani will tell you clearly if
one's missing rather than failing silently.

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
for anidb.app's skip-intro), and the Python deps in `requirements.txt`
(`pip install -r requirements.txt`).

Create a Discord Application at
<https://discord.com/developers/applications> and write its client ID to
`config.json`:

```json
{ "client_id": "..." }
```

Want AniList progress sync too? That's a separate one-time setup covered
in its own section above ("AniList tracker sync") -- it's optional and
off by default, so nothing here breaks if you skip it.

## Platform support

**Windows support hasn't been run on an actual Windows machine yet.**
Treat it as "should work, needs real testing," and file/fix issues as
they turn up.

Built and daily-driven on Linux (Arch/Hyprland). Windows support exists
-- every OS-specific decision (state/download directories, mpv IPC via
a named pipe instead of a Unix socket, binary discovery for
vlc/mpv/ffmpeg/curl-impersonate/ani-skip/qbittorrent, detached-process
spawning for the RPC daemon handoff) is centralized in
`platform_utils.py` and follows documented Windows conventions
(`%LOCALAPPDATA%`, `\\.\pipe\...`, `DETACHED_PROCESS`). A couple of
things are known-different there rather than broken:

- `qbittorrent-nox` has no official Windows build; falls back to the
  regular qBittorrent GUI, which shows a window (same WebUI API
  underneath, just not headless).
- `curl-impersonate` (`curl_chrome136`) needs a separate Windows binary
  on PATH -- it doesn't ship with Windows the way plain `curl.exe` does
  since 10 1803.

macOS: untested and not a current target, though most of the same
`platform_utils.py` groundwork (state dirs, Unix-socket mpv IPC) should
carry over -- binary discovery paths for vlc/mpv would need macOS
`/Applications` fallbacks added if anyone wants to pick that up.
