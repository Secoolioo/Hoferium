"""Hoferium - animierte Neon-Oberflaeche (customtkinter + Canvas-Effekte).

Design:
  * ASCII-Logo mit laufender Regenbogenwelle
  * Matrix-Regen, Gradient-Linien, Equalizer, ASCII-Spinner
  * Neonfarben je Seite, pulsierende Karten und Navigation

Performance:
  * EIN zentraler Ticker (Animator) treibt alle Effekte - keine 20 Einzel-Loops
  * Effekte, deren Seite nicht sichtbar ist, werden uebersprungen (Guards)
  * schwere Arbeit laeuft im Hintergrund-Thread, UI nur ueber Queue-Polling
"""
from __future__ import annotations

import math
import os
import queue
import random
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import backup as backup_mod
from . import config, downloader, restore, sysinfo, tweaks, uninstaller, updater, winutils
from . import __version__
from .config import (COLORS, FLAG, FLAG_BRIGHT, LEVEL_COLORS, LEVEL_MARKS,
                     NAV_ICONS, PAGE_COLORS, TAGLINES, backup_root, banner_lines,
                     local_dir, mix, stick_dir)
from .context import Reporter, RunContext
from .registry import list_installed
from .winutils import is_admin

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

UI_FONT = "Segoe UI"
MONO_FONT = "Consolas"
SPINNER = ["|", "/", "-", "\\"]
MATRIX_CHARS = "01<>[]{}/\\|=+*#$%&@ABCDEFHKLMNPRSTVXZ0123456789"
TICK_MS = 66  # ~15 fps: fluessig und guenstig


def _font(size=13, weight="normal"):
    return ctk.CTkFont(family=UI_FONT, size=size, weight=weight)


# ==================================================================
#  Zentraler Animations-Ticker
# ==================================================================
class Animator:
    """Treibt alle Effekte aus einer einzigen after()-Schleife.

    Drosselt sich selbst: Braucht ein Durchlauf auf schwacher Hardware zu
    lange, wird das Intervall gestreckt, damit die Bedienung immer fluessig
    bleibt (Animation wird langsamer statt die UI zaeh).
    """

    MAX_LOAD = 0.25      # Effekte duerfen hoechstens 25 % der Zeit brauchen
    MAX_INTERVAL = 200

    def __init__(self, root, interval=TICK_MS):
        self.root = root
        self.base_interval = interval
        self.interval = interval
        self.frame = 0
        self._subs = []          # (callback, guard)
        self._alive = True
        self._cost = 0.0         # gleitender Mittelwert in ms
        self.root.after(self.interval, self._tick)

    def add(self, callback, guard=None):
        """Registriert einen Effekt. Ohne eigenen Guard wird - sofern der
        Callback an ein Widget gebunden ist - automatisch uebersprungen,
        solange dieses Widget nicht sichtbar ist."""
        if guard is None:
            owner = getattr(callback, "__self__", None)
            if isinstance(owner, tk.Misc):
                guard = lambda w=owner: bool(w.winfo_ismapped())
        self._subs.append((callback, guard))

    def stop(self):
        self._alive = False

    def _tick(self):
        if not self._alive:
            return
        start = time.perf_counter()
        self.frame += 1
        f = self.frame
        for cb, guard in self._subs:
            try:
                if guard is not None and not guard():
                    continue
                cb(f)
            except tk.TclError:
                pass          # Widget wurde zerstoert
            except Exception:
                pass          # ein kaputter Effekt darf nie die App stoppen

        cost_ms = (time.perf_counter() - start) * 1000.0
        self._cost = self._cost * 0.8 + cost_ms * 0.2
        self.interval = int(min(self.MAX_INTERVAL,
                                max(self.base_interval, self._cost / self.MAX_LOAD)))
        try:
            self.root.after(self.interval, self._tick)
        except tk.TclError:
            self._alive = False


# ==================================================================
#  Effekt-Widgets
# ==================================================================
class AsciiBanner(tk.Text):
    """ASCII-Logo; eine Regenbogenwelle laeuft ueber die Spalten.

    Trick fuer Tempo: Jede Spalte bekommt EINMAL einen Gruppen-Tag.
    Pro Frame werden nur die Tag-Farben rotiert (wenige Calls statt tausende).
    """

    GROUPS = 28

    def __init__(self, parent, size=12):
        lines = banner_lines()
        self.rows = len(lines)
        self.cols = len(lines[0]) if lines else 0
        super().__init__(
            parent, height=self.rows, width=self.cols, wrap="none",
            bg=COLORS["bg"], fg=COLORS["cyan"], insertbackground=COLORS["bg"],
            relief="flat", borderwidth=0, highlightthickness=0,
            cursor="arrow", font=(MONO_FONT, size), padx=0, pady=0,
            selectbackground=COLORS["bg"], selectforeground=COLORS["cyan"],
        )
        self.insert("1.0", "\n".join(lines))

        # Farbgruppen nach RELATIVER Spaltenposition: so laeuft genau EIN
        # Verlauf ueber die Bannerbreite. (c % GROUPS wuerde ihn mehrfach
        # wiederholen und die Buchstabenformen optisch zerhacken.)
        width = max(self.cols, 1)
        self.groups = self.GROUPS
        for g in range(self.groups):
            c0 = (g * width) // self.groups
            c1 = ((g + 1) * width) // self.groups
            for r in range(self.rows):
                self.tag_add(f"g{g}", f"{r + 1}.{c0}", f"{r + 1}.{c1}")
        self.configure(state="disabled")

    def tick(self, frame):
        n = len(FLAG_BRIGHT)
        step = n / float(self.groups)
        for g in range(self.groups):
            self.tag_config(f"g{g}", foreground=FLAG_BRIGHT[int(g * step - frame) % n])


class GradientLine(tk.Canvas):
    """Duenne Linie mit laufendem Farbverlauf."""

    def __init__(self, parent, height=3, segments=48, soft=False):
        super().__init__(parent, height=height, bg=COLORS["bg"],
                         highlightthickness=0, borderwidth=0)
        self.segments = segments
        self.height_px = height
        self.ramp = FLAG_BRIGHT if soft else FLAG
        self._items = []
        self._last_w = -1
        self.bind("<Configure>", self._layout)

    def _layout(self, _event=None):
        # <Configure> feuert waehrend eines Fenster-Resize dutzendfach pro
        # Sekunde. Ohne diesen Vergleich wuerde der Canvas jedes Mal komplett
        # neu aufgebaut - das ruckelt sichtbar.
        w = max(self.winfo_width(), 10)
        if w == self._last_w:
            return
        self._last_w = w
        self.delete("all")
        self._items = []
        sw = w / float(self.segments)
        for i in range(self.segments):
            self._items.append(self.create_rectangle(
                i * sw, 0, (i + 1) * sw + 1, self.height_px + 2,
                width=0, fill=self.ramp[i % len(self.ramp)]))

    def tick(self, frame):
        n = len(self.ramp)
        for i, item in enumerate(self._items):
            self.itemconfig(item, fill=self.ramp[(i * 2 - frame) % n])


class Equalizer(tk.Canvas):
    """Pulsierende Balken - schneller, solange eine Aufgabe laeuft."""

    def __init__(self, parent, bars=26, width=168, height=26):
        super().__init__(parent, width=width, height=height, bg=COLORS["surface"],
                         highlightthickness=0, borderwidth=0)
        self.h = height
        self.active = False
        self._items = []
        self._x = []                     # x-Positionen merken statt coords() lesen
        bw = width / float(bars)
        for i in range(bars):
            x0 = i * bw + 1
            x1 = x0 + bw - 2
            self._x.append((x0, x1))
            self._items.append(self.create_rectangle(
                x0, height - 4, x1, height, width=0,
                fill=FLAG[i % len(FLAG)]))

    def tick(self, frame):
        speed = 4 if self.active else 1
        amp = 1.0 if self.active else 0.35
        n = len(FLAG)
        recolor = (frame % 2 == 0)       # Farbwechsel nur jedes 2. Frame
        for i, item in enumerate(self._items):
            v = (math.sin((frame * speed + i * 4) / 7.0) + 1) / 2.0
            bar_h = 3 + v * (self.h - 6) * amp
            x0, x1 = self._x[i]
            self.coords(item, x0, self.h - bar_h, x1, self.h)
            if recolor:
                self.itemconfig(item, fill=FLAG[(i * 2 - frame) % n])


class MatrixRain(tk.Canvas):
    """Herabrieselnde Zeichenspalten (dezenter Hintergrund-Effekt)."""

    SHADES = 12          # vorberechnete Helligkeitsstufen statt mix() pro Frame

    def __init__(self, parent, height=190, color=None, spacing=22):
        super().__init__(parent, height=height, bg=COLORS["bg2"],
                         highlightthickness=0, borderwidth=0)
        self.h = height
        self.spacing = spacing
        self.base_color = color or COLORS["cyan"]
        self._cols = []
        self._shades = []
        self._last_w = -1
        self._build_shades()
        self.bind("<Configure>", self._layout)

    def _build_shades(self):
        self._shades = [mix(COLORS["bg2"], self.base_color, (i + 1) / float(self.SHADES))
                        for i in range(self.SHADES)]

    def _text(self, length):
        return "\n".join(random.choice(MATRIX_CHARS) for _ in range(length))

    def _layout(self, _event=None):
        w = max(self.winfo_width(), 10)
        if w == self._last_w:
            return                    # kein Neuaufbau bei reiner Hoehenaenderung
        self._last_w = w
        self.delete("all")
        self._cols = []
        for x in range(6, w, self.spacing):
            length = random.randint(6, 14)
            y = random.randint(-self.h, 0)
            item = self.create_text(
                x, y, text=self._text(length), font=(MONO_FONT, 11),
                fill=self._shades[-1], anchor="n", justify="center")
            self._cols.append({
                "item": item, "x": x, "y": float(y),
                "speed": random.uniform(2.5, 8.0),
                "len": length,
                "shade": random.uniform(0.3, 1.0),
                "idx": -1,
            })

    def set_color(self, color):
        if color == self.base_color:
            return
        self.base_color = color
        self._build_shades()
        for col in self._cols:
            col["idx"] = -1          # erzwingt Neufaerbung

    def tick(self, frame):
        # y wird selbst gefuehrt (kein coords()-Lesecall), Farbe nur bei
        # echtem Stufenwechsel gesetzt -> deutlich weniger Tk-Calls.
        for col in self._cols:
            item = col["item"]
            y = col["y"] + col["speed"]
            if y > self.h + 10:
                y = -col["len"] * 14.0
                col["speed"] = random.uniform(2.5, 8.0)
                col["shade"] = random.uniform(0.3, 1.0)
                self.itemconfig(item, text=self._text(col["len"]))
            col["y"] = y
            self.coords(item, col["x"], y)
            fade = 1.0 - (y / float(self.h + 40))
            level = col["shade"] * (0.35 + 0.65 * max(0.0, min(1.0, fade)))
            idx = max(0, min(self.SHADES - 1, int(level * self.SHADES)))
            if idx != col["idx"]:
                col["idx"] = idx
                self.itemconfig(item, fill=self._shades[idx])


class NeonCard(ctk.CTkFrame):
    """Karte mit pulsierendem Neonrand.

    CTk-configure() ist teuer, deshalb: nur jedes 3. Frame rechnen und den
    Rand nur dann wirklich setzen, wenn sich die Farbstufe geaendert hat.
    """

    STEPS = 10

    def __init__(self, parent, accent, **kw):
        super().__init__(parent, fg_color=COLORS["surface"], corner_radius=14,
                         border_width=2, border_color=accent, **kw)
        self.accent = accent
        self.phase = random.uniform(0, math.pi * 2)
        self._ramp = [mix(COLORS["border"], accent, 0.35 + (i / float(self.STEPS - 1)) * 0.65)
                      for i in range(self.STEPS)]
        self._idx = -1

    def tick(self, frame):
        if frame % 3:
            return
        v = (math.sin(frame / 9.0 + self.phase) + 1) / 2.0
        idx = max(0, min(self.STEPS - 1, int(v * self.STEPS)))
        if idx != self._idx:
            self._idx = idx
            self.configure(border_color=self._ramp[idx])


