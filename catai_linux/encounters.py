"""Cat encounter classes for CATAI-Linux.

Extracted from ``catai_linux.app`` — standalone classes that take an ``app``
parameter (CatAIApp instance).
"""
from __future__ import annotations

import logging
import random
import time
import uuid

from gi.repository import GLib

from catai_linux.constants import CatState, CATSET_PERSONALITIES, CAT_TO_KITTEN
from catai_linux.l10n import L10n
from catai_linux.chat_backend import create_chat
from catai_linux import metrics as _metrics
from catai_linux import memory as _memory

log = logging.getLogger("catai")

# Kept here because only LoveEncounter uses it, and it was defined in the
# same section of app.py next to the encounter classes.
MAX_KITTENS = 6


class CatEncounter:
    """Manages a short AI-generated conversation between two nearby cats."""

    PROXIMITY = 180    # px — horizontal distance to trigger
    MSG_DURATION = 4500  # ms to display each message

    def __init__(self, cat_a, cat_b, app):
        self.cat_a = cat_a   # initiator
        self.cat_b = cat_b   # responder
        self.app = app
        self.n_exchanges = random.randint(1, 3)
        self._step = 0       # 0=A speaks, 1=B replies, 2=A again, …
        self._total_steps = self.n_exchanges * 2
        self._last_text = ""
        self._timer_id = None
        self.active = True

    def start(self):
        """Freeze cats and begin the conversation."""
        for cat in (self.cat_a, self.cat_b):
            cat.in_encounter = True
            cat.state = CatState.SOCIALIZING
            cat.meow_visible = False
        # Face each other
        if self.cat_b.x > self.cat_a.x:
            self.cat_a.direction = "east"
            self.cat_b.direction = "west"
        else:
            self.cat_a.direction = "west"
            self.cat_b.direction = "east"
        self._generate_next()

    def _speaker(self):
        return self.cat_a if self._step % 2 == 0 else self.cat_b

    def _listener(self):
        return self.cat_b if self._step % 2 == 0 else self.cat_a

    @staticmethod
    def _cat_traits(cat, lang):
        p = CATSET_PERSONALITIES.get(cat.config.get("char_id", "cat01"), CATSET_PERSONALITIES["cat01"])
        return p["traits"].get(lang, p["traits"]["fr"])

    def _build_prompt(self, speaker, listener):
        lang = L10n.lang
        s_name = speaker.config["name"]
        l_name = listener.config["name"]
        s_traits = self._cat_traits(speaker, lang)
        l_traits = self._cat_traits(listener, lang)
        if lang == "en":
            system = (f"You are {s_name}, a {s_traits} cat. You've just run into {l_name}, "
                      f"a {l_traits} cat. Reply with exactly 1 short sentence, in character, "
                      f"using cat sounds (meow, purr, mrrp). No quotation marks.")
            user = (f"Say hello to {l_name}." if self._step == 0 else
                    f"{l_name} just said: '{self._last_text}'. Reply briefly.")
        elif lang == "es":
            system = (f"Eres {s_name}, un gato {s_traits}. Acabas de cruzarte con {l_name}, "
                      f"un gato {l_traits}. Responde con exactamente 1 frase corta, en personaje, "
                      f"con sonidos de gato (miau, purr, mrrp). Sin comillas.")
            user = (f"Saluda a {l_name}." if self._step == 0 else
                    f"{l_name} acaba de decir: '{self._last_text}'. Respóndele brevemente.")
        else:
            system = (f"Tu es {s_name}, un chat {s_traits}. Tu croises {l_name}, "
                      f"un chat {l_traits}. Réponds avec exactement 1 courte phrase, dans le personnage, "
                      f"avec des sons de chat (miaou, purr, mrrp). Sans guillemets.")
            user = (f"Dis quelque chose à {l_name}." if self._step == 0 else
                    f"{l_name} vient de dire : '{self._last_text}'. Réponds-lui brièvement.")
        return system, user

    def _generate_next(self):
        if not self.active:
            return
        speaker = self._speaker()
        listener = self._listener()
        system, user = self._build_prompt(speaker, listener)

        backend = create_chat(self.app.selected_model)
        backend.messages = [{"role": "system", "content": system}]

        collected = []
        _spk = speaker
        _lst = listener

        def on_token(tok):
            collected.append(tok)
            return False

        def on_done():
            if not self.active:
                return False
            text = "".join(collected).strip()
            if not text:
                text = L10n.random_meow()
            self._last_text = text
            _spk.encounter_text = text
            _spk.encounter_visible = True
            _lst.encounter_visible = False
            self._timer_id = GLib.timeout_add(self.MSG_DURATION, self._advance)
            return False

        def on_error(msg):
            if not self.active:
                return False
            _spk.encounter_text = L10n.random_meow()
            _spk.encounter_visible = True
            self._timer_id = GLib.timeout_add(self.MSG_DURATION, self._advance)
            return False

        backend.send(user, on_token, on_done, on_error)

    def _advance(self):
        self._timer_id = None
        self._speaker().encounter_visible = False
        self._step += 1
        if self._step >= self._total_steps or not self.active:
            self._end()
        else:
            # Brief pause before next line
            self._timer_id = GLib.timeout_add(400, lambda: self._generate_next() or False)
        return False

    COOLDOWN = 120.0  # seconds before same cat can encounter again

    def _end(self):
        self.active = False
        cooldown_until = time.monotonic() + self.COOLDOWN
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat.encounter_visible = False
            cat.encounter_text = ""
            cat.idle_ticks = 0
            cat._encounter_cooldown_until = cooldown_until
        self.app._active_encounter = None

    def cancel(self):
        self.active = False
        if self._timer_id:
            try:
                GLib.source_remove(self._timer_id)
            except Exception:
                pass
            self._timer_id = None
        cooldown_until = time.monotonic() + self.COOLDOWN
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat.encounter_visible = False
            cat.encounter_text = ""
            cat._encounter_cooldown_until = cooldown_until


