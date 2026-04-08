# CATAI-Linux

[![PyPI](https://img.shields.io/pypi/v/catai-linux)](https://pypi.org/project/catai-linux/)
[![Python](https://img.shields.io/pypi/pyversions/catai-linux)](https://pypi.org/project/catai-linux/)
[![License](https://img.shields.io/github/license/Gheop/CATAI-Linux)](https://github.com/Gheop/CATAI-Linux/blob/main/LICENSE)
[![CI](https://github.com/Gheop/CATAI-Linux/actions/workflows/lint.yml/badge.svg)](https://github.com/Gheop/CATAI-Linux/actions)

Virtual desktop pet cats for Linux -- pixel art cats that roam your screen and chat with you via AI (Claude or Ollama).

![CATAI-Linux](https://raw.githubusercontent.com/Gheop/CATAI-Linux/main/screenshot2.png)

## Install

```bash
# System dependencies
# Fedora:
sudo dnf install python3-gobject python3-pillow xdotool wmctrl
# Ubuntu/Debian:
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 python3-pil xdotool wmctrl

# Install from PyPI
pip install catai-linux
```

## Run

```bash
catai
```

## Features

- Animated pixel-art cats roaming freely across your screen
- 6 cat personalities with AI chat (Claude API or Ollama)
- Always-on-top, drag & drop, random meows
- Settings UI, multilingual (FR/EN/ES), autostart option

Full documentation on [GitHub](https://github.com/Gheop/CATAI-Linux).
