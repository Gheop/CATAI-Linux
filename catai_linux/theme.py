"""Desktop dark/light preference detection.

Reads the GNOME color-scheme setting (``org.gnome.desktop.interface
color-scheme``) via ``gsettings``. The preference has three values:

    - ``'default'``         → light
    - ``'prefer-light'``    → light
    - ``'prefer-dark'``     → dark

Other desktops (KDE, XFCE) aren't supported yet — we fall back to light.
That matches the historical CATAI default, so nothing breaks.

Usage::

    from catai_linux.theme import is_dark_mode
    if is_dark_mode():
        drawing.set_theme(dark=True)

Kept deliberately tiny: one subprocess call, no caching, no D-Bus. Callers
(``CatAIApp``) poll this every 30 s on the GLib main loop.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("catai")


def is_dark_mode() -> bool:
    """Return True if the desktop is set to dark mode.

    Returns False on any error (missing gsettings, schema not present, etc.)
    so CATAI defaults to its original cream/brown palette on non-GNOME
    desktops.
    """
    if not shutil.which("gsettings"):
        return False
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=1,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("gsettings color-scheme probe failed: %s", e)
        return False
    if result.returncode != 0:
        return False
    value = result.stdout.strip().strip("'\"")
    return "dark" in value.lower()
