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
import state
import vlc_backend
import mpv_backend


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["vlc", "mpv"], required=True)
    parser.add_argument("--poll", type=float, default=5.0)
    args = parser.parse_args()

    player = vlc_backend.VlcPlayer() if args.backend == "vlc" else mpv_backend.MpvPlayer()
    if args.backend == "mpv":
        player._connect()  # mpv needs an explicit IPC connect; vlc is just HTTP requests

    presence = discord_presence.DiscordPresence()

    while True:
        status = player.get_status()
        last = state.load_last_session()
        if not status or not last:
            presence.disconnect()
            time.sleep(args.poll)
            continue
        presence.watching(
            last["anime_title"], last["ep_no"],
            status["time"], status["duration"], status["paused"],
        )
        time.sleep(args.poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
