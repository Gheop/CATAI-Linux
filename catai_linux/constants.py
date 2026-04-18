"""Shared constants for CATAI-Linux.

Extracted from ``catai_linux.app`` — pure data, no side-effects.
"""
from __future__ import annotations

import enum


# ── Rendering / behaviour timing ──────────────────────────────────────────────

RENDER_MS = 125        # 8 FPS
BEHAVIOR_MS = 1000     # 1 Hz
WALK_SPEED = 4
PETTING_THRESHOLD_MS = 800  # long-press duration to enter petting mode
DEFAULT_SCALE = 1.5
MIN_SCALE = 0.5
MAX_SCALE = 4.0

# Small safety margin at the bottom for sub-pixel rendering. The real
# offset for the GNOME top bar is computed per-cat via _canvas_y_offset.
BOTTOM_MARGIN = 5


# ── CatState enum ─────────────────────────────────────────────────────────────

class CatState(enum.Enum):
    IDLE = "idle"
    WALKING = "walking"
    EATING = "eating"                # transitory state while AI is thinking
    ANGRY = "angry"
    SLEEPING = "sleeping"
    WAKING_UP = "waking_up"
    SOCIALIZING = "socializing"      # frozen during cat-to-cat encounter
    SLEEPING_BALL = "sleeping_ball"  # curled in a ball, breathing slowly
    CHASING_MOUSE = "chasing_mouse"
    # catset-specific states
    FLAT = "flat"
    LOVE = "love"
    GROOMING = "grooming"
    ROLLING = "rolling"
    SURPRISED = "surprised"
    JUMPING = "jumping"
    CLIMBING = "climbing"
    DASHING = "dashing"
    DYING = "dying"
    FALLING = "falling"
    HURTING = "hurting"
    LANDING = "landing"
    LEDGECLIMB_STRUGGLE = "ledgeclimb_struggle"
    LEDGEGRAB = "ledgegrab"
    LEDGEIDLE = "ledgeidle"
    WALLCLIMB = "wallclimb"
    WALLGRAB = "wallgrab"
    CHASING_BUTTERFLY = "chasing_butterfly"
    PLAYING_BALL = "playing_ball"
    DANCING = "dancing"
    STRETCHING = "stretching"
    YAWNING = "yawning"
    POUNCING = "pouncing"
    SITTING_WITH_BIRD = "sitting_with_bird"
    FISHING = "fishing"
    SNEAKING = "sneaking"
    HELLO_KITTY = "hello_kitty"
    BANDAGED = "bandaged"
    PIROUETTE = "pirouette"
    ROLLING_ON_BACK = "rolling_on_back"
    BOTHERED_BY_BEE = "bothered_by_bee"
    BOTHERED_BY_FLY = "bothered_by_fly"
    SLEEPING_BY_FIRE = "sleeping_by_fire"
    WALKING_IN_PUDDLE = "walking_in_puddle"


ANIM_KEYS = {
    CatState.WALKING: "running-8-frames",
    CatState.EATING: "eating",
    CatState.ANGRY: "angry",
    CatState.WAKING_UP: "waking-getting-up",
    CatState.SLEEPING_BALL: "sleeping-ball",
    CatState.CHASING_MOUSE: "chasing-mouse",
    CatState.FLAT: "flat",
    CatState.LOVE: "love",
    CatState.GROOMING: "grooming",
    CatState.ROLLING: "rolling",
    CatState.SURPRISED: "surprised",
    CatState.JUMPING: "jumping",
    CatState.CLIMBING: "climbing",
    CatState.DASHING: "dash",
    CatState.DYING: "die",
    CatState.FALLING: "fall",
    CatState.HURTING: "hurt",
    CatState.LANDING: "land",
    CatState.LEDGECLIMB_STRUGGLE: "ledgeclimb-struggle",
    CatState.LEDGEGRAB: "ledgegrab",
    CatState.LEDGEIDLE: "ledgeidle",
    CatState.WALLCLIMB: "wallclimb",
    CatState.WALLGRAB: "wallgrab",
    CatState.CHASING_BUTTERFLY: "chasing-butterfly",
    CatState.PLAYING_BALL: "playing-ball",
    CatState.DANCING: "dancing",
    CatState.STRETCHING: "stretching",
    CatState.YAWNING: "yawning",
    CatState.POUNCING: "pouncing",
    CatState.SITTING_WITH_BIRD: "sitting-with-bird",
    CatState.FISHING: "fishing",
    CatState.SNEAKING: "sneaking",
    CatState.HELLO_KITTY: "hello-kitty",
    CatState.BANDAGED: "bandaged",
    CatState.PIROUETTE: "pirouette",
    CatState.ROLLING_ON_BACK: "rolling-on-back",
    CatState.BOTHERED_BY_BEE: "bothered-by-bee",
    CatState.BOTHERED_BY_FLY: "bothered-by-fly",
    CatState.SLEEPING_BY_FIRE: "sleeping-by-fire",
    CatState.WALKING_IN_PUDDLE: "walking-in-puddle",
}

