# CATAI-Linux

Virtual desktop pet cats for Linux (GNOME/Wayland) -- pixel art cats that roam your screen and chat with you via AI.

![Python](https://img.shields.io/badge/Python-GTK4-blue) ![Linux](https://img.shields.io/badge/Linux-Fedora%2FGNOME-orange) ![Claude](https://img.shields.io/badge/Claude-AI-blueviolet) ![Ollama](https://img.shields.io/badge/Ollama-LLM-green)

Port of [CATAI](https://github.com/wil-pe/CATAI) (macOS/Swift) to Linux.

![CATAI-Linux Screenshot](screenshot2.png)

![CATAI-Linux Chat](screenshot1.png)

## Features

- **Desktop companion** -- Cats roam freely across your screen with pixel-perfect animations
- **Click-through** -- Cats float above all windows, clicks pass through to apps below
- **Multi-cat** -- Up to 6 cats with distinct colors and personalities
- **AI chat** -- Click a cat to open a pixel-art chat bubble, powered by [Claude](https://claude.ai) or [Ollama](https://ollama.ai)
- **Random meows** -- Cats spontaneously say "Miaou~", "Prrr...", "Mrrp!" in cute speech bubbles
- **Drag & drop** -- Drag cats anywhere on your screen
- **Multilingual** -- French, English, Spanish
- **Persistent** -- Cats remember their conversations between sessions

## Cat Personalities

Each cat has a unique personality that shapes how it responds in conversations:

| Color | Name | Personality | Specialty |
|-------|------|-------------|-----------|
| Orange | Citrouille | Playful & mischievous | Jokes & puns |
| Black | Ombre | Mysterious & philosophical | Deep questions |
| White | Neige | Elegant & poetic | Poetry & grace |
| Grey | Einstein | Wise & scholarly | Science facts |
| Brown | Indiana | Adventurous storyteller | Epic tales |
| Cream | Caramel | Cuddly & comforting | Emotional support |

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
```

## Run

```bash
catai
```

## Settings

Right-click any cat to access Settings:

- **Language** -- French / English / Spanish
- **Cats** -- Click a sprite to add, click x to remove
- **Name** -- Rename each cat
- **Size** -- Scale slider
- **Model** -- Choose between Claude and Ollama models
- **Autostart** -- Launch at login

## How It Works

- Single fullscreen transparent canvas with Cairo rendering
- XShape input passthrough -- clicks go through to apps below
- Pillow for sprite loading and per-pixel HSB color tinting
- Claude API or Ollama for streaming AI chat
- 368 hand-drawn sprites (8 directions x 5 animations)
- Lazy loading + disk cache for instant startup
- Config persisted in `~/.config/catai/`

## Development

```bash
make lint    # Run ruff linter
make fix     # Auto-fix lint issues
make e2e     # Run E2E test suite (26 tests)
make run     # Launch the app
make build   # Build wheel + sdist
```

## Project Structure

```
.
├── catai_linux/          # Python package
│   ├── app.py            # Main application
│   ├── __main__.py       # Entry point (python -m catai_linux)
│   └── cute_orange_cat/  # Sprite assets (68x68 PNG)
├── tests/
│   └── e2e_test.py       # E2E test suite (socket-based)
├── pyproject.toml        # Package config + linter config
├── Makefile              # make run / lint / e2e / build
└── .github/workflows/    # CI: lint + PyPI publish
```

## Credits

- Original macOS version: [wil-pe/CATAI](https://github.com/wil-pe/CATAI)
- Sprite assets from the original project

## License

MIT
