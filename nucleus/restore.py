"""Zurueckholen einer zuvor angelegten Sicherung.

Findet Sicherungsordner (`Sicherung_<PC>_<Datum>`), erkennt anhand des
Rechnernamens die zu diesem Gerät passende und spielt auf Knopfdruck alles
zurueck, was sich maschinell wiederherstellen laesst.

Vor jedem Ueberschreiben wird der aktuelle Zustand weggesichert - ein Import
laesst sich dadurch rueckgaengig machen.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import BACKUP_ROOT_NAME
from .context import RunContext
from .winutils import (IS_WINDOWS, copy_path, is_admin, known_folders,
                       powershell, robocopy, run)

# Die Sekunden sind optional: Sicherungen aus Fassungen vor 1.8.0 haben sie
# nicht und muessen weiterhin gefunden werden.
FOLDER_RE = re.compile(
    r"^Sicherung_(?P<pc>.+)_(?P<stamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}(?:-\d{2})?)$")

# Unterordner der Sicherung -> was daraus wiederhergestellt werden kann
PARTS = {
    "wifi":        ("03_WLAN", "WLAN-Netze samt Passwoertern"),
    "firefox":     ("05_Browser", "Firefox-Profil (Passwoerter, Lesezeichen, Verlauf)"),
    "bookmarks":   ("05_Browser", "Lesezeichen der uebrigen Browser"),
    "sticky":      ("09_Sticky-Notes", "Kurznotizen"),
    "thunderbird": ("10_EMail", "Thunderbird-Profil"),
    "outlook":     ("10_EMail", "Outlook-Datendateien (.pst)"),
    "programs":    ("04_Programme", "Programme ueber winget nachinstallieren"),
    "userfiles":   ("06_Eigene-Dateien", "Eigene Dateien zurueckkopieren"),
    "drivers":     ("07_Treiber", "Treiber einspielen"),
    "hosts":       ("08_Sonstiges", "hosts-Datei"),
}

# Schritt -> Programm, das dafuer geschlossen sein muss (Schreibweise wie in
# _running_programs). Ein laufendes Programm haelt sein Profil offen und
# schreibt seinen eigenen Stand beim Beenden zurueck - der eingespielte Stand
# waere danach still wieder weg, der Schritt haette Erfolg gemeldet.
BLOCKERS = {
    "Firefox-Profil":     "Firefox",
    "Thunderbird-Profil": "Thunderbird",
    "Outlook-Dateien":    "Outlook",
}

# Vom Sicherungsauftrag geschriebenes Protokoll je Sicherungsordner.
MANIFEST_NAME = "sicherung.json"


@dataclass
class BackupSet:
    """Eine gefundene Sicherung."""
    path: Path
    computer: str = ""
    stamp: str = ""
    available: dict = field(default_factory=dict)   # key -> bool
    hinweise: dict = field(default_factory=dict)    # key -> Warntext aus der Sicherung

    @property
    def is_this_pc(self) -> bool:
        return self.computer.lower() == os.environ.get("COMPUTERNAME", "").lower()

    @property
    def when(self) -> str:
        for muster in ("%Y-%m-%d_%H-%M-%S", "%Y-%m-%d_%H-%M"):
            try:
                return datetime.strptime(self.stamp, muster).strftime("%d.%m.%Y %H:%M")
            except ValueError:
                continue
        return self.stamp

    @property
    def size_mb(self) -> float:
        total = 0
        for root, _dirs, files in os.walk(self.path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return round(total / 1048576, 1)


def find_backups(root) -> list:
    """Sucht Sicherungsordner.

    Zuerst im Sammelordner (dort legt Hoferium sie ab), danach direkt neben
    dem Programm und eine Ebene darunter - damit auch aeltere Sicherungen
    gefunden werden, die noch lose herumliegen.

    Sortierung: Sicherungen dieses Rechners zuerst, darin die neueste oben.
    """
    root = Path(root)
    if not root.is_dir():
        return []

    bases = []
    sammel = root / BACKUP_ROOT_NAME
    if sammel.is_dir():
        bases.append(sammel)
    bases.append(root)
    try:
        bases += [p for p in root.iterdir() if p.is_dir() and p != sammel]
    except (OSError, PermissionError):
        pass

    found, seen = [], set()
    for base in bases:
        try:
            entries = list(base.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if not entry.is_dir() or entry in seen:
                continue
            m = FOLDER_RE.match(entry.name)
            if not m:
                continue
            seen.add(entry)
            bset = BackupSet(entry, m.group("pc"), m.group("stamp"))
            bset.available, bset.hinweise = _detect_parts(entry)
            found.append(bset)

    # stabil sortieren: erst nach Datum (neueste zuerst), dann eigener PC nach oben
    found.sort(key=lambda b: b.stamp, reverse=True)
    found.sort(key=lambda b: not b.is_this_pc)
    return found


def _detect_parts(folder: Path) -> tuple:
    """Prueft, welche Bestandteile die Sicherung wirklich enthaelt.

    Liefert (vorhanden, hinweise). Die Hinweise stammen aus `sicherung.json`;
    fehlt die Datei - aeltere Sicherungen haben sie nicht -, bleibt der
    Hinweis-Teil leer und es zaehlt allein, was auf dem Datentraeger liegt.
    """
    b = folder
    vorhanden = {
        "wifi": any((b / "03_WLAN" / "profile_xml").glob("*.xml"))
        if (b / "03_WLAN" / "profile_xml").is_dir() else False,
        "firefox": (b / "05_Browser" / "Firefox").is_dir(),
        "bookmarks": any((b / "05_Browser" / "_Lesezeichen_HTML").glob("*.html"))
        if (b / "05_Browser" / "_Lesezeichen_HTML").is_dir() else False,
        "sticky": (b / "09_Sticky-Notes").is_dir() and any((b / "09_Sticky-Notes").iterdir()),
        "thunderbird": (b / "10_EMail" / "Thunderbird").is_dir(),
        "outlook": (b / "10_EMail" / "Outlook_PST").is_dir()
        and any((b / "10_EMail" / "Outlook_PST").glob("*.pst")),
        "programs": (b / "04_Programme" / "winget-programme.json").is_file(),
        "userfiles": (b / "06_Eigene-Dateien").is_dir()
        and any(p.is_dir() for p in (b / "06_Eigene-Dateien").iterdir()),
        "drivers": (b / "07_Treiber").is_dir() and any((b / "07_Treiber").iterdir()),
        "hosts": (b / "08_Sonstiges" / "hosts").is_file(),
    }
    return vorhanden, _read_manifest_notes(folder)


def _read_manifest_notes(folder: Path) -> dict:
    """Liest aus `sicherung.json`, welche Schritte beim Sichern nicht sauber
    durchliefen, und ordnet das ueber den in PARTS hinterlegten Ordnernamen
    den Bestandteilen zu.

    Die Datei stammt vom Rechner des Anwenders und darf deshalb alles
    enthalten - jeder Zugriff bleibt tolerant, ein kaputtes Protokoll darf die
    Sicherung nicht unbrauchbar machen.
    """
    datei = folder / MANIFEST_NAME
    if not datei.is_file():
        return {}
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(daten, dict):
        return {}

    hinweise: dict = {}
    for schritt in daten.get("schritte") or []:
        if not isinstance(schritt, dict):
            continue
        zustand = str(schritt.get("zustand", "")).lower()
        if zustand not in ("fehler", "teilweise"):
            continue
        name = str(schritt.get("name", "")).strip()
        detail = "; ".join(str(h) for h in (schritt.get("hinweise") or [])
                           if str(h).strip())
        was = f"\"{name}\" " if name else ""
        text = (f"{was}ist beim Sichern fehlgeschlagen" if zustand == "fehler"
                else f"{was}ist beim Sichern nur teilweise gelungen")
        if detail:
            text += f": {detail}"
        # Zugeordnet wird ueber den Ordner, den der Sicherungsauftrag zu jedem
        # Schritt mitschreibt. Ein Vergleich der Schrittnamen ginge nicht: sie
        # heissen "Browser-Daten", der Ordner heisst "05_Browser".
        ziel = str(schritt.get("ordner", ""))
        if not ziel:
            continue
        for key, (ordner, _label) in PARTS.items():
            if ordner.lower() == ziel.lower():
                hinweise[key] = (hinweise[key] + " / " + text
                                 if key in hinweise else text)
    return hinweise


def _env_dir(var: str, *parts) -> Path:
    """Zielpfad aus einer Umgebungsvariablen bauen.

    Fehlt die Variable, entstuende sonst ein RELATIVER Pfad - dann landete
    die Wiederherstellung im Arbeitsverzeichnis (also auf dem Stick) statt
    im Benutzerprofil. Lieber sauber abbrechen.
    """
    base = os.environ.get(var, "")
    if not base or not os.path.isabs(base):
        raise RuntimeError(f"%{var}% ist nicht gesetzt - Ziel nicht bestimmbar")
    return Path(base).joinpath(*parts)


@dataclass
class RestoreOptions:
    wifi: bool = True
    firefox: bool = True
    bookmarks: bool = True
    sticky: bool = True
    thunderbird: bool = True
    outlook: bool = True
    programs: bool = False       # laedt aus dem Netz - bewusst abgewaehlt
    userfiles: bool = False      # kann sehr gross sein
    drivers: bool = False        # nur noetig, wenn Geraete fehlen
    hosts: bool = False


class RestoreJob:
    """Spielt eine Sicherung zurueck."""

    def __init__(self, ctx: RunContext, bset: BackupSet):
        self.ctx = ctx
        self.r = ctx.reporter
        self.b = bset
        self.src = bset.path
        # Sicherheitskopie des JETZIGEN Zustands - der Import bleibt umkehrbar
        self.safety = ctx.output_dir / f"vor_import_{datetime.now():%Y-%m-%d_%H-%M}"
        self.results = []

    def run(self, opt: RestoreOptions) -> dict:
        steps = []
        if opt.wifi and self.b.available.get("wifi"):
            steps.append(("WLAN-Netze", self._wifi))
        if opt.firefox and self.b.available.get("firefox"):
            steps.append(("Firefox-Profil", self._firefox))
        if opt.thunderbird and self.b.available.get("thunderbird"):
            steps.append(("Thunderbird-Profil", self._thunderbird))
        if opt.sticky and self.b.available.get("sticky"):
            steps.append(("Kurznotizen", self._sticky))
        if opt.outlook and self.b.available.get("outlook"):
            steps.append(("Outlook-Dateien", self._outlook))
        if opt.hosts and self.b.available.get("hosts"):
            steps.append(("hosts-Datei", self._hosts))
        if opt.userfiles and self.b.available.get("userfiles"):
            steps.append(("Eigene Dateien", self._userfiles))
        if opt.programs and self.b.available.get("programs"):
            steps.append(("Programme (winget)", self._programs))
        if opt.drivers and self.b.available.get("drivers"):
            steps.append(("Treiber", self._drivers))
        if opt.bookmarks and self.b.available.get("bookmarks"):
            steps.append(("Lesezeichen", self._bookmarks))

        if not steps:
            self.r.warn("Nichts ausgewaehlt, was in dieser Sicherung vorhanden waere.")
            self.r.done({"ok": 0, "fail": 0})
            return {"ok": 0, "fail": 0}

        self.r.head(f"Stelle wieder her aus: {self.src.name}")
        if not self.b.is_this_pc:
            self.r.warn(f"Die Sicherung stammt von '{self.b.computer}', dieser "
                        f"Rechner heisst '{os.environ.get('COMPUTERNAME', '?')}'.")

        blocked = self._running_programs()
        if blocked:
            self.r.warn("Diese Programme laufen noch: " + ", ".join(blocked) +
                        ". Die zugehoerigen Schritte werden uebersprungen.")

        total = len(steps)
        for i, (name, fn) in enumerate(steps):
            if self.r.cancelled:
                self.r.warn("Abgebrochen.")
                break
            stoerer = BLOCKERS.get(name)
            if stoerer and stoerer in blocked:
                # Lieber gar nicht einspielen als scheinbar: das Programm
                # ueberschriebe den zurueckgeholten Stand beim Beenden wieder.
                self.results.append((name, False, f"{stoerer} laeuft noch"))
                self.r.warn(f"{name}: uebersprungen, weil {stoerer} noch laeuft "
                            f"- sonst waere der zurueckgeholte Stand nach dem "
                            f"naechsten Schliessen von {stoerer} wieder weg. "
                            f"Bitte {stoerer} beenden und die Wiederherstellung "
                            f"erneut starten.")
                self.r.progress((i + 1) / total)
                continue
            self.r.status(f"{name} ...")
            self.r.log(f"--- {name} ---", "head")
            try:
                fn()
                self.results.append((name, True, ""))
                self.r.ok(f"{name}: erledigt")
            except Exception as e:
                self.results.append((name, False, str(e)))
                self.r.err(f"{name}: {e}")
            self.r.progress((i + 1) / total)

        ok = sum(1 for _n, good, _d in self.results if good)
        fail = len(self.results) - ok
        if self.safety.exists():
            self.r.log(f"Der vorherige Zustand liegt in: {self.safety}")
        self.r.done({"ok": ok, "fail": fail})
        return {"ok": ok, "fail": fail}

    # ---------------- einzelne Schritte ----------------
    def _wifi(self) -> None:
        xml_dir = self.src / "03_WLAN" / "profile_xml"
        files = sorted(xml_dir.glob("*.xml"))
        if not is_admin():
            raise RuntimeError("Administrator-Rechte noetig")
        added = 0
        for xf in files:
            if self.r.cancelled:
                raise RuntimeError(f"abgebrochen nach {added} von {len(files)} Profil(en)")
            res = run(["netsh", "wlan", "add", "profile",
                       f"filename={xf}", "user=all"], timeout=60)
            if res.rc == 0:
                added += 1
            else:
                self.r.warn(f"{xf.stem}: {(res.out or res.err).strip()[:90]}")
        self.r.log(f"{added} von {len(files)} WLAN-Profil(en) eingetragen.")
        if not added:
            raise RuntimeError("kein Profil uebernommen")

    def _firefox(self) -> None:
        target = _env_dir("APPDATA", "Mozilla", "Firefox")
        self._preserve(target, "Firefox")
        src = self.src / "05_Browser" / "Firefox"
        target.mkdir(parents=True, exist_ok=True)
        res = robocopy(src, target)
        if not res.ok:
            raise RuntimeError(f"Kopie unvollstaendig (Code {res.rc})")
        self.r.log("Passwoerter, Lesezeichen und Verlauf sind wieder da.")

    def _thunderbird(self) -> None:
        target = _env_dir("APPDATA", "Thunderbird")
        self._preserve(target, "Thunderbird")
        res = robocopy(self.src / "10_EMail" / "Thunderbird", target)
        if not res.ok:
            raise RuntimeError(f"Kopie unvollstaendig (Code {res.rc})")

    def _sticky(self) -> None:
        target = _env_dir("LOCALAPPDATA", "Packages",
                          "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe",
                          "LocalState")
        if not target.parent.exists():
            raise RuntimeError("Kurznotizen-App ist auf diesem PC nicht eingerichtet")
        self._preserve(target, "StickyNotes")
        target.mkdir(parents=True, exist_ok=True)
        res = robocopy(self.src / "09_Sticky-Notes", target)
        if not res.ok:
            raise RuntimeError(f"Kopie unvollstaendig (Code {res.rc})")

    def _outlook(self) -> None:
        target = _env_dir("LOCALAPPDATA", "Microsoft", "Outlook")
        # Eine gleichnamige .pst wird kommentarlos ueberschrieben - das ist der
        # gesamte Mailbestand, der seit der Sicherung dazugekommen ist.
        self._preserve(target, "Outlook")
        target.mkdir(parents=True, exist_ok=True)
        n = 0
        for pst in (self.src / "10_EMail" / "Outlook_PST").glob("*.pst"):
            if self.r.cancelled:
                raise RuntimeError(f"abgebrochen nach {n} Datei(en)")
            res = copy_path(pst, target)
            if res.ok:
                n += 1
            else:
                self.r.warn(f"{pst.name}: {res.err or res.rc}")
        if not n:
            raise RuntimeError("keine Datei kopiert")
        self.r.log(f"{n} Datei(en) nach {target} gelegt.")
        self.r.log("In Outlook oeffnen: Datei -> Oeffnen -> Outlook-Datendatei.")

    def _hosts(self) -> None:
        if not is_admin():
            raise RuntimeError("Administrator-Rechte noetig")
        target = _env_dir("windir", "System32", "drivers", "etc")
        self._preserve(target / "hosts", "hosts")
        res = copy_path(self.src / "08_Sonstiges" / "hosts", target)
        if not res.ok:
            raise RuntimeError(res.err or f"Code {res.rc}")

    def _userfiles(self) -> None:
        base = self.src / "06_Eigene-Dateien"
        up = _env_dir("USERPROFILE")
        # Die Sicherung hat die Quellordner ueber known_folders() bestimmt; bei
        # aktiver OneDrive-Ordnerumleitung liegt "Documents" nicht unter
        # %USERPROFILE%, und stur dorthin kopierte Dateien faende der Anwender
        # im Explorer nirgends wieder.
        ziele = known_folders()
        copied = 0
        for folder in base.iterdir():
            if self.r.cancelled:
                break
            if not folder.is_dir():
                continue
            ziel = Path(ziele.get(folder.name, up / folder.name))
            res = robocopy(folder, ziel)
            if self.r.cancelled:
                # Ein von kill_all() abgewuergtes robocopy endet mit Code 1 und
                # gaelte sonst als geglueckte Kopie - der Ordner ist halb.
                raise RuntimeError(f"abgebrochen - {ziel} ist unvollstaendig")
            if res.ok:
                copied += 1
                self.r.log(f"{folder.name} -> {ziel}")
            else:
                self.r.warn(f"{folder.name}: Code {res.rc}")
        if self.r.cancelled:
            raise RuntimeError(f"abgebrochen nach {copied} Ordner(n)")
        if not copied:
            raise RuntimeError("nichts kopiert")
        self.r.log(f"{copied} Ordner zurueckgelegt.")

    def _programs(self) -> None:
        json_file = self.src / "04_Programme" / "winget-programme.json"
        if run(["winget", "--version"], timeout=30).rc != 0:
            # Auf einem frisch aufgesetzten PC fehlt winget oft - dann wird
            # es hier beschafft, statt den Schritt aufzugeben.
            from .downloader import DownloadManager
            if not DownloadManager(self.r).ensure_winget():
                raise RuntimeError("winget liess sich nicht einrichten")
        self.r.log("Das kann je nach Umfang lange dauern ...")
        res = run(["winget", "import", "-i", str(json_file),
                   "--accept-package-agreements", "--accept-source-agreements",
                   "--ignore-versions", "--disable-interactivity"], timeout=5400)
        # winget meldet auch dann != 0, wenn nur einzelne Pakete fehlen
        if res.rc not in (0, -1978335189, -1978335135):
            self.r.warn(f"winget endete mit Code {res.rc} - nicht alles konnte "
                        f"installiert werden.")
        tail = (res.out or "").strip().splitlines()[-6:]
        for line in tail:
            self.r.log("  " + line.strip()[:110])

    def _drivers(self) -> None:
        if not is_admin():
            raise RuntimeError("Administrator-Rechte noetig")
        folder = self.src / "07_Treiber"
        res = run(["pnputil", "/add-driver", str(folder / "*.inf"),
                   "/subdirs", "/install"], timeout=3600)
        if res.rc not in (0, 259, 3010):
            raise RuntimeError(f"pnputil endete mit Code {res.rc}")
        self.r.log("Treiber eingespielt (Neustart kann noetig sein).")

    def _bookmarks(self) -> None:
        """Lesezeichen lassen sich nicht ohne Zutun importieren - der Browser
        bietet dafuer keine Schnittstelle. Deshalb wird der Ordner geoeffnet
        und der Weg genannt."""
        folder = self.src / "05_Browser" / "_Lesezeichen_HTML"
        files = sorted(folder.glob("*.html"))
        self.r.log(f"{len(files)} Lesezeichen-Datei(en) liegen bereit:")
        for f in files[:8]:
            self.r.log(f"  {f.name}")
        self.r.log("Im Browser: Einstellungen -> Lesezeichen -> aus HTML-Datei "
                   "importieren.")
        if IS_WINDOWS:
            try:
                os.startfile(str(folder))       # Explorer oeffnen
            except Exception:
                pass

    # ---------------- Helfer ----------------
    def _preserve(self, path: Path, label: str) -> None:
        """Sichert den aktuellen Zustand, bevor er ueberschrieben wird.

        Der Fehlschlag muss den Schritt anhalten: robocopy und copy_path werfen
        nie eine Ausnahme, sondern melden ihn nur im Rueckgabewert - ungeprueft
        wuerde also genau dann ohne Rueckversicherung ueberschrieben, wenn sie
        gebraucht wird.
        """
        if not path.exists():
            return
        self.safety.mkdir(parents=True, exist_ok=True)
        target = self.safety / label
        res = robocopy(path, target) if path.is_dir() else copy_path(path, target)
        if not res.ok:
            raise RuntimeError(
                f"bisheriger Stand von {label} liess sich nicht sichern "
                f"(Code {res.rc}) - es wurde nichts ueberschrieben")
        self.r.log(f"Bisheriger Stand von {label} gesichert.")

    def _running_programs(self) -> list:
        """Programme, die den Import stoeren wuerden."""
        if not IS_WINDOWS:
            return []
        watch = {"firefox.exe": "Firefox", "thunderbird.exe": "Thunderbird",
                 "outlook.exe": "Outlook"}
        res = run(["tasklist", "/fo", "csv", "/nh"], timeout=60)
        running = []
        low = (res.out or "").lower()
        for exe, name in watch.items():
            if exe in low:
                running.append(name)
        return running
