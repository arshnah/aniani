"""Shared, source-agnostic state: continue-watching history, resume
positions, and last-session (for reattach-on-relaunch). Own state dir,
separate from ani-cli-discord-rpc's -- this is a different app.
"""
import json
import os

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "aniani",
)
HISTORY_PATH = os.path.join(STATE_DIR, "history.json")
POSITIONS_PATH = os.path.join(STATE_DIR, "positions.json")
LAST_SESSION_PATH = os.path.join(STATE_DIR, "last_session.json")

RESUME_MIN_SECONDS = 15
RESUME_END_MARGIN = 30


def _load(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _save(path, data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def read_history():
    """-> [{"source", "anime_id", "anime_title", "ep_no"}, ...]"""
    return _load(HISTORY_PATH) or []


def update_history(source, anime_id, anime_title, ep_no):
    entries = read_history()
    for e in entries:
        if e["source"] == source and e["anime_id"] == anime_id:
            e["ep_no"] = ep_no
            break
    else:
        entries.append({"source": source, "anime_id": anime_id, "anime_title": anime_title, "ep_no": ep_no})
    _save(HISTORY_PATH, entries)


def load_positions():
    return _load(POSITIONS_PATH) or {}


def save_positions(positions):
    _save(POSITIONS_PATH, positions)


def position_key(source, anime_title, ep_no):
    return f"{source}::{anime_title}::{ep_no}"


def load_last_session():
    return _load(LAST_SESSION_PATH)


def save_last_session(source, anime_id, anime_title, ep_no):
    _save(LAST_SESSION_PATH, {
        "source": source, "anime_id": anime_id, "anime_title": anime_title, "ep_no": ep_no,
    })
