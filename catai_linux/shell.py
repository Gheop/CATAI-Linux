"""Interactive CLI shell for CATAI — communicates via the Unix socket API.

Usage:
    catai-shell              # auto-detect socket
    catai-shell --socket /path/to/catai.sock

Requires the CATAI app to be running with api_enabled=true in config.
"""
from __future__ import annotations

import cmd
import json
import os
import readline
import socket
import sys
import textwrap

# ── ANSI helpers ─────────────────────────────────────────────────────────────

_BOLD = "\033[1m"
_DIM = "\033[2m"
_RST = "\033[0m"
_RED = "\033[91m"
_GRN = "\033[92m"
_YLW = "\033[93m"
_CYN = "\033[96m"
_MAG = "\033[95m"
_WHT = "\033[97m"


def _c(color: str, text: str) -> str:
    return f"{color}{text}{_RST}"


# ── Socket helpers ───────────────────────────────────────────────────────────

def _find_socket() -> str | None:
    """Return the first reachable CATAI socket path, or None."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    for name in ("catai.sock", "catai_test.sock"):
        path = os.path.join(runtime, name)
        if os.path.exists(path):
            return path
    return None


def _send(sock_path: str, command: str) -> str:
    """Send a command to the CATAI socket and return the response."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5.0)
        s.connect(sock_path)
        s.sendall(command.encode())
        s.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks).decode().strip()
    finally:
        s.close()


# ── Name resolution ──────────────────────────────────────────────────────────

def _parse_cat_name(name: str, cat_list: list[dict]) -> int | None:
    """Resolve a cat name (case-insensitive) or index string to an integer index.

    Returns None if unresolvable.
    """
    # Direct index?
    try:
        idx = int(name)
        if 0 <= idx < len(cat_list):
            return idx
        return None
    except ValueError:
        pass
    # Name lookup (case-insensitive)
    lower = name.lower()
    for cat in cat_list:
        if cat.get("name", "").lower() == lower:
            return cat["index"]
    return None


# ── Season list ──────────────────────────────────────────────────────────────

SEASON_NAMES = [
    "winter", "halloween", "christmas", "valentines", "nye",
    "spring", "autumn", "summer", "off", "on", "auto",
]


# ── Shell ────────────────────────────────────────────────────────────────────

