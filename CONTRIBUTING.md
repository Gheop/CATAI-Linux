# Contributing to CATAI

Thanks for your interest in contributing! This guide covers the most common
workflows.

## Getting started

```bash
git clone https://github.com/Gheop/CATAI.git
cd CATAI
pip install -e '.[dev,voice]'
make test          # headless unit tests
make lint          # ruff check
```

## Adding an easter egg

1. Open `catai_linux/easter_eggs.py`.
2. Add your trigger string to `EASTER_EGGS` (the key is a regex pattern,
   the value is the method name minus the `eg_` prefix).
3. If your egg requires a specific phrase typed in the chat, also add it
   to `MAGIC_EGG_PHRASES`.
4. Implement an `eg_<name>(self)` method on `EasterEggMixin`. The method
   receives `self` (the main app) and can call any GTK/animation helper.
5. Add a test in `tests/test_modules.py` that verifies the trigger is
   detected and the method exists.
6. Run `make test` to confirm.

## Adding a character pack

Character packs live in `~/.local/share/catai/characters/<pack_name>/`.

Required layout:

```
<pack_name>/
  metadata.json      # name, author, version, description
  personality.json   # system prompt, temperature, traits
  rotations/         # idle sprite PNGs (frame_00.png, frame_01.png, ...)
```

See `catai_linux/character_packs.py` for the validation logic. The
`validate_pack()` function checks that all required files are present and
that JSON files parse correctly.

## Adding a voice command

1. Open `catai_linux/wake_word.py` and add your verb to `COMMAND_VERBS`.
2. In `catai_linux/app.py`, find `_on_wake_word_heard` and add a handler
   branch for your new verb.
3. Write a unit test in `tests/test_modules.py` that asserts your verb is
   recognized and dispatched correctly.
4. Run `make test`.

## Adding a translation

1. Create the locale directory:
   ```bash
   mkdir -p catai_linux/locales/<lang>/LC_MESSAGES/
   ```
2. Copy an existing `.po` file as a template:
   ```bash
   cp catai_linux/locales/fr/LC_MESSAGES/catai.po \
      catai_linux/locales/<lang>/LC_MESSAGES/catai.po
   ```
3. Translate the `msgstr` entries.
4. Add a `msgfmt` line to the `messages` target in `Makefile`.
5. Run `make messages` to compile the `.mo` file.

## Code style

- Linter: **ruff** (`make lint` / `make fix`).
- Max line length: **140**.
- Target: **Python 3.10+**.
- All modules use `from __future__ import annotations`.
- Imports are sorted with ruff's isort rules.

## Testing

| Command      | What it does                              |
|--------------|-------------------------------------------|
| `make test`  | Headless unit tests (`test_modules.py`)   |
| `make e2e`   | End-to-end tests (needs a display / Xvfb) |
| `make lint`  | Ruff linting                              |

Tests must pass before merging. CI runs all three on every PR.

## Releases

1. Bump the version in `pyproject.toml`.
2. Update the `## Changelog` section in `README.md`.
3. Commit, tag (`git tag vX.Y.Z`), and push (`git push --tags`).
4. GitHub Actions builds the wheel and publishes to PyPI.
