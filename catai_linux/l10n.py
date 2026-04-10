"""Localization for CATAI-Linux — UI strings + random meow vocabulary.

All three supported languages (fr / en / es) live in a single flat table.
Access strings via `L10n.s("key")` which falls back to French if the current
language or the key is missing, then to the key itself as a last resort.
"""
import random


class L10n:
    lang = "fr"
    strings = {
        "title":    {"fr": ":: RÉGLAGES ::", "en": ":: SETTINGS ::", "es": ":: AJUSTES ::"},
        "cats":     {"fr": "MES CHATS", "en": "MY CATS", "es": "MIS GATOS"},
        "name":     {"fr": "Nom :", "en": "Name:", "es": "Nombre:"},
        "size":     {"fr": "TAILLE", "en": "SIZE", "es": "TAMAÑO"},
        "model":    {"fr": "MODÈLE IA", "en": "AI MODEL", "es": "MODELO IA"},
        "quit":     {"fr": "Quitter", "en": "Quit", "es": "Salir"},
        "settings": {"fr": "Réglages...", "en": "Settings...", "es": "Ajustes..."},
        "talk":     {"fr": "Parle au chat...", "en": "Talk to the cat...", "es": "Habla al gato..."},
        "hi":       {"fr": "Miaou! ~(=^..^=)~", "en": "Meow! ~(=^..^=)~", "es": "¡Miau! ~(=^..^=)~"},
        "loading":  {"fr": "Chargement...", "en": "Loading...", "es": "Cargando..."},
        "no_ollama": {"fr": "(Ollama indisponible)", "en": "(Ollama unavailable)", "es": "(Ollama no disponible)"},
        "err":      {"fr": "Mrrp... pas de connexion", "en": "Mrrp... no connection", "es": "Mrrp... sin conexión"},
        "err_auth": {"fr": "Mrrp... token expiré, relance 'claude' pour renouveler", "en": "Mrrp... token expired, run 'claude' to renew", "es": "Mrrp... token expirado, ejecuta 'claude' para renovar"},
        "refreshing_auth": {"fr": "Renouvellement du token Claude...", "en": "Refreshing Claude token...", "es": "Renovando token de Claude..."},
        "lang_label": {"fr": "LANGUE", "en": "LANGUAGE", "es": "IDIOMA"},
        "autostart": {"fr": "Lancer au démarrage", "en": "Start at login", "es": "Iniciar al arrancar"},
        "encounters": {"fr": "Rencontres entre chats", "en": "Cat encounters", "es": "Encuentros entre gatos"},
        # rm -rf easter egg — 'just kidding!' shown by the wiping cat at the end
        "rm_rf_jk": {
            "fr": "Je plaisante ! 😹",
            "en": "Just kidding! 😹",
            "es": "¡Es broma! 😹",
        },
        # Caps Lock easter egg — L10n fallback when the AI-generated pool
        # isn't ready yet (first trigger) or the AI backend isn't available.
        "capslock_yell": {
            "fr": "POURQUOI TU CRIES ??",
            "en": "WHY ARE YOU SHOUTING?!",
            "es": "¿POR QUÉ GRITAS?!",
        },
    }
    meows = {
        "fr": ["Miaou~", "Mrrp!", "Prrrr...", "Miaou miaou!", "Nyaa~", "*ronron*", "Mew!", "Prrrt?"],
        "en": ["Meow~", "Mrrp!", "Purrrr...", "Meow meow!", "Nyaa~", "*purr*", "Mew!", "Prrrt?"],
        "es": ["Miau~!", "Mrrp!", "Purrrr...", "Miau miau!", "Nyaa~", "*ronroneo*", "Mew!", "Prrrt?"],
    }

    @classmethod
    def s(cls, key: str) -> str:
        d = cls.strings.get(key, {})
        return d.get(cls.lang) or d.get("fr") or key

    @classmethod
    def random_meow(cls) -> str:
        return random.choice(cls.meows.get(cls.lang, cls.meows["fr"]))
