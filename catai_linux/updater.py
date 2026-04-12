"""Auto-update check + install — fetches the latest GitHub release and
silently upgrades the local pip install on the next launch.

Design goals:
    - **Install once, update forever**. The user runs ``pip install
      catai-linux`` once and from then on every launch checks GitHub
      and self-updates. No more manual ``pip install --upgrade`` runs.
    - **Default-on but settings-controllable**. Three modes:
        ``auto``    → check + install + meow bubble next-launch hint
        ``notify``  → check + meow bubble only, no install
        ``off``     → no check at all, no network call
    - **Cheap**. One GitHub API call per launch (cached for 1 h so a
      relaunch loop doesn't hammer the API). All work in a daemon
      thread so startup isn't blocked.
    - **Safe**. Never overwrites the running interpreter — pip is
      ``--user`` scoped, the new version takes effect at the NEXT
      launch. Failure modes (no pip, no network, externally-managed
      env, install crash) silently degrade, never break the running
      app.
    - **Privacy**. No analytics, no telemetry. Just a single
      anonymous GET to ``api.github.com``.

The cache file lives at ``~/.config/catai/update_cache.json`` and
stores the last-fetched tag + timestamp.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request

log = logging.getLogger("catai")

GITHUB_RELEASES_API = (
    "https://api.github.com/repos/Gheop/CATAI-Linux/releases/latest"
)
CACHE_FILE = os.path.expanduser("~/.config/catai/update_cache.json")
CACHE_TTL_SEC = 3600  # 1 h between GitHub calls

# Modes — exposed as constants so the settings UI and tests use the
# same string values
MODE_AUTO = "auto"
MODE_NOTIFY = "notify"
MODE_OFF = "off"
ALL_MODES = (MODE_AUTO, MODE_NOTIFY, MODE_OFF)


# ── Version helpers ──────────────────────────────────────────────────────────


def get_installed_version() -> str | None:
    """Return the installed catai-linux version via importlib.metadata.
    Returns None if the package can't be located (running from a checkout
    without ``pip install -e .``)."""
    try:
        import importlib.metadata
        return importlib.metadata.version("catai-linux")
    except Exception:
        return None


_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+.](\w+))?$")


def parse_version(v: str) -> tuple[int, int, int, str] | None:
    """Parse 'v0.6.1' or '0.6.1-beta' into a comparable tuple. Returns
    None if the string doesn't match a recognizable semver shape."""
    if not v:
        return None
    m = _VERSION_RE.match(v.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def compare_versions(a: str, b: str) -> int:
    """Return -1 if a<b, 0 if a==b, 1 if a>b.

    Pre-releases (anything with a suffix like ``-beta``, ``-rc1``,
    ``+dev``) are considered LOWER than the same major.minor.patch
    without a suffix. Unparseable input → 0 (treat as equal so we
    never trigger a phantom upgrade)."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return 0
    for i in range(3):
        if pa[i] != pb[i]:
            return -1 if pa[i] < pb[i] else 1
    if pa[3] == pb[3]:
        return 0
    if pa[3] == "":
        return 1
    if pb[3] == "":
        return -1
    return -1 if pa[3] < pb[3] else 1


# ── Cache I/O ────────────────────────────────────────────────────────────────


def _read_cache() -> dict | None:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        log.debug("updater: cache write failed", exc_info=True)


# ── GitHub fetch ─────────────────────────────────────────────────────────────


def fetch_latest_release(timeout: float = 5.0,
                         force: bool = False) -> str | None:
    """Hit the GitHub releases API and return the latest tag name
    (e.g. ``'v0.6.1'``) or None on any error.

    Caches the result for ``CACHE_TTL_SEC`` seconds so a relaunch loop
    doesn't hammer GitHub. Pass ``force=True`` from the 'Check now'
    button to bypass the cache."""
    cache = _read_cache()
    if cache and not force:
        age = time.time() - cache.get("ts", 0)
        if age < CACHE_TTL_SEC and cache.get("tag"):
            return cache["tag"]
    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "catai-linux-updater",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        tag = data.get("tag_name")
        if tag:
            _write_cache({"ts": time.time(), "tag": tag})
            log.info("updater: GitHub latest = %s", tag)
        return tag
    except Exception as e:
        log.debug("updater: github fetch failed: %s", e)
        return None


def check_for_update(force: bool = False) -> tuple[str, str] | None:
    """Return ``(installed, latest)`` if an update is available, None
    otherwise. Wrapper that combines the installed-version probe and
    the GitHub fetch + comparison."""
    installed = get_installed_version()
    if installed is None:
        return None
    latest_tag = fetch_latest_release(force=force)
    if not latest_tag:
        return None
    if compare_versions(installed, latest_tag) >= 0:
        return None
    return installed, latest_tag


# ── pip install --upgrade ────────────────────────────────────────────────────


def _has_voice_extra() -> bool:
    """Return True if faster-whisper is installed, indicating the user
    originally pip-installed with the [voice] extra."""
    try:
        import faster_whisper  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


def install_update_blocking() -> bool:
    """Run ``pip install --user --upgrade catai-linux[voice]`` in a
    subprocess and block until done. Returns True on success.

    Preserves the [voice] extra if it was originally installed (so
    faster-whisper + piper-tts stay up to date too). Falls back to
    ``--break-system-packages`` on PEP-668 systems (Fedora 40+,
    Debian 12+) where the user-site install is otherwise refused."""
    spec = "catai-linux[voice]" if _has_voice_extra() else "catai-linux"
    base_cmd = [sys.executable, "-m", "pip", "install",
                "--user", "--upgrade", "--quiet", spec]
    log.info("updater: running %s", " ".join(base_cmd))
    try:
        result = subprocess.run(
            base_cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            log.info("updater: install succeeded")
            return True
        # PEP 668 retry
        stderr = result.stderr or ""
        if "externally-managed-environment" in stderr:
            log.warning("updater: PEP 668 system — retrying with --break-system-packages. "
                        "This bypasses the system Python protection. If you prefer a venv, "
                        "set auto_update to 'off' in config.json and pip-upgrade manually.")
            cmd = base_cmd + ["--break-system-packages"]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                log.info("updater: install succeeded (--break-system-packages)")
                return True
        log.warning("updater: pip install failed (exit %d): %s",
                    result.returncode, (result.stderr or "").strip()[:500])
        return False
    except FileNotFoundError:
        log.warning("updater: %s -m pip not found", sys.executable)
        return False
    except subprocess.TimeoutExpired:
        log.warning("updater: pip install timed out after 5 min")
        return False
    except Exception:
        log.exception("updater: install crashed")
        return False
