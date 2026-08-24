# aniani

A desktop GUI for searching, watching, and downloading anime -- a
discover dashboard (Continue Watching / Trending / Popular cover-art
rows, click one to jump to search) → episode list → play, with
resume-from-timestamp, AniList tracker sync, and Discord Rich
Presence. Rust, egui/eframe, real slate-and-amber theme (not a
copy of the old Python version's Catppuccin Mocha look).

This is a full rewrite, not a port. The old Python/PyQt6 version is
still on the `main` branch if you want it; this branch replaces it
going forward.

## Why the rewrite

The Python version's dashboard has to decode 25+ anime cover jpegs
just to render the Discover tab, and that decode cost adds up fast in
an interpreted language. Going systems-mode: Rust for this, Go/Zig
staying in rotation for whatever else needs them. Confirmed the actual
perf gap before writing that as a claim: the same egui code, unoptimized
debug build vs. release build, showed multi-second slow-frame stalls
on startup in debug and zero in release -- compiled-and-optimized is
the part python can never do for cpu-bound work like image decode.

## Sources

- **anidb.app** (default) -- scraped directly, needs `curl_chrome136`
  (curl-impersonate) on PATH since anidb rejects plain curl's TLS
  fingerprint with a 403.
- **aniwatchtv.to (yuma)** -- search and episode listing work, scraped
  directly. Stream resolution is **not implemented**: the site's embed
  needs a per-request client key plus a CryptoJS-AES decrypt of the
  sources payload, both of which shift whenever the site's own JS
  changes. Reimplementing a moving-target crypto scheme from scratch
  wasn't worth faking; genuinely unimplemented, not a stub pretending
  to work. Tracked in `TODO.txt`.
- **nyaa.si** (torrent) -- RSS search, no scraping, no anti-bot.
  Streams via sequential download through qbittorrent-nox once enough
  has buffered (first/last-piece priority), or downloads fully for
  offline viewing.

## Players

- **mpv** -- driven over its JSON IPC socket (Unix domain socket on
  Linux/macOS, named pipe on Windows).
- **VLC** -- driven over its HTTP control interface, fresh random port
  and password every launch so a stale leftover VLC process can never
  get silently polled instead of the current one.

Switchable from the top bar at any time.

## Downloads / offline

ffmpeg remuxes HLS to mp4 (same approach `ani-cli` itself uses for
`-d`/`--download`), progress parsed off ffmpeg's own stderr,
cancellable. A Downloads tab shows in-progress jobs and a browsable
library of what's already saved, playable with no source or network
needed at all.

## AniList tracker sync

Optional, off by default. When enabled, finishing an episode pushes
your progress to your AniList list. One-time setup: create a free
client at
[anilist.co/settings/developer](https://anilist.co/settings/developer)
with redirect URL `https://anilist.co/api/v2/oauth/pin`, paste the
client id in, open the authorize page, paste the token back. No local
webserver or redirect handler needed. Currently one-way (local watch
pushes to AniList, no pulling an existing list).

## Discord Rich Presence

Shows "Browsing aniani" on launch and while picking something, then
the full watching state (cover art, elapsed time, a "View on AniList"
button) during playback. Same shared Discord application id the
Python version used, works with zero setup.

## Continue watching

Built from local watch history, covers fetched from Jikan first (MAL's
own image CDN) with an AniList GraphQL fallback if Jikan's having one
of its documented outages. Clicking a continue-watching card checks if
that episode is already downloaded and plays it straight from disk if
so, instead of re-resolving a live stream.

## Setup

Needs `curl_chrome136` (curl-impersonate, for anidb.app), `mpv` and/or
`vlc`, `qbittorrent-nox` (for the nyaa.si source), and `ffmpeg` (for
downloads) on PATH.

```
cargo build --release
```

## Platform support

Built and daily-driven on Linux (Arch/Hyprland). Windows support
exists in the source -- `platform.rs` has the Windows state/download
dirs, mpv named-pipe IPC, and vlc/mpv/qbittorrent install-path
fallbacks -- but **hasn't actually been compiled or run on Windows by
hand yet**, only through this repo's own GitHub Actions Windows
runner. Treat it as "should build, needs a real Windows machine to
actually trust." `qbittorrent-nox` has no Windows build, falls back to
the regular qBittorrent GUI (same WebUI API, just shows a window).

macOS: untested, not a current target. The `dirs`-crate-based paths
and the Unix-socket mpv IPC path should carry over as-is; VLC/mpv
binary discovery would need `/Applications` fallbacks added.

## What's left

See `TODO.txt` -- yuma stream resolution, resume-seek-bar/volume/speed
controls in the UI (backend methods exist, not wired to a widget),
two-way AniList sync, the compact/floating-panel mode the Python
version had.
