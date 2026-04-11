"""External character pack discovery — drop a folder under
``~/.local/share/catai/characters/`` and CATAI auto-loads it as a
new playable cat at startup.

This is the plugin API tier of issue #9: lets community contributors
ship their own catset characters without modifying the core code.
Each pack is a self-contained folder shaped like the bundled
``catai_linux/cat01/`` etc. directories, plus a small ``personality.json``
that describes the character's name, traits, and chat prompt.

Pack layout::

    ~/.local/share/catai/characters/
    └── my_pirate_cat/
        ├── metadata.json         # same schema as bundled cats
        ├── personality.json      # name + traits + prompt
        ├── rotations/
        │   ├── south.png
        │   ├── east.png
        │   └── west.png
        └── animations/
            ├── running-8-frames/
            │   ├── east/frame_000.png ... frame_007.png
            │   └── west/...
            ├── flat/south/frame_000.png
            ├── love/south/...
            ...

``personality.json`` schema::

    {
      "char_id": "my_pirate_cat",
      "name": {"fr": "Barbe-Rousse", "en": "Redbeard", "es": "Barbarroja"},
      "traits": {
        "fr": "aventurier et bruyant",
        "en": "adventurous and loud",
        "es": "aventurero y ruidoso"
      },
      "skills": {
        "fr": "Tu racontes des histoires de trésors et de tempêtes.",
        "en": "You tell tales of treasure and storms.",
        "es": "..."
      },
      "tts_voice": {
        "speaker_id": 1,
        "length_scale": 1.05
      }
    }

Validation is strict: a missing key, a wrong char_id, or a malformed
JSON file means the pack is silently skipped (with a warning in the
log) — never crash the app on a bad pack. The CHARACTERS_DIR path is
respected for the test suite via patching.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("catai")

CHARACTERS_DIR = os.path.expanduser("~/.local/share/catai/characters")

# Required top-level keys in personality.json
_REQUIRED_KEYS = ("char_id", "name", "traits", "skills")
# At least one of the supported languages must be present in each
# multi-lang dict (we'll fall back to French at runtime).
_SUPPORTED_LANGS = ("fr", "en", "es")


def discover_packs(base_dir: str | None = None) -> dict[str, dict]:
    """Scan ``base_dir`` (defaults to ``CHARACTERS_DIR``) and return a
    dict mapping ``char_id`` → personality dict augmented with two
    extra keys:

      ``_external_dir``  — absolute path to the pack folder (used by
                           ``CatInstance.setup`` to find sprites)
      ``_pack_name``     — the directory basename, for log messages

    Returns an empty dict if the base directory doesn't exist or is
    empty. Invalid packs are skipped with a warning, never raised."""
    base = base_dir or CHARACTERS_DIR
    if not os.path.isdir(base):
        return {}
    found: dict[str, dict] = {}
    for entry in sorted(os.listdir(base)):
        pack_dir = os.path.join(base, entry)
        if not os.path.isdir(pack_dir):
            continue
        result = _load_pack(pack_dir)
        if result is None:
            continue
        char_id, perso = result
        if char_id in found:
            log.warning("Character pack: duplicate char_id %r — skipping %s",
                        char_id, pack_dir)
            continue
        found[char_id] = perso
        log.info("Character pack loaded: %s (char_id=%s)", entry, char_id)
    return found


def _load_pack(pack_dir: str) -> tuple[str, dict] | None:
    """Validate one pack folder. Returns ``(char_id, personality)`` on
    success, None on any validation error."""
    name = os.path.basename(pack_dir)
    perso_path = os.path.join(pack_dir, "personality.json")
    meta_path = os.path.join(pack_dir, "metadata.json")
    if not os.path.isfile(perso_path):
        log.debug("pack %s: missing personality.json — skipping", name)
        return None
    if not os.path.isfile(meta_path):
        log.warning("pack %s: missing metadata.json — skipping", name)
        return None
    try:
        with open(perso_path) as f:
            perso = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("pack %s: bad personality.json (%s)", name, e)
        return None
    if not isinstance(perso, dict):
        log.warning("pack %s: personality.json is not a dict", name)
        return None
    # Required top-level keys
    missing = [k for k in _REQUIRED_KEYS if k not in perso]
    if missing:
        log.warning("pack %s: missing required keys %s", name, missing)
        return None
    # char_id must match the directory name (sanity guard against
    # accidental copy-paste mismatches)
    if perso["char_id"] != name:
        log.warning("pack %s: char_id=%r doesn't match directory name",
                    name, perso["char_id"])
        return None
    # Each multi-lang dict must have at least one supported language
    for k in ("name", "traits", "skills"):
        v = perso.get(k)
        if not isinstance(v, dict):
            log.warning("pack %s: %s must be a dict of {lang: text}", name, k)
            return None
        if not any(lang in v for lang in _SUPPORTED_LANGS):
            log.warning("pack %s: %s has no supported language (need one of %s)",
                        name, k, _SUPPORTED_LANGS)
            return None
    # Sprites: rotations/south.png is the bare minimum (the catset
    # loader needs at least one rotation to instantiate the cat)
    rot_dir = os.path.join(pack_dir, "rotations")
    if not os.path.isdir(rot_dir):
        log.warning("pack %s: missing rotations/ directory", name)
        return None
    # Tag with the absolute pack dir so CatInstance.setup knows where
    # to load sprites from
    perso["_external_dir"] = os.path.abspath(pack_dir)
    perso["_pack_name"] = name
    return perso["char_id"], perso


def is_external(personality: dict) -> bool:
    """Return True if a CATSET_PERSONALITIES entry came from an external
    pack (used by CatInstance.setup to pick the sprite directory)."""
    return "_external_dir" in personality


def external_sprite_dir(personality: dict) -> str | None:
    """Return the absolute pack directory for an external personality,
    or None for bundled characters."""
    return personality.get("_external_dir")