ONE_SHOT_STATES = {
    CatState.EATING, CatState.ANGRY, CatState.WAKING_UP,
    CatState.CHASING_MOUSE,
    CatState.FLAT, CatState.LOVE, CatState.GROOMING, CatState.ROLLING,
    CatState.SURPRISED, CatState.JUMPING, CatState.CLIMBING,
    CatState.FALLING,
    CatState.LANDING, CatState.LEDGECLIMB_STRUGGLE, CatState.LEDGEGRAB,
    CatState.WALLCLIMB,
    CatState.CHASING_BUTTERFLY, CatState.PLAYING_BALL, CatState.DANCING,
    CatState.STRETCHING, CatState.YAWNING, CatState.POUNCING,
    CatState.SITTING_WITH_BIRD, CatState.FISHING, CatState.SNEAKING,
    CatState.HELLO_KITTY, CatState.BANDAGED, CatState.PIROUETTE,
    CatState.ROLLING_ON_BACK, CatState.BOTHERED_BY_BEE,
    CatState.BOTHERED_BY_FLY, CatState.SLEEPING_BY_FIRE,
    CatState.WALKING_IN_PUDDLE,
}


# ── Sequences ─────────────────────────────────────────────────────────────────

class SequenceStep:
    __slots__ = ('state', 'direction_mode', 'pause_after', 'loop_count')
    def __init__(self, state, direction_mode="inherit", pause_after=0, loop_count=1):
        self.state = state
        self.direction_mode = direction_mode  # "inherit" | "south"
        self.pause_after = pause_after        # ticks to hold on last frame
        self.loop_count = loop_count          # how many times to play

SEQUENCES = {
    "wall_adventure": [
        SequenceStep(CatState.WALLCLIMB),
        SequenceStep(CatState.WALLGRAB, pause_after=8),
        SequenceStep(CatState.FALLING, direction_mode="south"),
        SequenceStep(CatState.LANDING, direction_mode="south"),
    ],
    "ledge_adventure": [
        SequenceStep(CatState.LEDGEGRAB),
        SequenceStep(CatState.LEDGEIDLE, loop_count=2),
        SequenceStep(CatState.LEDGECLIMB_STRUGGLE),
        SequenceStep(CatState.CLIMBING),
    ],
    "dash_crash": [
        SequenceStep(CatState.DASHING),
        SequenceStep(CatState.HURTING, direction_mode="south"),
    ],
    "full_jump": [
        SequenceStep(CatState.JUMPING, direction_mode="south"),
        SequenceStep(CatState.FALLING, direction_mode="south"),
        SequenceStep(CatState.LANDING, direction_mode="south"),
    ],
    "drama_queen": [
        SequenceStep(CatState.HURTING, direction_mode="south"),
        SequenceStep(CatState.DYING, direction_mode="south"),
        SequenceStep(CatState.HURTING, direction_mode="south", loop_count=3),
        SequenceStep(CatState.WAKING_UP, direction_mode="south"),
    ],
}


# ── Cat Personalities ─────────────────────────────────────────────────────────

CATSET_CHARS = [
    ("cat_orange", "🟠"),
    ("cat01",      "🟤"),
    ("cat02",      "⬛"),
    ("cat03",      "🟫"),
    ("cat04",      "🩶"),
    ("cat05",      "🖤"),
]

# Parent cat → child kitten (for love-encounter births)
CAT_TO_KITTEN = {
    "cat_orange": "kitten_orange",
    "cat01":      "kitten01",
    "cat02":      "kitten02",
    "cat03":      "kitten03",
    "cat04":      "kitten04",
    "cat05":      "kitten05",
}

# Circadian trait: nocturnal cats are more active at night and sleepy during
# the day; diurnal cats (everyone else) are the reverse. Picked from the
# CATSET_PERSONALITIES traits — cat02 (Ombre = "shadow, mysterious") and
# cat05 (Minuit = "midnight, nocturnal") are the two cats whose written
# personality explicitly evokes the night.
NOCTURNAL_CHAR_IDS = frozenset({"cat02", "cat05"})

