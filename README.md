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
  (curl-impersonate) since anidb rejects plain curl's TLS fingerprint
  with a 403. Checked for on PATH first, then next to the aniani
  binary itself -- so dropping `curl_chrome136`/`curl_chrome136.exe`
  in the same folder as `aniani`/`aniani.exe` works with no PATH setup.
  There's no official prebuilt Windows binary for this; the closest
  thing is [yuzd/curl-impersonate-win](https://github.com/yuzd/curl-impersonate-win),
  which needs building from source via MSYS2 (a `build.bat` script,
  no ready-made `.exe`). On Linux/macOS the upstream
  [lwthiker/curl-impersonate](https://github.com/lwthiker/curl-impersonate)
  releases page has real prebuilt binaries. Without it, anidb search
  just fails silently in the UI right now (logged to
  `debug.log` as `anidb_search(...) failed: ...`) -- yuma and nyaa
  don't need it and work regardless.
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

Two-way. Optional push, off by default: when enabled, finishing an
episode pushes your progress to your AniList list. One-time setup:
create a free client at
[anilist.co/settings/developer](https://anilist.co/settings/developer)
with redirect URL `https://anilist.co/api/v2/oauth/pin`, paste the
client id in, open the authorize page, paste the token back. No local
webserver or redirect handler needed. Pull happens automatically once
connected: your CURRENT/REPEATING list shows up as its own row on the
Discover tab, click one to jump to search. No in-app editing of AniList
entries beyond what playing an episode already pushes.

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

## Closing the window / running in the background

Closing the window hides it instead of quitting -- Discord presence and
playback keep running. Launching `aniani`/`aniani.exe` again while it's
already running doesn't open a second window, it just un-hides and
focuses the existing one (an `fd-lock`-based single-instance lock plus
a signal file, checked in `platform.rs`/`main.rs`). This is not a real
system tray icon -- there's no click-to-restore from a tray area, the
only way back in right now is relaunching the exe. A proper tray icon
would need the `tray-icon` crate wired into eframe's own winit event
loop, a bigger integration not attempted here.

## VLC window-close handling (Linux/Hyprland)

VLC's `-I dummy` interface means its process lifecycle isn't tied to
its own video window -- closing that window directly (e.g. Super+Q)
leaves the process running as an orphan otherwise. On Hyprland, the
worker thread polls `hyprctl clients -j` to notice when a window it
saw has gone missing while the process is still alive, and cleans it
up the same as a normal stop. Also reattaches to an already-running
VLC instance after a relaunch, instead of always starting from a blank
slate, by reading the port/pid/password a previous run wrote to shared
temp files. Windows/macOS get neither -- `hyprctl` doesn't exist
there, so this safely does nothing rather than breaking anything.

## Other extras

Update checker (pings this repo's GitHub releases API on launch, shows
a banner if newer), a disk-backed cover image cache (`cover_cache.json`
in the state dir, so covers aren't re-fetched over the network on every
launch), keyboard shortcuts while playing (space to pause, left/right
arrows to seek ±10s, `/` or ctrl/cmd+F to jump to search), an anime
detail popup before committing to a source search (synopsis, genres,
score, episode count), batch episode selection + download, a full
History tab (all watch history, not just the last 20), and genre/sort
filtering on Discover. A panic hook writes crashes to `debug.log`
instead of vanishing with the hidden console window.

## Setup

Needs `curl_chrome136` (curl-impersonate, for anidb.app), `mpv` and/or
`vlc`, `qbittorrent-nox` (for the nyaa.si source), and `ffmpeg` (for
downloads) on PATH.

```
cargo build --release
```

## Platform support

Built and daily-driven on Linux (Arch/Hyprland). Windows now compiles
and runs on real hardware, confirmed on two separate machines, not
just via this repo's GitHub Actions Windows runner. `qbittorrent-nox`
has no Windows build, falls back to the regular qBittorrent GUI (same
WebUI API, just shows a window). Still only tested on a couple of
machines -- not broad hardware/driver coverage.

macOS: untested, not a current target. The `dirs`-crate-based paths
and the Unix-socket mpv IPC path should carry over as-is; VLC/mpv
binary discovery would need `/Applications` fallbacks added.

## What's left

See `TODO.txt` -- yuma stream resolution (not planned), a real system
tray icon with click-to-restore (currently: hide-on-close +
relaunch-to-restore, not a tray icon), macOS support (completely
untouched), and broader Windows hardware testing.