# ==================================================================
#  Hauptfenster
# ==================================================================
class HoferiumApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(config.APP_TITLE)
        # customtkinter multipliziert geometry()/minsize() mit dem DPI-Faktor.
        # Bei 150 % (verbreitet auf Notebooks) wuerde aus 1280x860 sonst
        # 1920x1290 - die Statusleiste faende auf dem Schirm keinen Platz mehr.
        self.minsize(1000, 620)
        try:
            scale = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            scale = 1.0
        scale = scale or 1.0
        w = max(1000, min(1280, int(self.winfo_screenwidth() / scale) - 60))
        h = max(620, min(880, int(self.winfo_screenheight() / scale) - 90))
        self.geometry(f"{w}x{h}")
        self.configure(fg_color=COLORS["bg"])

        self.admin = is_admin()
        self.reporter: Reporter | None = None
        self._running = False
        self._current = "dashboard"
        self._flash = 0
        self._closing = False
        self._nav_pulse = None
        self._admin_mark = ""
        self._render_token = 0
        self._sysinfo = None
        self._clean_scanned = False
        self._clean_scanning = False
        self._last_cancel = 0.0
        self._update_state = "idle"
        self._backup_done = False
        self._reboot_win = None
        self._update_info = None
        # Rueckkanal fuer Hintergrund-Threads. Tk darf NUR aus dem
        # Hauptthread angefasst werden - Ergebnisse kommen deshalb hier an
        # und werden von _pump_ui_queue im Tk-Thread verarbeitet.
        self._ui_queue: "queue.Queue" = queue.Queue()
        self._nav_buttons: dict = {}
        self._pages: dict = {}
        self._page_cards: dict = {}
        self.uninstall_rows: dict = {}
        self.bloat_vars: dict = {}
        self.tweak_vars: dict = {}
        self.clean_vars: dict = {}
        self.software_vars: dict = {}

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._apply_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.anim = Animator(self)
        self._build_banner()
        self._build_sidebar()
        self._build_content()
        self._build_statusbar()
        self._build_all_pages()
        self._register_global_effects()
        self.show_page("dashboard")
        self.after(120, self._pump_ui_queue)
        self._start_update_check()

    # --------------------------------------------------------------
    #  Versionspruefung
    # --------------------------------------------------------------
    UPDATE_GIVE_UP_MS = 60_000     # ohne Verbindung nach 1 Minute aufgeben

    def _start_update_check(self):
        """Fragt im Hintergrund, ob eine neuere Fassung vorliegt.

        Laeuft in einem Daemon-Thread und haelt den Start nie auf. Antwortet
        das Netz nicht (kein Internet, DNS haengt, Firewall), wird die
        Pruefung nach einer Minute stillschweigend uebersprungen.
        """
        self._update_state = "pending"

        def worker():
            self._ui_queue.put(("update", updater.check(__version__)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(self.UPDATE_GIVE_UP_MS, self._update_give_up)

    def _update_give_up(self):
        if self._update_state != "pending":
            return
        self._update_state = "skipped"
        self.update_label.configure(text="Version: keine Verbindung",
                                    text_color=COLORS["dim"])
        self._log_line("Versionspruefung uebersprungen - keine Verbindung.", "info")

    def _apply_update_info(self, info):
        if self._update_state == "skipped":
            return                      # zu spaet - Antwort wird verworfen
        self._update_state = "done"
        self._update_info = info
        if not info.ok:
            self.update_label.configure(text="Version: nicht pruefbar",
                                        text_color=COLORS["dim"])
            self._log_line(f"Versionspruefung fehlgeschlagen: {info.error}", "info")
            return
        if info.available:
            self.update_label.configure(
                text=f"Neue Version {info.latest} verfuegbar",
                text_color=COLORS["amber"])
            self.update_btn.pack(side="right", padx=(8, 0))
            self._log_line(f"Neue Version verfuegbar: {info.latest} "
                           f"(installiert: {info.current})", "head")
        else:
            self.update_label.configure(text=f"Version {info.current} - aktuell",
                                        text_color=COLORS["green"])

    def _do_update(self):
        info = getattr(self, "_update_info", None)
        if not info or not info.available:
            return
        if not messagebox.askyesno(
                "Aktualisieren",
                f"Version {info.latest} wird von GitHub geladen und ersetzt die "
                f"vorhandenen Programmdateien.\n\n"
                f"Quelle: {updater.REPO_URL}\n\n"
                f"Die bisherige Fassung wird vorher gesichert. Hoferium startet "
                f"anschliessend von selbst neu.\n\nFortfahren?"):
            return
        target = stick_dir()

        def worker(rep):
            if updater.apply(target, rep):
                self._ui_queue.put(("restart", target))

        self.run_task(worker, f"Update auf {info.latest}")

    # ---- Neustart nach dem Update ----
    RESTART_DELAY_S = 4

    def _schedule_restart(self, target):
        """Zaehlt sichtbar herunter und startet dann neu."""
        self._restart_target = target
        self._restart_left = self.RESTART_DELAY_S
        self._log_line("Update fertig - Hoferium startet gleich neu.", "head")
        self._tick_restart()

    def _tick_restart(self):
        if self._closing:
            return
        if self._restart_left <= 0:
            self._perform_restart()
            return
        self._set_status(f"Neustart in {self._restart_left} ...", COLORS["amber"])
        self._restart_left -= 1
        self.after(1000, self._tick_restart)

    def _perform_restart(self):
        target = getattr(self, "_restart_target", None) or stick_dir()
        if updater.restart_app(target, delay=3):
            self._log_line("Neustart wird ausgefuehrt ...", "ok")
            self._closing = True
            try:
                self.anim.stop()
            except Exception:
                pass
            self.after(300, self.destroy)
        else:
            self._set_status("Neustart nicht moeglich - bitte manuell starten.",
                             COLORS["amber"])
            messagebox.showinfo(
                "Neustart noetig",
                "Das Update ist eingespielt, der automatische Neustart hat aber "
                "nicht geklappt.\n\nBitte Hoferium schliessen und ueber "
                "hoferium.bat neu starten.")

    def _pump_ui_queue(self):
        """Nimmt Ergebnisse aus Hintergrund-Threads entgegen (Tk-Thread)."""
        if self._closing:
            return
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "sysinfo":
                    self._apply_sysinfo(payload)
                elif kind == "apps":
                    self._populate_uninstall(payload)
                elif kind == "clean_sizes":
                    self._show_clean_sizes(payload)
                elif kind == "update":
                    self._apply_update_info(payload)
                elif kind == "restart":
                    self._schedule_restart(payload)
                elif kind == "backups":
                    self._show_backups(payload)
                elif kind == "catalog":
                    apps, source = payload
                    self._render_catalog(apps, source)
        except queue.Empty:
            pass
        except Exception as e:              # ein kaputter Handler darf die
            self._log_line(f"UI-Meldung: {e}", "warn")   # Schleife nicht toeten
        if not self._closing:
            self.after(120, self._pump_ui_queue)

    def _apply_icon(self):
        """Fenster- und Taskleisten-Symbol setzen.

        Die AppUserModelID ist noetig, damit Windows das Programm als eigene
        Anwendung fuehrt - sonst erscheint in der Taskleiste das Python-Symbol.
        """
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "Hoferium.PCToolkit")
            except Exception:
                pass
        ico = config.asset("hoferium.ico")
        if not ico.exists():
            return
        try:
            self.iconbitmap(default=str(ico))
        except tk.TclError:
            try:                       # Linux/Fallback: PNG-Weg
                self.iconphoto(True, tk.PhotoImage(file=str(ico)))
            except Exception:
                pass

    # --------------------------------------------------------------
    #  Kopfbereich: ASCII-Logo + Laufschrift
    # --------------------------------------------------------------
    def _build_banner(self):
        head = ctk.CTkFrame(self, corner_radius=0, fg_color=COLORS["bg"])
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.grid_columnconfigure(0, weight=1)

        wrap = ctk.CTkFrame(head, fg_color=COLORS["bg"])
        wrap.grid(row=0, column=0, pady=(12, 0))
        self.banner = AsciiBanner(wrap, size=12)
        self.banner.pack()

        self.tagline = ctk.CTkLabel(head, text="", font=(MONO_FONT, 13),
                                    text_color=COLORS["cyan"])
        self.tagline.grid(row=1, column=0, pady=(4, 8))

        self.topline = GradientLine(head, height=3)
        self.topline.grid(row=2, column=0, sticky="ew")

        self._tag_idx = 0
        self._tag_pos = 0
        self._tag_hold = 0

    def _tick_tagline(self, frame):
        if frame % 2:
            return
        text = TAGLINES[self._tag_idx % len(TAGLINES)]
        if self._tag_hold > 0:
            self._tag_hold -= 1
            if self._tag_hold == 0:
                self._tag_idx += 1
                self._tag_pos = 0
            return
        self._tag_pos += 1
        shown = text[:self._tag_pos]
        cursor = "_" if (frame // 4) % 2 else " "
        self.tagline.configure(text=f"> {shown}{cursor}")
        if self._tag_pos >= len(text):
            self._tag_hold = 26

    # --------------------------------------------------------------
    #  Navigation
    # --------------------------------------------------------------
    def _build_sidebar(self):
        bar = ctk.CTkFrame(self, width=246, corner_radius=0, fg_color=COLORS["sidebar"])
        bar.grid(row=1, column=0, sticky="nsew")
        bar.grid_propagate(False)
        # Aufteilung: Kopf / Navigation (dehnbar, notfalls scrollend) / Fussteil.
        # Nur so bleibt der Fussteil auch bei kleinem Fenster sichtbar - sonst
        # schoebe die Navigation ihn unten heraus.
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(bar, text=config.APP_SUBTITLE, font=(MONO_FONT, 11),
                     text_color=COLORS["dim"]).grid(row=0, column=0, sticky="w",
                                                    padx=20, pady=(16, 8))
        navbox = ctk.CTkScrollableFrame(
            bar, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=COLORS["sidebar"],
            scrollbar_button_hover_color=COLORS["surface2"])
        navbox.grid(row=1, column=0, sticky="nsew")
        navbox.grid_columnconfigure(0, weight=1)
        foot = ctk.CTkFrame(bar, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew")

        nav = [
            ("dashboard", "Start"),
            ("backup", "Datensicherung"),
            ("restore", "Zurueckholen"),
            ("software", "Software holen"),
            ("uninstall", "Deinstallieren"),
            ("debloat", "Debloat"),
            ("tweaks", "Tweaks"),
            ("cleaner", "Cleaner"),
            ("tools", "Extra-Tools"),
            ("log", "Log"),
            ("info", "Info"),
        ]
        for key, label in nav:
            btn = ctk.CTkButton(
                navbox, text=f"  {NAV_ICONS[key]}   {label}", anchor="w",
                font=_font(14), fg_color="transparent", text_color=COLORS["muted"],
                hover_color=COLORS["surface2"], corner_radius=9, height=38,
                border_width=0, command=lambda k=key: self.show_page(k),
            )
            btn.pack(fill="x", padx=6, pady=1)
            btn.bind("<Enter>", lambda _e, k=key: self._hover_nav(k, True))
            btn.bind("<Leave>", lambda _e, k=key: self._hover_nav(k, False))
            self._nav_buttons[key] = btn

        # Fussteil: immer erreichbar, egal wie hoch das Fenster ist.
        self.boot_btn = ctk.CTkButton(
            foot, text="⏻   VOM STICK BOOTEN", height=40, font=_font(12, "bold"),
            corner_radius=9, fg_color="transparent", border_width=2,
            border_color=COLORS["red"], text_color=COLORS["red"],
            hover_color=mix(COLORS["surface2"], COLORS["red"], 0.25),
            command=self._ask_boot)
        self.boot_btn.pack(fill="x", padx=10, pady=(10, 8))

        self.admin_label = ctk.CTkLabel(
            foot, text="", font=(MONO_FONT, 12),
            text_color=COLORS["green"] if self.admin else COLORS["amber"])
        self.admin_label.pack(anchor="w", padx=20, pady=(0, 6))

        self.sideline = GradientLine(foot, height=3, segments=24)
        self.sideline.pack(fill="x", padx=10, pady=(0, 12))

    # --------------------------------------------------------------
    #  Neustart zum Booten vom Windows-Stick
    # --------------------------------------------------------------
    REBOOT_DELAY_S = 15      # genug Zeit, es sich anders zu ueberlegen

    def _ask_boot(self):
        if self._running:
            return messagebox.showinfo(
                "Es laeuft noch etwas",
                "Warte, bis die laufende Aufgabe fertig ist - sonst bleibt sie "
                "unvollstaendig.")

        warn = ""
        if not self._backup_done:
            warn = ("\n\nACHTUNG: In dieser Sitzung wurde noch KEINE Sicherung "
                    "angelegt. Nach dem Neuinstallieren sind die Daten weg.")
        win = ctk.CTkToplevel(self)
        win.title("Vom Stick booten")
        win.configure(fg_color=COLORS["bg"])
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(win, text="⏻  NEU STARTEN UND VOM STICK BOOTEN",
                     font=(MONO_FONT, 16, "bold"), text_color=COLORS["red"]
                     ).pack(anchor="w", padx=24, pady=(22, 6))
        ctk.CTkLabel(
            win, text=("Der Rechner startet neu. Waehle, wohin es danach gehen "
                       "soll - der Windows-Stick muss dafuer eingesteckt sein."
                       + warn),
            font=_font(12), text_color=COLORS["muted"] if not warn else COLORS["amber"],
            justify="left", wraplength=520).pack(anchor="w", padx=24, pady=(0, 14))

        def choose(mode):
            win.grab_release()
            win.destroy()
            self._start_reboot(mode)

        opts = [("Startoptionen - Geraet auswaehlen", "options", COLORS["red"],
                 "Empfohlen: Windows zeigt 'Ein Geraet verwenden' mit der Liste "
                 "der bootfaehigen Datentraeger."),
                ("Firmware / BIOS-Setup", "firmware", COLORS["amber"],
                 "Direkt ins UEFI-Setup, dort Bootreihenfolge oder Bootmenue.")]
        for label, mode, col, note in opts:
            if mode == "firmware" and not winutils.is_uefi():
                continue                       # ohne UEFI gibt es diesen Weg nicht
            box = ctk.CTkFrame(win, fg_color=COLORS["surface"], corner_radius=10)
            box.pack(fill="x", padx=24, pady=5)
            ctk.CTkButton(box, text=label, height=42, font=_font(13, "bold"),
                          corner_radius=9, fg_color=col,
                          hover_color=mix(col, "#000000", 0.3), text_color="#08101c",
                          command=lambda m=mode: choose(m)).pack(
                fill="x", padx=12, pady=(12, 4))
            ctk.CTkLabel(box, text=note, font=_font(11), text_color=COLORS["dim"],
                         justify="left", wraplength=470).pack(anchor="w", padx=14,
                                                              pady=(0, 10))
        if not winutils.is_uefi():
            ctk.CTkLabel(win, text="(Dieser PC laeuft nicht im UEFI-Modus - der "
                                   "Weg ins Firmware-Setup entfaellt.)",
                         font=_font(11), text_color=COLORS["dim"]).pack(
                anchor="w", padx=24)

        ctk.CTkButton(win, text="ABBRECHEN", height=38, font=_font(12, "bold"),
                      fg_color="transparent", border_width=2,
                      border_color=COLORS["surface3"], text_color=COLORS["muted"],
                      hover_color=COLORS["surface2"],
                      command=lambda: (win.grab_release(), win.destroy())).pack(
            fill="x", padx=24, pady=(12, 22))
        win.update_idletasks()
        # mittig ueber dem Hauptfenster
        x = self.winfo_x() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _start_reboot(self, mode):
        res = winutils.reboot(mode, delay=self.REBOOT_DELAY_S)
        if res.rc != 0:
            self._log_line(f"Neustart nicht moeglich: "
                           f"{(res.err or res.out).strip()[:120] or res.rc}", "err")
            messagebox.showerror(
                "Neustart nicht moeglich",
                "Windows hat den Neustart abgelehnt.\n\n"
                f"{(res.err or res.out).strip()[:300]}\n\n"
                "Ohne Administrator-Rechte ist das nicht moeglich.")
            return
        self._log_line(f"Neustart angekuendigt ({self.REBOOT_DELAY_S} Sekunden).",
                       "head")
        self._reboot_left = self.REBOOT_DELAY_S
        self._reboot_win = ctk.CTkToplevel(self)
        w = self._reboot_win
        w.title("Neustart")
        w.configure(fg_color=COLORS["bg"])
        w.resizable(False, False)
        w.transient(self)
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        self._reboot_label = ctk.CTkLabel(w, text="", font=(MONO_FONT, 18, "bold"),
                                          text_color=COLORS["red"])
        self._reboot_label.pack(padx=40, pady=(26, 8))
        ctk.CTkLabel(w, text="Stick eingesteckt? Dann gleich im Menue auswaehlen.",
                     font=_font(12), text_color=COLORS["muted"]).pack(padx=40)
        ctk.CTkButton(w, text="DOCH NICHT - ABBRECHEN", height=42,
                      font=_font(13, "bold"), fg_color=COLORS["surface3"],
                      hover_color=COLORS["red_hover"], text_color=COLORS["text"],
                      command=self._abort_reboot).pack(padx=40, pady=(16, 26))
        w.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - w.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - w.winfo_height()) // 3
        w.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._tick_reboot()

    def _tick_reboot(self):
        if not getattr(self, "_reboot_win", None) or self._closing:
            return
        try:
            self._reboot_label.configure(
                text=f"Neustart in {self._reboot_left} Sekunden")
        except tk.TclError:
            return
        if self._reboot_left <= 0:
            return                      # Windows uebernimmt jetzt
        self._reboot_left -= 1
        self.after(1000, self._tick_reboot)

    def _abort_reboot(self):
        winutils.cancel_reboot()
        self._log_line("Neustart abgebrochen.", "warn")
        self._set_status("Neustart abgebrochen.", COLORS["muted"])
        win = getattr(self, "_reboot_win", None)
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
            self._reboot_win = None

    def _hover_nav(self, key, entering):
        if key == self._current:
            return
        btn = self._nav_buttons[key]
        btn.configure(text_color=PAGE_COLORS[key] if entering else COLORS["muted"])

    def _tick_nav(self, frame):
        # CTkButton.configure ist teuer -> nur jedes 2. Frame und nur bei
        # echtem Stufenwechsel neu faerben.
        if frame % 2:
            return
        v = (math.sin(frame / 7.0) + 1) / 2.0
        idx = int(v * 8)
        if (idx, self._current) != self._nav_pulse:
            self._nav_pulse = (idx, self._current)
            color = PAGE_COLORS[self._current]
            self._nav_buttons[self._current].configure(
                text_color=mix(color, "#ffffff", (idx / 8.0) * 0.6))
        mark = "█" if (frame // 6) % 2 else "▓"
        if mark != self._admin_mark:
            self._admin_mark = mark
            state = "ADMIN" if self.admin else "KEIN ADMIN"
            self.admin_label.configure(text=f"{mark} {state}")

    # --------------------------------------------------------------
    #  Inhaltsbereich
    # --------------------------------------------------------------
    def _build_content(self):
        self.content = ctk.CTkFrame(self, corner_radius=0, fg_color=COLORS["bg"])
        self.content.grid(row=1, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(self.content, fg_color=COLORS["bg"])
        head.grid(row=0, column=0, sticky="ew", padx=26, pady=(14, 0))
        head.grid_columnconfigure(0, weight=1)
        self.header_title = ctk.CTkLabel(head, text="", font=(MONO_FONT, 22, "bold"),
                                         text_color=COLORS["cyan"], anchor="w")
        self.header_title.grid(row=0, column=0, sticky="w")
        self.header_sub = ctk.CTkLabel(head, text="", font=_font(12),
                                       text_color=COLORS["muted"], anchor="w")
        self.header_sub.grid(row=1, column=0, sticky="w", pady=(2, 8))

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, height=62, corner_radius=0, fg_color=COLORS["surface"])
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(2, weight=1)

        self.equalizer = Equalizer(bar)
        self.equalizer.grid(row=0, column=0, padx=(16, 12), pady=12)

        self.progress = ctk.CTkProgressBar(bar, width=230, height=12,
                                           progress_color=COLORS["cyan"],
                                           fg_color=COLORS["surface3"])
        self.progress.set(0)
        self.progress.grid(row=0, column=1, padx=(0, 16))

        self.activity_label = ctk.CTkLabel(bar, text="Bereit.", font=(MONO_FONT, 12),
                                           text_color=COLORS["muted"], anchor="w")
        self.activity_label.grid(row=0, column=2, sticky="w")
        self._status_base = "Bereit."

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=3, padx=16)
        self.update_label = ctk.CTkLabel(right, text="Version wird geprueft ...",
                                         font=(MONO_FONT, 11),
                                         text_color=COLORS["dim"])
        self.update_label.pack(side="left", padx=(0, 12))
        self.update_btn = ctk.CTkButton(
            right, text="AKTUALISIEREN", width=140, height=34, font=_font(12, "bold"),
            fg_color=COLORS["amber"], hover_color=mix(COLORS["amber"], "#000000", 0.3),
            text_color="#08101c", corner_radius=9, command=self._do_update)
        # wird erst eingeblendet, wenn es wirklich etwas Neues gibt
        self.cancel_btn = ctk.CTkButton(
            right, text="ABBRECHEN", width=124, height=34, font=_font(12, "bold"),
            fg_color=COLORS["surface3"], hover_color=COLORS["red_hover"],
            text_color=COLORS["muted"], corner_radius=9,
            state="disabled", command=self._cancel)
        self.cancel_btn.pack(side="right")

    def _register_global_effects(self):
        self.anim.add(self.banner.tick)
        self.anim.add(self.topline.tick)
        self.anim.add(self.sideline.tick)
        self.anim.add(self.equalizer.tick)
        self.anim.add(self._tick_tagline)
        self.anim.add(self._tick_nav)
        self.anim.add(self._tick_status)
        self.anim.add(self._tick_cards)

    def _set_status(self, text, color=None):
        """Einzige Stelle, die den Statustext setzt (der Ticker haengt nur den
        Spinner davor - deshalb wird der Rohtext separat gehalten)."""
        self._status_base = text
        self.activity_label.configure(text=text,
                                      text_color=color or COLORS["muted"])

    def _tick_status(self, frame):
        if self._running:
            spin = SPINNER[(frame // 2) % len(SPINNER)]
            self.activity_label.configure(text=f"{spin}  {self._status_base}",
                                          text_color=COLORS["cyan"])
            self.progress.configure(progress_color=FLAG[frame % len(FLAG)])
        if self._flash > 0:
            self._flash -= 1

    def _tick_cards(self, frame):
        for card in self._page_cards.get(self._current, []):
            card.tick(frame)

    # --------------------------------------------------------------
    #  Seitengeruest
    # --------------------------------------------------------------
    def _page(self, key):
        frame = ctk.CTkScrollableFrame(
            self.content, fg_color=COLORS["bg"],
            scrollbar_button_color=COLORS["surface3"],
            scrollbar_button_hover_color=PAGE_COLORS[key])
        frame.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 10))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_remove()
        self._page_cards[key] = []
        return frame

    def _card(self, parent, page_key, title=None, icon=""):
        accent = PAGE_COLORS[page_key]
        card = NeonCard(parent, accent)
        card.pack(fill="x", padx=6, pady=9)
        self._page_cards[page_key].append(card)
        if title:
            head = ctk.CTkFrame(card, fg_color="transparent")
            head.pack(fill="x", padx=18, pady=(14, 2))
            ctk.CTkLabel(head, text=f"{icon}  {title}".strip(),
                         font=(MONO_FONT, 16, "bold"),
                         text_color=accent).pack(side="left")
        return card

    # Karten sind bei minsize (1120 px) rund 790 px breit - der Umbruch muss
    # darunter bleiben, sonst laeuft Text ueber den Kartenrand hinaus.
    WRAP = 700

    def _hint(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=_font(12), text_color=COLORS["muted"],
                     wraplength=self.WRAP, justify="left").pack(
            anchor="w", padx=18, pady=(2, 10))

    def _btn(self, parent, text, command, color, big=True):
        return ctk.CTkButton(
            parent, text=text, command=command, height=44 if big else 36,
            font=_font(14 if big else 12, "bold"), corner_radius=10,
            fg_color=color, hover_color=mix(color, "#000000", 0.35),
            text_color="#08101c", border_width=0)

    def _ghost(self, parent, text, command, color):
        return ctk.CTkButton(
            parent, text=text, command=command, height=44, font=_font(14, "bold"),
            corner_radius=10, fg_color="transparent", border_width=2,
            border_color=color, text_color=color,
            hover_color=mix(COLORS["surface2"], color, 0.25))

    def _checkbox(self, parent, text, var, color=None):
        c = color or COLORS["cyan"]
        return ctk.CTkCheckBox(parent, text=text, variable=var, font=_font(13),
                               fg_color=c, hover_color=c, checkmark_color="#08101c",
                               text_color=COLORS["text"], border_color=COLORS["border"],
                               border_width=2, corner_radius=5)

    def _build_all_pages(self):
        self._page_meta = {
            "dashboard": ("START", "Alles fuer den PC-Wechsel an einem Ort."),
            "backup":    ("DATENSICHERUNG", "Browser, Windows-Key, WLAN, Programme, Treiber ..."),
            "restore":   ("ZURUECKHOLEN", "Eine Sicherung auf diesen PC zurueckspielen."),
            "software":  ("SOFTWARE HOLEN", "Offizielle Installer speichern oder per winget setzen."),
            "uninstall": ("DEINSTALLIEREN", "Programme sauber entfernen - inklusive Reste."),
            "debloat":   ("DEBLOAT", "Windows-Bloatware entfernen - automatisch oder per Auswahl."),
            "tweaks":    ("TWEAKS", "Privatsphaere, Explorer und Taskleiste optimieren."),
            "cleaner":   ("CLEANER", "Temp-, Cache- und Update-Muell entfernen."),
            "tools":     ("EXTRA-TOOLS", "Bewaehrte externe Werkzeuge - transparent gestartet."),
            "log":       ("LOG", "Was passiert gerade?"),
            "info":      ("INFO", "Ueber Hoferium."),
        }
        self._pages["dashboard"] = self._build_dashboard()
        self._pages["backup"] = self._build_backup()
        self._pages["restore"] = self._build_restore()
        self._pages["software"] = self._build_software()
        self._pages["uninstall"] = self._build_uninstall()
        self._pages["debloat"] = self._build_debloat()
        self._pages["tweaks"] = self._build_tweaks()
        self._pages["cleaner"] = self._build_cleaner()
        self._pages["tools"] = self._build_tools()
        self._pages["log"] = self._build_log()
        self._pages["info"] = self._build_info()

    def show_page(self, key):
        for page in self._pages.values():
            page.grid_remove()
        for k, btn in self._nav_buttons.items():
            btn.configure(fg_color="transparent", text_color=COLORS["muted"])
        self._pages[key].grid()
        self._nav_buttons[key].configure(fg_color=COLORS["surface2"])
        self._current = key
        self._flash = 14
        color = PAGE_COLORS[key]
        title, sub = self._page_meta[key]
        self.header_title.configure(text=f"// {title}", text_color=color)
        self.header_sub.configure(text=sub)
        if hasattr(self, "matrix") and key == "dashboard":
            self.matrix.set_color(color)
        # Groessen erst berechnen, wenn die Seite wirklich gebraucht wird -
        # der Durchlauf ueber Temp-Ordner kostet ein paar Sekunden.
        if key == "cleaner" and not self._clean_scanned:
            self._clean_scanned = True
            self.after(150, self._do_clean_scan)

    # --------------------------------------------------------------
    #  Seiten
    # --------------------------------------------------------------
    def _build_dashboard(self):
        p = self._page("dashboard")

        rain_card = self._card(p, "dashboard")
        self.matrix = MatrixRain(rain_card, height=170, color=COLORS["cyan"])
        self.matrix.pack(fill="x", padx=3, pady=3)
        self.anim.add(self.matrix.tick, guard=lambda: self._current == "dashboard")

        quick = self._card(p, "dashboard", "SCHNELLSTART", "◈")
        self._hint(quick, "Stick einstecken, Aktion waehlen, fertig. Die drei "
                          "wichtigsten Wege:")
        row = ctk.CTkFrame(quick, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(2, 16))
        self._btn(row, "▼  KOMPLETT-SICHERUNG", lambda: self.show_page("backup"),
                  COLORS["green"]).pack(side="left", padx=6)
        self._btn(row, "☢  AUTO-DEBLOAT", lambda: self.show_page("debloat"),
                  COLORS["orange"]).pack(side="left", padx=6)
        self._btn(row, "≈  CLEANER", lambda: self.show_page("cleaner"),
                  COLORS["teal"]).pack(side="left", padx=6)

        # --- Windows-Schluessel (kopierbar) ---
        keycard = self._card(p, "dashboard", "WINDOWS-SCHLUESSEL", "◆")
        self._hint(keycard, "Der im BIOS/UEFI hinterlegte Schluessel. Bei "
                            "vorinstalliertem Windows wird er bei einer "
                            "Neuinstallation automatisch uebernommen.")
        krow = ctk.CTkFrame(keycard, fg_color="transparent")
        krow.pack(fill="x", padx=18, pady=(0, 8))
        self.key_var = tk.StringVar(value="wird gelesen ...")
        self.key_entry = ctk.CTkEntry(
            krow, textvariable=self.key_var, font=(MONO_FONT, 15, "bold"),
            height=44, fg_color=COLORS["surface2"], border_color=COLORS["border"],
            text_color=COLORS["amber"], justify="center")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.key_copy_btn = ctk.CTkButton(
            krow, text="KOPIEREN", width=130, height=44, font=_font(12, "bold"),
            fg_color=COLORS["amber"], hover_color=mix(COLORS["amber"], "#000000", 0.3),
            text_color="#08101c", command=self._copy_key)
        self.key_copy_btn.pack(side="left")
        self.activation_label = ctk.CTkLabel(keycard, text="", font=(MONO_FONT, 12),
                                             text_color=COLORS["muted"])
        self.activation_label.pack(anchor="w", padx=18, pady=(0, 14))

        # --- Systemdaten (gruppiert) ---
        self.sysinfo_cards = {}
        for title, keys in sysinfo.GROUPS:
            card = self._card(p, "dashboard", title, "▣")
            holder = ctk.CTkFrame(card, fg_color="transparent")
            holder.pack(fill="x", padx=18, pady=(0, 12))
            rows = {}
            for key in keys:
                r = ctk.CTkFrame(holder, fg_color="transparent")
                r.pack(fill="x", pady=2)
                ctk.CTkLabel(r, text=sysinfo.LABELS.get(key, key), width=150,
                             anchor="w", font=(MONO_FONT, 12),
                             text_color=COLORS["dim"]).pack(side="left")
                val = ctk.CTkLabel(r, text="...", anchor="w", font=(MONO_FONT, 12),
                                   text_color=COLORS["text"], justify="left",
                                   wraplength=self.WRAP - 60)
                val.pack(side="left", fill="x", expand=True)
                rows[key] = val
            self.sysinfo_cards[title] = rows

        env = self._card(p, "dashboard", "HOFERIUM", "◆")
        for k, v in (("Sicherungsziel", str(backup_root())),
                     ("Rechte", "Administrator" if self.admin else "eingeschraenkt"),
                     ("Version", __version__)):
            r = ctk.CTkFrame(env, fg_color="transparent")
            r.pack(fill="x", padx=18, pady=2)
            ctk.CTkLabel(r, text=k, width=150, anchor="w", font=(MONO_FONT, 12),
                         text_color=COLORS["dim"]).pack(side="left")
            ctk.CTkLabel(r, text=v, anchor="w", font=(MONO_FONT, 12),
                         text_color=COLORS["text"]).pack(side="left")
        ctk.CTkLabel(env, text="", height=8).pack()

        self._load_sysinfo()
        return p

    # --- Systemdaten im Hintergrund holen und eintragen ---
    def _load_sysinfo(self):
        def worker():
            info = sysinfo.collect()
            self._ui_queue.put(("sysinfo", info))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_sysinfo(self, info):
        self._sysinfo = info
        if not info.ok and info.error:
            self.key_var.set("(nicht lesbar)")
            self.activation_label.configure(
                text=f"Systemdaten nicht lesbar: {info.error}",
                text_color=COLORS["amber"])
            return
        key = info.windows_key
        if key:
            self.key_var.set(key)
        else:
            teil = info.get("KeyTeil")
            self.key_var.set(f"(kein Key im BIOS - Teilschluessel: {teil})"
                             if teil else "(kein Key im BIOS hinterlegt)")
            self.key_copy_btn.configure(state="disabled")
        akt = info.get("Aktivierung")
        if akt:
            good = akt.lower().startswith("aktiv")
            self.activation_label.configure(
                text=f"Aktivierung: {akt}",
                text_color=COLORS["green"] if good else COLORS["amber"])
        for title, keys in sysinfo.GROUPS:
            rows = self.sysinfo_cards.get(title, {})
            for key_name in keys:
                lbl = rows.get(key_name)
                if lbl is None:
                    continue
                value = info.get(key_name)
                lbl.configure(text=value if value else "-",
                              text_color=COLORS["text"] if value else COLORS["dim"])

    def _copy_key(self):
        key = self.key_var.get().strip()
        if not key or key.startswith("("):
            return
        self.clipboard_clear()
        self.clipboard_append(key)
        self.key_copy_btn.configure(text="KOPIERT")
        self.after(1400, lambda: self.key_copy_btn.configure(text="KOPIEREN"))

    def _build_backup(self):
        p = self._page("backup")
        opt = self._card(p, "backup", "WAS SOLL GESICHERT WERDEN?", "▼")
        self.backup_opts = {}
        items = [
            ("browsers", "Browser-Daten + Lesezeichen (HTML)", True),
            ("product_key", "Windows-Key & Aktivierung", True),
            ("wifi", "WLAN-Profile & Passwoerter", True),
            ("programs", "Liste installierter Programme (+winget)", True),
            ("system", "System-Infos", True),
            ("sticky_notes", "Sticky Notes", True),
            ("mail", "E-Mail-Profile (Thunderbird/Outlook)", True),
            ("misc", "Sonstiges (hosts, Drucker, Dienste ...)", True),
            ("drivers", "Treiber exportieren (gross/langsam)", True),
            ("user_files", "Eigene Dateien MITKOPIEREN (kann riesig sein)", False),
        ]
        grid = ctk.CTkFrame(opt, fg_color="transparent")
        grid.pack(fill="x", padx=14, pady=(4, 14))
        grid.grid_columnconfigure((0, 1), weight=1)
        for i, (key, label, default) in enumerate(items):
            var = tk.BooleanVar(value=default)
            self.backup_opts[key] = var
            self._checkbox(grid, label, var, COLORS["green"]).grid(
                row=i // 2, column=i % 2, sticky="w", padx=10, pady=6)

        tgt = self._card(p, "backup", "ZIELORDNER", "▤")
        r = ctk.CTkFrame(tgt, fg_color="transparent")
        r.pack(fill="x", padx=18, pady=(2, 16))
        self.backup_target = tk.StringVar(value=str(backup_root()))
        ctk.CTkEntry(r, textvariable=self.backup_target, font=(MONO_FONT, 12),
                     fg_color=COLORS["surface2"], border_color=COLORS["border"],
                     text_color=COLORS["text"], height=38).pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(r, text="DURCHSUCHEN", width=140, height=38,
                      font=_font(12, "bold"), fg_color="transparent", border_width=2,
                      border_color=COLORS["green"], text_color=COLORS["green"],
                      hover_color=COLORS["surface2"],
                      command=self._pick_backup_dir).pack(side="left")

        # --- Passwort-Assistent ---
        pw = self._card(p, "backup", "BROWSER-PASSWOERTER", "⚿")
        self._hint(pw, "Wichtig: Passwoerter von Chrome, Edge, Brave, Opera und "
                       "Vivaldi sind an dieses Windows-Konto gebunden und lassen "
                       "sich nach der Neuinstallation NICHT mehr entschluesseln - "
                       "auch nicht aus einer Kopie des Profils. Exportiere sie "
                       "deshalb JETZT, solange dieses Windows noch laeuft. "
                       "Firefox ist ausgenommen: dort reicht der gesicherte "
                       "Profilordner.")
        steps = ctk.CTkFrame(pw, fg_color=COLORS["surface2"], corner_radius=10)
        steps.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(steps, text=(
            "1.  Browser unten anklicken - die Passwortseite oeffnet sich\n"
            "2.  Dort auf 'Exportieren' klicken (Windows fragt nach dem "
            "Kontokennwort)\n"
            "3.  Die CSV-Datei in den Sicherungsordner legen\n"
            "4.  Nach dem Import auf dem neuen PC die CSV wieder loeschen"),
            font=(MONO_FONT, 12), text_color=COLORS["text"], justify="left"
        ).pack(anchor="w", padx=14, pady=10)

        self.pw_row = ctk.CTkFrame(pw, fg_color="transparent")
        self.pw_row.pack(fill="x", padx=16, pady=(0, 6))
        self.pw_status = ctk.CTkLabel(pw, text="Suche installierte Browser ...",
                                      font=(MONO_FONT, 11), text_color=COLORS["dim"])
        self.pw_status.pack(anchor="w", padx=18, pady=(0, 14))
        self.after(400, self._fill_password_helpers)

        act = ctk.CTkFrame(p, fg_color="transparent")
        act.pack(fill="x", padx=6, pady=8)
        self._btn(act, "►  SICHERUNG STARTEN", self._do_backup,
                  COLORS["green"]).pack(side="left", padx=6)
        return p

    def _fill_password_helpers(self):
        found = backup_mod.installed_password_browsers()
        for w in self.pw_row.winfo_children():
            w.destroy()
        if not found:
            self.pw_status.configure(text="Kein bekannter Browser gefunden.")
            return
        for name, exe, url in found:
            ctk.CTkButton(
                self.pw_row, text=name, width=118, height=38, font=_font(12, "bold"),
                fg_color="transparent", border_width=2, border_color=COLORS["green"],
                text_color=COLORS["green"], hover_color=COLORS["surface2"],
                command=lambda e=exe, u=url, n=name: self._open_passwords(e, u, n)
            ).pack(side="left", padx=(0, 8), pady=2)
        self.pw_status.configure(
            text=f"{len(found)} Browser gefunden - Firefox braucht keinen Export.")

    def _open_passwords(self, exe, url, name):
        try:
            subprocess.Popen([exe, url])
            self._log_line(f"Passwortseite in {name} geoeffnet - dort auf "
                           f"'Exportieren' klicken.", "head")
            self._set_status(f"{name}: Passwortseite geoeffnet", COLORS["green"])
        except Exception as e:
            self._log_line(f"{name} liess sich nicht oeffnen: {e}", "err")

    # Was sich zurueckholen laesst: Schluessel, Beschriftung, Standard, Hinweis
    RESTORE_ITEMS = [
        ("wifi", "WLAN-Netze samt Passwoertern", True, "vollautomatisch"),
        ("firefox", "Firefox-Profil", True, "Passwoerter, Lesezeichen, Verlauf"),
        ("thunderbird", "Thunderbird-Profil", True, "Konten und Mails"),
        ("sticky", "Kurznotizen", True, ""),
        ("outlook", "Outlook-Datendateien", True, "danach in Outlook oeffnen"),
        ("bookmarks", "Lesezeichen der uebrigen Browser", True,
         "oeffnet den Ordner - Import im Browser mit zwei Klicks"),
        ("hosts", "hosts-Datei", False, "nur bei eigenen Eintraegen noetig"),
        ("userfiles", "Eigene Dateien", False, "kann sehr gross sein"),
        ("programs", "Programme nachinstallieren", False,
         "laedt ueber winget aus dem Netz - dauert lange"),
        ("drivers", "Treiber einspielen", False, "nur wenn Geraete fehlen"),
    ]

    def _build_restore(self):
        p = self._page("restore")
        head = self._card(p, "restore", "SICHERUNG ZURUECKHOLEN", "▲")
        self._hint(head, "Hoferium sucht die Sicherungen neben dem Programm. Die "
                         "zu diesem Rechner passende ist vorausgewaehlt. Vor dem "
                         "Ueberschreiben wird der jetzige Zustand weggesichert - "
                         "der Import bleibt damit umkehrbar.")
        row = ctk.CTkFrame(head, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkButton(row, text="SICHERUNGEN SUCHEN", width=200, height=36,
                      font=_font(12, "bold"), fg_color="transparent", border_width=2,
                      border_color=COLORS["lime"], text_color=COLORS["lime"],
                      hover_color=COLORS["surface2"],
                      command=self._scan_backups).pack(side="left")
        ctk.CTkButton(row, text="ORDNER WAEHLEN ...", width=180, height=36,
                      font=_font(12, "bold"), fg_color="transparent", border_width=2,
                      border_color=COLORS["surface3"], text_color=COLORS["muted"],
                      hover_color=COLORS["surface2"],
                      command=self._pick_backup_folder).pack(side="left", padx=8)
        self.restore_status = ctk.CTkLabel(head, text="Noch nicht gesucht.",
                                           font=(MONO_FONT, 12),
                                           text_color=COLORS["muted"])
        self.restore_status.pack(anchor="w", padx=18, pady=(2, 12))

        self.restore_list = ctk.CTkFrame(p, fg_color="transparent")
        self.restore_list.pack(fill="x", padx=6, pady=2)

        sel = self._card(p, "restore", "WAS SOLL ZURUECK?", "☑")
        self.restore_vars = {}
        self.restore_rows = {}
        grid = ctk.CTkFrame(sel, fg_color="transparent")
        grid.pack(fill="x", padx=14, pady=(2, 14))
        for key, label, default, note in self.RESTORE_ITEMS:
            var = tk.BooleanVar(value=default)
            self.restore_vars[key] = var
            line = ctk.CTkFrame(grid, fg_color="transparent")
            line.pack(fill="x", pady=2)
            cb = self._checkbox(line, label, var, COLORS["lime"])
            cb.pack(side="left")
            state = ctk.CTkLabel(line, text="", font=(MONO_FONT, 11),
                                 text_color=COLORS["dim"])
            state.pack(side="right")
            if note:
                ctk.CTkLabel(line, text=note, font=_font(11),
                             text_color=COLORS["dim"]).pack(side="left", padx=10)
            self.restore_rows[key] = (cb, state)

        act = ctk.CTkFrame(p, fg_color="transparent")
        act.pack(fill="x", padx=6, pady=8)
        self._btn(act, "▲  JETZT ZURUECKHOLEN", self._do_restore,
                  COLORS["lime"]).pack(side="left", padx=6)
        self.after(600, self._scan_backups)
        return p

    def _scan_backups(self):
        self.restore_status.configure(text="Suche ...")

        def worker():
            try:
                found = restore.find_backups(stick_dir())
            except Exception as e:
                found = e
            self._ui_queue.put(("backups", found))

        threading.Thread(target=worker, daemon=True).start()

    def _pick_backup_folder(self):
        d = filedialog.askdirectory(title="Ordner mit den Sicherungen waehlen",
                                    initialdir=str(stick_dir()))
        if not d:
            return
        self.restore_status.configure(text="Suche ...")

        def worker():
            try:
                found = restore.find_backups(d)
            except Exception as e:
                found = e
            self._ui_queue.put(("backups", found))

        threading.Thread(target=worker, daemon=True).start()

    def _show_backups(self, found):
        for w in self.restore_list.winfo_children():
            w.destroy()
        self._backups = []
        if isinstance(found, Exception):
            self.restore_status.configure(text=f"Suche fehlgeschlagen: {found}",
                                          text_color=COLORS["amber"])
            return
        self._backups = found
        if not found:
            self.restore_status.configure(
                text="Keine Sicherung gefunden. Liegt der Stick nicht hier, "
                     "oben 'ORDNER WAEHLEN' benutzen.",
                text_color=COLORS["amber"])
            self._update_restore_marks(None)
            return

        eigen = sum(1 for b in found if b.is_this_pc)
        self.restore_status.configure(
            text=f"{len(found)} Sicherung(en) gefunden, davon {eigen} von diesem PC.",
            text_color=COLORS["muted"])
        self.backup_choice = tk.IntVar(value=0)
        for i, b in enumerate(found):
            card = NeonCard(self.restore_list,
                            COLORS["lime"] if b.is_this_pc else COLORS["surface3"])
            card.pack(fill="x", padx=0, pady=5)
            self._page_cards["restore"].append(card)
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=14, pady=(11, 2))
            ctk.CTkRadioButton(
                top, text=f"{b.computer}", variable=self.backup_choice, value=i,
                font=(MONO_FONT, 14, "bold"), fg_color=COLORS["lime"],
                hover_color=COLORS["lime"], border_color=COLORS["border"],
                text_color=COLORS["text"],
                command=lambda idx=i: self._update_restore_marks(found[idx])
            ).pack(side="left")
            tag = "dieser PC" if b.is_this_pc else "anderer PC"
            ctk.CTkLabel(top, text=tag, font=(MONO_FONT, 11),
                         text_color=COLORS["lime"] if b.is_this_pc else COLORS["dim"]
                         ).pack(side="left", padx=10)
            ctk.CTkLabel(top, text=f"{b.when}   {b.size_mb} MB",
                         font=(MONO_FONT, 12), text_color=COLORS["muted"]).pack(side="right")
            inhalt = [lbl for key, lbl, _d, _n in self.RESTORE_ITEMS
                      if b.available.get(key)]
            ctk.CTkLabel(card, text="enthaelt: " + (", ".join(inhalt) if inhalt else "nichts Verwertbares"),
                         font=_font(11), text_color=COLORS["dim"],
                         wraplength=self.WRAP, justify="left").pack(
                anchor="w", padx=(38, 14), pady=(0, 10))
        self._update_restore_marks(found[0])

    def _update_restore_marks(self, bset):
        """Zeigt je Eintrag, ob er in der gewaehlten Sicherung vorhanden ist,
        und waehlt Fehlendes ab."""
        for key, (cb, state) in self.restore_rows.items():
            has = bool(bset and bset.available.get(key))
            state.configure(text="vorhanden" if has else "nicht enthalten",
                            text_color=COLORS["lime"] if has else COLORS["dim"])
            cb.configure(state="normal" if has else "disabled")
            if not has:
                self.restore_vars[key].set(False)

    def _do_restore(self):
        chosen = getattr(self, "_backups", [])
        idx = self.backup_choice.get() if hasattr(self, "backup_choice") else -1
        if not chosen or idx < 0 or idx >= len(chosen):
            return messagebox.showinfo("Hoferium", "Bitte zuerst eine Sicherung waehlen.")
        bset = chosen[idx]
        opts = {k: v.get() for k, v in self.restore_vars.items()}
        gewaehlt = [lbl for key, lbl, _d, _n in self.RESTORE_ITEMS if opts.get(key)]
        if not gewaehlt:
            return self._need_selection()

        warn = ""
        if not bset.is_this_pc:
            warn = (f"\n\nACHTUNG: Die Sicherung stammt von '{bset.computer}', "
                    f"dieser Rechner heisst "
                    f"'{os.environ.get('COMPUTERNAME', '?')}'.")
        liste = "\n".join(f"  - {g}" for g in gewaehlt)
        if not messagebox.askyesno(
                "Zurueckholen bestaetigen",
                f"Aus {bset.path.name} wird zurueckgespielt:\n\n{liste}\n\n"
                f"Vorhandene Profile werden dabei ersetzt. Der jetzige Zustand "
                f"wird vorher gesichert.{warn}\n\nFortfahren?"):
            return

        backup_dir = local_dir() / "backups"
        options = restore.RestoreOptions(**opts)

        def worker(rep):
            ctx = RunContext(backup_dir, rep, admin=self.admin)
            restore.RestoreJob(ctx, bset).run(options)

        self.run_task(worker, f"Zurueckholen aus {bset.path.name}")

    def _build_software(self):
        p = self._page("software")
        card = self._card(p, "software", "PROGRAMME (OFFIZIELLE QUELLEN)", "↧")
        self._hint(card, "Auswaehlen und entweder als Installer speichern (fuer den "
                         "frischen PC) oder direkt per winget installieren. Die "
                         "Liste wird beim Start aus dem Projekt-Repo aufgefrischt - "
                         "so bleibt sie auch dann brauchbar, wenn sich "
                         "Download-Adressen aendern.")
        self.software_source = ctk.CTkLabel(card, text="", font=(MONO_FONT, 11),
                                            text_color=COLORS["dim"])
        self.software_source.pack(anchor="w", padx=18, pady=(0, 4))
        self.software_holder = ctk.CTkFrame(card, fg_color="transparent")
        self.software_holder.pack(fill="x", padx=10, pady=6)
        self._render_catalog(list(downloader.CATALOG), "eingebaut")

        act = ctk.CTkFrame(p, fg_color="transparent")
        act.pack(fill="x", padx=6, pady=8)
        self._btn(act, "↧  INSTALLER SPEICHERN", self._do_download,
                  COLORS["blue"]).pack(side="left", padx=6)
        self._btn(act, "►  DIREKT INSTALLIEREN (winget)", self._do_install,
                  COLORS["green"]).pack(side="left", padx=6)
        threading.Thread(target=self._load_catalog_bg, daemon=True).start()
        return p

    def _load_catalog_bg(self):
        try:
            apps, source = downloader.load_catalog()
        except Exception:
            apps, source = list(downloader.CATALOG), "eingebaut"
        self._ui_queue.put(("catalog", (apps, source)))

    def _render_catalog(self, apps, source):
        for w in self.software_holder.winfo_children():
            w.destroy()
        self.software_vars.clear()
        holder = self.software_holder
        holder.grid_columnconfigure((0, 1, 2), weight=1)
        cats: dict = {}
        for app in apps:
            cats.setdefault(app.category, []).append(app)
        palette = [COLORS["blue"], COLORS["cyan"], COLORS["violet"],
                   COLORS["teal"], COLORS["magenta"], COLORS["lime"]]
        for col, (cat, cat_apps) in enumerate(cats.items()):
            accent = palette[col % len(palette)]
            box = ctk.CTkFrame(holder, fg_color=COLORS["surface2"], corner_radius=10,
                               border_width=1, border_color=COLORS["border"])
            box.grid(row=col // 3, column=col % 3, sticky="nsew", padx=6, pady=6)
            ctk.CTkLabel(box, text=cat.upper(), font=(MONO_FONT, 12, "bold"),
                         text_color=accent).pack(anchor="w", padx=12, pady=(9, 3))
            for app in cat_apps:
                var = tk.BooleanVar(value=False)
                self.software_vars[app.name] = (app, var)
                self._checkbox(box, app.name, var, accent).pack(
                    anchor="w", padx=12, pady=3)
            ctk.CTkLabel(box, text="", height=4).pack()

        herkunft = {"online": "aus dem Repo aktualisiert",
                    "lokal": "aus apps.json geladen",
                    "eingebaut": "eingebaute Liste (kein Netz)"}.get(source, source)
        self.software_source.configure(text=f"{len(apps)} Programme - {herkunft}")

    def _build_uninstall(self):
        p = self._page("uninstall")
        top = self._card(p, "uninstall", "INSTALLIERTE PROGRAMME", "✕")
        r = ctk.CTkFrame(top, fg_color="transparent")
        r.pack(fill="x", padx=18, pady=(2, 8))
        self.uninstall_search = tk.StringVar()
        self.uninstall_search.trace_add("write", lambda *_a: self._filter_uninstall())
        ctk.CTkEntry(r, textvariable=self.uninstall_search, height=38,
                     placeholder_text="suchen ...", font=(MONO_FONT, 12),
                     fg_color=COLORS["surface2"], border_color=COLORS["border"],
                     text_color=COLORS["text"]).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(r, text="LADEN / AKTUALISIEREN", width=190, height=38,
                      font=_font(12, "bold"), fg_color="transparent", border_width=2,
                      border_color=COLORS["red"], text_color=COLORS["red"],
                      hover_color=COLORS["surface2"],
                      command=self._load_uninstall).pack(side="left", padx=(8, 0))

        self.deep_var = tk.BooleanVar(value=False)
        self._checkbox(top, "DEEP-UNINSTALL: auch Datei- und Registry-Reste entfernen "
                            "(mit Backup)", self.deep_var, COLORS["red"]).pack(
            anchor="w", padx=18, pady=(0, 6))
        self.uninstall_status = ctk.CTkLabel(top, text="Noch nichts geladen.",
                                             font=(MONO_FONT, 12),
                                             text_color=COLORS["muted"])
        self.uninstall_status.pack(anchor="w", padx=18, pady=(0, 6))
        self.uninstall_list = ctk.CTkScrollableFrame(
            top, fg_color=COLORS["surface2"], height=340, corner_radius=10,
            scrollbar_button_color=COLORS["surface3"],
            scrollbar_button_hover_color=COLORS["red"])
        self.uninstall_list.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        act = ctk.CTkFrame(p, fg_color="transparent")
        act.pack(fill="x", padx=6, pady=8)
        self._btn(act, "✕  AUSGEWAEHLTE DEINSTALLIEREN", self._do_uninstall,
                  COLORS["red"]).pack(side="left", padx=6)
        return p

    def _build_debloat(self):
        p = self._page("debloat")
        auto = self._card(p, "debloat", "AUTO-DEBLOAT (1 KLICK)", "☢")
        self._hint(auto, "Entfernt bekannte Windows-Bloatware (Xbox, Bing, Teams-"
                         "Consumer, Werbe-Apps ...). Vorher werden ein Wiederher"
                         "stellungspunkt und Registry-Backups angelegt.")
        self.edge_var = tk.BooleanVar(value=False)
        self.deep_debloat_var = tk.BooleanVar(value=False)
        self._checkbox(auto, "Microsoft Edge mitentfernen (kann fehlschlagen)",
                       self.edge_var, COLORS["orange"]).pack(anchor="w", padx=18, pady=3)
        self._checkbox(auto, "DEEP: auch Reste bereinigen", self.deep_debloat_var,
                       COLORS["orange"]).pack(anchor="w", padx=18, pady=3)
        self._btn(auto, "☢  JETZT AUTOMATISCH ENTBLOATEN", self._do_debloat_auto,
                  COLORS["orange"]).pack(anchor="w", padx=18, pady=(10, 18))

        man = self._card(p, "debloat", "MANUELL AUSWAEHLEN (APPX)", "☰")
        self._hint(man, "Nur die angehakten Pakete werden entfernt.")
        box = ctk.CTkFrame(man, fg_color="transparent")
        box.pack(fill="x", padx=14, pady=6)
        box.grid_columnconfigure((0, 1, 2), weight=1)
        for i, frag in enumerate(uninstaller.BLOAT_APPX_SAFE):
            var = tk.BooleanVar(value=False)
            self.bloat_vars[frag] = var
            self._checkbox(box, frag.split(".")[-1], var, COLORS["amber"]).grid(
                row=i // 3, column=i % 3, sticky="w", padx=8, pady=3)
        self._btn(man, "✕  AUSGEWAEHLTE APPX ENTFERNEN", self._do_debloat_manual,
                  COLORS["red"]).pack(anchor="w", padx=18, pady=(10, 18))
        return p

    CATEGORY_STYLE = {
        "Privatsphaere": (COLORS["violet"], "⛨"),
        "Explorer":      (COLORS["blue"], "▤"),
        "Taskleiste":    (COLORS["teal"], "▬"),
        "Tempo":         (COLORS["amber"], "⚡"),
        "Updates":       (COLORS["magenta"], "↻"),
        "Gaming":        (COLORS["green"], "◈"),
    }

    def _build_tweaks(self):
        p = self._page("tweaks")

        head = self._card(p, "tweaks", "SYSTEM-TWEAKS", "⚙")
        self._hint(head, "Jede Aenderung wird vorher als .reg gesichert und laesst sich "
                         "mit ZURUECKSETZEN rueckgaengig machen. Mit einem Punkt "
                         "markierte Eintraege sind die empfohlene Grundeinstellung.")
        bar = ctk.CTkFrame(head, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(0, 14))
        for text, fn, col in (
                ("EMPFOHLENE", lambda: self._select_tweaks("recommended"), COLORS["violet"]),
                ("ALLE", lambda: self._select_tweaks("all"), COLORS["surface3"]),
                ("KEINE", lambda: self._select_tweaks("none"), COLORS["surface3"])):
            ctk.CTkButton(bar, text=text, width=132, height=34, font=_font(12, "bold"),
                          fg_color="transparent", border_width=2, border_color=col,
                          text_color=col if col != COLORS["surface3"] else COLORS["muted"],
                          hover_color=COLORS["surface2"], command=fn).pack(side="left", padx=(0, 8))
        self.tweak_count = ctk.CTkLabel(bar, text="", font=(MONO_FONT, 12),
                                        text_color=COLORS["muted"])
        self.tweak_count.pack(side="left", padx=12)

        # Kategorien zweispaltig - bei ueber 40 Eintraegen bleibt die Seite
        # sonst endlos lang.
        cats: dict = {}
        for tw in tweaks.TWEAKS:
            cats.setdefault(tw.category, []).append(tw)
        grid = ctk.CTkFrame(p, fg_color="transparent")
        grid.pack(fill="both", expand=True, padx=6, pady=4)
        grid.grid_columnconfigure((0, 1), weight=1, uniform="tw")

        for i, (cat, tws) in enumerate(cats.items()):
            accent, icon = self.CATEGORY_STYLE.get(cat, (COLORS["violet"], "•"))
            box = NeonCard(grid, accent)
            box.grid(row=i // 2, column=i % 2, sticky="nsew", padx=6, pady=7)
            self._page_cards["tweaks"].append(box)

            title = ctk.CTkFrame(box, fg_color="transparent")
            title.pack(fill="x", padx=16, pady=(12, 6))
            ctk.CTkLabel(title, text=f"{icon}  {cat.upper()}",
                         font=(MONO_FONT, 14, "bold"), text_color=accent).pack(side="left")
            ctk.CTkLabel(title, text=f"{len(tws)}", font=(MONO_FONT, 12),
                         text_color=COLORS["dim"]).pack(side="right")

            for tw in tws:
                var = tk.BooleanVar(value=tw.recommended)
                var.trace_add("write", lambda *_a: self._update_tweak_count())
                self.tweak_vars[tw.key] = (tw, var)
                row = ctk.CTkFrame(box, fg_color="transparent")
                row.pack(fill="x", padx=14, pady=(3, 0))
                mark = "●" if tw.recommended else "○"
                ctk.CTkLabel(row, text=mark, width=16, font=(MONO_FONT, 11),
                             text_color=accent if tw.recommended else COLORS["dim"]
                             ).pack(side="left")
                self._checkbox(row, tw.name, var, accent).pack(side="left")
                ctk.CTkLabel(box, text=tw.description, font=_font(11),
                             text_color=COLORS["dim"], wraplength=340,
                             justify="left").pack(anchor="w", padx=(46, 14), pady=(0, 4))
            ctk.CTkLabel(box, text="", height=6).pack()

        act = ctk.CTkFrame(p, fg_color="transparent")
        act.pack(fill="x", padx=6, pady=(8, 4))
        self._btn(act, "►  ANWENDEN", lambda: self._do_tweaks(True),
                  COLORS["violet"]).pack(side="left", padx=6)
        self._ghost(act, "◄  ZURUECKSETZEN", lambda: self._do_tweaks(False),
                    COLORS["muted"]).pack(side="left", padx=6)
        self._update_tweak_count()
        return p

    def _select_tweaks(self, mode):
        for tw, var in self.tweak_vars.values():
            var.set(tw.recommended if mode == "recommended" else (mode == "all"))

    def _update_tweak_count(self):
        n = sum(1 for _tw, v in self.tweak_vars.values() if v.get())
        self.tweak_count.configure(text=f"{n} von {len(self.tweak_vars)} ausgewaehlt")

    def _build_cleaner(self):
        p = self._page("cleaner")
        head = self._card(p, "cleaner", "DATENMUELL ENTFERNEN", "≈")
        self._hint(head, "Die Groessen werden beim Oeffnen der Seite ermittelt. "
                         "Der Papierkorb ist bewusst NICHT vorausgewaehlt - dort "
                         "liegen oft noch gebrauchte Dateien.")
        sumrow = ctk.CTkFrame(head, fg_color="transparent")
        sumrow.pack(fill="x", padx=16, pady=(0, 14))
        self.clean_total = ctk.CTkLabel(sumrow, text="wird berechnet ...",
                                        font=(MONO_FONT, 20, "bold"),
                                        text_color=COLORS["teal"])
        self.clean_total.pack(side="left")
        ctk.CTkButton(sumrow, text="NEU BERECHNEN", width=150, height=34,
                      font=_font(12, "bold"), fg_color="transparent", border_width=2,
                      border_color=COLORS["teal"], text_color=COLORS["teal"],
                      hover_color=COLORS["surface2"],
                      command=self._do_clean_scan).pack(side="right")

        self.clean_size_labels = {}
        self.clean_detail_labels = {}
        self.clean_bars = {}
        for t in tweaks.clean_targets():
            var = tk.BooleanVar(value=(t.key != "recycle"))
            self.clean_vars[t.key] = (t, var)
            box = NeonCard(p, COLORS["teal"])
            box.pack(fill="x", padx=6, pady=6)
            self._page_cards["cleaner"].append(box)

            top = ctk.CTkFrame(box, fg_color="transparent")
            top.pack(fill="x", padx=16, pady=(12, 2))
            self._checkbox(top, t.name, var, COLORS["teal"]).pack(side="left")
            size_lbl = ctk.CTkLabel(top, text="...", font=(MONO_FONT, 15, "bold"),
                                    text_color=COLORS["teal"])
            size_lbl.pack(side="right")
            self.clean_size_labels[t.key] = size_lbl

            ctk.CTkLabel(box, text=t.description, font=_font(11),
                         text_color=COLORS["dim"], wraplength=self.WRAP,
                         justify="left").pack(anchor="w", padx=(42, 16))

            bar = ctk.CTkProgressBar(box, height=6, progress_color=COLORS["teal"],
                                     fg_color=COLORS["surface3"])
            bar.set(0)
            bar.pack(fill="x", padx=(42, 16), pady=(6, 2))
            self.clean_bars[t.key] = bar

            det = ctk.CTkLabel(box, text="", font=(MONO_FONT, 11),
                               text_color=COLORS["dim"], justify="left", anchor="w")
            det.pack(anchor="w", padx=(42, 16), pady=(2, 12))
            self.clean_detail_labels[t.key] = det

        act = ctk.CTkFrame(p, fg_color="transparent")
        act.pack(fill="x", padx=6, pady=8)
        self._btn(act, "≈  JETZT BEREINIGEN", self._do_clean,
                  COLORS["teal"]).pack(side="left", padx=6)
        return p

    @staticmethod
    def _human(n) -> str:
        """Bytes lesbar machen."""
        try:
            n = float(n)
        except (TypeError, ValueError):
            return "?"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024 or unit == "TB":
                return f"{n:,.1f} {unit}".replace(",", ".") if unit != "B" else f"{int(n)} B"
            n /= 1024.0
        return "?"

    def _build_tools(self):
        p = self._page("tools")
        head = self._card(p, "tools", "BEWAEHRTE WERKZEUGE", "⌘")
        self._hint(head, "Alles wird von den offiziellen Quellen geladen und in einem "
                         "eigenen, sichtbaren Fenster gestartet - nichts laeuft "
                         "versteckt. Portable Werkzeuge landen unter "
                         f"{local_dir() / 'tools'}.")
        for tool in tweaks.EXT_TOOLS:
            card = NeonCard(p, COLORS["amber"])
            card.pack(fill="x", padx=6, pady=7)
            self._page_cards["tools"].append(card)
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=16, pady=(13, 2))
            ctk.CTkLabel(top, text=tool.name, font=(MONO_FONT, 15, "bold"),
                         text_color=COLORS["amber"]).pack(side="left")
            kind = {"ps": "Skript", "download": "portabel", "zip": "portabel"}.get(tool.kind, "")
            ctk.CTkLabel(top, text=kind, font=(MONO_FONT, 11),
                         text_color=COLORS["dim"]).pack(side="right")
            ctk.CTkLabel(card, text=tool.description, font=_font(12),
                         text_color=COLORS["muted"], wraplength=self.WRAP,
                         justify="left").pack(anchor="w", padx=16, pady=(2, 4))
            if tool.note:
                ctk.CTkLabel(card, text="Hinweis: " + tool.note, font=_font(11),
                             text_color=COLORS["dim"], wraplength=self.WRAP,
                             justify="left").pack(anchor="w", padx=16, pady=(0, 4))
            self._btn(card, "►  STARTEN", lambda t=tool: self._do_ext_tool(t),
                      COLORS["amber"]).pack(anchor="w", padx=16, pady=(6, 14))
        return p

    def _do_ext_tool(self, tool):
        extra = f"\n\n{tool.note}" if tool.note else ""
        if not messagebox.askyesno(
                tool.name,
                f"{tool.description}{extra}\n\nQuelle:\n{tool.source}\n\n"
                f"Das Werkzeug wird geladen und in einem eigenen Fenster "
                f"gestartet. Fortfahren?"):
            return
        dest = local_dir() / "tools"
        self.run_task(lambda rep: tweaks.ExternalTools(rep).run(tool, dest), tool.name)

    def _build_log(self):
        p = self._page("log")
        card = self._card(p, "log", "LIVE-PROTOKOLL", "≡")
        self.log_text = tk.Text(card, height=26, bg=COLORS["bg2"], fg=COLORS["text"],
                                insertbackground=COLORS["magenta"], relief="flat",
                                font=(MONO_FONT, 11), padx=14, pady=12, wrap="word",
                                borderwidth=0, highlightthickness=0)
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(4, 8))
        for lvl, col in LEVEL_COLORS.items():
            self.log_text.tag_config(lvl, foreground=col)
        self.log_text.tag_config("fresh", foreground=COLORS["text"])
        self.log_text.configure(state="disabled")
        ctk.CTkButton(card, text="LOG LEEREN", width=130, height=32,
                      font=_font(12, "bold"), fg_color="transparent", border_width=2,
                      border_color=COLORS["magenta"], text_color=COLORS["magenta"],
                      hover_color=COLORS["surface2"], command=self._clear_log).pack(
            anchor="e", padx=14, pady=(0, 12))
        return p

    def _build_info(self):
        p = self._page("info")
        card = self._card(p, "info", "Hoferium", "?")
        txt = (
            "Version 1.0\n\n"
            "Werkzeug fuer den Wechsel und das Neuaufsetzen von Windows-PCs:\n"
            "  [+] Datensicherung (Browser, Windows-Key, WLAN, Programme, Treiber ...)\n"
            "  [+] Software von offiziellen Quellen holen (Download oder winget)\n"
            "  [+] Programme sauber deinstallieren - inklusive Reste\n"
            "  [+] Windows entbloaten und Tweaks setzen (umkehrbar)\n"
            "  [+] Datenmuell entfernen\n"
            "  [+] bewaehrte externe Tools starten\n\n"
            "Destruktive Aktionen legen vorher Backups (.reg) und - wenn moeglich -\n"
            "einen Wiederherstellungspunkt an. Backups liegen unter:\n"
            f"   {local_dir() / 'backups'}\n\n"
            "Passwoerter werden NICHT geknackt. Browser-Passwoerter werden als Teil\n"
            "des Profils gesichert; fuer Chrome/Edge zusaetzlich den manuellen CSV-\n"
            "Export nutzen (siehe Hinweis im Sicherungsordner)."
        )
        ctk.CTkLabel(card, text=txt, font=(MONO_FONT, 12), text_color=COLORS["text"],
                     justify="left", wraplength=self.WRAP).pack(anchor="w", padx=18, pady=14)
        return p

    # ==============================================================
    #  Aktionen
    # ==============================================================
    def _pick_backup_dir(self):
        d = filedialog.askdirectory(initialdir=str(backup_root().parent))
        if d:
            self.backup_target.set(d)

    def _do_backup(self):
        # Wurde der UAC-Dialog mit einem anderen Konto bestaetigt, zeigen
        # %USERPROFILE%/%APPDATA% auf DESSEN Profil - die Sicherung waere leer.
        caller = os.environ.get("HOFERIUM_CALLER", "")
        current = os.environ.get("USERNAME", "")
        if caller and current and caller.lower() != current.lower():
            if not messagebox.askyesno(
                    "Anderes Benutzerkonto",
                    f"Das Programm laeuft als '{current}', angemeldet ist aber "
                    f"'{caller}'.\n\nGesichert wuerde das Profil von '{current}' - "
                    f"also NICHT die Daten von '{caller}'.\n\n"
                    f"Empfehlung: abbrechen und hoferium.bat direkt als '{caller}' "
                    f"mit Administrator-Rechten starten.\n\nTrotzdem fortfahren?"):
                return
        opt = backup_mod.BackupOptions(**{k: v.get() for k, v in self.backup_opts.items()})
        root = Path(self.backup_target.get() or str(backup_root()))
        outdir = root / config.backup_folder_name()

        def worker(rep):
            outdir.mkdir(parents=True, exist_ok=True)
            ctx = RunContext(outdir, rep, admin=self.admin)
            result = backup_mod.BackupJob(ctx).run(opt)
            if result.get("ok"):
                self._backup_done = True

        self.run_task(worker, "Sicherung laeuft")

    def _do_download(self):
        apps = [app for (app, v) in self.software_vars.values() if v.get()]
        if not apps:
            return self._need_selection()
        dest = stick_dir() / "Installer"
        self.run_task(lambda rep: downloader.DownloadManager(rep).download_all(apps, dest),
                      "Lade Installer")

    def _do_install(self):
        apps = [app for (app, v) in self.software_vars.values() if v.get()]
        if not apps:
            return self._need_selection()
        if not downloader.DownloadManager.winget_available():
            return messagebox.showwarning(
                "winget fehlt",
                "winget ist nicht verfuegbar. Bitte 'Installer speichern' nutzen.")
        self.run_task(lambda rep: downloader.DownloadManager(rep).install_all(apps),
                      "Installiere per winget")

    def _load_uninstall(self):
        self.uninstall_status.configure(text="Lade Programme ...")
        for w in self.uninstall_list.winfo_children():
            w.destroy()
        self.uninstall_rows.clear()

        def worker():
            self._ui_queue.put(("apps", list_installed(include_appx=True)))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_uninstall(self, apps):
        self._all_uninstall_apps = apps
        self.uninstall_status.configure(text=f"{len(apps)} Programme geladen.")
        self._filter_uninstall()

    def _filter_uninstall(self):
        if not hasattr(self, "_all_uninstall_apps"):
            return
        term = self.uninstall_search.get().lower().strip()
        for w in self.uninstall_list.winfo_children():
            w.destroy()
        self.uninstall_rows.clear()
        items = [a for a in self._all_uninstall_apps if not term or term in a.name.lower()]
        # Jeder Tastendruck startet einen neuen Renderlauf. Ohne dieses Token
        # wuerde die noch laufende Kette der VORHERIGEN Suche weiter Zeilen
        # nachliefern - die Liste zeigte dann Treffer beider Suchbegriffe.
        self._render_token += 1
        self._render_uninstall_chunk(items, 0, self._render_token)

    def _render_uninstall_chunk(self, items, idx, token):
        if token != self._render_token:
            return
        end = min(idx + 30, len(items))
        for app in items[idx:end]:
            var = tk.BooleanVar(value=False)
            self.uninstall_rows[app.key] = (app, var)
            row = ctk.CTkFrame(self.uninstall_list, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=1)
            tag = "APPX" if app.kind == "appx" else "WIN32"
            color = COLORS["violet"] if app.kind == "appx" else COLORS["dim"]
            self._checkbox(row, app.name[:58], var, COLORS["red"]).pack(side="left", padx=4)
            ctk.CTkLabel(row, text=f"[{tag}]", font=(MONO_FONT, 10),
                         text_color=color).pack(side="left", padx=4)
            meta = f"{app.version}  {app.publisher}".strip()
            if meta:
                ctk.CTkLabel(row, text=meta[:60], font=(MONO_FONT, 10),
                             text_color=COLORS["dim"]).pack(side="left")
        if end < len(items):
            self.after(1, lambda: self._render_uninstall_chunk(items, end, token))

    def _do_uninstall(self):
        sel = [app for (app, v) in self.uninstall_rows.values() if v.get()]
        if not sel:
            return self._need_selection()
        names = "\n".join(f"  - {a.name}" for a in sel[:15])
        more = f"\n  ... und {len(sel) - 15} weitere" if len(sel) > 15 else ""
        deep = self.deep_var.get()
        if not messagebox.askyesno(
                "Deinstallieren bestaetigen",
                f"{len(sel)} Programm(e) werden entfernt"
                f"{' (inkl. Reste)' if deep else ''}:\n\n{names}{more}\n\nFortfahren?"):
            return
        backup_dir = local_dir() / "backups" / config.timestamp()
        self.run_task(
            lambda rep: uninstaller.Uninstaller(rep, backup_dir).uninstall_many(sel, deep=deep),
            "Deinstalliere")

    def _do_debloat_auto(self):
        extra = "\n  - Microsoft Edge (Versuch)" if self.edge_var.get() else ""
        if not messagebox.askyesno(
                "Auto-Debloat",
                "Automatisch entfernt werden:\n"
                "  - Windows-Apps wie Xbox, Bing, Solitaire, Karten, Teams-Consumer\n"
                "  - vorinstallierter Herstellerkram (HP/Lenovo/Dell/Acer, Testversionen)"
                + extra + "\n\n"
                "NICHT angetastet werden selbst installierte Programme.\n"
                "Die genaue Liste erscheint vor dem Entfernen im Log.\n\n"
                "Vorher werden - soweit moeglich - ein Wiederherstellungspunkt und\n"
                "Registry-Backups angelegt. Fortfahren?"):
            return
        deep = self.deep_debloat_var.get()
        edge = self.edge_var.get()
        backup_dir = local_dir() / "backups" / config.timestamp()
        self.run_task(
            lambda rep: uninstaller.Uninstaller(rep, backup_dir).debloat_auto(
                deep=deep, remove_edge=edge),
            "Auto-Debloat")

    def _do_debloat_manual(self):
        frags = [f for f, v in self.bloat_vars.items() if v.get()]
        if not frags:
            return self._need_selection()
        backup_dir = local_dir() / "backups" / config.timestamp()

        def worker(rep):
            un = uninstaller.Uninstaller(rep, backup_dir)
            apps = uninstaller.mark_bloatware(list_installed(include_appx=True))
            targets = [a for a in apps
                       if a.kind == "appx" and any(f.lower() in a.name.lower() for f in frags)]
            if not targets:
                rep.warn("Keine passenden AppX-Pakete installiert.")
                rep.done({"ok": 0, "fail": 0})
                return
            un.uninstall_many(targets, deep=False)

        self.run_task(worker, "Entferne AppX")

    def _do_tweaks(self, enable):
        sel = [tw for (tw, v) in self.tweak_vars.values() if v.get()]
        if not sel:
            return self._need_selection()
        backup_dir = local_dir() / "backups" / config.timestamp()
        self.run_task(
            lambda rep: tweaks.TweakEngine(rep, backup_dir).apply(sel, enable=enable),
            "Tweaks anwenden" if enable else "Tweaks zuruecksetzen")

    def _do_clean_scan(self):
        if self._clean_scanning:
            return
        self._clean_scanning = True
        targets = [t for (t, _v) in self.clean_vars.values()]
        self.clean_total.configure(text="wird berechnet ...")
        for lbl in self.clean_size_labels.values():
            lbl.configure(text="...")

        def worker():
            try:
                data = tweaks.CleanupEngine(Reporter()).scan(targets)
            except Exception as e:
                data = {"__error__": str(e)}
            self._ui_queue.put(("clean_sizes", data))

        threading.Thread(target=worker, daemon=True).start()
        self._set_status("Berechne Groessen ...", COLORS["teal"])

    def _show_clean_sizes(self, data):
        self._clean_scanning = False
        self._last_cancel = 0.0
        if "__error__" in data:
            self.clean_total.configure(text="Berechnung fehlgeschlagen")
            self._set_status(f"Cleaner: {data['__error__']}", COLORS["amber"])
            return
        total = sum(d.get("size", 0) for d in data.values())
        biggest = max((d.get("size", 0) for d in data.values()), default=0) or 1
        for key, lbl in self.clean_size_labels.items():
            info = data.get(key) or {}
            size = info.get("size", 0)
            lbl.configure(text=self._human(size),
                          text_color=COLORS["teal"] if size else COLORS["dim"])
            bar = self.clean_bars.get(key)
            if bar is not None:
                bar.set(min(1.0, size / float(biggest)))
            det = self.clean_detail_labels.get(key)
            if det is not None:
                items = info.get("items") or []
                files = info.get("files", 0)
                if items:
                    lines = [f"   {name[:34]:<34} {self._human(sz):>10}"
                             for name, sz in items[:6]]
                    head = f"{files} Dateien - groesste Posten:"
                    det.configure(text=head + "\n" + "\n".join(lines))
                else:
                    det.configure(text="   nichts zu bereinigen")
        self.clean_total.configure(text=f"Gesamt frei machbar: {self._human(total)}")
        self._set_status(f"Cleaner: {self._human(total)} koennen frei werden.",
                         COLORS["teal"])

    def _do_clean(self):
        targets = [t for (t, v) in self.clean_vars.values() if v.get()]
        if not targets:
            return self._need_selection()
        namen = "\n".join(f"  - {t.name}" for t in targets)
        if not messagebox.askyesno(
                "Bereinigen bestaetigen",
                f"Folgendes wird unwiderruflich geloescht:\n\n{namen}\n\nFortfahren?"):
            return
        self.run_task(lambda rep: tweaks.CleanupEngine(rep).clean(targets), "Bereinige")

    def _need_selection(self):
        messagebox.showinfo("Hoferium", "Bitte zuerst etwas auswaehlen.")

    # ==============================================================
    #  Task-Runner
    # ==============================================================
    def run_task(self, worker, name="Aufgabe"):
        if self._running:
            messagebox.showinfo("Hoferium", "Es laeuft bereits eine Aufgabe.")
            return
        self._running = True
        self.equalizer.active = True
        self.reporter = Reporter(logfile=local_dir() / "hoferium.log")
        self.progress.set(0)
        self._last_cancel = 0.0
        self.cancel_btn.configure(state="normal", text="ABBRECHEN",
                                  text_color=COLORS["red"], border_width=2,
                                  border_color=COLORS["red"])
        self._set_status(name + " ...", COLORS["cyan"])
        self._log_line(f"=== {name} ===", "head")
        threading.Thread(target=self._thread_wrap, args=(worker,), daemon=True).start()
        self.after(80, self._poll)

    def _thread_wrap(self, worker):
        # done() MUSS in jedem Fall gesendet werden - sonst pollt die UI
        # endlos weiter und die Anwendung nimmt keine neue Aufgabe mehr an.
        rep = self.reporter
        try:
            worker(rep)
        except Exception as e:
            rep.err(f"Unerwarteter Fehler: {e}")
            rep.done({"error": str(e)})
        finally:
            rep.done()      # idempotent - greift nur, wenn der Worker selbst nichts meldete

    def _on_close(self):
        """Fenster schliessen: erst Animation und laufende Aufgabe stoppen,
        damit keine after()-Rueckrufe auf zerstoerte Widgets treffen."""
        self._closing = True
        try:
            self.anim.stop()
        except Exception:
            pass
        if self.reporter is not None:
            self.reporter.cancel()
        self._running = False
        self.destroy()

    def _poll(self):
        r = self.reporter
        if r is None or self._closing:
            return
        try:
            while True:
                kind, a, b = r.q.get_nowait()
                if kind == "log":
                    self._log_line(b, a)
                elif kind == "progress":
                    self.progress.set(a)
                    if b:
                        self._set_status(b, COLORS["cyan"])
                elif kind == "status":
                    self._set_status(a, COLORS["cyan"])
                elif kind == "done":
                    self._on_done(a)
                    return
        except queue.Empty:
            pass
        if self._running:
            self.after(80, self._poll)

    def _on_done(self, summary):
        self._running = False
        self.equalizer.active = False
        self.progress.set(1)
        self.progress.configure(progress_color=COLORS["green"])
        self.cancel_btn.configure(state="disabled", text_color=COLORS["muted"],
                                  border_width=0)
        msg = "Fertig."
        good = True
        if isinstance(summary, dict):
            if "error" in summary:
                msg = "Mit Fehler beendet: " + str(summary["error"])
                good = False
            elif "ok" in summary:
                fails = summary.get("fail", 0)
                msg = f"Fertig - {summary.get('ok', 0)} ok, {fails} Fehler."
                good = not fails
            elif "freed_mb" in summary:
                msg = f"Fertig - {summary['freed_mb']} MB frei."
        self._set_status(msg, COLORS["green"] if good else COLORS["amber"])
        self._log_line(msg, "ok" if good else "warn")
        if isinstance(summary, dict) and summary.get("dir"):
            self._log_line(f"Ordner: {summary['dir']}", "info")

    def _cancel(self):
        """Einmal klicken = sauber abbrechen (nach dem laufenden Schritt).
        Nochmal klicken = sofort abbrechen und laufende Programme beenden."""
        now = time.monotonic()
        if self._last_cancel and (now - self._last_cancel) < 1.8:
            self._force_cancel()
            return
        self._last_cancel = now
        if self.reporter:
            self.reporter.cancel()
        self._set_status("Abbruch angefordert - nochmal klicken fuer Sofort-Stopp",
                         COLORS["amber"])
        self.cancel_btn.configure(text="SOFORT STOPPEN",
                                  fg_color=COLORS["red"], text_color="#08101c")

    def _force_cancel(self):
        """Not-Aus: beendet auch laufende Installer/Deinstaller sofort."""
        self._last_cancel = 0.0
        if self.reporter:
            self.reporter.cancel()
        killed = winutils.kill_all()
        self._log_line(f"Sofort-Abbruch: {killed} laufende(s) Programm(e) beendet.",
                       "warn")
        self._set_status(f"Sofort abgebrochen ({killed} Programme beendet).",
                         COLORS["red"])
        self.cancel_btn.configure(text="ABBRECHEN", fg_color=COLORS["surface3"],
                                  text_color=COLORS["muted"])
        # Bleibt der Worker trotzdem haengen, wird die Oberflaeche nach kurzer
        # Zeit freigegeben - sonst nimmt sie keine neue Aufgabe mehr an.
        self.after(4000, self._release_if_stuck)

    def _release_if_stuck(self):
        if self._running:
            self._log_line("Aufgabe reagierte nicht mehr - Oberflaeche freigegeben.",
                           "warn")
            self._on_done({"ok": 0, "fail": 1})

    def _log_line(self, text, level="info"):
        mark = LEVEL_MARKS.get(level, "  ·")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{mark} {text}\n",
                             level if level in LEVEL_COLORS else "info")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
