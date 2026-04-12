"""Chat backends for CATAI-Linux — Claude API + Ollama HTTP.

Exposes a common `ChatBackend` base class with a non-blocking `.send()` that
streams tokens back to the main thread via GLib.idle_add, plus `create_chat()`
which picks the best backend based on the requested model name and what's
available on the current system.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable

import httpx
from gi.repository import GLib

from catai_linux.l10n import L10n

log = logging.getLogger("catai")

# ── Constants ─────────────────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_CREDS = os.path.expanduser("~/.claude/.credentials.json")
OLLAMA_URL = "http://localhost:11434"
OLLAMA_TIMEOUT = 60
MEM_MAX = 20  # number of message pairs retained in history before truncation


# ── Claude credential helpers ─────────────────────────────────────────────────

def _get_claude_api_key() -> str | None:
    """Get API key from env var or Claude Code's OAuth token."""
    return os.environ.get("ANTHROPIC_API_KEY") or _read_claude_oauth()


def _find_claude_cli() -> str | None:
    """Find the claude CLI binary, checking common locations."""
    for path in [
        shutil.which("claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
    ]:
        if path and os.path.isfile(path):
            return path
    return None


def _refresh_claude_token() -> bool:
    """Force Claude Code to refresh the OAuth token by calling it.

    **CRITICAL** : we run ``claude -p ok`` in a *headless-only* env
    (DISPLAY / WAYLAND_DISPLAY / BROWSER stripped, BROWSER=/bin/false
    set) so that if the **refresh token is also expired** (not just
    the access token), the CLI **cannot pop a browser window** to
    ask the user to re-authenticate.

    Without this guard, every background chat attempt — encounters,
    reactions, drift, memory extraction — could spontaneously open a
    Claude.ai marketing/login page out of nowhere, miles from any
    user action. Extremely surprising and slightly creepy.

    When refresh genuinely fails the chat path raises ``err_auth``
    and the user gets a polite bubble. They can re-auth manually
    by running ``claude -p ok`` from a real terminal whenever they
    want."""
    cli = _find_claude_cli()
    if not cli:
        log.debug("Claude CLI not found, cannot refresh token")
        return False
    try:
        log.debug("Refreshing Claude token via %s...", cli)
        # Strip GUI env so the CLI can't fork a browser even if its
        # internal refresh fails. Belt + suspenders: BROWSER=/bin/false
        # so xdg-open / sensible-browser fall through to a no-op.
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER")
        }
        env["BROWSER"] = "/bin/false"
        subprocess.run(
            [cli, "-p", "ok", "--output-format", "text"],
            capture_output=True, timeout=30, env=env,
        )
        return True
    except Exception as e:
        log.debug("Token refresh failed: %s", e)
        return False


def _read_claude_oauth_raw() -> dict | None:
    """Read credentials JSON without refreshing. Returns oauth dict or None."""
    try:
        if os.path.exists(CLAUDE_CREDS):
            mode = os.stat(CLAUDE_CREDS).st_mode
            if mode & 0o077:
                # Warn but don't refuse to read — the alternative (failing
                # to start the AI backend entirely) is worse for a desktop
                # pet app. The user should fix permissions manually if this
                # warning appears in the logs.
                log.warning("Credentials file %s is accessible by others (mode %o)", CLAUDE_CREDS, mode)
        with open(CLAUDE_CREDS) as f:
            return json.load(f).get("claudeAiOauth")
    except Exception:
        return None


def _read_claude_oauth() -> str | None:
    """Return a valid access token, proactively refreshing if near expiry."""
    oa = _read_claude_oauth_raw()
    if not oa:
        return None
    exp_ms = oa.get("expiresAt", 0)
    now_ms = time.time() * 1000
    if exp_ms and (exp_ms - now_ms) < 5 * 60 * 1000:
        log.debug("Claude token expires in < 5min, refreshing proactively")
        if _refresh_claude_token():
            oa = _read_claude_oauth_raw() or oa
    return oa.get("accessToken")


_claude_available: bool | None = None


def claude_available() -> bool:
    global _claude_available
    if _claude_available is None:
        _claude_available = _get_claude_api_key() is not None
    return _claude_available


# ── Ollama probing ────────────────────────────────────────────────────────────

_ollama_models_cache: list[str] | None = None


def fetch_ollama_models() -> list[str]:
    global _ollama_models_cache
    if _ollama_models_cache is not None:
        return _ollama_models_cache
    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1)
        _ollama_models_cache = [m["name"] for m in resp.json().get("models", [])]
        return _ollama_models_cache
    except Exception as e:
        log.debug("Ollama unavailable: %s", e)
        return []


_ollama_checked: bool | None = None


def _ollama_available() -> bool:
    global _ollama_checked
    if _ollama_checked is None:
        try:
            httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1)
            _ollama_checked = True
        except Exception:
            _ollama_checked = False
    return _ollama_checked


