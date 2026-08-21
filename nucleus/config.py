"""Zentrale Konstanten: Pfade, Theme-Farben, Paletten, ASCII-Art."""
from __future__ import annotations

import colorsys
import os
from datetime import datetime
from pathlib import Path

APP_TITLE = "Hoferium"
APP_SUBTITLE = "RIP · BACKUP · DEBLOAT · REBUILD"

TAGLINES = [
    "Daten sichern, bevor der PC platt gemacht wird.",
    "Browser, Keys, WLAN - alles auf den Stick.",
    "Bloatware raus. Tempo rein.",
    "Ein Klick statt zwei Stunden Handarbeit.",
    "Deep-Uninstall: keine Reste, kein Muell.",
    "Backup zuerst. Immer.",
]


def asset(name: str) -> Path:
    """Pfad zu einer mitgelieferten Datei (Icon o. ae.)."""
    return Path(__file__).resolve().parent / "assets" / name


def stick_dir() -> Path:
    """Ordner, in dem hoferium.bat liegt (i.d.R. der USB-Stick)."""
    p = os.environ.get("HOFERIUM_HOME")
    if p and Path(p).exists():
        return Path(p)
    return Path.cwd()


def local_dir() -> Path:
    """Maschinenlokaler Arbeitsordner (Logs etc.), nicht auf dem Stick."""
    d = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Hoferium"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M")


def computer_name() -> str:
    return os.environ.get("COMPUTERNAME", "PC")


def backup_folder_name() -> str:
    return f"Sicherung_{computer_name()}_{timestamp()}"


# ==================================================================
#  Farben - dunkles Neon/Cyberpunk-Theme
# ==================================================================
COLORS = {
    "bg":        "#080b12",
    "bg2":       "#0c111b",
    "sidebar":   "#0a0f18",
    "surface":   "#121a28",
    "surface2":  "#1a2436",
    "surface3":  "#243049",
    "border":    "#2b3a55",
    "text":      "#e8f0fb",
    "muted":     "#7d8ca6",
    "dim":       "#4a5a75",

    # Neon-Akzente
    "cyan":      "#22d3ee",
    "blue":      "#60a5fa",
    "violet":    "#a78bfa",
    "magenta":   "#f472b6",
    "red":       "#fb7185",
    "orange":    "#fb923c",
    "amber":     "#fbbf24",
    "lime":      "#a3e635",
    "green":     "#34d399",
    "teal":      "#2dd4bf",

    # Zustands-Aliase (von den Modulen genutzt)
    "accent":       "#22d3ee",
    "accent_hover": "#0891b2",
    "green_hover":  "#059669",
    "red_hover":    "#e11d48",
}

# Signaturfarbe je Seite
PAGE_COLORS = {
    "dashboard": COLORS["cyan"],
    "backup":    COLORS["green"],
    "software":  COLORS["blue"],
    "uninstall": COLORS["red"],
    "debloat":   COLORS["orange"],
    "tweaks":    COLORS["violet"],
    "cleaner":   COLORS["teal"],
    "tools":     COLORS["amber"],
    "log":       COLORS["magenta"],
    "info":      COLORS["muted"],
}

LEVEL_COLORS = {
    "info": COLORS["muted"],
    "ok":   COLORS["green"],
    "warn": COLORS["amber"],
    "err":  COLORS["red"],
    "head": COLORS["cyan"],
}

LEVEL_MARKS = {
    "info": "  ·",
    "ok":   "  +",
    "warn": "  !",
    "err":  "  x",
    "head": ">>>",
}


def mix(c1: str, c2: str, t: float) -> str:
    """Blendet zwei Hex-Farben (t = 0..1)."""
    t = max(0.0, min(1.0, t))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def color_ramp(stops: list, n: int, hold: float = 0.55) -> list:
    """Zyklische Rampe durch mehrere Farbstopps.

    `hold` gibt an, welcher Anteil jedes Abschnitts die reine Farbe behaelt,
    bevor in die naechste uebergeblendet wird. Ohne dieses Plateau verwaschen
    Schwarz, Rot und Gold zu einem unklaren Verlauf.
    """
    out = []
    count = len(stops)
    for i in range(n):
        pos = (i / float(n)) * count
        idx = int(pos) % count
        frac = pos - int(pos)
        if frac <= hold:
            out.append(stops[idx])
        else:
            t = (frac - hold) / (1.0 - hold)
            out.append(mix(stops[idx], stops[(idx + 1) % count], t))
    return out


# Schwarz-Rot-Gold. Reines Schwarz waere auf dem dunklen Hintergrund
# unsichtbar, deshalb ein sehr dunkles Anthrazit als "schwarzer" Anteil.
FLAG_STOPS = ["#1e1e26", "#d40000", "#ffce00"]
# Aufgehellte Variante fuers Logo: der dunkle Abschnitt muss sich noch klar
# vom Hintergrund abheben, sonst wirken die Buchstaben ausgestanzt.
FLAG_STOPS_BRIGHT = ["#4e4e5c", "#e01019", "#ffd21e"]

FLAG = color_ramp(FLAG_STOPS, 48)                 # Linien, Equalizer, Fortschritt
FLAG_BRIGHT = color_ramp(FLAG_STOPS_BRIGHT, 48)   # Bannerwelle


# ==================================================================
#  ASCII-Banner
# ==================================================================
BANNER = r"""
 ██╗  ██╗ ██████╗ ███████╗███████╗██████╗ ██╗██╗   ██╗███╗   ███╗
 ██║  ██║██╔═══██╗██╔════╝██╔════╝██╔══██╗██║██║   ██║████╗ ████║
 ███████║██║   ██║█████╗  █████╗  ██████╔╝██║██║   ██║██╔████╔██║
 ██╔══██║██║   ██║██╔══╝  ██╔══╝  ██╔══██╗██║██║   ██║██║╚██╔╝██║
 ██║  ██║╚██████╔╝██║     ███████╗██║  ██║██║╚██████╔╝██║ ╚═╝ ██║
 ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═╝╚═╝ ╚═════╝ ╚═╝     ╚═╝
"""


def banner_lines() -> list:
    """Banner als Zeilenliste, alle auf gleiche Breite aufgefuellt."""
    lines = [ln for ln in BANNER.split("\n") if ln.strip()]
    width = max((len(ln) for ln in lines), default=0)
    return [ln.ljust(width) for ln in lines]


# Icons/Bullets der Navigation (reines ASCII/Unicode-Symbol, keine Grafik)
NAV_ICONS = {
    "dashboard": "◈",
    "backup":    "▼",
    "software":  "↧",
    "uninstall": "✕",
    "debloat":   "☢",
    "tweaks":    "⚙",
    "cleaner":   "≈",
    "tools":     "⌘",
    "log":       "≡",
    "info":      "?",
}
