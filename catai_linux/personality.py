"""Personality drift — slow, LLM-driven evolution of a cat's traits.

Every N chat exchanges we ask the AI backend to reflect on the recent
conversation history and emit 1 new short "quirk" the cat has picked up
from the user. Quirks accumulate into a per-cat list persisted to
``~/.config/catai/personality_<cat_id>.json``. At chat setup the list
is appended to the static system prompt so the cat visibly grows into
its history over many sessions, without us needing embeddings, vector
stores, RAG, or any other heavy ML infra.

Design goals:
    - No embeddings. Pure prompt → JSON response.
    - Non-blocking. Drift calls run in a background thread.
    - Bounded growth. We keep at most ``MAX_TRAITS`` quirks, oldest
      popped when the list overflows.
    - Cheap. Drift fires only every ``DRIFT_EVERY_MESSAGES`` chat turns.
    - Opt-out via ``config.json`` key ``"personality_drift": false``.

The reflection prompt carries the sentinel ``[CATAI_PERSONALITY_DRIFT]``
so MockChat (CI e2e) can recognize it and return a canned JSON string.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("catai")

# Number of (user, assistant) message pairs between drift attempts. At the
# default of 10 this is roughly "every serious conversation", while still
# being frequent enough that the user notices changes over a week of use.
DRIFT_EVERY_MESSAGES = 10

# Upper bound on drifted traits — older quirks fall off the list when a
# new one is appended. Keeps the system prompt under control.
MAX_TRAITS = 5

# Marker the reflection prompt starts with, so MockChat and any future
# test/debug harness can spot a drift request without doing text search.
DRIFT_PROMPT_MARKER = "[CATAI_PERSONALITY_DRIFT]"

# Where per-cat personality state lives (under the app's CONFIG_DIR). The
# path is resolved lazily so the test suite can override HOME.
_CONFIG_SUBDIR = os.path.expanduser("~/.config/catai")


def _path_for(cat_id: str) -> str:
    return os.path.join(_CONFIG_SUBDIR, f"personality_{cat_id}.json")


@dataclass
class PersonalityState:
    """Per-cat personality drift state persisted across sessions."""

    cat_id: str
    drifted_traits: list[str] = field(default_factory=list)
    message_count: int = 0
    last_drift_at: float = 0.0  # unix ts, 0 = never drifted

    # ── Persistence ─────────────────────────────────────────────────────

    @classmethod
    def load(cls, cat_id: str) -> PersonalityState:
        """Load state from disk. Returns a fresh default on any error —
        a corrupted file never blocks the app from starting."""
        path = _path_for(cat_id)
        if not os.path.exists(path):
            return cls(cat_id=cat_id)
        try:
            with open(path) as f:
                data = json.load(f)
            return cls(
                cat_id=cat_id,
                drifted_traits=[str(t) for t in (data.get("drifted_traits") or [])][:MAX_TRAITS],
                message_count=int(data.get("message_count", 0)),
                last_drift_at=float(data.get("last_drift_at", 0.0)),
            )
        except (OSError, ValueError, TypeError) as e:
            log.warning("Corrupted personality state for %s: %s", cat_id, e)
            return cls(cat_id=cat_id)

    def save(self) -> None:
        os.makedirs(_CONFIG_SUBDIR, exist_ok=True)
        path = _path_for(self.cat_id)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({
                    "drifted_traits": self.drifted_traits,
                    "message_count": self.message_count,
                    "last_drift_at": self.last_drift_at,
                }, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Failed to save personality state for %s: %s", self.cat_id, e)

    # ── Prompt integration ──────────────────────────────────────────────

    def append_to_prompt(self, base_prompt: str, lang: str) -> str:
        """Return ``base_prompt`` with the drifted quirks appended. No-op
        if there are no quirks yet (the cat hasn't talked enough)."""
        if not self.drifted_traits:
            return base_prompt
        quirks = ", ".join(self.drifted_traits)
        if lang == "en":
            suffix = f" Over time you've picked up these quirks from the user: {quirks}."
        elif lang == "es":
            suffix = f" Con el tiempo has adquirido estas manías del usuario: {quirks}."
        else:
            suffix = f" Au fil du temps tu as pris ces petites manies avec l'utilisateur : {quirks}."
        return base_prompt + suffix

    # ── Drift scheduling ────────────────────────────────────────────────

    def on_message_added(self) -> None:
        """Called once per user→assistant round-trip."""
        self.message_count += 1

    def should_drift(self) -> bool:
        """True when we've accumulated enough messages for another drift
        attempt. Caller is responsible for kicking off the background
        thread and updating ``last_drift_at`` on success."""
        return (
            self.message_count > 0
            and self.message_count % DRIFT_EVERY_MESSAGES == 0
        )

    def apply_drift(self, new_trait: str) -> None:
        """Append ``new_trait`` to the quirks, trimming the oldest if we
        overflow MAX_TRAITS. Also stamps ``last_drift_at``."""
        cleaned = new_trait.strip().strip('"\'.').strip()
        if not cleaned or len(cleaned) > 80:
            return
        # Dedup (case-insensitive) — if the model keeps proposing the
        # same trait, we don't want to fill the list with duplicates.
        lower = cleaned.lower()
        if any(t.lower() == lower for t in self.drifted_traits):
            return
        self.drifted_traits.append(cleaned)
        if len(self.drifted_traits) > MAX_TRAITS:
            self.drifted_traits = self.drifted_traits[-MAX_TRAITS:]
        self.last_drift_at = time.time()


# ── Drift engine ─────────────────────────────────────────────────────────────

def build_reflection_prompt(cat_name: str, base_traits: str, lang: str,
                             existing_quirks: list[str]) -> str:
    """Build the system prompt we send to the reflection backend."""
    lang_name = {"fr": "French", "en": "English", "es": "Spanish"}.get(lang, "French")
    existing = ", ".join(existing_quirks) if existing_quirks else "(none yet)"
    return (
        f"{DRIFT_PROMPT_MARKER}\n"
        f"You are a meta-analysis assistant, not the cat. Your job is to "
        f"reflect on a recent conversation with a user and propose ONE "
        f"short new personality quirk that the cat {cat_name} has picked "
        f"up from the user during this chat.\n\n"
        f"Base personality: {base_traits}\n"
        f"Existing drifted quirks: {existing}\n\n"
        f"Output EXACTLY a JSON object with a single key 'trait' whose "
        f"value is a short {lang_name} adjective or noun phrase, less "
        f"than 60 characters, NOT already in the existing quirks. No "
        f"markdown, no prose, no explanation — just the raw JSON.\n"
        f"Example: {{\"trait\": \"aime parler de jardinage\"}}"
    )


def parse_drift_response(raw: str) -> str | None:
    """Extract a single new trait from an LLM response. Handles JSON,
    code-fenced JSON, and bare string fallbacks."""
    if not raw:
        return None
    # Strip markdown fences
    cleaned = re.sub(r"^\s*```(?:json|JSON)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()
    # Attempt JSON first
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "trait" in obj:
            val = obj["trait"]
            if isinstance(val, str) and val.strip():
                return val.strip()
    except (ValueError, TypeError):
        pass
    # Second: find the first {...} substring
    match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict) and "trait" in obj:
                val = obj["trait"]
                if isinstance(val, str) and val.strip():
                    return val.strip()
        except (ValueError, TypeError):
            pass
    # Last resort: the whole response is one short phrase
    if 2 <= len(cleaned) <= 80 and "\n" not in cleaned:
        return cleaned.strip('"\'.')
    return None


_drift_lock = threading.Lock()
_drift_in_flight: set[str] = set()


def drift_async(state: PersonalityState, cat_name: str, base_traits: str,
                lang: str, create_chat_fn, model: str) -> None:
    """Kick off a background drift — idempotent per cat_id: if a drift
    is already in flight for this cat, returns immediately."""
    with _drift_lock:
        if state.cat_id in _drift_in_flight:
            return
        _drift_in_flight.add(state.cat_id)

    def _run() -> None:
        try:
            prompt = build_reflection_prompt(
                cat_name, base_traits, lang, state.drifted_traits)
            backend = create_chat_fn(model)
            backend.messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Emit the JSON object now."},
            ]
            chunks = []
            for chunk in backend._stream_chunks():
                chunks.append(chunk)
            raw = "".join(chunks)
            trait = parse_drift_response(raw)
            if trait:
                state.apply_drift(trait)
                state.save()
                log.info("Personality drift applied for %s: %r (%d total)",
                         state.cat_id, trait, len(state.drifted_traits))
            else:
                log.warning("Personality drift parse failed for %s: raw=%r",
                            state.cat_id, raw[:200])
        except Exception:
            log.exception("Personality drift crashed for %s", state.cat_id)
        finally:
            with _drift_lock:
                _drift_in_flight.discard(state.cat_id)

    threading.Thread(target=_run, daemon=True).start()
