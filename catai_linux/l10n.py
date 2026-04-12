"""Localization for CATAI-Linux — gettext-based UI strings + random meow vocabulary.

Supports three languages (fr / en / es) via compiled .mo catalogs under
``catai_linux/locales/``.  The module-level ``set_lang()`` / ``s()`` helpers
are the canonical API; the ``L10n`` class is kept for backward compatibility
so existing ``L10n.s("key")`` and ``L10n.lang`` call sites keep working.
"""
from __future__ import annotations

import gettext
import os
import random

# ── Locale directory (relative to this file) ───────────────────────────────
_LOCALE_DIR = os.path.join(os.path.dirname(__file__), "locales")
_DOMAIN = "catai"

# Module-level state
_current_lang: str = "fr"
_translation: gettext.GNUTranslations | gettext.NullTranslations = gettext.NullTranslations()


def set_lang(lang: str) -> None:
    """Activate the translation catalog for *lang* (fr / en / es).

    Falls back to French when *lang* is not available.
    """
    global _current_lang, _translation
    _current_lang = lang
    try:
        _translation = gettext.translation(_DOMAIN, _LOCALE_DIR, languages=[lang, "fr"])
    except FileNotFoundError:
        _translation = gettext.translation(_DOMAIN, _LOCALE_DIR, languages=["fr"], fallback=True)


def s(key: str) -> str:
    """Return the translated string for *key*, falling back to the key itself."""
    translated = _translation.gettext(key)
    # gettext returns the key unchanged when no translation is found
    return translated


# Initialise with default language
set_lang(_current_lang)


# ── Backward-compatible class API ──────────────────────────────────────────


class L10n:
    """Legacy class API kept for backward compatibility.

    ``L10n.s("key")`` delegates to the module-level ``s()`` function.
    Assigning ``L10n.lang = "en"`` calls ``set_lang("en")`` under the hood.
    """

    # meows stay as plain dicts — they're random pools, not translatable strings.
    meows = {
        "fr": ["Miaou~", "Mrrp!", "Prrrr...", "Miaou miaou!", "Nyaa~", "*ronron*", "Mew!", "Prrrt?"],
        "en": ["Meow~", "Mrrp!", "Purrrr...", "Meow meow!", "Nyaa~", "*purr*", "Mew!", "Prrrt?"],
        "es": ["Miau~!", "Mrrp!", "Purrrr...", "Miau miau!", "Nyaa~", "*ronroneo*", "Mew!", "Prrrt?"],
    }

    # --- lang property so ``L10n.lang = x`` triggers set_lang() ---
    class _LangDescriptor:
        """Descriptor that syncs the class attribute with the module-level set_lang()."""
        def __get__(self, obj, objtype=None):
            return _current_lang
        def __set__(self, obj, value):
            set_lang(value)

    lang = _LangDescriptor()

    # Also support ``L10n.lang = "en"`` on the *class itself* (not an instance).
    # Python descriptors on a class need a metaclass __set__ to intercept
    # class-level assignment.  Instead we use __init_subclass__ isn't needed;
    # we override __class_getitem__ isn't right either.  The simplest
    # backward-compat approach: make set_lang a classmethod and have callers
    # use L10n.set_lang("en") OR direct attribute assignment via a metaclass.

    class _Meta(type):
        @property
        def lang(cls):
            return _current_lang

        @lang.setter
        def lang(cls, value):
            set_lang(value)

    # Rebuild L10n with the metaclass — we need a small indirection.

    @classmethod
    def s(cls, key: str) -> str:  # noqa: N805
        return s(key)

    @classmethod
    def set_lang(cls, lang: str) -> None:
        set_lang(lang)

    @classmethod
    def random_meow(cls) -> str:
        return random.choice(cls.meows.get(_current_lang, cls.meows["fr"]))


# Apply metaclass so ``L10n.lang = "en"`` works at class level.
class L10n(L10n, metaclass=L10n._Meta):  # type: ignore[no-redef]
    pass
