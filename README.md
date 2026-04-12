# CATAI-Linux

Virtual desktop pet cats for Linux (GNOME/Wayland) -- pixel art cats that roam your screen and chat with you via AI.

![Python](https://img.shields.io/badge/Python-GTK4-blue) ![Linux](https://img.shields.io/badge/Linux-Fedora%2FGNOME-orange) ![Claude](https://img.shields.io/badge/Claude-AI-blueviolet) ![Ollama](https://img.shields.io/badge/Ollama-LLM-green)

Port of [CATAI](https://github.com/wil-pe/CATAI) (macOS/Swift) to Linux.

![CATAI-Linux demo](demo.gif)

![Love encounters and kitten births](love_demo.gif)

![Overlays showcase — sleeping, love, surprised, grooming](screenshot1.png)

![Chat bubble with AI response](screenshot2.png)

![Animation sequences — dashing, drama queen, wall climb](screenshot3.png)

## Features

- **Desktop companion** -- Cats roam freely across your screen with pixel-perfect animations
- **Click-through** -- Cats float above all windows, clicks pass through to apps below
- **6 unique characters** -- Pre-colored 80×80 sprites from the catset collection, each with a distinct look and personality
- **AI chat** -- Click a cat to open a pixel-art chat bubble, powered by [Claude](https://claude.ai) or [Ollama](https://ollama.ai)
- **Voice chat** 🎤 (optional) -- Hold the mic button or simply hold **Space** to talk to your cats. 100% local transcription via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), GPU-accelerated if you have CUDA
- **Wake word** 👂 (optional) -- Each cat answers to its own renameable first name. Say "Mandarine" or "Tabby" out loud and the matching cat opens its chat bubble + starts listening. Powered by [Vosk](https://alphacephei.com/vosk/) (offline, ~41 MB FR model, Apache-2.0)
- **Voice commands** 🗣️ -- Chain a verb after a cat's name to pilot it directly: *"Mandarine dors"* (sleep), *"Tabby viens"* (come to cursor), *"Ombre raconte"* (tell a story), *"Noisette danse"* (mini-disco), plus *saute* and *roule*. No chat bubble, just instant action.
- **Rich animations** -- 23 animation states including running, dashing, sleeping, grooming, climbing, wall grab, ledge climbing, dying & resurrection, and more
- **Animation sequences** -- Multi-step scripted behaviors: wall adventures, ledge climbing, dash crashes, dramatic deaths with resurrection
- **Visual overlays** -- Floating ZzZ, hearts, speed lines, hurt stars, skulls, sparkles above cats during animations
- **Random meows** -- Cats spontaneously say "Miaou~", "Prrr...", "Mrrp!" in cute speech bubbles
- **Drag & drop** -- Drag cats anywhere on your screen
- **Cat encounters** -- When two cats cross paths, they stop and have a short AI-generated conversation
- **Love encounters & kittens** -- Sometimes a cat falls in love; if the other reciprocates, a kitten is born with the parent's color (fade-in + sparkles). Kittens can't chat yet — they meow with baby emojis 🐭 🍼 ♥ 🧸
- **22 hidden easter eggs** 🥚 -- type `easter egg` in a chat bubble for the full menu, or trigger any one directly with a magic phrase: `nyan`, `rain`, `hug`, `42`, `matrix`, `thanos`, `boss fight`, `disco`, `stampede`, `meow party`, `don't panic`... and many more. Even Nyan Cat with a proper 6-frame animated sprite and wavy tiled rainbow trail!
- **Multilingual** -- French, English, Spanish
- **Persistent** -- Cats remember their conversations between sessions

## Cat Characters

Each character is a pre-colored sprite with a unique personality:

| Sprite | Name (default) | Personality | Specialty |
|--------|---------------|-------------|-----------|
| 🟠 Orange | Mandarine | Espiègle et joueur | Bêtises et mouvement |
| 🟤 Tabby | Tabby | Curieux et aventurier | Explorer chaque recoin |
| ⬛ Dark | Ombre | Mystérieux et silencieux | Paroles rares mais profondes |
| 🟫 Brown | Noisette | Doux et réconfortant | Câlins et bonne humeur |
| 🩶 Grey | Brume | Sage et philosophe | Pensées profondes sur la vie |
| 🖤 Black | Minuit | Élégant et nocturne | Histoires mystérieuses |

Names and languages adapt to your selected language (FR / EN / ES).

## AI Backend

CATAI-Linux supports two AI backends for cat conversations:

| Backend | Setup | Speed | Cost |
|---------|-------|-------|------|
| **Claude** (recommended) | Auto-detected if [Claude Code](https://claude.ai/download) is installed | ~1-2s | Included with Claude subscription |
| **Ollama** | `./setup-ollama.sh` | ~2-5s | Free (local) |

Claude is auto-detected from Claude Code's credentials (`~/.claude/.credentials.json`) or the `ANTHROPIC_API_KEY` environment variable. If neither is available, CATAI falls back to Ollama.

## Requirements

- Linux with GNOME, KDE, or any X11/XWayland desktop
- Python 3.10+

## Install

```bash
# System dependencies (GTK4 + Cairo bindings, not available via pip)
# Fedora:
sudo dnf install python3-gobject
# Ubuntu/Debian:
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0

# Install from PyPI
pip install catai-linux

# Optional: adds ~100 MB of extra deps (faster-whisper + CTranslate2)
# needed for the push-to-talk voice chat feature. Only install this if
# you actually want to talk to your cats.
pip install catai-linux[voice]
```

## Run

```bash
catai
```

## Settings

Right-click any cat to access Settings:

- **Language** -- French / English / Spanish
- **Characters** -- Click a sprite to add it, click again to select it (shows name + personality), click × to remove
- **Name** -- Rename each cat directly in the settings panel
- **Size** -- Scale slider
- **Model** -- Choose between Claude and Ollama models
- **Autostart** -- Launch at login
- **Cat encounters** -- Enable/disable random cat-to-cat conversations
- **Voice chat** -- Enable push-to-talk mic button + hold-Space recording (requires `pip install catai-linux[voice]`)
- **Voice model** -- Pick the Whisper model (tiny → large-v3), each annotated with size and recommended device

## How It Works

- Single fullscreen transparent canvas with Cairo rendering
- XShape / GDK input region passthrough -- clicks go through to apps below
- 80×80 pre-colored PNG sprites (catset by seethingswarm, upscaled 2× with nearest-neighbor)
- 23 animation states with multi-step sequences (wall adventure, ledge climbing, dash crash, dramatic death)
- Generalized pixel-accurate offset compensation for seamless transitions between any animation
- Visual overlays via PangoCairo: ZzZ, hearts, speed lines, hurt stars, skulls, sparkles, anger marks
- Claude API or Ollama for streaming AI chat
- Lazy loading + disk cache for instant startup
- Config persisted in `~/.config/catai/`

## Development

```bash
make lint    # Run ruff linter
make fix     # Auto-fix lint issues
make test    # Run headless unit tests (59 assertions, no GTK display needed)
make e2e     # Run the full E2E test suite (48 assertions via Unix socket)
make run     # Launch the app
make build   # Build wheel + sdist
```

`make e2e` runs locally against a real GDK display + a real AI backend. In
CI, the same suite runs inside the `e2e-tests` job of
`.github/workflows/test.yml` via Xvfb + metacity + a `MockChat` backend
activated by `CATAI_MOCK_CHAT=1`, so every PR gets the 48 assertions
validated against a fresh Ubuntu runner before merge.

Sprite conversion (catset spritesheets → CATAI character directories):

```bash
python3 scripts/catset_to_catai.py catset_assets/catset_spritesheets catai_linux/
```

Regenerate README assets (screenshots + demo GIF, no screen capture required):

```bash
python3 tools/render_screenshots.py   # → screenshot1..3.png
python3 tools/render_demo_gif.py      # → demo.gif
```

## Project Structure

```
.
├── catai_linux/          # Python package
│   ├── __main__.py       # Entry point (python -m catai_linux)
│   ├── app.py            # Main application + CatAIApp + CatInstance
│   ├── chat_backend.py   # Claude + Ollama streaming backends
│   ├── voice.py          # Push-to-talk recording + Whisper transcription
│   ├── drawing.py        # Cairo/Pango helpers (bubbles, overlays, CSS)
│   ├── x11_helpers.py    # Low-level Xlib/XShape via ctypes
│   ├── l10n.py           # Localization strings (fr/en/es)
│   ├── cat_orange/       # Sprite assets — orange cat (80×80 PNG)
│   ├── cat01..05/        # Sprite assets — tabby/dark/brown/grey/black
│   └── kitten*/          # Kitten sprites (inherited from cat parent)
├── scripts/
│   ├── catset_to_catai.py   # Catset spritesheet conversion
│   ├── kittens_to_catai.py  # Kitten spritesheet conversion
│   └── sd_postprocess.py    # Stable Diffusion output post-processing
├── tools/
│   ├── sprite_preview.py    # Visual preview of all sprites + overlays
│   ├── render_screenshots.py  # Generate README screenshots
│   ├── render_demo_gif.py     # Generate demo.gif
│   └── render_love_demo.py    # Generate love_demo.gif
├── tests/
│   └── e2e_test.py       # E2E test suite (26 tests via Unix socket)
├── pyproject.toml        # Package config + linter config
├── Makefile              # make run / lint / e2e / build
└── .github/workflows/    # CI: lint + PyPI publish
```

## Credits

- Original macOS version: [wil-pe/CATAI](https://github.com/wil-pe/CATAI)
- Catset sprites: [seethingswarm/catset](https://github.com/seethingswarm/catset) (CC0)

## License

MIT

---

## Changelog

### v0.8.0 — Audit profond : dead code, bugs, sécurité, perfs (2026-04-12)

Audit complet du codebase après la série v0.7.x. Zéro nouvelle feature,
100% qualité.

#### Code mort supprimé
- `_CatsetMarker` + `_CATSET_COLOR_DEF` (vestige du color-tinting v0.3.0)
- `self.color_def` + paramètre `color_def_obj` dans `CatInstance.__init__`
- `_check_deps()` méthode vide + appel dans `do_activate`
- `active_ids` (legacy `color_id` set) dans `SettingsWindow._build`
- `self._entry_window` (attribut jamais lu)
- `import re` inline dans `send_chat()` → déplacé au top-level
- `from math import log` inline dans `memory.py` → `import math` top-level

#### Bugs corrigés
- **`for/else` piège** dans `_apply_sequence_offset_compensation` — le
  `else` du `for` s'exécutait quand la boucle ne breakait pas, pas quand
  `_sequence` était `None`. Remplacé par un `if/else` explicite avec
  `min()` bound-safe.
- **Double `now = time.monotonic()`** dans `_check_encounters` — la
  deuxième affectation écrasait inutilement la première.
- **Attributs dynamiques** sur `CatInstance` — `_state_tick`,
  `_die_threshold`, `_die_resurrect`, `_sleep_tick`, `_petting_active`,
  `_hidden`, `_boss_scale`, `_beam_ticks`, `_rm_rf_active`,
  `_nyan_active` sont maintenant tous initialisés explicitement dans
  `__init__`, pas via `getattr()` dispersés dans 4000 lignes.

#### Sécurité
- **Test socket déplacé** de `/tmp/catai_test.sock` vers
  `$XDG_RUNTIME_DIR/catai_test.sock` + `os.chmod(path, 0o600)` après
  bind. Avant, n'importe quel user du système pouvait y envoyer des
  commandes (force_state, quit, type_chat, etc.).
- **Timeout DoS sur les sockets** — `conn.settimeout(5.0)` ajouté sur
  les deux sockets (API + test) après `setblocking(True)`. Un client
  malveillant qui connecte sans envoyer ne bloque plus le GTK main loop
  indéfiniment.
- **Log d'avertissement visible** dans l'auto-updater avant d'utiliser
  `--break-system-packages` (PEP 668 fallback). Choix de design
  conscient, mais maintenant l'utilisateur le voit dans ses logs.
- **Commentaire explicite** dans `chat_backend.py` sur le choix de ne
  pas refuser de lire les credentials en cas de permissions trop
  ouvertes (trade-off : casser l'app vs alerter l'user).

#### Optimisations
- **`pil_to_surface`** — l'échange RGBA→BGRA passe par `PIL.Image.split()`
  + `merge()` natif au lieu d'une boucle Python par pixel. ~50x plus
  rapide sur un sprite 80×80.
- **`_sprite_floor_y` + `_sprite_center_x`** — passent par
  `PIL.Image.getbbox()` sur le canal alpha au lieu d'un scan pixel par
  pixel. De O(w×h) Python à un seul appel C-level.
- **Metrics** — `track()` accumule maintenant en mémoire et flush tous
  les 30 s (+ au shutdown) au lieu de faire load()+save() à chaque
  appel. Économise 2-3 écritures disque par seconde pendant un chat
  actif.
- **`memory.py`** — `from math import log` sorti de la boucle hot-path
  vers un `import math` top-level.

#### Non inclus (trop risqué pour cette passe)
- Rename `cat01` → `cat_01` (convention de nommage) — nécessite une
  migration des données utilisateur. Reporté.
- Extraction de `SettingsWindow` et `CatEncounter` dans des modules
  séparés — `app.py` fait 6700+ lignes mais le refactoring structurel
  risque des régressions en cascade. Reporté.

### v0.7.5 — Fix browser pop-up sneaky (2026-04-11)

**Bug surprise** : quand le token Claude OAuth était complètement
expiré (pas juste l'access token mais aussi le refresh token), notre
helper `_refresh_claude_token` lançait `claude -p ok` qui se rabattait
sur le flow d'auth interactif et **ouvrait une fenêtre browser sur la
page Claude marketing/login** sans prévenir, en plein milieu de
n'importe quelle action de fond (encounter, reaction pool, drift,
extraction de mémoire, etc.).

**Fix** : on lance maintenant `claude -p ok` dans un environnement
*headless-only* en strippant `DISPLAY`, `WAYLAND_DISPLAY` et `BROWSER`,
plus en forçant `BROWSER=/bin/false`. Le CLI ne peut plus pop de
browser. Si le refresh échoue vraiment, le chat lève proprement
`err_auth` et la bulle affiche un message poli — l'utilisateur peut
re-auth manuellement avec `claude -p ok` depuis un vrai terminal
quand il en a envie.

5 nouveaux unit tests qui mockent `subprocess.run` et asserent que
l'env passé strip bien tout ce qu'il faut. Total 353 passed / 0 failed.

### v0.7.4 — Voice commands directes (2026-04-11)

Extension du wake word : au lieu d'ouvrir un chat à chaque fois qu'on
appelle un chat par son prénom, on peut maintenant lui chaîner un
**verbe** pour le piloter directement, sans bulle de chat ni écoute
push-to-talk. Six verbes français pour commencer.

| Phrase | Action |
|---|---|
| *« Mandarine »* | Comportement précédent : ouvre le chat + démarre une PTT 6 s |
| *« Mandarine dors »* | Le chat se met en SLEEPING_BALL immédiatement |
| *« Mandarine viens »* | Le chat marche jusqu'au curseur de la souris |
| *« Mandarine raconte »* | Ouvre le chat et injecte un prompt « raconte-moi une anecdote » à l'IA |
| *« Mandarine danse »* | Mini-disco loop sur ce chat (LOVE/ROLLING/GROOMING alternance, 5 sec) |
| *« Mandarine saute »* | Animation JUMPING |
| *« Mandarine roule »* | Animation ROLLING |

Détails techniques :

- **Grammaire Vosk étendue** — les 6 verbes sont ajoutés à la grammaire
  fermée du recognizer en plus des prénoms. Vosk compose librement, donc
  *« Mandarine dors »* est transcrit en un seul résultat.
- **Parser look-ahead 2 tokens** — après avoir matché un prénom, on
  cherche un verbe dans les 2 tokens suivants. Permet à l'utilisateur de
  hésiter (« euh Mandarine, dors ») mais évite les faux positifs sur des
  verbes très éloignés.
- **`viens` utilise XQueryPointer** — nouveau helper Xlib ctypes
  `get_mouse_position()` qui lit la position absolue du curseur sans
  fork. Fallback à un point random sur monitor 0 si la query échoue.
- **`raconte` localisé** — le prompt envoyé au LLM est traduit en FR/EN/ES
  selon `L10n.lang`, donc la langue de la réponse suit ta config.
- **`danse` scopé à un seul chat** — réutilise la liste d'états
  célébratoires de l'easter egg `eg_disco` mais ne met que le chat appelé
  en mode danse, les autres continuent leur vie. Auto-cleanup après 5 s.
- **Backward compat** — l'ancienne signature `on_wake(cat_id)` reste
  supportée par un fallback `TypeError` dans `_fire`. Tous les
  consumers existants continuent de marcher.
- **23 nouveaux unit tests** : COMMAND_VERBS, parsing, look-ahead,
  callback dispatch, legacy compat, get_mouse_position safety. Total
  348 passed / 0 failed.

Pas de nouvelle dépendance — Vosk était déjà dans `[voice]`.

### v0.7.3 — Cleanup subprocess X11 (2026-04-11)

Suite à la discussion autour du portage Wayland natif (#7) : tant que
GNOME Mutter ne supporte pas `wlr-layer-shell` (issue ouverte depuis
2019, sans signe de mouvement), CATAI reste obligatoirement en
**Xwayland sur GNOME** parce que `XShape` n'a pas d'équivalent sans
layer-shell. Du coup on a fait un cleanup intermédiaire pour gagner
en propreté côté code sans toucher à l'architecture.

- **`xprintidle` subprocess viré** — l'idle detection passe par
  `org.gnome.Mutter.IdleMonitor` D-Bus via `Gio.DBusProxy` (PyGObject
  natif, zéro nouvelle dep). Fallback subprocess `xprintidle` sur
  setups non-GNOME, fallback `0` sinon. Coût d'un appel : ~0.1 ms vs
  ~5-15 ms pour une fork().
- **`xprop` subprocess pour fullscreen viré** — `_is_any_fullscreen()`
  passe par 2 appels `XGetWindowProperty` directs en ctypes
  (`_NET_ACTIVE_WINDOW` puis `_NET_WM_STATE`). ~50 µs au total contre
  ~30 ms pour les 2 fork() précédents. Le poll fullscreen tourne à
  0.67 Hz donc l'économie cumulée est ~20 ms/sec.
- **`xdotool getwindowgeometry` viré** — la détection du Y offset GNOME
  top bar passe par `XTranslateCoordinates` direct en ctypes. One-shot
  au boot, mais une fork() en moins.
- **Fallbacks subprocess morts retirés** — `_run_x11()` et les
  fallbacks `xdotool windowmove` / `wmctrl` / `xprop -set` étaient
  inatteignables en pratique (libX11 toujours dispo). 30 lignes de
  plomberie en moins.
- **Module `subprocess` retiré complètement de `app.py`** — un fichier
  de moins à parcourir pour comprendre les dépendances externes.
- **3 deps runtime virées** : `xdotool`, `xprop`, `xprintidle` ne sont
  plus jamais appelées. Tu peux désinstaller ces packages sans rien
  casser. Toujours requis pour `make e2e` (le harnais de test, pas
  l'app elle-même).

Aucun changement fonctionnel visible. Économie CPU : ~6 fork()
supprimés par seconde, sub-1% de charge. Plus important : code plus
propre, plus testable, future-proof contre la dépréciation progressive
de la session X11 sur GNOME.

### v0.7.2 — Bubble truncation fix (2026-04-11)

- **Fix** — les réponses de Claude/Ollama étaient parfois tronquées avec
  « … » en plein milieu d'une phrase parce que la bulle de chat était
  capée à 8 lignes alors que `max_tokens=256` produit jusqu'à ~14 lignes
  au width courant. Cap relevé à 16 lignes (`drawing.py` + `app.py` en
  lockstep). Plus aucune réponse normale ne devrait se faire couper.

### v0.7.1 — Wake word par prénom de chat (2026-04-11)

Issue #4 Tier 2 : appeler un chat par son **prénom** ouvre son chat
bubble et démarre une écoute push-to-talk automatique. Pas besoin de
toucher le clavier ni la souris.

- **Wake word renommable à la volée** — chaque chat répond à son
  propre prénom (`Mandarine`, `Tabby`, `Ombre`, …). Renommer un chat
  depuis Settings → la grammaire est rebâtie immédiatement, l'ancien
  nom oublié, le nouveau actif. Aucun retraining.
- **Backend Vosk** (Apache-2.0, 100 % offline). Modèle FR small
  (~41 MB) téléchargé en arrière-plan au premier lancement, mis en
  cache dans `~/.cache/catai/vosk/`. Idle ~3-8 % CPU.
- **Coexistence avec le push-to-talk existant** — le pipeline wake
  word libère `autoaudiosrc` automatiquement avant chaque
  enregistrement Whisper, puis le récupère ensuite.
- **Opt-in** — désactivé par défaut, case à cocher dans la section
  Voice du panneau Settings. Une seconde case règle le miaou de
  confirmation. Pas de vosk installé ? Pas de souci, le module
  no-op silencieusement et le reste de la voix continue de marcher.
- Nouvelle dépendance optionnelle dans l'extra `[voice]` :
  `vosk>=0.3.45`. Métriques `wake_words_triggered` (par chat) si
  les stats locales sont activées.

### v0.7.0 — Mémoire et écosystème (2026-04-11)

Two big themes: **cats now remember you across sessions** and the
**ecosystem opens up** for community character packs + shell-script
integration. Five PRs landed (#26-#30) closing issues #5 and #9.

#### AI personality depth (#5)

- **Long-term memory** (#29) — each cat keeps a per-cat sqlite store
  of "memorable facts" at `~/.config/catai/memory.db`. Every 20 chat
  exchanges, the AI extracts 0-3 facts about you from the recent
  conversation. On every new message, the keyword-overlap retriever
  finds the top 3 facts that share words with your input and injects
  them into the system prompt as "things you remember about this
  user". **No embeddings, no heavy ML deps** — pure stdlib + sqlite.
- **Inter-cat gossip** (#30) — when two cats encounter each other on
  the canvas (the existing love encounter trigger), they each pick
  one random fact from their memory pile and add it to the other
  cat's memory, prefixed with `<cat name> m'a raconté: `. Recipients
  later surface that fact as second-hand knowledge. Effect: chat
  with Mandarine for an hour → she remembers your name → she meets
  Ombre → Ombre also knows.

#### Dev & ecosystem (#9)

- **Plugin API for character packs** (#28) — drop a folder under
  `~/.local/share/catai/characters/<char_id>/` with `metadata.json`,
  `personality.json`, and sprite directories, and CATAI auto-loads
  it as a new playable cat at startup. Strict validation, silent
  skip on errors. Lets contributors ship custom catset characters
  without forking. New module `catai_linux/character_packs.py`.
- **Scriptable hooks via Unix socket** (#27) — opt-in via
  `api_enabled` in config.json. Opens
  `$XDG_RUNTIME_DIR/catai.sock` (mode 0600, same-user only) with
  curated commands: `status`, `list_cats`, `list_eggs`,
  `meow <idx> [text]`, `egg <key>`, `notify`, `help`. Lets shell
  scripts make cats react to external events:
  ```bash
  make test && echo 'egg meow_party' | nc -U $XDG_RUNTIME_DIR/catai.sock
  ```
- **Local metrics dashboard** (#26) — opt-in privacy-first stats
  tracker at `~/.config/catai/stats.json`. Tracks chats sent, voice
  recordings, eggs triggered, love encounters, kittens born, pet
  sessions, per-cat counters. Settings panel shows live summary
  with top petted cats and top eggs, plus a 'Reset stats' button.
  **Zero telemetry**, never transmitted anywhere.

### v0.6.2 — Set and forget (2026-04-11)

CATAI now updates itself. Once installed via pip, every launch
silently checks GitHub for a new release, runs `pip install --upgrade`
in the background, and notifies you with a meow bubble that the new
version is ready for the next launch. (#24, #25)

- **Auto-update on launch** — new `catai_linux/updater.py` polls
  `api.github.com/repos/Gheop/CATAI-Linux/releases/latest` (cached
  1 h), compares to the installed version via `importlib.metadata`,
  and runs `python -m pip install --user --upgrade catai-linux[voice]`
  in a subprocess if a non-prerelease update is available. PEP 668
  systems get an automatic `--break-system-packages` retry.
- **3 modes** in Settings → Updates dropdown:
    - `Auto-install on launch` (default) — check + install + notify
    - `Notify only` — check + meow bubble, no install
    - `Off` — no network call
- **'Check now' button** in settings bypasses the 1 h cache.
- **Privacy + safety** — one anonymous GET to api.github.com per
  hour, no telemetry, `pip --user` scoped (never touches the system
  Python), all failure modes silent so a network blip can't break
  the running app. New code activates only on the NEXT launch.

### v0.6.1 — Les chats parlent (2026-04-11)

Closes the loop with v0.4.0 voice input: **cats now speak their chat
responses out loud**. Hybrid pipeline mixes real CC0 cat sound samples
(meow, purr, mrrp, hiss) with Piper TTS for the text portions, so
'*ronron* Bonjour mon ami!' plays a real purr sample then a French
voice saying 'Bonjour mon ami', not a human pronouncing 'ronron'
phonetically. (#23)

- **TTS hybrid pipeline** — new `catai_linux/tts.py` splits each
  AI response into alternating cat-sound + text chunks. Cat tokens
  play CC0 WAV samples bundled in `catai_linux/sounds/` (165 KB).
  Text chunks go through Piper TTS (`fr_FR-upmc-medium`, ~74 MB,
  downloaded to `~/.cache/catai/piper/` on first use).
- **6 distinct cat voices** — `fr_FR-upmc-medium` is multi-speaker
  (jessica, pierre); each catset character gets its own
  speaker_id × length_scale combo (Mandarine perky, Ombre slow grave,
  Brume sage, etc).
- **Speaker icon in chat bubble** — 🔊 / 🔇 toggle in the top-right
  of every chat bubble. Click to mute that specific cat. Clicking
  while a cat is mid-sentence kills playback immediately.
- **Sound effects toggle** — Settings → 'Voice output' has a sub
  checkbox to play TTS text only (no cat samples) for users who
  find the interjections distracting.
- **Pixel-art mic / speaker / sablier icons** — bundled hand-drawn
  PNGs in `catai_linux/icons/` matching the cream/brown bubble palette.
- **Seasonal overlay first-launch-only** — the 30 s season announce
  (snow, petals, pumpkins, etc) now only fires on the first launch
  per day, tracked via `~/.config/catai/seasonal_last_shown`. No
  more falling petals every time you restart CATAI.
- **Smarter prompt** — system prompt now requires AT LEAST one full
  real sentence per response and forbids `*stage directions*` and
  emoji, so the TTS never reads 's'étire' phonetically.
- **Robustness fixes** — every TTS playback runs in an isolated
  `gst-launch-1.0` subprocess to avoid in-process GStreamer state
  contamination, and pressing the mic button stops any in-flight
  TTS so the recording isn't echoed back. Chat bubble tail flips
  to the top edge when the cat is near the screen top, and the
  text wraps around the speaker icon instead of running under it.

### v0.6.0 — Les chats prennent vie (2026-04-11)

Major interactivity + visual update. Five self-contained features landed
together — petting, moods, drift, theme sync, seasonal overlays, 3 more
easter eggs, and multi-monitor awareness.

- **Petting + invisible mood** — long-press a cat to pet it. Every cat
  tracks four hidden stats (happiness, energy, bored, hunger) that drift
  over time and bias its emergent behavior. A grumpy cat lounges. A
  bored one bounces around. Persisted in `~/.config/catai/mood_*.json`.
- **User activity awareness** — cats detect when you go AFK (> 10 min
  idle) and curl up to sleep until you return. They also dim activity
  late at night.
- **D-Bus notification reactions** — when a desktop notification arrives,
  the nearest cat reacts with a curious meow. Reaction lines are
  AI-generated per cat so each one sounds like themselves.
- **Dark mode sync** — bubbles + menus now follow your GNOME dark/light
  preference automatically (polled every 30 s). Opt in bit: no config
  needed. (#18)
- **Seasonal overlays** — date-aware particles drift behind the cats:
  ❄ snowflakes in winter, 🌼 cherry petals in spring, 🍂 autumn leaves,
  🎃 Halloween pumpkins, 🎄 + heavier snow for Christmas, ♥ Valentine's,
  ✨ NYE firework bursts. The overlay announces the current season for
  30 s on launch then fades out to leave the canvas clean. Opt out via
  `"seasonal": false` or `"seasonal_duration_sec": 0` for permanent
  display. (#19)
- **30 easter eggs** — added 🎮 Konami code (cheat-mode cascade
  SURPRISED → LOVE → ROLLING + mood refresh), ☕ coffee rush (2× cat
  speed for 15 s), and 🧘 zen mode (all cats freeze in perfect
  meditation for 10 s). Magic phrases: `konami`, `up up down down`,
  `cheat code`, `coffee`, `espresso`, `caffeine`, `zen`, `meditate`,
  `calm`. Existing 27 eggs still there. (#20)
- **Multi-monitor intelligence** — cats now spawn distributed across
  every attached screen instead of stacked on monitor 0. New
  `monitors` socket command inspects layout and detects dead-zones
  between mismatched displays. (#21)
- **Personality drift** — every 10 chat messages the cat silently asks
  the AI backend to reflect on the conversation and propose ONE new
  "quirk" it picked up from you. Quirks accumulate (max 5, oldest
  falls off) in `~/.config/catai/personality_*.json` and flavor every
  future chat's system prompt. No embeddings, no RAG — pure prompt
  engineering. Opt out via `"personality_drift": false`. (#22)

### v0.5.0 — Big cleanup: modular refactor + smaller wheel (2026-04-10)

Internal overhaul — no user-facing feature changes. If v0.4.0 works for
you, v0.5.0 works the same, just lighter and cleaner under the hood.

- **Wheel size: 2.5 MB → 1.6 MB** (−36%) — removed the legacy
  `cute_orange_cat/` sprite directory (2.1 MB of 68×68 sprites that
  were shipped in every release since v0.1.0 but have not been loaded
  at runtime since the v0.3.0 catset migration). Removed the obsolete
  `MANIFEST.in` that was keeping them in the distribution.
- **Modular architecture**: `catai_linux/app.py` split from 6044 lines
  into 6 focused modules. Smaller files, easier to navigate, easier to
  test, lazy imports for optional features:
  - `app.py` (4200 lines) — `CatAIApp`, render loop, main
  - `chat_backend.py` (310 lines) — `ChatBackend`, `ClaudeChat`,
    `OllamaChat`, `create_chat`, Claude OAuth helpers, Ollama probing
  - `voice.py` (220 lines) — `VoiceRecorder`, Whisper model metadata,
    HuggingFace cache detection
  - `drawing.py` (480 lines) — CSS theme + all Cairo/Pango drawing
    helpers (speech bubbles, overlays, context menu, sparkles)
  - `x11_helpers.py` (310 lines) — low-level Xlib/XShape via ctypes
  - `l10n.py` (44 lines) — localization strings + random meow pool
- **Dead code purge: −800 lines** of unused classes, helpers, and
  unreachable legacy branches:
  - `ChatBubbleController` class (150 lines) — old Gtk.Window-based
    bubble, replaced by the Cairo canvas rendering in v0.3.x
  - Legacy color-tinting system (`CatColorDef` class, `CAT_COLORS`
    table with 6 pre-baked personalities, `tint_sprite`,
    `rgb_to_hsb`/`hsb_to_rgb`, `load_and_tint` with disk cache)
  - 6 unreachable `CatState` enum members (`DRINKING`, `PLAYING_BALL`,
    `BUTTERFLY`, `SCRATCHING_TREE`, `PEEING`, `POOPING`) plus their
    dispatch branches and all 7 drawing helpers that existed only to
    render their props (butterfly, tree with bark+foliage+scratches,
    pee drops, poop drops with flies, yarn ball)
  - `CatAIApp.add_cat` / `remove_cat` / `rename_cat` — legacy
    color-based cat management, superseded by `add_catset_char` etc.
  - `SettingsWindow._on_bubble_*` / `_on_name_changed` — handlers for
    the old color-bubble UI, never connected to any widget
  - `draw_pixel_border`, `_pango_text_width`, `_load_anims_bg`,
    `_get_preview`, `_get_anim_frames` — helpers with zero callers
- **Perf**: cached Pango layout across render ticks in
  `_position_chat_entry()` (was re-allocating a `cairo.ImageSurface`,
  context, layout, and font description every frame when a chat bubble
  was visible — ~0.5–1 ms/frame saved). Dirty-key cache for
  `_update_input_regions()` skips the expensive `cairo.Region` rebuild
  on frames where no cat has moved (the common idle case — ~0.5 ms
  saved per skipped frame on an 8 FPS loop).
- **Code quality**: `_handle_test_cmd()` (197 lines, 19 if/elif
  branches) refactored into 21 small `_cmd_*` handlers dispatched via
  a dict — each command is now an independently-readable 5–10 line
  method. Added `_get_cat_at_idx()` helper to deduplicate index
  parsing.
- **Type hints** on all public APIs of the new modules
  (`ChatBackend.send`, `VoiceRecorder.*`, drawing helpers, etc.) for
  IDE autocomplete and future `mypy` support.
- **Housekeeping**: `.gitignore` now excludes raw sprite sources
  (`catset_assets/`, `kittens_assets/`); removed obsolete
  `scripts/generate_sprites.py`, `scripts/catai_sprites.ipynb`,
  `docs/feature_kittens.md`, and empty `scripts/references/` /
  `scripts/sd_workflows/` directories.

### v0.4.0 — Voice chat + instant startup (2026-04-10)

- **Voice chat (push-to-talk)** 🎤 — talk to your cats instead of typing. Hold the mic button next to the chat entry, or simply **hold Space** while the entry is focused and empty (just like `/voice` in Claude Code). Release to auto-transcribe and send.
- **100% local & private** — speech-to-text runs on-device via [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Nothing is sent to any cloud service for transcription.
- **GPU auto-detection** — CUDA is used automatically if available (float16), otherwise CPU int8. Tested on RTX 2050 / 5090.
- **Whisper model picker in Settings** — 7 models from `tiny` (39 MB, CPU) to `large-v3` (1.5 GB, GPU), each annotated with size and recommended device. Changes take effect immediately, no restart needed.
- **Model preload at startup** — if the selected Whisper model is already cached, it loads into memory in the background while the cats spawn, so the first recording is instant (no 1–3 s wait).
- **Token refresh feedback** — when the Claude OAuth token needs refreshing mid-conversation, the chat bubble now shows an animated braille spinner with "Renouvellement du token Claude..." instead of a silent stall.
- **Instant click on startup** — fixed a 4–5 s freeze where clicking on a cat did nothing during the first seconds. All heavy per-cat setup (sprite loading, animation offsets, pixel-scan floor detection, chat backend creation) now runs in background threads so the main event loop is responsive from t=0.
- **Soft dependency** — the voice feature is opt-in. Install with `pip install catai-linux[voice]`, then enable via `--voice` CLI flag or Settings checkbox. The mic button only appears when enabled, and the feature degrades gracefully if `faster-whisper` is not installed.
- **Settings window** — auto-sized to fit screen height (max 900 px, min 480 px), scrollable for smaller displays.

### v0.3.6 — 22 easter eggs + Nyan Cat + magic phrases (2026-04-10)

- **Easter egg menu** 🥚 — type `easter egg` in any chat bubble to open a clickable menu of 22 fun effects. The menu disappears as soon as you click an egg so you can actually see the effect
- **22 easter eggs** with direct magic phrases (type them in a chat bubble):
  - 💥 Apocalypse (`apocalypse`, `kaboom`, or `Don't panic`) — kittens double every second up to 1000
  - 🌀 Circle 42 (`42`, `circle`, `answer`) — cats form a HGttG circle
  - 🎉 Meow party (`meow`, `party`) — all cats meow at once
  - 🏃 Stampede (`stampede`, `run`) — everyone dashes together
  - 😴 Sleepy time (`sleep`, `zzz`, `bedtime`) — mass nap
  - 🤗 Group hug (`hug`, `hugs`) — cluster in LOVE
  - 🕺 Disco (`disco`, `dance`) — cats cycle through colorful states
  - 🌧 Rain of cats (`rain`, `raining`) — cats fall from the sky with gravity
  - 📳 Shake (`shake`, `earthquake`) — global screen shake
  - 🌿 Catnip (`catnip`, `nip`) — everyone rolls
  - 📈 Stonks (`stonks`) — cats climb slowly upward
  - 🐌 / ⏩ Slow/Fast motion (`slow`, `fast`) — 3× slower or 2× faster for 10s
  - 💀 Thanos snap (`snap`, `thanos`) — half the cats fade and vanish
  - 🛸 Beam me up (`beam`, `teleport`) — random cat shoots up with light beam
  - 🌍 Hello, World! (`hello`, `hi`) — a cat says it
  - 🥪 sudo sandwich (`sudo`, `sandwich`) — xkcd reference
  - 🙈 Hide & seek (`hide`, `hide and seek`) — all cats vanish except one
  - 🟢 Matrix (`matrix`, `neo`) — green digital rain overlay
  - 👹 Boss fight (`boss`, `fight`) — one cat grows 2.2× into an ANGRY boss, others circle around. After 5s the boss dies in drama_queen while shrinking and the crowd reacts with SURPRISED
  - 👣 Follow the leader (`follow`, `follow me`) — all cats walk toward the current focused cat
  - 🌈 **Nyan Cat!** (`nyan`, `nyan cat`) — the classic Nyan Cat with 6-frame animation flies across the screen, trailing a tiled wavy rainbow
- **Nyan Cat asset pipeline**: `catai_linux/nyan_cat.png` (6-frame sprite sheet) + `catai_linux/nyan_rainbow.png` (clean 6-stripe tile) drawn via Cairo with nearest-neighbor filter
- **DASHING speed lines polished**: foot streaks flush with the cat + dust particles around — much more "wind/motion" than the previous ladder effect
- **`rain` egg**: cats now actually fall with gravity acceleration, then transition to LANDING when they hit the floor
- **`follow_leader`**: robust fallback when there's no active chat cat
- **Test socket additions**: `easter_menu`, `egg <key>`, `love_encounter <a> <b>` for scripted demos

### v0.3.5 — Screen-edge clamp with top bar + sprite padding awareness (2026-04-10)

- **Cats no longer drop off the bottom of the screen**: the clamp now accounts for the GNOME top bar offset (detected via `_canvas_y_offset`) — previously the canvas window extended ~32 px below the visible area, so cats at `screen_h - display_h` were rendered partially off-screen
- **Flush-to-bottom alignment**: the clamp also factors in the sprite's own empty bottom padding (transparent rows between the cat's feet and the sprite box edge), so cats sit right against the screen edge with their feet visible instead of hovering 15–20 px above
- **Per-cat `_sprite_bottom_padding`**: computed once at setup from the idle rotation's `_sprite_floor_y`, recomputed on scale change
- **Test socket `status`** now reports screen resolution and top bar offset for easier debugging
- **`move_cat` test command** routes through `_clamp_to_screen` so scripted tests honour the same bounds as normal behaviour

### v0.3.4 — Aggressive love + wall slide polish (2026-04-10)

- **Love encounter flow swap (ANGRY)**: when the reaction is angry, cat A is now the aggressor (starts in ANGRY from the beginning) and cat B becomes the victim who plays the drama_queen sequence — previously it was the opposite
- **Cats face each other during encounters**: new `_face_toward()` helper on CatInstance flips the sprite horizontally when needed, since love/angry/flat sprites only exist as south (east-facing) frames
- **No more random deaths**: the `drama_queen` sequence is no longer triggered randomly during idle — deaths now only happen contextually:
  - Wall slide crash at the bottom of the screen (40% chance after a significant slide)
  - ANGRY reaction during a love encounter (cat B dies and revives)
- **Wall slide "glass surface" feel**: WALLGRAB now slides with acceleration (2 → 8 px/tick), slides up to ~360 px over ~4 s, much more natural movement vs the previous 1 px/tick constant
- **Rebalanced behavior**: climbing probability boosted (3% → 7%) and wall_adventure reduced (4% → 2%) to prevent cats accumulating at the bottom of the screen over time
- **Screen-bounds clamp**: triple defensive clamp (render_tick start + end + draw loop) with a 5px safety margin so cats can never be drawn off the bottom of the screen, regardless of animation or sequence side-effects
- **Fix love_demo.gif**: cat B now faces west (toward cat A) in all still scenes — previously was facing away
- **Fix test socket**: survives `ncat --send-only` (BrokenPipeError is caught) so scripted testing works reliably
- **Makefile**: new `make run-test` target launches with `--test-socket`

### v0.3.3 — Love encounters + baby kittens (2026-04-10)

- **Love encounters**: 40% of cat-to-cat encounters now skip the dialogue and become silent love moments. The initiator enters LOVE state with floating hearts, and the other cat reacts with ANGRY 💢 (30%), SURPRISED !!! (30%), or LOVE ♥ (40%)
- **Kitten births**: when both cats reciprocate, a kitten is born at the midpoint with a magical fade-in + grow + ✨ sparkles animation
- **Kitten characters**: 6 new 64×64 sprites (kitten_orange, kitten01..05) extracted from seethingswarm's kitten spritesheets via `scripts/kittens_to_catai.py`. Kittens inherit one parent's color at birth (cat01→kitten01, cat_orange→kitten_orange, etc.)
- **Baby meows**: kittens can't talk yet — their meows and click responses show random baby emojis (🐭 🍼 ♥ 🧸 🐣 🥛 ✨ 🎀 💨 🐱...)
- **Kitten rules**: ephemeral (not saved across restarts), max 6 at once, no chat dialogue, no transformation to adult, excluded from regular encounters
- **Sprite viewer**: 6 new characters in `sprite_viewer.html`, proportionally smaller (kittens at 192px vs cats at 240px)
- **New tool**: `tools/render_love_demo.py` generates `love_demo.gif` showing the full love encounter flow + birth
- **Test socket**: new `love_encounter <idx_a> <idx_b>` command to manually trigger a love encounter
- **Fix**: test socket no longer crashes on `ncat --send-only` (BrokenPipeError is caught and logged); `CatEncounter.cancel` no longer warns when a timer has already fired
- **Makefile**: new `make run-test` target that launches with `--test-socket` for automation

### v0.3.2 — README demo + proactive auth refresh (2026-04-10)

- **New README assets**: animated `demo.gif` + 3 static screenshots rendered directly via Cairo (no screen capture needed — bypasses Wayland entirely)
- **Rendering tools**: `tools/render_screenshots.py` and `tools/render_demo_gif.py` — reproducible, deterministic, GIF palette-optimized (~230 KB for 14s @ 12fps)
- **Proactive Claude auth refresh**: token is now refreshed BEFORE it expires (5 min buffer) instead of waiting for a 401, eliminating authentication errors during chat
- **Test socket extensions**: new commands `force_state`, `start_sequence`, `meow`, `move_cat`, `fake_chat` for scripted testing and automation
- **Fix**: `cat_positions` test command was using the old `color_id` field that no longer exists on catset cats

### v0.3.1 — Animation sequences + visual overlays (2026-04-09)

- **10 new animations**: dash, die, fall, hurt, land, wall climb, wall grab, ledge grab, ledge idle, ledge struggle
- **Multi-step sequences**: wall adventure (climb → grab → fall → land), ledge adventure, dash crash, full jump, drama queen (hurt → die → resurrect)
- **Visual overlays**: ZzZ (sleep), ♥ (love), !!! (surprised), ✦ (hurt), 💀 (dying), ✨ (grooming), 💢 (angry), speed lines (dash)
- **Dashing**: cats sprint across the screen at 3× walk speed with speed lines behind them
- **Dying & resurrection**: cats stay dead 5-10s with floating skull, then hurt animation plays 3× before waking up
- **Wall grab sliding**: cats slide slowly downward while grabbing a wall, then let go
- **Sprite preview tool**: `python3 tools/sprite_preview.py` — visual grid of all cats, animations, overlays, and bubbles
- **Fix**: meow bubble and overlay positioning (relative to cat head, not bounding box top)

### v0.3.0 — Catset characters + new animations (2026-04-09)

- **6 pre-colored characters** replacing the old single-sprite + color-tinting system: orange, tabby, dark, brown, grey, black (80×80 catset sprites)
- **New animation states**: flat/sit, love loaf, grooming, rolling, surprised, jumping, climbing
- **Climbing mechanic**: cats climb walls with pixel-accurate floor/centroid measurement so transitions to the next animation are seamless
- **Settings rework**: click a cat sprite to select it and see its name (editable), personality trait, and description
- **Walk directions**: east/west only — catset sprites don't have north/south/diagonal walk frames
- **Removed**: old color-tinting system, drinking, playing ball, butterfly, scratching tree, peeing, pooping animations (not in catset)
- **Fix**: click-to-chat bubble not triggering on Wayland (GDK input region was skipped)
- **Fix**: meow bubble squished — height now based on actual Pango text metrics, not a hardcoded `24px`

### v0.2.2 — Emoji in bubbles + bubble overflow fix (2026-04-04)

- Speech bubbles now render emoji correctly via PangoCairo (COLRv1 fonts)
- Chat bubble no longer overflows screen edges — clamps and wraps text properly

### v0.2.1 — Cat encounters (2026-04-01)

- When two cats cross paths they stop, face each other, and exchange 1–3 AI-generated lines before going their separate ways
- Encounter bubbles shown above each cat with pixel-art style

### v0.2.0 — Multi-cat + drag & drop (2026-03-28)

- Up to 6 simultaneous cats, each with a distinct color and AI personality
- Drag cats anywhere on screen
- Persistent chat memory per cat across sessions

### v0.1.0 — Initial release (2026-03-20)

- Single desktop cat with pixel-art animations
- AI chat via Claude or Ollama
- Click-through transparent window (XShape)
- French / English / Spanish support
