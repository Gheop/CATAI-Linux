"""Configuration schema and validation for CATAI.

Provides a declarative schema for every config key and a ``validate_config``
function that fills defaults, clamps numerics, and validates choice values
without ever raising — garbage in, sane defaults out.
"""
from __future__ import annotations

import logging

log = logging.getLogger("catai")

# Each entry: type, default, optional min/max, optional choices list.
# "type" is the Python builtin used for coercion (int, float, str, bool, list).
CONFIG_SCHEMA: dict[str, dict] = {
    "scale": {
        "type": float,
        "default": 1.5,
        "min": 0.5,
        "max": 4.0,
    },
    "model": {
        "type": str,
        "default": "gemma3:1b",
    },
    "lang": {
        "type": str,
        "default": "fr",
        "choices": ["fr", "en", "es", "de", "it", "pt", "ja", "ko", "zh"],
    },
    "encounters": {
        "type": bool,
        "default": True,
    },
    "seasonal": {
        "type": bool,
        "default": True,
    },
    "seasonal_duration_sec": {
        "type": int,
        "default": 30,
        "min": 0,
        "max": 3600,
    },
    "personality_drift": {
        "type": bool,
        "default": True,
    },
    "tts_enabled": {
        "type": bool,
        "default": False,
    },
    "tts_cat_sounds_enabled": {
        "type": bool,
        "default": True,
    },
    "auto_update": {
        "type": str,
        "default": "auto",
        "choices": ["auto", "notify", "off"],
    },
    "metrics_enabled": {
        "type": bool,
        "default": False,
    },
    "api_enabled": {
        "type": bool,
        "default": False,
    },
    "long_term_memory": {
        "type": bool,
        "default": True,
    },
    "voice_enabled": {
        "type": bool,
        "default": False,
    },
    "voice_model": {
        "type": str,
        "default": "base",
        "choices": ["tiny", "base", "small", "medium", "large-v3"],
    },
    "wake_word_enabled": {
        "type": bool,
        "default": False,
    },
    "wake_word_ack_sound": {
        "type": bool,
        "default": True,
    },
    "cats": {
        "type": list,
        "default": [],
    },
}


def validate_config(cfg: dict) -> dict:
    """Validate and normalise a raw config dict against CONFIG_SCHEMA.

    * Missing keys are filled with their defaults.
    * Numeric values are clamped to [min, max] when bounds exist.
    * Choice values fall back to the default on invalid input.
    * Unknown keys are kept (forward-compat) but logged as warnings.
    * Never raises — tolerates garbage gracefully.
    """
    out: dict = {}

    for key, spec in CONFIG_SCHEMA.items():
        expected_type = spec["type"]
        default = spec["default"]

        if key not in cfg:
            out[key] = default
            continue

        raw = cfg[key]

        # --- type coercion / check ---
        try:
            if expected_type is bool:
                # bool must be checked before int (bool is subclass of int)
                if not isinstance(raw, bool):
                    raw = bool(raw)
            elif expected_type is int:
                raw = int(raw)
            elif expected_type is float:
                raw = float(raw)
            elif expected_type is str:
                raw = str(raw)
            elif expected_type is list:
                if not isinstance(raw, list):
                    log.warning("Config key %r: expected list, got %s — using default", key, type(raw).__name__)
                    out[key] = default
                    continue
        except (ValueError, TypeError):
            log.warning("Config key %r: cannot coerce %r to %s — using default", key, raw, expected_type.__name__)
            out[key] = default
            continue

        # --- choices ---
        choices = spec.get("choices")
        if choices is not None and raw not in choices:
            log.warning("Config key %r: invalid value %r (expected one of %r) — using default", key, raw, choices)
            out[key] = default
            continue

        # --- min/max clamping ---
        vmin = spec.get("min")
        vmax = spec.get("max")
        if vmin is not None and raw < vmin:
            log.warning("Config key %r: value %r below min %r — clamping", key, raw, vmin)
            raw = vmin
        if vmax is not None and raw > vmax:
            log.warning("Config key %r: value %r above max %r — clamping", key, raw, vmax)
            raw = vmax

        out[key] = raw

    # Keep unknown keys (forward-compat) but warn
    for key in cfg:
        if key not in CONFIG_SCHEMA:
            log.warning("Config: unknown key %r (kept as-is)", key)
            out[key] = cfg[key]

    return out
