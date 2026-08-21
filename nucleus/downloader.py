"""DownloadManager - offizielle Installer holen oder Programme per winget setzen.

Katalog: bekannte Programme mit direkter (stabiler) Download-URL und/oder
winget-ID. 'Installer speichern' laedt die Datei in einen Ordner (fuer den
frischen PC), 'Direkt installieren' nutzt winget.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .context import Reporter
from .winutils import powershell, run

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")


CATALOG_URL = ("https://raw.githubusercontent.com/Secoolioo/Hoferium/"
               "main/apps.json")


@dataclass
class App:
    name: str
    category: str = "Allgemein"
    url: str = ""          # direkter, stabiler Download (optional)
    winget_id: str = ""    # winget-Paket-ID (optional)
    filename: str = ""     # Zieldateiname beim Direkt-Download

    @property
    def can_download(self) -> bool:
        return bool(self.url) or bool(self.winget_id)


# Kuratierte Auswahl. Direkt-URLs nur dort, wo sie stabil/"latest" sind;
# alles Weitere laeuft ueber winget.
CATALOG: list = [
    App("Mozilla Firefox", "Browser",
        url="https://download.mozilla.org/?product=firefox-latest-ssl&os=win64&lang=de",
        winget_id="Mozilla.Firefox", filename="firefox-setup.exe"),
    App("Google Chrome", "Browser",
        url="https://dl.google.com/chrome/install/standalonesetup64.exe",
        winget_id="Google.Chrome", filename="chrome-setup.exe"),
    App("Brave", "Browser", winget_id="Brave.Brave"),
    App("7-Zip", "Werkzeuge", winget_id="7zip.7zip"),
    App("VLC media player", "Medien", winget_id="VideoLAN.VLC"),
    App("Notepad++", "Werkzeuge", winget_id="Notepad++.Notepad++"),
    App("LibreOffice", "Office", winget_id="TheDocumentFoundation.LibreOffice"),
    App("Adobe Acrobat Reader", "Office", winget_id="Adobe.Acrobat.Reader.64-bit"),
    App("GIMP", "Grafik", winget_id="GIMP.GIMP"),
    App("PowerToys", "Werkzeuge", winget_id="Microsoft.PowerToys"),
    App("VS Code", "Entwicklung", winget_id="Microsoft.VisualStudioCode"),
    App("Git", "Entwicklung", winget_id="Git.Git"),
    App("Spotify", "Medien", winget_id="Spotify.Spotify"),
    App("Discord", "Kommunikation", winget_id="Discord.Discord"),
    App("Steam", "Gaming", winget_id="Valve.Steam"),
    App("qBittorrent", "Werkzeuge", winget_id="qBittorrent.qBittorrent"),
]


def load_catalog(reporter=None) -> tuple:
    """Liefert (Apps, Quelle).

    Damit das Werkzeug auch in Jahren noch brauchbar ist, wird der Katalog in
    drei Stufen geholt:
      1. aus dem Repo (dort kann er gepflegt werden, ohne das Programm zu
         aktualisieren),
      2. aus einer apps.json neben dem Programm,
      3. aus der eingebauten Liste.
    """
    def note(msg, level="log"):
        if reporter is not None:
            getattr(reporter, level, reporter.log)(msg)

    try:
        req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        apps = _apps_from(data)
        if apps:
            note(f"App-Liste aus dem Netz geladen ({len(apps)} Eintraege).")
            return apps, "online"
    except Exception:
        pass                       # offline ist voellig in Ordnung

    local = Path(__file__).resolve().parent.parent / "apps.json"
    try:
        if local.exists():
            apps = _apps_from(json.loads(local.read_text(encoding="utf-8")))
            if apps:
                note(f"App-Liste aus {local.name} geladen ({len(apps)} Eintraege).")
                return apps, "lokal"
    except Exception as e:
        note(f"apps.json nicht lesbar: {e}", "warn")

    return list(CATALOG), "eingebaut"


def _apps_from(data) -> list:
    entries = data.get("apps", data) if isinstance(data, dict) else data
    apps = []
    for it in entries or []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        apps.append(App(
            name=str(it["name"]),
            category=str(it.get("category", "Allgemein")),
            url=str(it.get("url", "")),
            winget_id=str(it.get("winget_id", "")),
            filename=str(it.get("filename", "")),
        ))
    return apps


class DownloadManager:
    def __init__(self, reporter: Reporter):
        self.r = reporter

    # ---------- Installer als Datei speichern ----------
    def download_all(self, apps: list, dest: Path) -> dict:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        ok = 0
        fail = 0
        total = len(apps)
        for i, app in enumerate(apps):
            if self.r.cancelled:
                self.r.warn("Abgebrochen.")
                break
            self.r.status(f"Lade {app.name} ...")
            try:
                if app.url:
                    self._download_url(app, dest)
                elif app.winget_id:
                    self._winget_download(app, dest)
                else:
                    raise RuntimeError("keine Quelle")
                ok += 1
                self.r.ok(f"{app.name} gespeichert.")
            except Exception as e:
                fail += 1
                self.r.err(f"{app.name}: {e}")
            self.r.progress((i + 1) / max(total, 1))
        self.r.done({"ok": ok, "fail": fail, "dir": str(dest)})
        return {"ok": ok, "fail": fail, "dir": str(dest)}

    def _download_url(self, app: App, dest: Path) -> None:
        fname = app.filename or (app.name.replace(" ", "_") + ".exe")
        target = dest / fname
        # Erst in eine .part-Datei laden und nur bei Erfolg umbenennen - sonst
        # bleibt bei Abbruch eine halbe .exe liegen, die auf dem frisch
        # aufgesetzten PC wie ein fertiger Installer aussieht.
        part = target.with_name(target.name + ".part")
        req = urllib.request.Request(app.url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", 0) or 0)
                read = 0
                with open(part, "wb") as fh:
                    while True:
                        if self.r.cancelled:
                            raise RuntimeError("abgebrochen")
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
                        read += len(chunk)
                        if total:
                            self.r.status(
                                f"{app.name}: {read // 1048576} / {total // 1048576} MB")
            if part.stat().st_size < 1024:
                raise RuntimeError("Datei verdaechtig klein")
            if total and part.stat().st_size < total * 0.99:
                raise RuntimeError("Download unvollstaendig")
            part.replace(target)
        except Exception:
            try:
                part.unlink()
            except OSError:
                pass
            raise

    def _winget_download(self, app: App, dest: Path) -> None:
        res = run(["winget", "download", "--id", app.winget_id, "-e",
                   "--accept-package-agreements", "--accept-source-agreements",
                   "-d", str(dest)], timeout=600)
        if res.rc != 0:
            raise RuntimeError("winget download nicht moeglich (Version zu alt?) - "
                               "stattdessen 'Direkt installieren' nutzen")

    # ---------- Direkt per winget installieren ----------
    def install_all(self, apps: list) -> dict:
        ok = 0
        fail = 0
        total = len(apps)
        for i, app in enumerate(apps):
            if self.r.cancelled:
                self.r.warn("Abgebrochen.")
                break
            if not app.winget_id:
                self.r.warn(f"{app.name}: keine winget-ID - bitte Installer speichern.")
                fail += 1
                continue
            self.r.status(f"Installiere {app.name} ...")
            res = run(["winget", "install", "--id", app.winget_id, "-e",
                       "--accept-package-agreements", "--accept-source-agreements",
                       "--disable-interactivity"], timeout=1200)
            # winget-Codes: 0 = installiert, 0x8A150061 = bereits vorhanden,
            # 0x8A15002B = kein Update noetig (beide als Erfolg werten).
            if res.rc == 0:
                ok += 1
                self.r.ok(f"{app.name} installiert.")
            elif res.rc in (-1978335135, -1978335189):
                ok += 1
                self.r.ok(f"{app.name} war bereits installiert.")
            else:
                fail += 1
                self.r.err(f"{app.name}: winget-Code {res.rc}")
            self.r.progress((i + 1) / max(total, 1))
        self.r.done({"ok": ok, "fail": fail})
        return {"ok": ok, "fail": fail}

    @staticmethod
    def winget_available() -> bool:
        return run(["winget", "--version"], timeout=30).rc == 0

    def ensure_winget(self) -> bool:
        """Sorgt dafuer, dass winget nutzbar ist.

        Auf aelteren Windows-10-Staenden fehlt es. Statt zu scheitern, wird
        der offizielle App Installer aus dem Microsoft Store angestossen.
        """
        if self.winget_available():
            return True
        self.r.warn("winget ist nicht verfuegbar - versuche den App Installer "
                    "zu oeffnen ...")
        res = run(["powershell", "-NoProfile", "-Command",
                   "Start-Process 'ms-windows-store://pdp/?productid=9NBLGGH4NNS1'"],
                  timeout=60)
        if res.rc == 0:
            self.r.log("Microsoft Store wurde geoeffnet. Dort 'App Installer' "
                       "installieren und danach erneut versuchen.")
        else:
            self.r.err("winget fehlt und der Store liess sich nicht oeffnen. "
                       "Bitte 'Installer speichern' nutzen.")
        return False
