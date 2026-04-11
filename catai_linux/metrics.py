"""Local metrics — opt-in usage stats stored in ~/.config/catai/stats.json.

100% local, **never** transmitted anywhere. The point isn't analytics
for us — it's a small "your stats" panel for the user themselves, the
kind of thing that creates emotional attachment to a desktop pet
("I've petted Mandarine 247 times").

Design goals:
    - **Opt-in**. Off by default; the settings UI has a checkbox.
      Once enabled, every relevant event flows through ``track()``
      and updates the JSON file in place.
    - **Tiny**. ~150 lines, no deps beyond stdlib.
    - **Robust**. Corrupt JSON → reset to defaults, never crash.
    - **Forward-compatible**. New event keys are added with ``setdefault``
      so old stats files keep working after upgrades.
    - **Privacy-first**. The on-disk format is human-readable JSON;
      the user can audit / delete / port it. There is a "Reset stats"
      button in the settings UI to nuke everything.

Tracked events:
    chat_sent             — every time the user sends a chat to a cat
    voice_recording       — every successful voice transcription
    egg_triggered/<key>   — counts per easter-egg key (nyan, apocalypse, …)
    love_encounter/<kind> — love | surprised | angry
    kitten_born           — increment when a love encounter produces a kitten
    pet_session           — increment when a petting session ends
    per_cat/<id>/<event>  — per-cat counters (chats, pets)
    session/start         — first_run, total_sessions, total_session_minutes

Usage from elsewhere:
    from catai_linux import metrics
    metrics.track("chat_sent", cat_id="cat_orange")
    metrics.track("egg_triggered", key="nyan")

The track() function is a no-op if the user hasn't opted in, so
callers don't need to gate their calls — sprinkle them anywhere.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger("catai")

STATS_FILE = os.path.expanduser("~/.config/catai/stats.json")

# Module-level enabled flag — flipped from CatAIApp at startup based
# on the config.json key. Calls to track() return immediately when
# False, so the function is a no-op for users who never opt in.
_enabled = False
_session_start_ts: float | None = None


def set_enabled(flag: bool) -> None:
    """Toggle stats tracking globally. Called once from CatAIApp during
    do_activate based on the config.json `metrics_enabled` key, and
    again when the user toggles the settings checkbox."""
    global _enabled, _session_start_ts
    was = _enabled
    _enabled = bool(flag)
    if _enabled and not was:
        _session_start_ts = time.monotonic()
        _bump_session_start()
    elif was and not _enabled:
        _flush_session_minutes()
        _session_start_ts = None


def is_enabled() -> bool:
    return _enabled


# ── Defaults ────────────────────────────────────────────────────────────────


def _default_stats() -> dict:
    return {
        "version": 1,
        "first_run": datetime.now(timezone.utc).isoformat(),
        "total_sessions": 0,
        "total_session_minutes": 0,
        "chats_sent": 0,
        "voice_recordings": 0,
        "easter_eggs_triggered": {},
        "love_encounters": {"love": 0, "surprised": 0, "angry": 0},
        "kittens_born": 0,
        "pet_sessions": 0,
        "per_cat": {},
    }


# ── Load / save ─────────────────────────────────────────────────────────────


def load() -> dict:
    """Read stats.json or return a fresh default. Tolerates corruption
    by silently resetting — we never want a bad stats file to break
    a launch."""
    try:
        with open(STATS_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        # Forward-compat: fill in any keys added in newer versions
        defaults = _default_stats()
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except (OSError, ValueError):
        return _default_stats()


def save(data: dict) -> None:
    """Atomic write — temp + rename — so a crash mid-write can't
    corrupt the existing file."""
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATS_FILE)
    except OSError:
        log.debug("metrics: save failed", exc_info=True)


def reset() -> None:
    """Wipe stats.json back to defaults. Triggered by the settings
    'Reset stats' button."""
    save(_default_stats())


# ── Tracking ────────────────────────────────────────────────────────────────


def track(event: str, **kwargs) -> None:
    """No-op if metrics are disabled. Otherwise update the stats file
    in place. Recognized events:

      chat_sent          (cat_id=...)
      voice_recording
      egg_triggered      (key=...)
      love_encounter     (kind='love'|'surprised'|'angry')
      kitten_born
      pet_session        (cat_id=...)
    """
    if not _enabled:
        return
    try:
        data = load()
        if event == "chat_sent":
            data["chats_sent"] += 1
            cat_id = kwargs.get("cat_id")
            if cat_id:
                bucket = data["per_cat"].setdefault(cat_id, {})
                bucket["chats"] = bucket.get("chats", 0) + 1
        elif event == "voice_recording":
            data["voice_recordings"] += 1
        elif event == "egg_triggered":
            key = kwargs.get("key", "unknown")
            eggs = data["easter_eggs_triggered"]
            eggs[key] = eggs.get(key, 0) + 1
        elif event == "love_encounter":
            kind = kwargs.get("kind", "love")
            if kind in data["love_encounters"]:
                data["love_encounters"][kind] += 1
        elif event == "kitten_born":
            data["kittens_born"] += 1
        elif event == "pet_session":
            data["pet_sessions"] += 1
            cat_id = kwargs.get("cat_id")
            if cat_id:
                bucket = data["per_cat"].setdefault(cat_id, {})
                bucket["petted"] = bucket.get("petted", 0) + 1
        else:
            log.debug("metrics: unknown event %r", event)
            return
        save(data)
    except Exception:
        log.debug("metrics: track(%r) failed", event, exc_info=True)


def _bump_session_start() -> None:
    """Increment the session counter on app launch."""
    if not _enabled:
        return
    try:
        data = load()
        data["total_sessions"] += 1
        save(data)
    except Exception:
        log.debug("metrics: session start bump failed", exc_info=True)


def _flush_session_minutes() -> None:
    """Add the elapsed minutes since the session start to the total.
    Called on settings-toggle off and on shutdown."""
    if _session_start_ts is None:
        return
    try:
        elapsed_min = max(0, int((time.monotonic() - _session_start_ts) / 60))
        if elapsed_min == 0:
            return
        data = load()
        data["total_session_minutes"] += elapsed_min
        save(data)
    except Exception:
        log.debug("metrics: session minute flush failed", exc_info=True)


def shutdown() -> None:
    """Called from CatAIApp.do_shutdown to flush the final session
    duration before the process exits."""
    _flush_session_minutes()


# ── Read helpers for the settings UI ────────────────────────────────────────


def top_cats(data: dict, key: str = "petted", n: int = 3) -> list[tuple[str, int]]:
    """Return the top N cats by a per-cat counter (e.g. 'petted',
    'chats'). Used by the settings panel."""
    pairs = [
        (cat_id, bucket.get(key, 0))
        for cat_id, bucket in data.get("per_cat", {}).items()
    ]
    pairs.sort(key=lambda p: -p[1])
    return [p for p in pairs[:n] if p[1] > 0]


def top_eggs(data: dict, n: int = 3) -> list[tuple[str, int]]:
    """Return the top N most-triggered easter eggs."""
    pairs = list(data.get("easter_eggs_triggered", {}).items())
    pairs.sort(key=lambda p: -p[1])
    return pairs[:n]
