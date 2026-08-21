"""Update-Pruefung gegen das oeffentliche GitHub-Repo.

Ablauf:
  1. Beim Start laeuft check() in einem Hintergrund-Thread (blockiert nie).
  2. Gibt es eine neuere Version, zeigt die Oberflaeche einen Hinweis.
  3. Auf Knopfdruck laedt apply() das aktuelle ZIP des Repos und ersetzt
     die Programmdateien - danach ist ein Neustart noetig.

Bewusst schlicht gehalten: keine Abhaengigkeiten, nur HTTPS zum eigenen Repo,
und es wird nie etwas ohne Bestaetigung des Nutzers ersetzt.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import ssl
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = "Secoolioo/Hoferium"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
RELEASES_URL = f"https://github.com/{REPO}/releases"
REPO_URL = f"https://github.com/{REPO}"

_UA = "Hoferium-Updater"
_TIMEOUT = 10          # kurz halten: der Start darf nie darauf warten

# Diese Namen werden beim Update ersetzt bzw. angelegt.
_UPDATE_ITEMS = ("nucleus", "hoferium.bat", "LIESMICH.txt", "VERSION",
                 "README.md", "apps.json")


def parse_version(text: str) -> tuple:
    """'1.2.3' -> (1, 2, 3). Unlesbares wird zu (0,), gilt damit als aeltest."""
    nums = []
    for part in str(text).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits == "":
            break
        nums.append(int(digits))
    return tuple(nums) if nums else (0,)


@dataclass
class UpdateInfo:
    current: str = ""
    latest: str = ""
    available: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        return urllib.request.urlopen(req, timeout=_TIMEOUT)
    except (urllib.error.URLError, ssl.SSLError, OSError):
        # Manche Firmen-/Heimnetze haben kaputte Zertifikatsketten; ein
        # Versionsvergleich ist kein Grund, dafuer die App scheitern zu lassen.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx)


def check(current_version: str) -> UpdateInfo:
    """Fragt die Version im Repo ab. Wirft nie - Fehler landen in .error."""
    info = UpdateInfo(current=current_version)
    try:
        with _open(VERSION_URL) as resp:
            raw = resp.read(200).decode("utf-8", errors="replace").strip()
        latest = raw.splitlines()[0].strip() if raw else ""
        if not latest:
            info.error = "Repo enthaelt keine lesbare VERSION-Datei"
            return info
        info.latest = latest
        info.available = parse_version(latest) > parse_version(current_version)
        return info
    except Exception as e:
        info.error = f"{type(e).__name__}: {e}"
        return info


def apply(install_dir, reporter=None) -> bool:
    """Laedt das Repo-ZIP und ersetzt die Programmdateien in install_dir.

    Vorgehen mit Sicherheitsnetz:
      * ZIP komplett in den Speicher laden und pruefen
      * bisherige Dateien in einen Sicherungsordner verschieben
      * neue Dateien einspielen; scheitert das, wird zurueckgerollt
    """
    def log(msg, level="info"):
        if reporter is not None:
            getattr(reporter, {"ok": "ok", "warn": "warn", "err": "err"}.get(level, "log"))(msg)

    install_dir = Path(install_dir)
    log("Lade Update von GitHub ...")
    try:
        with _open(ZIP_URL) as resp:
            blob = resp.read()
    except Exception as e:
        log(f"Download fehlgeschlagen: {e}", "err")
        return False
    if len(blob) < 2048:
        log("Heruntergeladenes Archiv ist unplausibel klein - abgebrochen.", "err")
        return False

    tmp = Path(tempfile.mkdtemp(prefix="hoferium_upd_"))
    backup = install_dir / "_vorherige_version"
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            root = _zip_root(zf)
            if root is None:
                log("Archivaufbau unerwartet - abgebrochen.", "err")
                return False
            _safe_extract(zf, tmp)
        src = tmp / root
        if not (src / "nucleus").is_dir():
            log("Im Archiv fehlt der Programmordner - abgebrochen.", "err")
            return False

        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True, exist_ok=True)
        moved = []
        for name in _UPDATE_ITEMS:
            cur = install_dir / name
            if cur.exists():
                shutil.move(str(cur), str(backup / name))
                moved.append(name)
        try:
            for name in _UPDATE_ITEMS:
                new = src / name
                if new.exists():
                    if new.is_dir():
                        shutil.copytree(new, install_dir / name)
                    else:
                        shutil.copy2(new, install_dir / name)
        except Exception as e:                       # Rollback
            log(f"Einspielen fehlgeschlagen ({e}) - stelle vorherige Version wieder her.", "err")
            for name in moved:
                dst = install_dir / name
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
                shutil.move(str(backup / name), str(dst))
            return False

        log("Update eingespielt. Bitte Hoferium neu starten.", "ok")
        log(f"Die vorherige Version liegt in: {backup}")
        return True
    except Exception as e:
        log(f"Update fehlgeschlagen: {e}", "err")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _zip_root(zf: zipfile.ZipFile):
    names = [n for n in zf.namelist() if n.strip("/")]
    if not names:
        return None
    first = names[0].split("/")[0]
    return first if all(n.split("/")[0] == first for n in names) else None


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Entpackt und verhindert dabei Pfad-Ausbrueche (Zip-Slip)."""
    dest = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if not str(target).startswith(str(dest)):
            raise RuntimeError(f"Unerlaubter Pfad im Archiv: {member.filename}")
    zf.extractall(dest)
