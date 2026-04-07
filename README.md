# CATAI-Linux

Virtual desktop pet cats for Linux (GNOME/Wayland) -- pixel art cats that roam your screen and chat with you via AI.

![Python](https://img.shields.io/badge/Python-GTK4-blue) ![Linux](https://img.shields.io/badge/Linux-Fedora%2FGNOME-orange) ![Claude](https://img.shields.io/badge/Claude-AI-blueviolet) ![Ollama](https://img.shields.io/badge/Ollama-LLM-green)

Port of [CATAI](https://github.com/wil-pe/CATAI) (macOS/Swift) to Linux.

![CATAI-Linux Screenshot](screenshot2.png)

![CATAI-Linux Chat](screenshot1.png)

## Features

- **Desktop companion** -- Cats roam freely across your screen with pixel-perfect animations
- **Always on top** -- Cats stay visible above all windows
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

- Linux with X11 or XWayland (GNOME, KDE, etc.)
- Python 3.10+

## Install (Fedora)

```bash
sudo dnf install python3-gobject python3-pillow xdotool wmctrl
pip install httpx anthropic
```

## Run

```bash
python3 catai.py
# or
make run
```

## Setup Ollama (optional)

```bash
./setup-ollama.sh
```

Pulls `gemma3:1b` (~815MB), a lightweight model. Other options:

```bash
ollama pull gemma3:4b     # Better quality, ~3GB
ollama pull phi4-mini     # Microsoft, ~2.5GB
ollama pull qwen3:1.7b    # Alibaba, ~1.3GB
```

## Settings

Right-click any cat to access Settings:

- **Language** -- French / English / Spanish
- **Cats** -- Click a color to add, click x to remove
- **Name** -- Rename each cat
- **Size** -- Scale slider
- **Model** -- Choose between Claude and Ollama models

## How It Works

- Single Python file, zero framework dependencies beyond GTK4
- GTK4 + XWayland for transparent, always-on-top overlay windows
- Pillow for sprite loading and per-pixel HSB color tinting
- Claude API or Ollama for streaming AI chat
- 368 hand-drawn sprites (8 directions x 5 animations)
- Config persisted in `~/.config/catai/`

## Development

```bash
make lint    # Run ruff linter
make fix     # Auto-fix lint issues
```

CI runs ruff on every push/PR via GitHub Actions.

## Project Structure

```
.
├── catai.py              # Entire application (single file)
├── Makefile              # make run / lint / fix
├── ruff.toml             # Linter config
├── build.sh              # Launch script with dependency check
├── setup-ollama.sh       # Ollama installer
└── cute_orange_cat/      # Sprite assets (68x68 PNG)
    ├── metadata.json     # Animation definitions
    ├── rotations/        # 8 direction sprites
    └── animations/       # 5 animations x 8 directions
```

## Credits

- Original macOS version: [wil-pe/CATAI](https://github.com/wil-pe/CATAI)
- Sprite assets from the original project

## License

MIT