# Personality-driven "preferred animations" for the all_new bucket in
# behavior_tick. When a cat rolls into that 58% bucket, there's a 40%
# chance it picks from its own preferred set instead of the uniform
# 16-entry list. Derived from each cat's written traits:
#   - Mandarine "espiègle et joueur"       → bouncy/chasey
#   - Tabby     "curieux et aventurier"    → exploration / chase
#   - Ombre     "mystérieux et silencieux" → stealth / perches
#   - Noisette  "doux et réconfortant"     → cuddly / relaxed
#   - Brume     "sage et philosophe"       → contemplative / stillness
#   - Minuit    "élégant et nocturne"      → graceful / composed
PERSONALITY_PREFERRED_ANIMS = {
    "cat_orange": ("chasing_butterfly", "playing_ball", "pouncing"),
    "cat01":      ("chasing_butterfly", "sneaking", "pouncing"),
    "cat02":      ("sneaking", "sitting_with_bird", "sleeping_by_fire"),
    "cat03":      ("rolling_on_back", "sleeping_by_fire", "hello_kitty"),
    "cat04":      ("stretching", "yawning", "sitting_with_bird", "fishing", "sleeping_by_fire"),
    "cat05":      ("pirouette", "stretching", "sitting_with_bird"),
}

CATSET_PERSONALITIES = {
    "cat_orange": {
        "name": {"fr": "Mandarine", "en": "Tangerine", "es": "Mandarina"},
        "traits": {"fr": "espiègle et joueur", "en": "mischievous and playful", "es": "travieso y juguetón"},
        "skills": {"fr": "Tu adores faire des bêtises et tu es toujours en mouvement.",
                   "en": "You love getting into mischief and are always on the move.",
                   "es": "Te encanta hacer travesuras y siempre estás en movimiento."},
        # fr_FR-upmc-medium has 2 speakers: jessica=0, pierre=1. We
        # combine speaker_id with length_scale variations so each of
        # the 6 catset characters has a distinct voice profile without
        # needing a second voice model download.
        "tts_voice": {"speaker_id": 0, "length_scale": 0.90},  # perky jessica
    },
    "cat01": {
        "name": {"fr": "Tabby", "en": "Tabby", "es": "Tabby"},
        "traits": {"fr": "curieux et aventurier", "en": "curious and adventurous", "es": "curioso y aventurero"},
        "skills": {"fr": "Tu explores chaque recoin et poses beaucoup de questions.",
                   "en": "You explore every corner and ask lots of questions.",
                   "es": "Exploras cada rincón y haces muchas preguntas."},
        "tts_voice": {"speaker_id": 1, "length_scale": 0.95},  # curious pierre
    },
    "cat02": {
        "name": {"fr": "Ombre", "en": "Shadow", "es": "Sombra"},
        "traits": {"fr": "mystérieux et silencieux", "en": "mysterious and silent", "es": "misterioso y silencioso"},
        "skills": {"fr": "Tu observes tout sans rien dire et parles peu, mais avec profondeur.",
                   "en": "You observe everything silently and speak rarely but deeply.",
                   "es": "Observas todo en silencio y hablas poco, pero con profundidad."},
        "tts_voice": {"speaker_id": 1, "length_scale": 1.18},  # slow grave pierre
    },
    "cat03": {
        "name": {"fr": "Noisette", "en": "Hazel", "es": "Avellana"},
        "traits": {"fr": "doux et réconfortant", "en": "gentle and comforting", "es": "dulce y reconfortante"},
        "skills": {"fr": "Tu aimes câliner et remonter le moral de tout le monde.",
                   "en": "You love to cuddle and cheer everyone up.",
                   "es": "Te encanta mimar y animar a todos."},
        "tts_voice": {"speaker_id": 0, "length_scale": 1.05},  # soft jessica
    },
    "cat04": {
        "name": {"fr": "Brume", "en": "Mist", "es": "Niebla"},
        "traits": {"fr": "sage et philosophe", "en": "wise and philosophical", "es": "sabio y filosófico"},
        "skills": {"fr": "Tu médites et partages des pensées profondes sur la vie.",
                   "en": "You meditate and share deep thoughts about life.",
                   "es": "Meditas y compartes pensamientos profundos sobre la vida."},
        "tts_voice": {"speaker_id": 1, "length_scale": 1.25},  # slow wise pierre
    },
    "cat05": {
        "name": {"fr": "Minuit", "en": "Midnight", "es": "Medianoche"},
        "traits": {"fr": "élégant et nocturne", "en": "elegant and nocturnal", "es": "elegante y nocturno"},
        "skills": {"fr": "Tu es à ton aise dans l'obscurité et racontes des histoires mystérieuses.",
                   "en": "You're at home in the dark and tell mysterious stories.",
                   "es": "Te sientes a gusto en la oscuridad y cuentas historias misteriosas."},
        "tts_voice": {"speaker_id": 0, "length_scale": 1.10},  # elegant jessica
    },
}