class LoveEncounter:
    """Silent encounter between two cats. Cat A is the initiator; the outcome
    is decided up front:
      - LOVE (40%):       both in LOVE → a kitten is born
      - SURPRISED (30%):  A in LOVE, B surprised, no drama
      - ANGRY (30%):      A attacks with ANGRY, B is the victim → drama_queen
    """

    PROXIMITY = CatEncounter.PROXIMITY
    COOLDOWN = 300.0  # 5 min — no baby-boom

    def __init__(self, cat_a, cat_b, app, forced_outcome=None):
        self.cat_a = cat_a  # initiator
        self.cat_b = cat_b  # responder / potential victim
        self.app = app
        self.active = True
        self._timers = []
        # None = random; "love" / "surprised" / "angry" = forced (used by E2E tests)
        self._forced_outcome = forced_outcome
        self._outcome = None  # resolved in start()

    def start(self):
        for cat in (self.cat_a, self.cat_b):
            cat.in_encounter = True
            cat.meow_visible = False
            cat.chat_visible = False

        # Decide the outcome up front so cat A shows the right initial state
        if self._forced_outcome in ("love", "surprised", "angry"):
            self._outcome = self._forced_outcome
        else:
            r = random.random()
            if r < 0.40:
                self._outcome = "love"
            elif r < 0.70:
                self._outcome = "surprised"
            else:
                self._outcome = "angry"

        # Cat A enters its initial state based on outcome
        if self._outcome == "angry":
            self.cat_a.state = CatState.ANGRY  # aggressor
            self.cat_a._face_toward(self.cat_b, CatState.ANGRY)
        else:
            self.cat_a.state = CatState.LOVE
            self.cat_a._face_toward(self.cat_b, CatState.LOVE)
        self.cat_a.frame_index = 0

        # Cat B stays idle but faces cat A
        self.cat_b.state = CatState.IDLE
        self.cat_b._face_toward(self.cat_a, CatState.IDLE)

        # After 1.2s, cat B reacts
        tid = GLib.timeout_add(1200, self._cat_b_reacts)
        self._timers.append(tid)

    def _cat_b_reacts(self):
        if not self.active:
            return False
        # Reaction depends on the pre-decided outcome
        if self._outcome == "love":
            self.cat_b.state = CatState.LOVE
        elif self._outcome == "surprised":
            self.cat_b.state = CatState.SURPRISED
        else:  # angry → cat B is surprised/scared before the attack
            self.cat_b.state = CatState.SURPRISED
        self.cat_b._face_toward(self.cat_a, self.cat_b.state)
        self.cat_b.frame_index = 0

        # Hold reaction for 3s, then decide outcome
        tid = GLib.timeout_add(3000, self._decide_outcome)
        self._timers.append(tid)
        return False

    def _decide_outcome(self):
        if not self.active:
            return False
        _metrics.track("love_encounter", kind=self._outcome)
        # Inter-cat gossip (#5 Tier 2): regardless of outcome, the
        # two cats trade one random fact each from their memory pile.
        # The fact lands in the recipient's memory tagged as "<other> m'a
        # dit que ..." so when it surfaces in retrieval, the cat speaks
        # of it as second-hand knowledge.
        self._exchange_gossip()
        if self._outcome == "love":
            # Both in love → birth!
            self._give_birth()
            tid = GLib.timeout_add(3500, self._end)
            self._timers.append(tid)
        elif self._outcome == "angry":
            # Attack! Cat A was the aggressor, cat B is now the victim
            self._attack_cat_b()
        else:
            self._end()
        return False

    def _exchange_gossip(self) -> None:
        """Have each cat give one of their memories to the other.

        Skips silently when long-term memory is disabled or when one
        of the cats has no memories yet. The gossip is wrapped in a
        prefix indicating the source so the recipient cat speaks of
        it as a story it heard, not as personal knowledge.
        """
        if not getattr(self.app, "_long_term_memory_enabled", True):
            return
        if not self.cat_a or not self.cat_b:
            return
        try:
            a_facts = _memory.MemoryStore.all_facts(self.cat_a.config["id"])
            b_facts = _memory.MemoryStore.all_facts(self.cat_b.config["id"])
            if a_facts:
                shared = random.choice(a_facts)
                _memory.MemoryStore.add_fact(
                    self.cat_b.config["id"],
                    f"{self.cat_a.config.get('name', 'un autre chat')} "
                    f"m'a raconté: {shared}"[:280]
                )
            if b_facts:
                shared = random.choice(b_facts)
                _memory.MemoryStore.add_fact(
                    self.cat_a.config["id"],
                    f"{self.cat_b.config.get('name', 'un autre chat')} "
                    f"m'a raconté: {shared}"[:280]
                )
            log.info("Gossip exchange: %s ↔ %s",
                     self.cat_a.config.get("name"),
                     self.cat_b.config.get("name"))
        except Exception:
            log.debug("gossip exchange failed", exc_info=True)

    def _attack_cat_b(self):
        """Cat A attacks cat B. Cat B plays drama_queen, both exit encounter."""
        log.info("Love encounter attack: %s -> %s", self.cat_a.config["name"], self.cat_b.config["name"])
        # Release cat_b from encounter so drama_queen can play freely
        self.cat_b.in_encounter = False
        self.cat_b._flip_h = False
        self.cat_b._start_sequence("drama_queen")
        # Cat A steps out with cooldown
        self.cat_a.state = CatState.IDLE
        self.cat_a.in_encounter = False
        self.cat_a._flip_h = False
        self.cat_a._encounter_cooldown_until = time.monotonic() + self.COOLDOWN
        self.cat_a.idle_ticks = 0
        # Global encounter end (but cat_b still running drama_queen on its own)
        self.active = False
        self.app._active_encounter = None

    def _give_birth(self):
        # Check global kitten limit
        kitten_count = sum(1 for c in self.app.cat_instances if c.is_kitten)
        if kitten_count >= MAX_KITTENS:
            log.info("Love encounter: skipping birth, kitten limit reached (%d)", MAX_KITTENS)
            return

        # Pick a random parent for genetics
        parent = random.choice([self.cat_a, self.cat_b])
        kitten_char_id = CAT_TO_KITTEN.get(parent.config.get("char_id"))
        if not kitten_char_id:
            log.warning("No kitten mapping for char_id %s", parent.config.get("char_id"))
            return

        # Create ephemeral kitten config (NOT saved to disk)
        kitten_cfg = {
            "id": f"kitten_{uuid.uuid4().hex[:8]}",
            "char_id": kitten_char_id,
            "name": parent.config["name"] + " Jr.",
        }
        idx = len(self.app.cat_instances)
        try:
            self.app._create_instance(kitten_cfg, idx)
        except Exception:
            log.exception("Failed to create kitten")
            return

        kitten = self.app.cat_instances[-1]
        kitten.is_kitten = True
        kitten._birth_progress = 0.0
        _metrics.track("kitten_born")
        # Place at midpoint between parents, slightly below
        kitten.x = (self.cat_a.x + self.cat_b.x) / 2 + (self.cat_a.display_w - kitten.display_w) / 2
        kitten.y = (self.cat_a.y + self.cat_b.y) / 2 + 20
        kitten.x = max(0, min(kitten.x, kitten.screen_w - kitten.display_w))
        kitten.y = max(0, min(kitten.y, kitten.screen_h - kitten.display_h))
        log.info("Birth! %s + %s → %s (%s)",
                 self.cat_a.config["name"], self.cat_b.config["name"],
                 kitten.config["name"], kitten_char_id)

    def _end(self):
        self.active = False
        cooldown = time.monotonic() + self.COOLDOWN
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat._flip_h = False
            cat._encounter_cooldown_until = cooldown
            cat.idle_ticks = 0
        self.app._active_encounter = None
        return False

    def cancel(self):
        self.active = False
        for tid in self._timers:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._timers.clear()
        for cat in (self.cat_a, self.cat_b):
            cat.state = CatState.IDLE
            cat.in_encounter = False
            cat._flip_h = False