class CatAIShell(cmd.Cmd):
    """Interactive CATAI shell."""

    intro = ""  # set in preloop
    prompt = f"  {_BOLD}{_CYN}catai>{_RST} "

    def __init__(self, sock_path: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.sock_path = sock_path or _find_socket()
        self._cat_cache: list[dict] | None = None
        self._egg_cache: list[str] | None = None
        # readline history
        self._history_path = os.path.expanduser("~/.config/catai/shell_history")
        os.makedirs(os.path.dirname(self._history_path), exist_ok=True)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def preloop(self):
        # Load readline history
        try:
            readline.read_history_file(self._history_path)
        except FileNotFoundError:
            pass

        # Banner
        banner_lines = [
            "",
            _c(_BOLD + _MAG, "  =^._.^=  CATAI Shell"),
            "",
        ]
        if not self.sock_path:
            banner_lines.append(_c(_RED, "  Aucun socket trouve !"))
            banner_lines.append(
                _c(_DIM, "  Verifiez que CATAI tourne avec api_enabled=true")
            )
            banner_lines.append(
                _c(_DIM, "  ou lancez avec --socket /chemin/vers/catai.sock")
            )
        else:
            # Test connection
            try:
                resp = self._send("status")
                banner_lines.append(
                    f"  {_c(_GRN, 'Connecte')} a {_c(_DIM, self.sock_path)}"
                )
                # Parse status response
                if resp.startswith("OK"):
                    parts = resp.split()
                    for p in parts[1:]:
                        if p.startswith("cats="):
                            n = p.split("=", 1)[1]
                            banner_lines.append(
                                f"  {_c(_CYN, n)} chat(s) actif(s)"
                            )
                        elif p.startswith("version="):
                            v = p.split("=", 1)[1]
                            banner_lines.append(
                                f"  Version {_c(_YLW, v)}"
                            )
            except Exception as e:
                banner_lines.append(
                    _c(_RED, f"  Socket trouve mais connexion echouee : {e}")
                )

        banner_lines.append("")
        banner_lines.append(
            _c(_DIM, "  Tapez 'help' pour la liste des commandes, Tab pour completer.")
        )
        banner_lines.append("")
        self.intro = "\n".join(banner_lines)

    def postloop(self):
        try:
            readline.write_history_file(self._history_path)
        except Exception:
            pass

    def emptyline(self):
        pass  # don't repeat last command

    def default(self, line: str):
        print(_c(_RED, f"  Commande inconnue : {line.split()[0]}"))
        print(_c(_DIM, "  Tapez 'help' pour voir les commandes disponibles."))

    # ── Aliases ──────────────────────────────────────────────────────────

    def do_ls(self, arg):
        """Alias pour 'cats'."""
        self.do_cats(arg)

    def do_q(self, arg):
        """Alias pour 'quit'."""
        return self.do_quit(arg)

    def do_exit(self, arg):
        """Alias pour 'quit'."""
        return self.do_quit(arg)

    # ── Socket helper ────────────────────────────────────────────────────

    def _send(self, command: str) -> str:
        if not self.sock_path:
            raise ConnectionError("Pas de socket CATAI")
        return _send(self.sock_path, command)

    def _require_socket(self) -> bool:
        """Print error if no socket. Returns True if OK."""
        if not self.sock_path:
            print(_c(_RED, "  Pas de socket CATAI connecte."))
            return False
        return True

    # ── Cat / egg caches ─────────────────────────────────────────────────

    def _fetch_cats(self) -> list[dict]:
        if self._cat_cache is not None:
            return self._cat_cache
        try:
            resp = self._send("cats")
            if resp.startswith("OK"):
                self._cat_cache = json.loads(resp[3:])
                return self._cat_cache
        except Exception:
            pass
        # Fallback: try list_cats (older API)
        try:
            resp = self._send("list_cats")
            if resp.startswith("OK"):
                cats = []
                for item in resp[3:].strip().split(" | "):
                    parts = item.strip().split(":")
                    if len(parts) >= 3:
                        cats.append({
                            "index": int(parts[0]),
                            "char_id": parts[1],
                            "name": parts[2],
                        })
                self._cat_cache = cats
                return self._cat_cache
        except Exception:
            pass
        return []

    def _cat_names(self) -> list[str]:
        return [c.get("name", "") for c in self._fetch_cats()]

    def _fetch_eggs(self) -> list[str]:
        if self._egg_cache is not None:
            return self._egg_cache
        try:
            resp = self._send("list_eggs")
            if resp.startswith("OK"):
                self._egg_cache = resp[3:].strip().split()
                return self._egg_cache
        except Exception:
            pass
        return []

    def _invalidate_cache(self):
        self._cat_cache = None
        self._egg_cache = None

    # ── Completers ───────────────────────────────────────────────────────

    def _complete_cat_name(self, text: str) -> list[str]:
        names = self._cat_names()
        if not text:
            return names
        low = text.lower()
        return [n for n in names if n.lower().startswith(low)]

    def _complete_egg_name(self, text: str) -> list[str]:
        eggs = self._fetch_eggs()
        if not text:
            return eggs
        low = text.lower()
        return [e for e in eggs if e.lower().startswith(low)]

    def _complete_season(self, text: str) -> list[str]:
        if not text:
            return SEASON_NAMES
        low = text.lower()
        return [s for s in SEASON_NAMES if s.startswith(low)]

    def complete_meow(self, text, line, begidx, endidx):
        return self._complete_cat_name(text)

    def complete_say(self, text, line, begidx, endidx):
        return self._complete_cat_name(text)

    def complete_sleep(self, text, line, begidx, endidx):
        return self._complete_cat_name(text)

    def complete_wake(self, text, line, begidx, endidx):
        return self._complete_cat_name(text)

    def complete_dance(self, text, line, begidx, endidx):
        return self._complete_cat_name(text)

    def complete_come(self, text, line, begidx, endidx):
        return self._complete_cat_name(text)

    def complete_mood(self, text, line, begidx, endidx):
        return self._complete_cat_name(text)

    def complete_egg(self, text, line, begidx, endidx):
        return self._complete_egg_name(text)

    def complete_season(self, text, line, begidx, endidx):
        return self._complete_season(text)

    # ── Resolve cat name → index ─────────────────────────────────────────

    def _resolve_cat(self, name: str) -> int | None:
        """Resolve a cat name or index. Print error if not found."""
        cats = self._fetch_cats()
        idx = _parse_cat_name(name, cats)
        if idx is None:
            print(_c(_RED, f"  Chat introuvable : '{name}'"))
            available = ", ".join(self._cat_names()) or "(aucun)"
            print(_c(_DIM, f"  Disponibles : {available}"))
        return idx

    # ── Commands ─────────────────────────────────────────────────────────

    def do_status(self, arg):
        """Affiche le statut de l'application."""
        if not self._require_socket():
            return
        resp = self._send("status")
        if resp.startswith("OK"):
            parts = resp.split()
            print()
            for p in parts[1:]:
                k, _, v = p.partition("=")
                print(f"  {_c(_CYN, k):>20s}  {v}")
            print()
        else:
            print(f"  {resp}")

    def do_cats(self, arg):
        """Liste tous les chats avec noms, positions et etats."""
        if not self._require_socket():
            return
        self._invalidate_cache()
        cats = self._fetch_cats()
        if not cats:
            print(_c(_YLW, "  Aucun chat trouve."))
            return
        print()
        print(_c(_BOLD, "  # | Nom              | Etat            | Position"))
        print(f"  {'-' * 60}")
        for c in cats:
            idx = c.get("index", "?")
            name = c.get("name", "?")
            state = c.get("state", "?")
            x = c.get("x", "?")
            y = c.get("y", "?")
            print(
                f"  {_c(_CYN, str(idx)):>5s} | "
                f"{_c(_WHT, name):<25s} | "
                f"{_c(_YLW, state):<24s} | "
                f"{x}, {y}"
            )
        print()

    do_list = do_cats

    def do_meow(self, arg):
        """meow <nom> [texte] — Affiche une bulle de meow sur un chat."""
        if not self._require_socket():
            return
        parts = arg.split(maxsplit=1)
        if not parts:
            print(_c(_RED, "  Usage : meow <nom> [texte]"))
            return
        idx = self._resolve_cat(parts[0])
        if idx is None:
            return
        text = parts[1] if len(parts) > 1 else ""
        cmd_str = f"meow {idx}" + (f" {text}" if text else "")
        resp = self._send(cmd_str)
        print(f"  {_c(_GRN, resp)}")

    def do_say(self, arg):
        """say <nom> <texte> — Envoie un message chat comme si l'utilisateur l'avait tape."""
        if not self._require_socket():
            return
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            print(_c(_RED, "  Usage : say <nom> <texte>"))
            return
        idx = self._resolve_cat(parts[0])
        if idx is None:
            return
        resp = self._send(f"say {idx} {parts[1]}")
        print(f"  {_c(_GRN, resp)}")

    def do_egg(self, arg):
        """egg <cle> — Declenche un easter egg."""
        if not self._require_socket():
            return
        if not arg.strip():
            print(_c(_RED, "  Usage : egg <cle>"))
            return
        resp = self._send(f"egg {arg.strip()}")
        print(f"  {_c(_GRN, resp)}")

    def do_eggs(self, arg):
        """Liste les easter eggs disponibles."""
        if not self._require_socket():
            return
        self._egg_cache = None
        eggs = self._fetch_eggs()
        if eggs:
            print(f"\n  {_c(_BOLD, 'Easter eggs :')} {', '.join(_c(_MAG, e) for e in eggs)}\n")
        else:
            print(_c(_YLW, "  Aucun easter egg disponible."))

    def do_sleep(self, arg):
        """sleep <nom> — Met un chat en dodo (SLEEPING_BALL)."""
        if not self._require_socket():
            return
        if not arg.strip():
            print(_c(_RED, "  Usage : sleep <nom>"))
            return
        idx = self._resolve_cat(arg.strip())
        if idx is None:
            return
        resp = self._send(f"force_state {idx} sleeping_ball")
        print(f"  {_c(_GRN, resp)}")

    def do_wake(self, arg):
        """wake <nom> — Reveille un chat (IDLE)."""
        if not self._require_socket():
            return
        if not arg.strip():
            print(_c(_RED, "  Usage : wake <nom>"))
            return
        idx = self._resolve_cat(arg.strip())
        if idx is None:
            return
        resp = self._send(f"force_state {idx} idle")
        print(f"  {_c(_GRN, resp)}")

    def do_dance(self, arg):
        """dance <nom> — Fait danser un chat (LOVE)."""
        if not self._require_socket():
            return
        if not arg.strip():
            print(_c(_RED, "  Usage : dance <nom>"))
            return
        idx = self._resolve_cat(arg.strip())
        if idx is None:
            return
        resp = self._send(f"force_state {idx} love")
        print(f"  {_c(_GRN, resp)}")

    def do_come(self, arg):
        """come <nom> — Fait venir un chat au centre de l'ecran."""
        if not self._require_socket():
            return
        if not arg.strip():
            print(_c(_RED, "  Usage : come <nom>"))
            return
        idx = self._resolve_cat(arg.strip())
        if idx is None:
            return
        # Use move command — center of screen is approximated; the app
        # will figure out actual screen dimensions via the socket.
        resp = self._send(f"move {idx} 960 540")
        print(f"  {_c(_GRN, resp)}")

    def do_mood(self, arg):
        """mood <nom> — Affiche l'humeur d'un chat."""
        if not self._require_socket():
            return
        if not arg.strip():
            print(_c(_RED, "  Usage : mood <nom>"))
            return
        idx = self._resolve_cat(arg.strip())
        if idx is None:
            return
        resp = self._send(f"mood {idx}")
        print(f"  {_c(_GRN, resp)}")

    def do_season(self, arg):
        """season [nom] — Change ou affiche l'overlay saisonnier."""
        if not self._require_socket():
            return
        cmd_str = "season" + (f" {arg.strip()}" if arg.strip() else "")
        resp = self._send(cmd_str)
        print(f"  {_c(_GRN, resp)}")

    def do_notify(self, arg):
        """notify [app] [resume] — Declenche une reaction de notification."""
        if not self._require_socket():
            return
        cmd_str = "notify" + (f" {arg}" if arg.strip() else "")
        resp = self._send(cmd_str)
        print(f"  {_c(_GRN, resp)}")

    def do_ai(self, arg):
        """ai <texte> — L'IA interprete votre demande en commandes socket."""
        if not self._require_socket():
            return
        if not arg.strip():
            print(_c(_RED, "  Usage : ai <votre demande en langage naturel>"))
            return

        # Fetch current cats for context
        cats = self._fetch_cats()
        cat_list_str = "\n".join(
            f"  index={c.get('index', '?')} name={c.get('name', '?')} state={c.get('state', '?')}"
            for c in cats
        ) or "  (aucun chat)"

        system_prompt = textwrap.dedent(f"""\
            You are a CATAI command interpreter. Given a natural language request,
            output ONLY the raw socket commands to execute, one per line.
            Do not add explanations or markdown.

            Available commands:
            - meow <cat_index> <text>
            - egg <key>
            - notify [app] [summary]
            - force_state <cat_index> <state>
            - season <name>
            - say <cat_index> <text>
            - move <cat_index> <x> <y>

            Available states: idle, sleeping_ball, walking, love, rolling, grooming,
            flat, surprised, jumping, dashing, dying

            Current cats:
            {cat_list_str}

            Examples:
            User: "mets tous les chats en dodo"
            force_state 0 sleeping_ball
            force_state 1 sleeping_ball

            User: "fait danser Mandarine"
            force_state 0 love
        """)

        print(_c(_DIM, "  Interrogation de l'IA..."))

        try:
            from catai_linux.chat_backend import create_chat
            backend = create_chat("claude-haiku-4-5")
            # Set system prompt
            backend.messages = [{"role": "system", "content": system_prompt}]

            # Use _stream_chunks directly (no GTK main loop needed)
            backend.messages.append({"role": "user", "content": arg.strip()})
            full = ""
            for chunk in backend._stream_chunks():
                full += chunk

            if not full.strip():
                print(_c(_YLW, "  L'IA n'a retourne aucune commande."))
                return

            commands = [line.strip() for line in full.strip().splitlines() if line.strip()]
            print()
            for c in commands:
                print(f"  {_c(_MAG, '[AI]')} {c}")
            print()

            # Execute
            for c in commands:
                try:
                    resp = self._send(c)
                    print(f"  {_c(_GRN, '  OK')} {resp}")
                except Exception as e:
                    print(f"  {_c(_RED, ' ERR')} {e}")
            print()

        except Exception as e:
            print(_c(_RED, f"  Erreur IA : {e}"))

    def do_help(self, arg):
        """Affiche l'aide."""
        if arg:
            super().do_help(arg)
            return
        print()
        print(_c(_BOLD, "  Commandes CATAI Shell :"))
        print()
        cmds = [
            ("status", "Statut de l'application"),
            ("cats / list / ls", "Liste des chats"),
            ("meow <nom> [texte]", "Bulle de meow"),
            ("say <nom> <texte>", "Envoyer un message chat"),
            ("egg <cle>", "Declencher un easter egg"),
            ("eggs", "Lister les easter eggs"),
            ("sleep <nom>", "Mettre un chat en dodo"),
            ("wake <nom>", "Reveiller un chat"),
            ("dance <nom>", "Faire danser un chat"),
            ("come <nom>", "Faire venir un chat au centre"),
            ("mood <nom>", "Voir l'humeur d'un chat"),
            ("season [nom]", "Overlay saisonnier"),
            ("notify [app] [resume]", "Reaction de notification"),
            ("ai <texte>", "IA interprete en commandes"),
            ("help", "Cette aide"),
            ("quit / exit / q", "Quitter"),
        ]
        for name, desc in cmds:
            print(f"  {_c(_CYN, name):<35s}  {desc}")
        print()

    def do_quit(self, arg):
        """Quitter le shell."""
        print(_c(_DIM, "\n  A bientot ! =^._.^=\n"))
        return True

    do_EOF = do_quit  # Ctrl-D


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Shell interactif CATAI",
        prog="catai-shell",
    )
    parser.add_argument(
        "--socket", "-s",
        help="Chemin vers le socket Unix CATAI",
        default=None,
    )
    args = parser.parse_args()

    shell = CatAIShell(sock_path=args.socket)
    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        print(_c(_DIM, "\n\n  A bientot ! =^._.^=\n"))
        sys.exit(0)


if __name__ == "__main__":
    main()