# ── Base class ────────────────────────────────────────────────────────────────

class ChatBackend:
    """Base class for chat backends. Handles message history and threading."""

    def __init__(self, model: str):
        self.model = model
        self.messages: list[dict] = []
        self.is_streaming = False
        self._cancel = False
        self._lock = threading.Lock()
        self._on_status: Callable[[str], None] | None = None

    def send(
        self,
        text: str,
        on_token: Callable[[str], None],
        on_done: Callable[[], None],
        on_error: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        with self._lock:
            self.messages.append({"role": "user", "content": text})
            if len(self.messages) > MEM_MAX * 2 + 1:
                self.messages = [self.messages[0]] + self.messages[-(MEM_MAX * 2):]
        self.is_streaming = True
        self._cancel = False
        self._on_status = on_status

        def _run():
            full = ""
            try:
                for chunk in self._stream_chunks():
                    if self._cancel:
                        break
                    full += chunk
                    GLib.idle_add(on_token, chunk)
            except Exception as e:
                if on_error and not full:
                    err_str = str(e)
                    log.warning("Chat error: %s", err_str)
                    if "401" in err_str or "authentication" in err_str.lower() or "token" in err_str.lower():
                        GLib.idle_add(on_error, L10n.s("err_auth"))
                    else:
                        GLib.idle_add(on_error, L10n.s("err"))
            finally:
                with self._lock:
                    if full:
                        self.messages.append({"role": "assistant", "content": full})
                self.is_streaming = False
                GLib.idle_add(on_done)

        threading.Thread(target=_run, daemon=True).start()

    def _stream_chunks(self):
        raise NotImplementedError

    def cancel(self) -> None:
        self._cancel = True


# ── Claude backend ────────────────────────────────────────────────────────────

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        key = _get_claude_api_key()
        if key and not key.startswith('sk-ant-'):
            # OAuth token from claude.ai — must be sent as Bearer, not x-api-key
            _anthropic_client = anthropic.Anthropic(
                api_key="placeholder",
                default_headers={"Authorization": f"Bearer {key}"},
            )
        else:
            _anthropic_client = anthropic.Anthropic(api_key=key)
    return _anthropic_client


class ClaudeChat(ChatBackend):

    def __init__(self, model: str = CLAUDE_MODEL):
        super().__init__(model)
        self.client = _get_anthropic_client()

    def _stream_chunks(self):
        system_prompt = ""
        api_messages = []
        for msg in self.messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                api_messages.append(msg)
        try:
            with self.client.messages.stream(
                model=self.model, max_tokens=256,
                system=system_prompt, messages=api_messages,
            ) as stream:
                yield from stream.text_stream
        except Exception as e:
            if "401" in str(e) or "authentication" in str(e).lower():
                log.warning("Auth failed, refreshing token via Claude CLI...")
                if self._on_status:
                    GLib.idle_add(self._on_status, "refreshing")
                _refresh_claude_token()
                new_key = _read_claude_oauth()
                if new_key:
                    import anthropic
                    global _anthropic_client
                    if new_key.startswith('sk-ant-'):
                        _anthropic_client = anthropic.Anthropic(api_key=new_key)
                    else:
                        _anthropic_client = anthropic.Anthropic(
                            api_key="placeholder",
                            default_headers={"Authorization": f"Bearer {new_key}"},
                        )
                    self.client = _anthropic_client
                    with self.client.messages.stream(
                        model=self.model, max_tokens=256,
                        system=system_prompt, messages=api_messages,
                    ) as stream:
                        yield from stream.text_stream
                else:
                    raise ValueError(L10n.s("err_auth"))
            else:
                raise


# ── Ollama backend ────────────────────────────────────────────────────────────

class OllamaChat(ChatBackend):

    def _stream_chunks(self):
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            with client.stream("POST", f"{OLLAMA_URL}/api/chat",
                               json={"model": self.model, "messages": self.messages, "stream": True}) as resp:
                for line in resp.iter_lines():
                    if self._cancel:
                        return
                    try:
                        content = json.loads(line).get("message", {}).get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        pass


# ── Mock backend (CI e2e tests) ───────────────────────────────────────────────

class MockChat(ChatBackend):
    """Deterministic mock backend used by the E2E test suite in CI.

    Activated when ``CATAI_MOCK_CHAT=1`` is set in the environment (see
    ``create_chat`` below). Streams a fixed cat-ish response word-by-word
    so the T6 "Got AI response" and T12 "Chat still active after drag"
    assertions pass without needing a real Claude key or a running Ollama
    server.

    Also detects requests from ``catai_linux.reactions.ReactionPool`` via a
    sentinel marker in the system prompt (``[CATAI_REACTION_POOL]``) and
    returns a canned JSON array so the full reaction-pool pipeline is
    exercised in tests — not just the L10n fallback path.
    """

    MOCK_RESPONSE = "Miaou mon ami ! Voici une réponse mockée pour la CI. Prrr~"

    # Canned reaction pools, keyed by the scenario keywords present in the
    # system prompt. Falls through to a generic pool if no keyword matches.
    MOCK_POOL_CAPSLOCK = (
        '["POURQUOI CRIES ?!", "ON SE CALME !", "TROP FORT !!!", '
        '"AÏE MES OREILLES", "CHUT !!", "BAISSE LE TON"]'
    )
    MOCK_POOL_PETTING = (
        '["*ronron*", "*prrrr*", "mrrrp~", "miaou \u2665", "*snurgle*", "oh ouiiii"]'
    )
    MOCK_POOL_NOTIFICATION = (
        '["Quoi ?! \U0001f514", "Qui \u00e7a ?", "Ooh !", "\U0001f431 ?!", '
        '"Mrrp ?!", "Encore ?!"]'
    )
    MOCK_POOL_GENERIC = '["Miaou ?", "Prrrt ?", "Hmm ?", "Quoi ?", "Oui ?", "Mrrp ?"]'

    # Canned personality drift response — returned whenever the system
    # prompt carries the [CATAI_PERSONALITY_DRIFT] marker. The trait is
    # intentionally recognizable so e2e tests can assert that the drift
    # pipeline actually ran end-to-end (not just the L10n fallback).
    MOCK_DRIFT_RESPONSE = '{"trait": "aime parler de tests CI"}'

    # Canned long-term memory extraction response — returned whenever
    # the system prompt carries [CATAI_MEMORY_EXTRACT]. A short JSON
    # array of recognizable facts so the e2e suite can verify that
    # extracted facts get persisted in the memory.db.
    MOCK_MEMORY_RESPONSE = (
        '["L\'utilisateur teste CATAI en CI", '
        '"Aime débuguer les pipelines GStreamer"]'
    )

    def _stream_chunks(self):
        sys_prompt = next(
            (m["content"] for m in self.messages if m.get("role") == "system"),
            "",
        )
        if "[CATAI_PERSONALITY_DRIFT]" in sys_prompt:
            # Personality drift uses a JSON object (not array) — a single
            # canned trait is enough to prove the pipeline works.
            yield self.MOCK_DRIFT_RESPONSE
            return
        if "[CATAI_MEMORY_EXTRACT]" in sys_prompt:
            # Long-term memory extraction returns a JSON array of facts.
            yield self.MOCK_MEMORY_RESPONSE
            return
        if "[CATAI_REACTION_POOL]" in sys_prompt:
            # Pick a canned pool matching the scenario if we can.
            low = sys_prompt.lower()
            if "caps lock" in low or "verr. maj." in low or "bloq mayús" in low:
                payload = self.MOCK_POOL_CAPSLOCK
            elif "petting" in low or "caresse" in low or "acaricia" in low:
                payload = self.MOCK_POOL_PETTING
            elif "notification" in low or "desktop notification" in low:
                payload = self.MOCK_POOL_NOTIFICATION
            else:
                payload = self.MOCK_POOL_GENERIC
            # Stream as a single chunk — the parser doesn't care about chunking.
            yield payload
            return

        # Default: stream the fixed cat-ish response word-by-word so
        # on_token fires several times.
        for word in self.MOCK_RESPONSE.split():
            if self._cancel:
                return
            yield word + " "
            time.sleep(0.02)


# ── Dispatcher ────────────────────────────────────────────────────────────────

def create_chat(model: str) -> ChatBackend:
    """Create the best available chat backend for the requested model."""
    # CI escape hatch: short-circuit to a deterministic mock so the e2e
    # suite can run without a real AI backend.
    if os.environ.get("CATAI_MOCK_CHAT") == "1":
        log.debug("Using MockChat (CATAI_MOCK_CHAT=1)")
        return MockChat(model)
    if model.startswith("claude-") and claude_available():
        log.debug("Using Claude API (%s)", model)
        return ClaudeChat(model)
    if not model.startswith("claude-") and _ollama_available():
        available = fetch_ollama_models()
        if available and model in available:
            log.debug("Using Ollama (%s)", model)
            return OllamaChat(model)
        elif available:
            log.debug("Model %s not in Ollama (available: %s), trying Claude", model, available)
        else:
            log.debug("Ollama running but no models installed")
    if claude_available():
        log.debug("Using Claude API (fallback)")
        return ClaudeChat(CLAUDE_MODEL)
    if _ollama_available():
        models = fetch_ollama_models()
        if models:
            log.debug("Using Ollama with first available model: %s", models[0])
            return OllamaChat(models[0])
    log.warning("No AI backend available")
    return OllamaChat(model)
