# CATAI-Linux

[![PyPI](https://img.shields.io/pypi/v/catai-linux)](https://pypi.org/project/catai-linux/)
[![Python](https://img.shields.io/pypi/pyversions/catai-linux)](https://pypi.org/project/catai-linux/)
[![License](https://img.shields.io/github/license/Gheop/CATAI-Linux)](https://github.com/Gheop/CATAI-Linux/blob/main/LICENSE)
[![CI](https://github.com/Gheop/CATAI-Linux/actions/workflows/lint.yml/badge.svg)](https://github.com/Gheop/CATAI-Linux/actions)

Virtual desktop pet cats for Linux -- pixel art cats that roam your screen and chat with you via AI (Claude or Ollama).

![CATAI-Linux](https://raw.githubusercontent.com/Gheop/CATAI-Linux/main/screenshot2.png)

## Install

```bash
# System dependencies (GTK4 + Cairo, not available via pip)
# Fedora:
sudo dnf install python3-gobject
# Ubuntu/Debian:
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0

pip install catai-linux

# Optional: adds ~100 MB of deps (faster-whisper) for voice chat
pip install catai-linux[voice]
```

## Run

```bash
catai
```

## Features

- Animated pixel-art cats roaming freely across your screen
- 6 cat personalities with AI chat (Claude API or Ollama)
- **Voice chat** 🎤 (optional) — hold Space or the mic button to talk to your cats. 100% local transcription via faster-whisper, GPU-accelerated if you have CUDA
- **Cat encounters**: when two cats cross paths, they stop and have a short AI-generated conversation
- **Love encounters & kittens**: occasional love → kitten births with fade-in + sparkles
- **22 easter eggs** with magic phrases (`nyan`, `matrix`, `thanos`, `don't panic`...)
- Click-through: cats float above windows, clicks pass through
- Drag & drop, random meows, settings UI
- Multilingual (FR/EN/ES), autostart at login
- Lazy loading + disk cache for instant startup

Full documentation on [GitHub](https://github.com/Gheop/CATAI-Linux).
