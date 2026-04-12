"""Long-term memory for cats — sqlite-backed, no embeddings.

Every N chat exchanges we ask the AI backend to extract 1-3 short
"memorable facts" from the recent conversation and store them in a
per-cat sqlite table. On every subsequent chat, we retrieve the top
facts that share the most keywords with the user's new message and
inject them into the system prompt as ``"Things you remember about
this user"``.

Why no embeddings?
    - sentence-transformers + chromadb adds 200+ MB of dependencies
      and a CUDA-or-not split that's painful for a desktop pet app.
    - The conversation domain is small (a few cats × a few hundred
      facts × ~30 words each). Pure keyword overlap is good enough
      and totally explainable.
    - sqlite ships with stdlib. Zero extra deps.

Why per-cat?
    - Each cat has its own personality and its own memory of
      conversations. Plus inter-cat gossip (issue #5 Tier 2) will
      later let cats share specific facts during love encounters.

Design constraints:
    - Bounded growth: at most ``MAX_FACTS_PER_CAT`` facts per cat.
      The oldest get pruned when the cap is hit.
    - Forgiving I/O: a corrupt db is silently rebuilt; missing
      sqlite is impossible (stdlib).
    - Non-blocking: the LLM extraction runs in a daemon thread, the
      retrieval at chat time is a single sqlite query (~ms).

Schema (one table)::

    CREATE TABLE cat_memories (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      cat_id TEXT NOT NULL,
      content TEXT NOT NULL,
      created_at REAL NOT NULL,
      last_referenced_at REAL,
      reference_count INTEGER DEFAULT 0
    );

    CREATE INDEX idx_cat_memories_cat ON cat_memories(cat_id);

The :class:`MemoryStore` opens the db lazily so importing this
module never touches the filesystem.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time

log = logging.getLogger("catai")

DB_PATH = os.path.expanduser("~/.config/catai/memory.db")

# Cap on stored facts per cat. When exceeded, the oldest get pruned.
MAX_FACTS_PER_CAT = 50

# How often (in chat messages) we run a fact-extraction LLM call.
EXTRACT_EVERY_MESSAGES = 20

# Marker the extraction prompt starts with — MockChat detects this
# and yields a canned JSON response so the e2e suite can exercise
# the full pipeline without a real LLM.
EXTRACT_PROMPT_MARKER = "[CATAI_MEMORY_EXTRACT]"


# ── MemoryStore ──────────────────────────────────────────────────────────────


class MemoryStore:
    """Thread-safe wrapper around the per-cat sqlite memory table."""

    _lock = threading.RLock()
    _conn: sqlite3.Connection | None = None
    _path: str = DB_PATH

    @classmethod
    def _connect(cls) -> sqlite3.Connection:
        """Lazily open the sqlite connection. Creates the schema on
        first use. The connection is reused across calls (sqlite
        handles cross-thread reads when ``check_same_thread=False``)."""
        with cls._lock:
            if cls._conn is not None:
                return cls._conn
            os.makedirs(os.path.dirname(cls._path), exist_ok=True)
            try:
                conn = sqlite3.connect(cls._path, check_same_thread=False)
            except sqlite3.DatabaseError:
                # Corrupt db — wipe and recreate. Losing memories
                # silently beats crashing the app.
                log.warning("memory: corrupt db at %s, rebuilding", cls._path)
                try:
                    os.remove(cls._path)
                except OSError:
                    pass
                conn = sqlite3.connect(cls._path, check_same_thread=False)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cat_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cat_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_referenced_at REAL,
                    reference_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cat_memories_cat
                ON cat_memories(cat_id)
            """)
            conn.commit()
            cls._conn = conn
            return conn

    @classmethod
    def set_path(cls, path: str) -> None:
        """Override the db path (used by tests to redirect to a
        tempdir). Resets any open connection."""
        with cls._lock:
            if cls._conn is not None:
                cls._conn.close()
                cls._conn = None
            cls._path = path

    @classmethod
    def add_fact(cls, cat_id: str, content: str) -> None:
        """Insert a new memorable fact and prune oldest if over cap."""
        content = content.strip()
        if not content or len(content) > 280:
            return
        conn = cls._connect()
        with cls._lock:
            try:
                conn.execute(
                    "INSERT INTO cat_memories (cat_id, content, created_at) "
                    "VALUES (?, ?, ?)",
                    (cat_id, content, time.time()),
                )
                conn.commit()
            except sqlite3.Error:
                log.exception("memory: insert failed")
                return
        cls._prune_if_needed(cat_id)

    @classmethod
    def _prune_if_needed(cls, cat_id: str) -> None:
        conn = cls._connect()
        with cls._lock:
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM cat_memories WHERE cat_id = ?",
                    (cat_id,),
                )
                count = cur.fetchone()[0]
                if count <= MAX_FACTS_PER_CAT:
                    return
                # Drop the oldest excess rows
                excess = count - MAX_FACTS_PER_CAT
                conn.execute("""
                    DELETE FROM cat_memories
                    WHERE id IN (
                        SELECT id FROM cat_memories
                        WHERE cat_id = ?
                        ORDER BY created_at ASC
                        LIMIT ?
                    )
                """, (cat_id, excess))
                conn.commit()
                log.debug("memory: pruned %d old facts for %s", excess, cat_id)
            except sqlite3.Error:
                log.exception("memory: prune failed")

    @classmethod
    def all_facts(cls, cat_id: str) -> list[str]:
        """Return every stored fact for a cat (used by gossip + tests)."""
        conn = cls._connect()
        with cls._lock:
            try:
                cur = conn.execute(
                    "SELECT content FROM cat_memories WHERE cat_id = ? "
                    "ORDER BY created_at ASC",
                    (cat_id,),
                )
                return [row[0] for row in cur.fetchall()]
            except sqlite3.Error:
                return []

    @classmethod
    def count(cls, cat_id: str) -> int:
        conn = cls._connect()
        with cls._lock:
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM cat_memories WHERE cat_id = ?",
                    (cat_id,),
                )
                return cur.fetchone()[0]
            except sqlite3.Error:
                return 0

    @classmethod
    def clear(cls, cat_id: str | None = None) -> None:
        """Wipe one cat's memories or all of them."""
        conn = cls._connect()
        with cls._lock:
            try:
                if cat_id is None:
                    conn.execute("DELETE FROM cat_memories")
                else:
                    conn.execute(
                        "DELETE FROM cat_memories WHERE cat_id = ?",
                        (cat_id,),
                    )
                conn.commit()
            except sqlite3.Error:
                log.exception("memory: clear failed")

    @classmethod
    def retrieve_relevant(cls, cat_id: str, query: str,
                          n: int = 3) -> list[str]:
        """Return the top ``n`` facts whose tokens overlap most with
        ``query``. Pure stdlib keyword scoring — no embeddings."""
        if not query.strip():
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        conn = cls._connect()
        with cls._lock:
            try:
                cur = conn.execute(
                    "SELECT id, content FROM cat_memories WHERE cat_id = ?",
                    (cat_id,),
                )
                rows = cur.fetchall()
            except sqlite3.Error:
                return []
        scored: list[tuple[float, int, str]] = []
        for row_id, content in rows:
            f_tokens = _tokenize(content)
            if not f_tokens:
                continue
            overlap = q_tokens & f_tokens
            if not overlap:
                continue
            # Jaccard-ish: |intersection| / log(|fact| + 1) so we don't
            # over-favor very long facts that contain everything.
            score = len(overlap) / math.log(len(f_tokens) + 1.5)
            scored.append((score, row_id, content))
        scored.sort(reverse=True)
        top = scored[:n]
        if top:
            # Bump reference counters so future pruning could prefer
            # rarely-used facts (not implemented yet, but tracked)
            now = time.time()
            ids = [row_id for _, row_id, _ in top]
            with cls._lock:
                try:
                    conn.executemany(
                        "UPDATE cat_memories SET last_referenced_at = ?, "
                        "reference_count = reference_count + 1 WHERE id = ?",
                        [(now, i) for i in ids],
                    )
                    conn.commit()
                except sqlite3.Error:
                    pass
        return [content for _, _, content in top]


