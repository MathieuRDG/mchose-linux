#!/usr/bin/env python3
"""Settings window for the MCHOSE mouse (GTK 4 + libadwaita).

Every change is sent as soon as it is made (DPI waits for its Apply button),
then the mouse is read back and the window redrawn from what it actually holds.
Device work runs on one worker thread, through mchose.py.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import mchose  # noqa: E402

MODE_HINTS = {
    1: "Bureau et jeu léger, meilleure autonomie",
    2: "Jeu rapide, consommation un peu plus élevée",
    3: "Moteur ≥ 20K, autonomie nettement réduite",
}


class Worker:
    """One thread owns the mouse; jobs queue up and report back on the main loop."""

    def __init__(self):
        self.mouse: mchose.Mouse | None = None
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, op, done):
        self.jobs.put((op, done))

    def _run(self):
        while True:
            op, done = self.jobs.get()
            try:
                if self.mouse is None:
                    self.mouse = mchose.open_mouse()
                with self.mouse:
                    self.mouse.read_status()
                    cfg = op(self.mouse) if op else None
                    if cfg is None:
                        cfg = self.mouse.read_config()
                    result, error = mchose.describe(self.mouse, cfg), None
            except (mchose.MchoseError, OSError) as e:
                result, error = None, e
                if isinstance(e, OSError):
                    self.mouse = None   # unplugged: look for it again next time
            GLib.idle_add(done, result, error)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Souris MCHOSE", default_width=460, default_height=760)
        self.worker = Worker()
        self.state: dict | None = None
        self._loading = False
        self._busy = 0
        self._pending: dict[str, int] = {}

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Souris MCHOSE", subtitle="lecture…")
        header.set_title_widget(self.title)
        self.spinner = Adw.Spinner(visible=False)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Relire la souris")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_start(refresh)
        header.pack_end(self.spinner)
        view.add_top_bar(header)

        self.stack = Gtk.Stack()
        self.asleep = Adw.StatusPage(icon_name="input-mouse-symbolic", title="La souris ne répond pas",
                                     description="Elle dort sans doute : bouge-la, puis réessaie.")
        retry = Gtk.Button(label="Réessayer", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        retry.connect("clicked", lambda *_: self.refresh())
        self.asleep.set_child(retry)
        self.stack.add_named(self.asleep, "asleep")
        self.stack.add_named(self._build_page(), "settings")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.refresh()

    # ------------------------------------------------------------ layout

    def _build_page(self):
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Batterie")
        row = Adw.ActionRow(title="Niveau")
        self.level_bar = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER, width_request=140)
        self.level_bar.add_offset_value("low", 20)
        self.level_bar.add_offset_value("high", 50)
        self.level_label = Gtk.Label(width_chars=6, xalign=1)
        row.add_suffix(self.level_bar)
        row.add_suffix(self.level_label)
        g.add(row)
        page.add(g)

        g = Adw.PreferencesGroup(title="DPI", description="Le palier coché est celui utilisé maintenant.")
        self.count_row = Adw.SpinRow.new_with_range(1, 6, 1)
        self.count_row.set_title("Paliers actifs")
        self.count_row.set_subtitle("Ceux que le bouton DPI fait défiler")
        self.count_row.connect("notify::value", self._dpi_edited)
        g.add(self.count_row)
        self.stage_checks, self.dpi_spins, self.stage_rows = [], [], []
        for i in range(6):
            row = Adw.ActionRow(title=f"Palier {i + 1}")
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if self.stage_checks:
                check.set_group(self.stage_checks[0])
            check.connect("toggled", self._dpi_edited)
            spin = Gtk.SpinButton.new_with_range(mchose.DPI_MIN, 42000, 50)
            spin.set_valign(Gtk.Align.CENTER)
            spin.set_width_chars(6)
            spin.connect("value-changed", self._dpi_edited)
            row.add_prefix(check)
            row.add_suffix(spin)
            row.set_activatable_widget(check)
            g.add(row)
            self.stage_checks.append(check)
            self.dpi_spins.append(spin)
            self.stage_rows.append(row)
        self.dpi_apply = Gtk.Button(label="Appliquer les DPI", sensitive=False, margin_top=12,
                                    halign=Gtk.Align.END, css_classes=["suggested-action"])
        self.dpi_apply.connect("clicked", self._apply_dpi)
        g.add(self.dpi_apply)
        page.add(g)

        g = Adw.PreferencesGroup(title="Capteur")
        self.rate_row = Adw.ComboRow(title="Polling rate", subtitle="Pour le lien actuel")
        self.rate_row.connect("notify::selected", self._rate_changed)
        g.add(self.rate_row)
        self.mode_row = Adw.ComboRow(title="Mode", model=Gtk.StringList.new(list(mchose.MODE_LABELS.values())))
        self.mode_row.connect("notify::selected", self._mode_changed)
        g.add(self.mode_row)
        self.lod_row = Adw.ComboRow(title="Lift-off distance")
        self.lod_row.connect("notify::selected", self._lod_changed)
        g.add(self.lod_row)
        self.flag_rows = {}
        for key, title, sub in (("ripple", "Ripple control", "Lissage : à éviter en jeu"),
                                ("angle", "Angle snapping", "Redresse les lignes"),
                                ("motion", "Motion sync", "Synchronise capteur et polling (≈ +1 ms)")):
            row = Adw.SwitchRow(title=title, subtitle=sub)
            row.connect("notify::active", self._flag_changed, key)
            g.add(row)
            self.flag_rows[key] = row
        page.add(g)

        g = Adw.PreferencesGroup(title="Clics et veille")
        self.debounce_row = Adw.SpinRow.new_with_range(0, mchose.DEBOUNCE_MAX, 1)
        self.debounce_row.set_title("Anti-rebond (ms)")
        self.debounce_row.set_subtitle("Plus haut si un clic se double")
        self.debounce_row.connect("notify::value", self._spin_changed, "debounce")
        g.add(self.debounce_row)
        self.sleep_row = Adw.SpinRow.new_with_range(0, mchose.SLEEP_MAX, 1)
        self.sleep_row.set_title("Veille (minutes)")
        self.sleep_row.set_subtitle("0 = jamais (vide la batterie)")
        self.sleep_row.connect("notify::value", self._spin_changed, "sleep")
        g.add(self.sleep_row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Sauvegarde")
        row = Adw.ActionRow(title="Configuration d'origine",
                            subtitle="Copiée automatiquement avant la toute première modification")
        btn = Gtk.Button(label="Restaurer", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._restore_original)
        row.add_suffix(btn)
        g.add(row)
        row = Adw.ActionRow(title="Sauvegarder l'état actuel", subtitle=str(mchose.STATE_DIR))
        btn = Gtk.Button(label="Sauvegarder", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._backup)
        row.add_suffix(btn)
        g.add(row)
        page.add(g)
        return page

    # ------------------------------------------------------------ device I/O

    def run(self, op, ok_message: str | None = None):
        self._busy += 1
        self.spinner.set_visible(True)
        self.stack.set_sensitive(False)

        def done(result, error):
            self._busy -= 1
            if not self._busy:
                self.spinner.set_visible(False)
                self.stack.set_sensitive(True)
            if error:
                if self.state is None:
                    self.stack.set_visible_child_name("asleep")
                else:
                    self.toasts.add_toast(Adw.Toast(title=str(error), timeout=5))
                return False
            self.show(result)
            if ok_message:
                self.toasts.add_toast(Adw.Toast(title=ok_message, timeout=2))
            return False

        self.worker.submit(op, done)

    def refresh(self):
        self.run(None)

    def show(self, d: dict):
        self.state = d
        self._loading = True
        try:
            self.stack.set_visible_child_name("settings")
            charge = " · en charge" if d["charging"] else ""
            self.title.set_title(d["model"])
            self.title.set_subtitle(f"firmware {d['firmware']} · {d['link']}{charge}")
            self.level_bar.set_value(d["battery"] or 0)
            self.level_label.set_label(f"{d['battery']} %")

            self.count_row.get_adjustment().set_upper(6)
            self.count_row.set_value(d["stages"])
            for i, (check, spin) in enumerate(zip(self.stage_checks, self.dpi_spins)):
                spin.get_adjustment().set_upper(d["dpi_max"])
                spin.set_value(d["dpi"][i])
                check.set_active(i == d["active_stage"])
            self._dim_stages()
            self.dpi_apply.set_sensitive(False)

            self.rate_row.set_model(Gtk.StringList.new([f"{r} Hz" for r in d["rates"]]))
            if d["rate"] in d["rates"]:
                self.rate_row.set_selected(d["rates"].index(d["rate"]))
            self.mode_row.set_selected(list(mchose.MODES.values()).index(d["mode"]))
            self.mode_row.set_subtitle(MODE_HINTS[self.mode_row.get_selected() + 1])
            self.lod_row.set_model(Gtk.StringList.new([f"{mm:g} mm" for mm in d["lods_mm"]]))
            if d["lod_mm"] in d["lods_mm"]:
                self.lod_row.set_selected(d["lods_mm"].index(d["lod_mm"]))
            self.flag_rows["ripple"].set_active(d["ripple"])
            self.flag_rows["angle"].set_active(d["angle_snap"])
            self.flag_rows["motion"].set_active(d["motion_sync"])
            self.debounce_row.set_value(d["debounce_ms"])
            self.sleep_row.set_value(d["sleep_min"])
        finally:
            self._loading = False

    # ------------------------------------------------------------ handlers

    def _dim_stages(self):
        n = int(self.count_row.get_value())
        for i, row in enumerate(self.stage_rows):
            row.set_opacity(1.0 if i < n else 0.45)

    def _dpi_edited(self, *_):
        if self._loading:
            return
        self._dim_stages()
        self.dpi_apply.set_sensitive(True)

    def _apply_dpi(self, *_):
        dpis = [int(s.get_value()) for s in self.dpi_spins]
        count = int(self.count_row.get_value())
        stage = next(i for i, c in enumerate(self.stage_checks) if c.get_active())
        if stage >= count:
            self.toasts.add_toast(Adw.Toast(title="Le palier coché doit faire partie des paliers actifs"))
            return
        self.run(lambda m: m.set_dpi(dpis, stage, count), "DPI appliqués")

    def _rate_changed(self, row, _):
        if self._loading or not self.state:
            return
        hz = self.state["rates"][row.get_selected()]
        self.run(lambda m: m.set_rate(hz), f"Polling : {hz} Hz")

    def _mode_changed(self, row, _):
        if self._loading:
            return
        mode = row.get_selected() + 1
        self.run(lambda m: m.set_mode(mode), f"Mode {mchose.MODE_LABELS[mode]}")

    def _lod_changed(self, row, _):
        if self._loading or not self.state:
            return
        mm = self.state["lods_mm"][row.get_selected()]
        code = next(c for c, v in mchose.LOD_MM.items() if v == mm)
        self.run(lambda m: m.set_lod(code), f"LOD : {mm:g} mm")

    def _flag_changed(self, row, _, key):
        if self._loading:
            return
        on = row.get_active()
        self.run(lambda m: m.set_flags(**{key: on}), f"{row.get_title()} : {'activé' if on else 'désactivé'}")

    def _spin_changed(self, row, _, key):
        """Spin rows fire on every step; wait until the value settles."""
        if self._loading:
            return
        if key in self._pending:
            GLib.source_remove(self._pending[key])
        self._pending[key] = GLib.timeout_add(700, self._spin_settled, row, key)

    def _spin_settled(self, row, key):
        self._pending.pop(key, None)
        v = int(row.get_value())
        if key == "debounce":
            self.run(lambda m: m.set_debounce(v), f"Anti-rebond : {v} ms")
        else:
            self.run(lambda m: m.set_sleep(v), "Veille : " + (f"{v} min" if v else "jamais"))
        return False

    def _backup(self, *_):
        path = mchose.STATE_DIR / time.strftime("config-%Y%m%d-%H%M%S.bin")

        def op(m):
            cfg = m.read_config()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(cfg.b))
            return cfg
        self.run(op, f"Sauvegardé : {path.name}")

    def _restore_original(self, *_):
        path = mchose.STATE_DIR / "config-original.bin"
        if not path.exists():
            self.toasts.add_toast(Adw.Toast(title="Aucune modification faite pour l'instant, rien à restaurer"))
            return
        dialog = Adw.AlertDialog(heading="Restaurer la configuration d'origine ?",
                                 body="DPI, polling, capteur, boutons, veille : tout revient à l'état "
                                      "d'avant la première modification.")
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("restore", "Restaurer")
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(d, response):
            if response == "restore":
                body = path.read_bytes()
                self.run(lambda m: m.restore(body), "Configuration d'origine restaurée")
        dialog.connect("response", answered)
        dialog.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.mchose.Settings")

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
