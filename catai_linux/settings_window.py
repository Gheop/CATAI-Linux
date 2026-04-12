"""Settings window for CATAI-Linux.

Extracted from ``catai_linux.app`` — standalone class that takes an ``app``
parameter (CatAIApp instance).
"""
from __future__ import annotations

import logging
import threading

from gi.repository import Gdk, GLib, Gtk

from catai_linux.constants import (
    CATSET_CHARS, CATSET_PERSONALITIES, DEFAULT_SCALE, MIN_SCALE, MAX_SCALE,
)
from catai_linux.l10n import L10n
from catai_linux.voice import (
    VOICE_AVAILABLE, VoiceRecorder,
    is_model_cached as _whisper_model_cached,
)
from catai_linux.x11_helpers import (
    move_window, set_always_on_top, set_notification_type,
)
from catai_linux.chat_backend import (
    CLAUDE_MODEL, claude_available, fetch_ollama_models, _ollama_available,
)
from catai_linux import updater as _updater
from catai_linux import metrics as _metrics

log = logging.getLogger("catai")

# Lazy-resolved reference to pil_to_texture (defined in app.py).
_pil_to_texture = None


def _ensure_app_imports() -> None:
    """Resolve names from catai_linux.app that can't be imported at module
    load time (circular import).
    """
    global _pil_to_texture
    if _pil_to_texture is not None:
        return
    from catai_linux.app import pil_to_texture
    _pil_to_texture = pil_to_texture


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.window = None
        self.selected_char_id = None
        self.current_scale = DEFAULT_SCALE
        self.current_model = ""
        self._scale_timer = None

        self.on_add_catset = None
        self.on_remove_catset = None
        self.on_rename_catset = None
        self.on_scale_changed = None
        self.on_model_changed = None
        self.on_lang_changed = None
        self.on_encounters_changed = None
        self.get_configs = None
        self.get_catset_preview = None
        self._anim_pictures = []
        self._anim_timer = None

    def setup(self, scale, model):
        self.current_scale = scale
        self.current_model = model
        if not self.window:
            self.window = Gtk.Window()
            self.window.set_hide_on_close(True)
            self.window.set_decorated(False)
            set_notification_type(self.window)
            set_always_on_top(self.window)
            # Clamp height to fit small screens (keep 80 px margin for top bar etc.)
            screen_h = getattr(self.app, "screen_h", 0) or 900
            win_h = min(900, max(480, screen_h - 80))
            self.window.set_default_size(340, win_h)
            self.window.set_resizable(False)
            self.window.add_css_class("settings-window")
            self.window.connect("close-request", self._on_close)
        cfgs = self.get_configs() if self.get_configs else []
        if not self.selected_char_id:
            first_catset = next((c for c in cfgs if c.get("char_id")), None)
            if first_catset:
                self.selected_char_id = first_catset["char_id"]
        self._build()

    def _on_close(self, *args):
        self._stop_timers()
        self.window.set_visible(False)
        return True

    def _stop_timers(self):
        for attr in ('_anim_timer', '_scale_timer'):
            tid = getattr(self, attr, None)
            if tid:
                GLib.source_remove(tid)
                setattr(self, attr, None)
        self._anim_pictures = []

    def refresh(self):
        self._build()

    def _build(self):
        _ensure_app_imports()
        self._stop_timers()
        configs = self.get_configs() if self.get_configs else []
        active_char_ids = {c["char_id"] for c in configs if c.get("char_id")}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Close button (top-right)
        close_btn = Gtk.Button(label="\u00d7")
        close_css = Gtk.CssProvider()
        close_css.load_from_data(b"button { background: transparent; color: #4d3319; font-size: 18px; font-weight: bold; min-width: 24px; min-height: 24px; padding: 0; border: none; }")
        close_btn.get_style_context().add_provider(close_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda b: self._on_close())
        box.append(close_btn)

        # Title
        title = Gtk.Label(label=L10n.s("title"))
        title.add_css_class("pixel-title")
        box.append(title)

        # Language
        lang_label = Gtk.Label(label=L10n.s("lang_label"))
        lang_label.add_css_class("pixel-label-small")
        lang_label.set_margin_top(8)
        box.append(lang_label)

        flags_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        flags_box.set_halign(Gtk.Align.CENTER)
        for lang_code, flag in [("fr", "\U0001f1eb\U0001f1f7"), ("en", "\U0001f1ec\U0001f1e7"), ("es", "\U0001f1ea\U0001f1f8")]:
            btn = Gtk.Button(label=flag)
            btn.set_size_request(50, 36)
            if lang_code == L10n.lang:
                btn.add_css_class("suggested-action")
            btn.connect("clicked", self._on_lang_click, lang_code)
            flags_box.append(btn)
        box.append(flags_box)

        # MY CATS
        cats_label = Gtk.Label(label=L10n.s("cats"))
        cats_label.add_css_class("pixel-label")
        cats_label.set_margin_top(12)
        box.append(cats_label)

        self._anim_pictures = []

        # ── Catset character row ──────────────────────────────────────────────
        catset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        catset_box.set_halign(Gtk.Align.CENTER)
        catset_box.set_margin_top(4)
        total_cats = len(active_char_ids)
        for char_id, emoji in CATSET_CHARS:
            is_active = char_id in active_char_ids
            is_selected = char_id == self.selected_char_id
            cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            btn = Gtk.Button()
            sprite_size = 40
            pic = Gtk.Picture()
            pic.set_size_request(sprite_size, sprite_size)
            pic.set_can_shrink(True)
            if self.get_catset_preview:
                pil_img = self.get_catset_preview(char_id)
                if pil_img:
                    pic.set_paintable(_pil_to_texture(pil_img, sprite_size, sprite_size))
            btn.set_child(pic)
            btn_css = Gtk.CssProvider()
            if is_selected:
                border_color = '#ffaa22'
            elif is_active:
                border_color = '#4d3319'
            else:
                border_color = 'transparent'
            btn_css.load_from_data(f"""
                button {{ background: transparent; padding: 2px;
                         border: 2px solid {border_color};
                         border-radius: 6px; opacity: {1.0 if is_active else 0.4}; }}
                button:hover {{ opacity: 1.0; }}
            """.encode())
            btn.get_style_context().add_provider(btn_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            if is_active:
                btn.connect("clicked", self._on_catset_select, char_id)
            else:
                btn.connect("clicked", self._on_catset_add, char_id)
            cbox.append(btn)
            if is_active and total_cats > 1:
                rm_btn = Gtk.Button(label="\u00d7")
                rm_css = Gtk.CssProvider()
                rm_css.load_from_data(b"button { background: #cc3333; color: white; border-radius: 50%; min-width: 16px; min-height: 16px; font-size: 10px; padding: 0; }")
                rm_btn.get_style_context().add_provider(rm_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                rm_btn.set_halign(Gtk.Align.CENTER)
                rm_btn.connect("clicked", self._on_catset_remove, char_id)
                cbox.append(rm_btn)
            catset_box.append(cbox)
        box.append(catset_box)

        # ── Detail panel for selected catset char ─────────────────────────────
        if self.selected_char_id and self.selected_char_id in active_char_ids:
            char_id = self.selected_char_id
            p = CATSET_PERSONALITIES.get(char_id, CATSET_PERSONALITIES["cat01"])
            cfg = next((c for c in configs if c.get("char_id") == char_id), None)

            name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name_box.set_margin_top(8)
            nl = Gtk.Label(label=L10n.s("name"))
            nl.add_css_class("pixel-label-small")
            name_box.append(nl)
            ne = Gtk.Entry()
            ne.set_text(cfg["name"] if cfg else p["name"].get(L10n.lang, p["name"]["fr"]))
            ne.set_max_length(30)
            ne.add_css_class("pixel-entry")
            ne.set_hexpand(True)
            ne.connect("changed", self._on_catset_name_changed, char_id)
            name_box.append(ne)
            box.append(name_box)

            trait_lbl = Gtk.Label(label=f"\u2726 {p['traits'].get(L10n.lang, p['traits']['fr'])}")
            trait_lbl.add_css_class("pixel-trait")
            trait_lbl.set_xalign(0)
            trait_lbl.set_margin_start(4)
            box.append(trait_lbl)

            skill_lbl = Gtk.Label(label=p["skills"].get(L10n.lang, p["skills"]["fr"]))
            skill_lbl.add_css_class("pixel-trait")
            skill_lbl.set_xalign(0)
            skill_lbl.set_wrap(True)
            skill_lbl.set_margin_start(4)
            box.append(skill_lbl)

        if getattr(self, '_anim_timer', None):
            GLib.source_remove(self._anim_timer)
            self._anim_timer = None
        if self._anim_pictures:
            self._anim_timer = GLib.timeout_add(150, self._animate_previews)

        # SIZE
        size_label = Gtk.Label(label=L10n.s("size"))
        size_label.add_css_class("pixel-label")
        size_label.set_margin_top(16)
        box.append(size_label)

        size_value = Gtk.Label(label=f"x{self.current_scale:.1f}")
        size_value.add_css_class("pixel-label-small")
        box.append(size_value)

        scale_widget = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, MIN_SCALE, MAX_SCALE, 0.1)
        scale_widget.set_value(self.current_scale)
        scale_widget.set_draw_value(False)
        def on_scale(s):
            v = s.get_value()
            size_value.set_label(f"x{v:.1f}")
            self.current_scale = v
            if self._scale_timer:
                try:
                    GLib.source_remove(self._scale_timer)
                except Exception:
                    pass
            self._scale_timer = GLib.timeout_add(800, self._do_scale_change, v)
        scale_widget.connect("value-changed", on_scale)
        box.append(scale_widget)

        # MODEL
        model_label = Gtk.Label(label=L10n.s("model"))
        model_label.add_css_class("pixel-label")
        model_label.set_margin_top(12)
        box.append(model_label)

        model_combo = Gtk.DropDown()
        model_combo.set_margin_top(4)
        model_strings = Gtk.StringList.new([L10n.s("loading")])
        model_combo.set_model(model_strings)

        self._model_loading = True

        def _load_models():
            all_models = []
            if claude_available():
                all_models.append(f"{CLAUDE_MODEL} (Claude)")
            if _ollama_available():
                all_models.extend(fetch_ollama_models())
            def _update():
                self._model_loading = True
                model_strings.splice(0, model_strings.get_n_items(),
                                     all_models if all_models else [L10n.s("no_ollama")])
                if all_models:
                    current = self.current_model
                    for i, m in enumerate(all_models):
                        if m.startswith(current):
                            model_combo.set_selected(i)
                            break
                self._model_loading = False
                return False
            GLib.idle_add(_update)
        threading.Thread(target=_load_models, daemon=True).start()

        model_combo.connect("notify::selected", self._on_model_select, model_strings)
        box.append(model_combo)

        # AUTOSTART
        from catai_linux.app import is_autostart, set_autostart
        autostart_check = Gtk.CheckButton(label=L10n.s("autostart"))
        autostart_check.set_active(is_autostart())
        autostart_check.add_css_class("pixel-label-small")
        autostart_check.set_margin_top(16)
        autostart_check.connect("toggled", lambda btn: set_autostart(btn.get_active()))
        box.append(autostart_check)

        # ENCOUNTERS
        enc_enabled = True
        if self.on_encounters_changed and hasattr(self.app, 'encounters_enabled'):
            enc_enabled = self.app.encounters_enabled
        enc_check = Gtk.CheckButton(label=L10n.s("encounters"))
        enc_check.set_active(enc_enabled)
        enc_check.add_css_class("pixel-label-small")
        enc_check.set_margin_top(4)
        enc_check.connect("toggled", lambda btn: self.on_encounters_changed(btn.get_active()) if self.on_encounters_changed else None)
        box.append(enc_check)

        # Voice chat (push-to-talk) — optional feature
        voice_check = Gtk.CheckButton(label="Voice chat (hold mic button)")
        voice_check.set_active(getattr(self.app, "_voice_enabled", False))
        voice_check.add_css_class("pixel-label-small")
        voice_check.set_margin_top(4)
        if not VOICE_AVAILABLE:
            voice_check.set_sensitive(False)
        def _on_voice_toggled(btn):
            enabled = btn.get_active() and VOICE_AVAILABLE
            self.app._voice_enabled = enabled
            if enabled and self.app._voice_recorder is None:
                self.app._voice_recorder = VoiceRecorder()
            self.app._save_all()
        voice_check.connect("toggled", _on_voice_toggled)
        box.append(voice_check)

        if not VOICE_AVAILABLE:
            hint = Gtk.Label()
            hint.set_markup(
                '<span foreground="#cc2222" size="x-small">'
                'Not installed — run: <tt>pip install catai-linux[voice]</tt>'
                '</span>'
            )
            hint.set_wrap(True)
            hint.set_xalign(0)
            hint.set_margin_start(24)
            box.append(hint)
        else:
            restart_hint = Gtk.Label()
            restart_hint.set_markup(
                '<span foreground="#888888" size="x-small">'
                'Restart CATAI to apply enable/disable'
                '</span>'
            )
            restart_hint.set_xalign(0)
            restart_hint.set_margin_start(24)
            box.append(restart_hint)

            # Whisper model dropdown (size + recommended device hint)
            # Entry format: "name — <size> MB (<device>)"
            voice_models = [
                ("tiny",             39, "CPU"),
                ("base",             74, "CPU"),
                ("small",           244, "CPU/GPU"),
                ("medium",          769, "GPU"),
                ("distil-large-v3", 756, "GPU"),
                ("large-v3-turbo",  809, "GPU"),
                ("large-v3",       1550, "GPU"),
            ]
            model_label = Gtk.Label(label="Voice model")
            model_label.add_css_class("pixel-label-small")
            model_label.set_margin_top(8)
            model_label.set_margin_start(24)
            model_label.set_xalign(0)
            box.append(model_label)

            voice_drop = Gtk.DropDown()
            voice_drop.set_margin_top(2)
            voice_drop.set_margin_start(24)
            voice_labels = [f"{name} — {size} MB ({dev})" for name, size, dev in voice_models]
            voice_drop.set_model(Gtk.StringList.new(voice_labels))
            current_model = getattr(self.app, "_voice_model", "base")
            for i, (name, _sz, _d) in enumerate(voice_models):
                if name == current_model:
                    voice_drop.set_selected(i)
                    break
            voice_drop.set_sensitive(VOICE_AVAILABLE)

            def _on_voice_model_changed(drop, _param):
                idx = drop.get_selected()
                if 0 <= idx < len(voice_models):
                    name = voice_models[idx][0]
                    self.app._voice_model = name
                    if self.app._voice_recorder:
                        self.app._voice_recorder.set_model(name)
                        # Preload the new model in background if already cached
                        if _whisper_model_cached(name):
                            rec = self.app._voice_recorder
                            def _preload():
                                try:
                                    rec._ensure_model()
                                except Exception:
                                    log.exception("Whisper preload failed")
                            threading.Thread(target=_preload, daemon=True).start()
                    self.app._save_all()
                    log.info("Voice model set to %r", name)
            voice_drop.connect("notify::selected", _on_voice_model_changed)
            box.append(voice_drop)

            voice_model_hint = Gtk.Label()
            voice_model_hint.set_markup(
                '<span foreground="#888888" size="x-small">'
                'Larger = more accurate but slower. GPU needs CUDA.'
                '</span>'
            )
            voice_model_hint.set_xalign(0)
            voice_model_hint.set_margin_start(24)
            box.append(voice_model_hint)

            # Wake word — each cat answers to its own first name
            from catai_linux.wake_word import WAKE_AVAILABLE as _WAKE_OK
            wake_check = Gtk.CheckButton(label="Wake word (call cats by name)")
            wake_check.set_active(getattr(self.app, "_wake_word_enabled", False))
            wake_check.add_css_class("pixel-label-small")
            wake_check.set_margin_top(8)
            wake_check.set_margin_start(24)
            wake_check.set_sensitive(_WAKE_OK)

            def _on_wake_toggled(btn):
                enabled = btn.get_active() and _WAKE_OK
                self.app._wake_word_enabled = enabled
                self.app._save_all()
                # Live start/stop. The first start kicks off the model
                # download in a background thread (~41 MB) so the UI
                # never blocks; subsequent toggles are instant.
                if enabled:
                    if self.app._wake is None:
                        from catai_linux.wake_word import WakeWordListener as _WW
                        self.app._wake = _WW(on_wake=self.app._on_wake_word_heard)
                    self.app._wake.set_names({
                        c.get("char_id"): c.get("name", "")
                        for c in self.app.cat_configs if c.get("char_id")
                    })
                    self.app._wake.start()
                else:
                    if self.app._wake is not None:
                        self.app._wake.stop()
            wake_check.connect("toggled", _on_wake_toggled)
            box.append(wake_check)

            wake_ack_check = Gtk.CheckButton(label="Acknowledge meow on wake")
            wake_ack_check.set_active(getattr(self.app, "_wake_ack_sound", True))
            wake_ack_check.add_css_class("pixel-label-small")
            wake_ack_check.set_margin_start(48)
            wake_ack_check.set_sensitive(_WAKE_OK)

            def _on_wake_ack_toggled(btn):
                self.app._wake_ack_sound = btn.get_active()
                self.app._save_all()
            wake_ack_check.connect("toggled", _on_wake_ack_toggled)
            box.append(wake_ack_check)

            wake_hint = Gtk.Label()
            wake_hint.set_markup(
                '<span foreground="#888888" size="x-small">'
                'First launch downloads ~41 MB Vosk model. '
                'Cats respond to their renameable first name.'
                '</span>'
            )
            wake_hint.set_wrap(True)
            wake_hint.set_xalign(0)
            wake_hint.set_margin_start(24)
            box.append(wake_hint)

        # Voice output (TTS) — global kill switch. Per-cat toggles live
        # on the speaker icon in each chat bubble.
        tts_check = Gtk.CheckButton(label="Voice output (cat sound effects)")
        tts_check.set_active(getattr(self.app, "_tts_enabled", False))
        tts_check.add_css_class("pixel-label-small")
        tts_check.set_margin_top(8)

        def _on_tts_toggled(btn):
            self.app._tts_enabled = btn.get_active()
            self.app._save_all()
        tts_check.connect("toggled", _on_tts_toggled)
        box.append(tts_check)

        tts_hint = Gtk.Label()
        tts_hint.set_markup(
            '<span foreground="#888888" size="x-small">'
            'Plays CC0 cat samples (~165 KB) on each chat response. '
            'Click the 🔊 icon in a chat bubble to mute an individual cat.'
            '</span>'
        )
        tts_hint.set_wrap(True)
        tts_hint.set_xalign(0)
        tts_hint.set_margin_start(24)
        box.append(tts_hint)

        # Sub-toggle: cat sound effects (on top of the Piper text voice)
        cat_sounds_check = Gtk.CheckButton(
            label="  ↳ Play cat sound effects (meow/purr/hiss)")
        cat_sounds_check.set_active(
            getattr(self.app, "_tts_cat_sounds_enabled", True))
        cat_sounds_check.add_css_class("pixel-label-small")
        cat_sounds_check.set_margin_start(16)

        def _on_cat_sounds_toggled(btn):
            self.app._tts_cat_sounds_enabled = btn.get_active()
            self.app._save_all()
        cat_sounds_check.connect("toggled", _on_cat_sounds_toggled)
        box.append(cat_sounds_check)

        # ── Auto-update section (#24) ─────────────────────────────────────
        update_label = Gtk.Label(label="UPDATES")
        update_label.add_css_class("pixel-label")
        update_label.set_margin_top(12)
        update_label.set_xalign(0)
        box.append(update_label)

        installed_ver = _updater.get_installed_version() or "dev"
        ver_label = Gtk.Label()
        ver_label.set_markup(
            f'<span foreground="#888888" size="x-small">'
            f'Installed: v{installed_ver}</span>'
        )
        ver_label.set_xalign(0)
        ver_label.set_margin_start(8)
        box.append(ver_label)

        update_drop = Gtk.DropDown()
        update_drop.set_model(Gtk.StringList.new([
            "Auto-install on launch (recommended)",
            "Notify only",
            "Off",
        ]))
        update_drop.set_margin_top(4)
        mode_to_idx = {
            _updater.MODE_AUTO: 0,
            _updater.MODE_NOTIFY: 1,
            _updater.MODE_OFF: 2,
        }
        idx_to_mode = [_updater.MODE_AUTO, _updater.MODE_NOTIFY, _updater.MODE_OFF]
        cur_mode = getattr(self.app, "_auto_update_mode", _updater.MODE_AUTO)
        update_drop.set_selected(mode_to_idx.get(cur_mode, 0))

        def _on_update_mode_changed(drop, _param):
            idx = drop.get_selected()
            if 0 <= idx < len(idx_to_mode):
                self.app._auto_update_mode = idx_to_mode[idx]
                self.app._save_all()
        update_drop.connect("notify::selected", _on_update_mode_changed)
        box.append(update_drop)

        check_btn = Gtk.Button(label="Check now")
        check_btn.set_margin_top(4)
        check_btn.add_css_class("pixel-mic-btn")

        def _on_check_now(btn):
            # Force a fresh GitHub fetch (bypass the 1 h cache) and run
            # the same worker logic in a daemon thread so the UI stays
            # responsive while pip downloads run.
            def _runner():
                result = _updater.check_for_update(force=True)
                if result is None:
                    GLib.idle_add(
                        self.app._meow_first_cat,
                        f"À jour! v{installed_ver}")
                    return
                old, new = result
                if self.app._auto_update_mode == _updater.MODE_AUTO:
                    ok = _updater.install_update_blocking()
                    if ok:
                        GLib.idle_add(self.app._on_update_installed, old, new)
                    else:
                        GLib.idle_add(self.app._on_update_failed, new)
                else:
                    GLib.idle_add(self.app._on_update_available, old, new)
            threading.Thread(target=_runner, daemon=True).start()

        check_btn.connect("clicked", _on_check_now)
        box.append(check_btn)

        # ── Local metrics section (#9) ────────────────────────────────────
        stats_label = Gtk.Label(label="YOUR STATS")
        stats_label.add_css_class("pixel-label")
        stats_label.set_margin_top(12)
        stats_label.set_xalign(0)
        box.append(stats_label)

        stats_check = Gtk.CheckButton(
            label="Track local stats (chats, eggs, pets, kittens)")
        stats_check.set_active(getattr(self.app, "_metrics_enabled", False))
        stats_check.add_css_class("pixel-label-small")

        # Live summary label updated whenever the panel rebuilds
        stats_summary = Gtk.Label()
        stats_summary.set_xalign(0)
        stats_summary.set_margin_start(8)
        stats_summary.set_margin_top(2)
        stats_summary.set_wrap(True)

        def _refresh_summary():
            if not getattr(self.app, "_metrics_enabled", False):
                stats_summary.set_markup(
                    '<span foreground="#888888" size="x-small">'
                    '(enable above to start tracking)</span>'
                )
                return
            data = _metrics.load()
            top_pet = _metrics.top_cats(data, "petted", 3)
            top_eggs = _metrics.top_eggs(data, 3)
            lines = [
                f"<b>Sessions:</b> {data['total_sessions']}  "
                f"<b>Chats:</b> {data['chats_sent']}  "
                f"<b>Voice:</b> {data['voice_recordings']}",
                f"<b>Pets:</b> {data['pet_sessions']}  "
                f"<b>Kittens born:</b> {data['kittens_born']}",
            ]
            le = data["love_encounters"]
            lines.append(
                f"<b>Loves:</b> 💕 {le['love']}  "
                f"😲 {le['surprised']}  😾 {le['angry']}"
            )
            if top_pet:
                lines.append("<b>Most petted:</b> " + ", ".join(
                    f"{c} ({n})" for c, n in top_pet))
            if top_eggs:
                lines.append("<b>Top eggs:</b> " + ", ".join(
                    f"{k} ({n})" for k, n in top_eggs))
            stats_summary.set_markup(
                '<span size="x-small">' + "\n".join(lines) + '</span>'
            )

        def _on_stats_toggled(btn):
            self.app._metrics_enabled = btn.get_active()
            _metrics.set_enabled(btn.get_active())
            self.app._save_all()
            _refresh_summary()

        stats_check.connect("toggled", _on_stats_toggled)
        box.append(stats_check)
        box.append(stats_summary)
        _refresh_summary()

        reset_btn = Gtk.Button(label="Reset stats")
        reset_btn.set_margin_top(2)
        reset_btn.add_css_class("pixel-mic-btn")

        def _on_reset_stats(btn):
            _metrics.reset()
            _refresh_summary()
        reset_btn.connect("clicked", _on_reset_stats)
        box.append(reset_btn)

        # ── Public API socket section (#9) ────────────────────────────────
        api_label = Gtk.Label(label="SCRIPTABLE API")
        api_label.add_css_class("pixel-label")
        api_label.set_margin_top(12)
        api_label.set_xalign(0)
        box.append(api_label)

        api_check = Gtk.CheckButton(
            label="Enable Unix socket for shell scripts")
        api_check.set_active(getattr(self.app, "_api_enabled", False))
        api_check.add_css_class("pixel-label-small")

        def _on_api_toggled(btn):
            self.app._api_enabled = btn.get_active()
            self.app._save_all()
        api_check.connect("toggled", _on_api_toggled)
        box.append(api_check)

        api_hint = Gtk.Label()
        api_hint.set_markup(
            '<span foreground="#888888" size="x-small">'
            'Requires restart. Socket at $XDG_RUNTIME_DIR/catai.sock '
            '(mode 0600). Commands: status, list_cats, list_eggs, '
            'meow &lt;idx&gt; [text], egg &lt;key&gt;, notify, help.'
            '</span>'
        )
        api_hint.set_wrap(True)
        api_hint.set_xalign(0)
        api_hint.set_margin_start(8)
        box.append(api_hint)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(box)
        self.window.set_child(scroll)

    def _animate_previews(self):
        for pic, frames, idx in self._anim_pictures:
            idx[0] = (idx[0] + 1) % len(frames)
            pic.set_paintable(frames[idx[0]])
        return True

    def _on_catset_select(self, btn, char_id):
        self.selected_char_id = char_id
        self._build()

    def _on_catset_add(self, btn, char_id):
        if self.on_add_catset:
            self.on_add_catset(char_id)

    def _on_catset_remove(self, btn, char_id):
        if self.on_remove_catset:
            self.on_remove_catset(char_id)

    def _on_catset_name_changed(self, entry, char_id):
        if self.on_rename_catset:
            self.on_rename_catset(char_id, entry.get_text())

    def _on_lang_click(self, btn, lang_code):
        if self.on_lang_changed:
            self.on_lang_changed(lang_code)

    def _on_model_select(self, dropdown, pspec, string_list):
        if getattr(self, '_model_loading', False):
            return
        idx = dropdown.get_selected()
        if idx < string_list.get_n_items():
            name = string_list.get_string(idx)
            if name and not name.startswith("(") and name != L10n.s("loading"):
                model_id = name.split(" (")[0] if " (" in name else name
                self.current_model = model_id
                if self.on_model_changed:
                    self.on_model_changed(model_id)

    def _do_scale_change(self, v):
        self._scale_timer = None
        if self.on_scale_changed:
            self.on_scale_changed(v)
        return False

    def show(self):
        self.window.set_visible(True)
        self.window.present()
        # Center on screen
        display = Gdk.Display.get_default()
        if display:
            monitors = display.get_monitors()
            if monitors.get_n_items() > 0:
                geo = monitors.get_item(0).get_geometry()
                cx = (geo.width - 340) // 2
                cy = (geo.height - 680) // 2
                move_window(self.window, cx, cy)