# ── Tokenization (very simple) ────────────────────────────────────────────────


# Lowercase the input, strip accents in a stupid way (NFD + drop non-ASCII),
# split on word characters, drop ultra-common French/English stop words.
_STOPWORDS = frozenset({
    # French
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou", "mais",
    "que", "qui", "quoi", "ce", "ces", "cette", "ça", "tu", "te", "ton",
    "ta", "tes", "je", "j", "me", "mon", "ma", "mes", "il", "elle", "on",
    "nous", "vous", "ils", "elles", "leur", "leurs", "se", "lui", "y",
    "en", "pas", "ne", "n", "plus", "moins", "à", "au", "aux", "avec",
    "sans", "pour", "par", "sur", "sous", "dans", "comme", "si", "très",
    "tout", "tous", "toute", "toutes", "est", "es", "suis", "sont", "été",
    "être", "ai", "as", "a", "ont", "avons", "avez", "fais", "fait",
    # English
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "have", "has", "had", "do", "does", "did", "i", "me",
    "my", "you", "your", "he", "him", "his", "she", "her", "it", "its",
    "we", "us", "our", "they", "them", "their", "this", "that", "these",
    "those", "to", "of", "in", "on", "at", "by", "for", "with", "from",
    "as", "if", "so", "not", "no", "yes", "can", "will", "would", "should",
    # Both
    "miaou", "meow", "purr", "prrt", "mrrp",
})

_WORD_RE = re.compile(r"[a-zàâäéèêëîïôöùûüç]+", re.IGNORECASE)


