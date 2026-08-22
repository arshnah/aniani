#!/usr/bin/env python3
"""Standalone Discord RPC updater, spawned by gui.py on close so presence
keeps showing while the player (VLC/mpv) keeps running on its own.
Reads which show/episode is playing from state.py's last_session (written
by the GUI right before playback starts) rather than parsing it back out
of the player, since VLC and mpv expose title metadata very differently.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "player"))

import discord_presence
import platform_utils
import state
import vlc_backend
import mpv_backend


def watchdog_main(gui_pid, daemon_pid_path, poll):
    # closeEvent()/the SIGTERM handler are the normal cleanup path, but
    # they only run if the GUI process gets a chance to react at all --
    # confirmed directly that some window-close paths (a custom Hyprland
    # keybind routed through a Lua dispatcher, not the plain killactive
    # SIGTERM this app otherwise handles fine) kill the process without
    # delivering anything catchable, leaving Discord's presence panel
    # stuck showing "Browsing aniani" forever with nothing left alive to
    # clear it. This process doesn't depend on the GUI cooperating at
    # all -- it just watches the pid from the outside and cleans up
    # once it's gone, whatever killed it.
    print(f"[rpc_daemon] watchdog started, watching gui pid={gui_pid}", flush=True)
    while platform_utils.pid_alive(gui_pid):
        time.sleep(poll)
    print("[rpc_daemon] watchdog: gui pid gone", flush=True)
    # give a clean shutdown's own disconnect (and a real playing-video
    # handoff spawning the real daemon above) a moment to land first --
    # only step in if neither happened.
    time.sleep(1.5)
    if os.path.exists(daemon_pid_path):
        try:
            with open(daemon_pid_path) as f:
                if platform_utils.pid_alive(int(f.read().strip())):
                    print("[rpc_daemon] watchdog: a real handoff daemon is already running -- nothing to do", flush=True)
                    return
        except (OSError, ValueError):
            pass
    presence = discord_presence.DiscordPresence()
    presence.disconnect()
    print("[rpc_daemon] watchdog: cleared stale presence", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["vlc", "mpv"])
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--watchdog", action="store_true")
    parser.add_argument("--gui-pid", type=int)
    parser.add_argument("--daemon-pid-path")
    args = parser.parse_args()

    if args.watchdog:
        watchdog_main(args.gui_pid, args.daemon_pid_path, args.poll)
        return

    player = vlc_backend.VlcPlayer() if args.backend == "vlc" else mpv_backend.MpvPlayer()
    if args.backend == "mpv":
        player._connect()  # mpv needs an explicit IPC connect; vlc is just HTTP requests

    presence = discord_presence.DiscordPresence()
    print(f"[rpc_daemon] started, backend={args.backend}", flush=True)

    while True:
        # the GUI panel isn't running to catch this while we're the only
        # thing watching -- e.g. VLC's -I dummy process doesn't exit on
        # its own when its window is closed via SUPER+Q (confirmed
        # directly), so without this it'd linger as an orphan forever
        # with nothing left to clean it up.
        if hasattr(player, "window_gone") and player.window_gone():
            print("[rpc_daemon] window_gone() -- stopping and exiting", flush=True)
            player.stop()
            presence.disconnect()
            return

        status = player.get_status()
        last = state.load_last_session()
        if not status or not last:
            print(f"[rpc_daemon] no status ({status is None}) or no last_session ({last is None}) -- disconnecting", flush=True)
            presence.disconnect()
            time.sleep(args.poll)
            continue
        ok = presence.watching(
            last["anime_title"], last["ep_no"],
            status["time"], status["duration"], status["paused"],
        )
        if not ok:
            print("[rpc_daemon] presence.watching() reported not connected -- see connect errors above", flush=True)
        elif int(time.time()) % 60 < args.poll:
            # a successful update is otherwise completely silent -- one
            # heartbeat line a minute confirms the daemon is alive and
            # actually posting, without spamming the log every poll tick.
            print(f"[rpc_daemon] presence OK: {last['anime_title']} ep {last['ep_no']}", flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
