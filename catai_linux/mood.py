"""Invisible mood system — 4 stats per cat that drift over time and bias
the behavior tick's random state transitions.

Design goals:
  - **Invisible by default**: the user never sees raw numbers, only the
    emergent behavior. Mandarine naturally starts sleeping more in the
    evening, Ombre gets irritable when neglected, Einstein bounces after
    a chat. No UI needed.
  - **Persistent across sessions**: stored in ``~/.config/catai/mood_<cat_id>.json``,
    auto-saved every 30 s and on shutdown. A cat that fell asleep yesterday
    wakes up rested.
  - **Zero performance cost**: the stats are just four floats per cat.
    update() is called once per 1 s behavior tick.
  - **Cheap to reason about**: all four stats live in ``[0, 100]``, decay
    is linear, recovery is linear with explicit multipliers.

Stats:
  happiness — how content this cat is. Decays slowly, boosted by petting,
              chat, love encounters. Affects whether it picks LOVE over
              ANGRY as a random idle action.
  energy    — how rested. Decays while active, recovers during
              SLEEPING_BALL / FLAT. Low energy biases toward rest.
  bored     — how understimulated. Grows over time, reset by any
              interaction. High bored biases toward active states
              (DASHING, JUMPING, CHASING_MOUSE).
  hunger    — placeholder for a future feeding action. Slowly grows,
              no recovery yet. Doesn't affect behavior (v1).

Usage from CatInstance:
    from catai_linux.mood import CatMood
    ...
    self.mood = CatMood.load(self.config["id"])
    # each behavior tick:
    self.mood.tick(self.state)
    # on petting start:
    self.mood.on_petting_start()
    # on chat sent:
    self.mood.on_chat()
    # on shutdown / periodic save:
    self.mood.save(self.config["id"])

All stats can be inspected via ``self.mood.snapshot()`` for the debug
socket command.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger("catai")

CONFIG_DIR = os.path.expanduser("~/.config/catai")


@dataclass
class CatMood:
    """Mutable in-memory mood state for a single cat."""

    happiness: float = 60.0
    energy: float = 80.0
    bored: float = 30.0
    hunger: float = 20.0
    last_update: float = field(default_factory=time.monotonic)
    # Tracks the start time of an ongoing petting session so we can
    # reward duration rather than just the start event.
    _petting_start: float | None = None

    # ── Tick-driven decay / recovery ─────────────────────────────────────────

    # Per-tick (1 s) deltas. Calibrated so a full day of neglect saturates
    # the stats to their extremes.
    HAPPINESS_DECAY_PER_SEC = 60.0 / (6 * 3600)   # full decay in 6h
    ENERGY_DECAY_PER_SEC = 80.0 / (8 * 3600)      # 8h awake
    BORED_GROWTH_PER_SEC = 70.0 / (2 * 3600)      # peaks in 2h
    HUNGER_GROWTH_PER_SEC = 80.0 / (12 * 3600)    # 12h to hungry

    # Recovery rates when the cat is doing the right thing.
    ENERGY_RECOVERY_SLEEPING_PER_SEC = 80.0 / 300  # 5 min sleep → full
    BORED_DECAY_WALKING_PER_SEC = 70.0 / 120       # 2 min walk clears it

    def tick(self, current_state_name: str) -> None:
        """Advance the stats by the elapsed wall-clock time since the last
        tick. Takes the current CatState value name (e.g. 'idle',
        'sleeping_ball') so recovery can be conditioned on it. Called from
        the 1 Hz behavior tick but handles irregular intervals correctly."""
        now = time.monotonic()
        dt = max(0.0, min(60.0, now - self.last_update))
        self.last_update = now
        if dt == 0:
            return

        # Base decay / growth (always active)
        self.happiness = _clamp(self.happiness - self.HAPPINESS_DECAY_PER_SEC * dt)
        self.energy = _clamp(self.energy - self.ENERGY_DECAY_PER_SEC * dt)
        self.bored = _clamp(self.bored + self.BORED_GROWTH_PER_SEC * dt)
        self.hunger = _clamp(self.hunger + self.HUNGER_GROWTH_PER_SEC * dt)

        # State-dependent recovery
        if current_state_name in ("sleeping_ball", "sleeping", "flat"):
            self.energy = _clamp(
                self.energy + self.ENERGY_RECOVERY_SLEEPING_PER_SEC * dt
            )
            # Can't get bored while napping
            self.bored = _clamp(self.bored - self.BORED_GROWTH_PER_SEC * dt)

        if current_state_name in ("walking", "dashing", "chasing_mouse", "jumping"):
            self.bored = _clamp(self.bored - self.BORED_DECAY_WALKING_PER_SEC * dt)

    # ── Event-driven bumps ───────────────────────────────────────────────────

    def on_petting_start(self) -> None:
        self._petting_start = time.monotonic()
        # Immediate small boost — the cat noticed being picked
        self.happiness = _clamp(self.happiness + 3.0)
        self.bored = _clamp(self.bored - 8.0)

    def on_petting_end(self) -> None:
        if self._petting_start is None:
            return
        duration = time.monotonic() - self._petting_start
        self._petting_start = None
        # Linear reward: 1 s of petting ≈ +2 happiness, capped at +20 for
        # a single session to prevent farming
        delta = min(20.0, 2.0 * duration)
        self.happiness = _clamp(self.happiness + delta)

    def on_chat_sent(self) -> None:
        """User sent the cat a message — treated as attention."""
        self.happiness = _clamp(self.happiness + 2.0)
        self.bored = _clamp(self.bored - 15.0)

    def on_love_encounter(self) -> None:
        self.happiness = _clamp(self.happiness + 10.0)

    def on_kitten_born(self) -> None:
        """Big happiness boost for new parents."""
        self.happiness = _clamp(self.happiness + 20.0)

    # ── Behavior biasing ─────────────────────────────────────────────────────

    def wants_rest(self) -> bool:
        """True when this cat is currently more likely to sleep than move."""
        return self.energy < 30.0

    def is_bored(self) -> bool:
        """True when this cat wants to do something active."""
        return self.bored > 70.0

    def is_grumpy(self) -> bool:
        """True when this cat's happiness is low enough to bias toward angry."""
        return self.happiness < 25.0

    def is_affectionate(self) -> bool:
        """True when this cat is more likely to pick LOVE randomly."""
        return self.happiness > 75.0

    def snapshot(self) -> dict[str, float]:
        """Plain dict snapshot for debugging / the socket probe."""
        return {
            "happiness": round(self.happiness, 1),
            "energy": round(self.energy, 1),
            "bored": round(self.bored, 1),
            "hunger": round(self.hunger, 1),
        }

    # ── Persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, cat_id: str) -> CatMood:
        """Load from ~/.config/catai/mood_<cat_id>.json. Returns a fresh
        default instance on any error (missing file, bad JSON, partial data)."""
        path = cls._path(cat_id)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls()
        # Only copy in keys we know, discard unknowns
        inst = cls()
        for k in ("happiness", "energy", "bored", "hunger"):
            if isinstance(data.get(k), (int, float)):
                setattr(inst, k, _clamp(float(data[k])))
        # Reset the timestamp so we don't double-decay the gap since last save
        inst.last_update = time.monotonic()
        inst._petting_start = None
        return inst

    def save(self, cat_id: str) -> None:
        """Atomic write to ~/.config/catai/mood_<cat_id>.json. Silent on
        failure — mood persistence is nice-to-have, not critical."""
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            path = self._path(cat_id)
            tmp = path + ".tmp"
            data = asdict(self)
            # Drop runtime-only fields from the JSON
            data.pop("last_update", None)
            data.pop("_petting_start", None)
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except OSError:
            log.debug("mood.save failed for %s", cat_id, exc_info=True)

    @staticmethod
    def _path(cat_id: str) -> str:
        return os.path.join(CONFIG_DIR, f"mood_{cat_id}.json")


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v