def _tokenize(text: str) -> set[str]:
    """Return the set of meaningful lowercase tokens in ``text``."""
    if not text:
        return set()
    tokens = _WORD_RE.findall(text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}


# ── LLM extraction ────────────────────────────────────────────────────────────


def build_extract_prompt(cat_name: str, recent_messages: list[dict],
                         lang: str) -> str:
    """Build the system prompt sent to the AI backend to extract facts.

    The prompt asks for a JSON array of 1-3 short strings, each
    describing one *thing the cat learned about the user* during the
    conversation. Personal-detail bias on purpose — generic chitchat
    facts ('it's raining') are useless for memory."""
    lang_name = {"fr": "French", "en": "English",
                 "es": "Spanish"}.get(lang, "French")
    convo = "\n".join(
        f"{m.get('role', '?').upper()}: {m.get('content', '')[:300]}"
        for m in recent_messages
        if m.get("role") in ("user", "assistant")
    )
    return (
        f"{EXTRACT_PROMPT_MARKER}\n"
        f"You are a meta-analysis assistant, not the cat. Your job is to "
        f"read a recent conversation between a user and the cat {cat_name} "
        f"and extract 0 to 3 SHORT facts the cat would want to remember "
        f"about the user. Focus on personal details: name, profession, "
        f"family, hobbies, preferences, mood, recurring topics. Skip "
        f"generic small talk.\n\n"
        f"Conversation:\n{convo}\n\n"
        f"Output EXACTLY a JSON array of 0-3 short {lang_name} sentences. "
        f"Each fact must be < 120 characters and self-contained (no "
        f"'they said'). No markdown, no prose, no explanation — just the "
        f"raw JSON array.\n"
        f"Example: [\"L'utilisateur s'appelle Sib\", "
        f"\"Travaille comme développeur Linux\"]"
    )


def parse_extract_response(raw: str) -> list[str]:
    """Pull a list of strings out of the LLM's JSON response. Tolerates
    markdown fences and trailing prose. Returns [] on any error."""
    if not raw:
        return []
    cleaned = re.sub(r"^\s*```(?:json|JSON)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()
    try:
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            return [str(x).strip() for x in arr if isinstance(x, str) and x.strip()]
    except (ValueError, TypeError):
        pass
    # Last-ditch: extract the first [...] substring
    match = re.search(r"\[[^\[\]]*\]", cleaned, re.DOTALL)
    if match:
        try:
            arr = json.loads(match.group(0))
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if isinstance(x, str) and x.strip()]
        except (ValueError, TypeError):
            pass
    return []


_extract_lock = threading.Lock()
_extract_in_flight: set[str] = set()


def extract_facts_async(cat_id: str, cat_name: str,
                        recent_messages: list[dict], lang: str,
                        create_chat_fn, model: str) -> None:
    """Kick off a background fact-extraction call. Idempotent per
    cat_id: if a previous extraction is still running, the new call
    is a no-op."""
    with _extract_lock:
        if cat_id in _extract_in_flight:
            return
        _extract_in_flight.add(cat_id)

    def _run():
        try:
            prompt = build_extract_prompt(cat_name, recent_messages, lang)
            backend = create_chat_fn(model)
            backend.messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Emit the JSON array now."},
            ]
            chunks = []
            for chunk in backend._stream_chunks():
                chunks.append(chunk)
            raw = "".join(chunks)
            facts = parse_extract_response(raw)
            for fact in facts:
                MemoryStore.add_fact(cat_id, fact)
            if facts:
                log.info("memory: extracted %d fact(s) for %s",
                         len(facts), cat_id)
            else:
                log.debug("memory: parse failed for %s, raw=%r",
                          cat_id, raw[:200])
        except Exception:
            log.exception("memory: extraction crashed for %s", cat_id)
        finally:
            with _extract_lock:
                _extract_in_flight.discard(cat_id)

    threading.Thread(target=_run, daemon=True).start()


# ── Prompt injection ─────────────────────────────────────────────────────────


def append_memories_to_prompt(base_prompt: str, cat_id: str,
                              query: str, lang: str) -> str:
    """Look up the most relevant facts for ``query`` and append them
    to ``base_prompt`` as a 'things you remember' section. Returns the
    base prompt unchanged if no facts match."""
    facts = MemoryStore.retrieve_relevant(cat_id, query, n=3)
    if not facts:
        return base_prompt
    bullet = "\n - " + "\n - ".join(facts)
    if lang == "en":
        suffix = (
            f"\n\nThings you remember about this user (use them naturally, "
            f"don't list them):{bullet}"
        )
    elif lang == "es":
        suffix = (
            f"\n\nLo que recuerdas sobre este usuario (úsalo de forma "
            f"natural, no lo enumeres):{bullet}"
        )
    else:
        suffix = (
            f"\n\nCe dont tu te souviens à propos de cet utilisateur "
            f"(utilise-le naturellement, ne le liste pas):{bullet}"
        )
    return base_prompt + suffix
