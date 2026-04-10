"""Reaction pools — varied, AI-generated, per-cat, per-event one-liners.

When the app needs a short reactive line ("why are you shouting?", "ooh what's
that?", "going to nap...") — typically fired by a system event rather than by
the user's explicit message — we don't want to show the same hardcoded L10n
string every time. That's boring.

Instead, each (cat, event) pair gets a **lazy-filled pool of ~6 reactions**.
The first time an event fires for a cat, we ask its AI backend in the
background to generate 6 short replies matching the cat's personality. While
that generation is in flight, we return a deterministic L10n fallback. Once
the pool is ready, subsequent triggers pick a random element instantly.

Pools live in memory only — regenerated on every app restart so the cats
feel fresh each session. This also avoids having to ship a cache invalidation
strategy; the session boundary is the natural TTL.

Event names live as constants on `ReactionPool` (EVT_*) so callers never
pass stringly-typed keys.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading

from catai_linux.l10n import L10n

log = logging.getLogger("catai")

# Sentinel in the system prompt so MockChat (and anyone else) can detect
# that the caller is asking for a JSON reaction pool, not a chat response.
POOL_PROMPT_MARKER = "[CATAI_REACTION_POOL]"


class ReactionPool:
    """Lazy-filled cache of short reaction lines per (cat_id, event) pair."""

    # Canonical event names — add new constants here as new events are wired up.
    EVT_CAPSLOCK = "capslock_on"
    # Future:
    # EVT_NOTIFICATION = "notification_received"
    # EVT_IDLE_START   = "user_idle"
    # EVT_TYPING_FAST  = "typing_fast"

    POOL_SIZE = 6
    MAX_REPLY_LEN = 40  # chars — keep bubbles small

    def __init__(self, create_chat_fn, get_model_fn):
        """
        Args:
            create_chat_fn: callable(model: str) -> ChatBackend
                Usually ``catai_linux.chat_backend.create_chat``. Parameterized
                so tests can inject a mock.
            get_model_fn: callable() -> str
                Returns the currently-selected AI model. Usually a lambda
                over the app's ``self.selected_model``.
        """
        self._create_chat = create_chat_fn
        self._get_model = get_model_fn
        self._pools: dict[tuple[str, str], list[str]] = {}
        self._generating: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, cat, event: str) -> str:
        """Return a reaction line for (cat, event). Non-blocking.

        If the pool is filled, picks a random element. Otherwise kicks off a
        background generation and returns the L10n fallback immediately.
        """
        key = (cat.config["id"], event)
        with self._lock:
            pool = self._pools.get(key)
        if pool:
            return random.choice(pool)
        self._start_generation(cat, event, key)
        return self._fallback(event)

    def clear(self, cat_id: str | None = None) -> None:
        """Clear pools — for all cats if cat_id is None, else only that cat.
        Used when the user renames a cat, changes the model, or toggles
        personality drift (once #5 ships)."""
        with self._lock:
            if cat_id is None:
                self._pools.clear()
            else:
                self._pools = {k: v for k, v in self._pools.items() if k[0] != cat_id}

    # ── Background generation ────────────────────────────────────────────────

    def _start_generation(self, cat, event: str, key) -> None:
        with self._lock:
            if key in self._generating or key in self._pools:
                return
            self._generating.add(key)

        # Snapshot the data the background thread will need — never touch
        # the CatInstance concurrently from a worker thread.
        char_id = cat.config.get("char_id", "cat01")
        cat_name = cat.config.get("name", "Cat")
        model = self._get_model()
        lang = L10n.lang

        thread = threading.Thread(
            target=self._generate_bg,
            args=(key, char_id, cat_name, event, model, lang),
            daemon=True,
        )
        thread.start()

    def _generate_bg(self, key, char_id: str, cat_name: str,
                     event: str, model: str, lang: str) -> None:
        """Background worker — builds the prompt, streams the AI, parses
        the pool, and commits it on success."""
        try:
            prompt = self._build_prompt(char_id, cat_name, event, lang)
            backend = self._create_chat(model)
            backend.messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Generate the JSON array now."},
            ]
            # Stream synchronously in this worker — don't go through
            # backend.send() which would spawn yet another thread.
            chunks = []
            for chunk in backend._stream_chunks():
                chunks.append(chunk)
            raw = "".join(chunks).strip()
            pool = self._parse_pool(raw)
            if pool:
                with self._lock:
                    self._pools[key] = pool
                log.info("Reaction pool ready: %s (%d items)", key, len(pool))
            else:
                log.warning("Reaction pool parse failed for %s: raw=%r", key, raw[:200])
        except Exception:
            log.exception("Reaction pool generation crashed for %s", key)
        finally:
            with self._lock:
                self._generating.discard(key)

    # ── Prompt building ──────────────────────────────────────────────────────

    def _build_prompt(self, char_id: str, cat_name: str,
                      event: str, lang: str) -> str:
        # Import lazily to avoid a circular import with app.py which is the
        # source of truth for CATSET_PERSONALITIES today.
        from catai_linux.app import CATSET_PERSONALITIES
        perso = CATSET_PERSONALITIES.get(char_id) or CATSET_PERSONALITIES.get("cat01", {})
        traits = (perso.get("traits") or {}).get(lang, "")

        scenario = self._scenario_for(event, lang)
        lang_name = {"fr": "French", "en": "English", "es": "Spanish"}.get(lang, "French")

        return (
            f"{POOL_PROMPT_MARKER}\n"
            f"You are {cat_name}, a {traits} cat from the CATAI desktop pet app.\n"
            f"{scenario}\n\n"
            f"Respond with EXACTLY a JSON array of {self.POOL_SIZE} short "
            f"reactions in {lang_name}. Each reaction must be less than "
            f"{self.MAX_REPLY_LEN} characters. No markdown, no prose, no "
            f"explanation — just the raw JSON array.\n"
            f"Example format: [\"reply 1\", \"reply 2\", \"reply 3\", \"reply 4\", \"reply 5\", \"reply 6\"]"
        )

    def _scenario_for(self, event: str, lang: str) -> str:
        if event == self.EVT_CAPSLOCK:
            if lang == "fr":
                return (
                    "L'utilisateur vient d'appuyer sur Verr. Maj. — tout est "
                    "en MAJUSCULES. Réagis comme un chat légèrement agacé ou "
                    "inquiet, en très court. Reste dans ta personnalité."
                )
            if lang == "es":
                return (
                    "El usuario acaba de activar Bloq Mayús — todo está en "
                    "MAYÚSCULAS. Reacciona como un gato ligeramente molesto "
                    "o preocupado, muy breve. Mantén tu personalidad."
                )
            return (
                "The user just turned Caps Lock ON — everything is in CAPS. "
                "React as a mildly annoyed or concerned cat, very briefly. "
                "Stay in character."
            )
        return f"React briefly to the event '{event}'."

    # ── Parser ───────────────────────────────────────────────────────────────

    def _parse_pool(self, raw: str) -> list[str] | None:
        """Extract a list of 2-6 short strings from an LLM response.

        Handles:
          - Plain JSON array: [\"a\", \"b\"]
          - JSON in markdown code fence: ```json\\n[...]\\n```
          - Line-by-line plain text as last resort
        """
        if not raw:
            return None

        # Strip common markdown fences
        cleaned = re.sub(r"^\s*```(?:json|JSON)?\s*", "", raw)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()

        # Try direct JSON parse
        try:
            arr = json.loads(cleaned)
            if isinstance(arr, list):
                pool = [
                    str(x).strip()[: self.MAX_REPLY_LEN]
                    for x in arr
                    if isinstance(x, (str, int, float))
                ]
                pool = [p for p in pool if len(p) >= 2]
                if len(pool) >= 2:
                    return pool[: self.POOL_SIZE]
        except (ValueError, TypeError):
            pass

        # Second attempt: extract the first [...] substring anywhere in the text
        match = re.search(r"\[[^\[\]]*\]", cleaned, re.DOTALL)
        if match:
            try:
                arr = json.loads(match.group(0))
                if isinstance(arr, list):
                    pool = [
                        str(x).strip()[: self.MAX_REPLY_LEN]
                        for x in arr
                        if isinstance(x, (str, int, float))
                    ]
                    pool = [p for p in pool if len(p) >= 2]
                    if len(pool) >= 2:
                        return pool[: self.POOL_SIZE]
            except (ValueError, TypeError):
                pass

        # Last resort: line-by-line, strip bullet/quote junk
        lines = []
        for line in cleaned.split("\n"):
            stripped = line.strip(" -•*\"'\t`")
            if 2 <= len(stripped) <= self.MAX_REPLY_LEN * 2:
                lines.append(stripped[: self.MAX_REPLY_LEN])
        if len(lines) >= 3:
            return lines[: self.POOL_SIZE]

        return None

    # ── Fallback strings ─────────────────────────────────────────────────────

    def _fallback(self, event: str) -> str:
        """Deterministic L10n fallback when the pool isn't ready yet or
        the AI backend isn't available (e.g. CI / offline)."""
        if event == self.EVT_CAPSLOCK:
            return L10n.s("capslock_yell")
        return "?"
