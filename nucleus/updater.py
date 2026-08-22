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

import fnmatch
import io
import json
import os
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = "Secoolioo/Hoferium"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
API_VERSION_URL = f"https://api.github.com/repos/{REPO}/contents/VERSION?ref={BRANCH}"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
REPO_URL = f"https://github.com/{REPO}"

_UA = "Hoferium-Updater"
_TIMEOUT = 10          # kurz halten: der Start darf nie darauf warten
_MAX_ZIP = 50 * 1024 * 1024   # ein defekter Proxy soll den Speicher nicht vollschreiben

BACKUP_DIR = "_vorherige_version"
STAGING_DIR = "_neue_version"

# Diese Eintraege ueberstehen jedes Update unveraendert: die Ergebnisse des
# Nutzers und der Sicherungsordner des Updates selbst.
KEEP_ALWAYS = (
    BACKUP_DIR,
    STAGING_DIR,        # Zwischenablage des laufenden Updates
    "Hoferium-Sicherungen",   # Sammelordner der Datensicherungen
    "Sicherung_*",      # aeltere, lose abgelegte Sicherungen
    "Installer",        # heruntergeladene Installationsdateien
    "*.log",
)


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


def _open(url: str, allow_insecure: bool = False):
    """Oeffnet die Adresse. allow_insecure gilt nur fuer die Versionsabfrage.

    Ausfuehrbarer Code (das Update-ZIP) darf ausschliesslich ueber eine
    geprueft verschluesselte Verbindung kommen - sonst genuegt die
    Netzwerkposition, um ein eigenes Archiv unterzuschieben.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        return urllib.request.urlopen(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError:
        # HTTPError ist eine Unterklasse von URLError: ein Statuscode wie 403
        # (Rate-Limit) oder 404 wird von einem zweiten Versuch nicht besser,
        # er verdoppelt nur den Verbrauch am Anfragekontingent.
        raise
    except (urllib.error.URLError, ssl.SSLError, OSError):
        if not allow_insecure:
            raise
        # Manche Firmen-/Heimnetze haben kaputte Zertifikatsketten; ein
        # Versionsvergleich ist kein Grund, dafuer die App scheitern zu lassen.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx)


def _version_via_api() -> str:
    """Version ueber die GitHub-API lesen.

    Noetig, weil raw.githubusercontent.com ueber ein CDN ausgeliefert wird und
    eine frisch veroeffentlichte Fassung dort minutenlang alt bleibt - ein
    Update wuerde sonst verspaetet erkannt.
    """
    with _open(API_VERSION_URL, allow_insecure=True) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    content = data.get("content") or ""
    if data.get("encoding") == "base64" and content:
        import base64
        return base64.b64decode(content).decode("utf-8", errors="replace").strip()
    return ""


def _version_via_raw() -> str:
    with _open(VERSION_URL, allow_insecure=True) as resp:
        return resp.read(200).decode("utf-8", errors="replace").strip()


def check(current_version: str) -> UpdateInfo:
    """Fragt die Version im Repo ab. Wirft nie - Fehler landen in .error."""
    info = UpdateInfo(current=current_version)
    last_error = ""
    for source in (_version_via_api, _version_via_raw):
        try:
            raw = source()
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            continue
        latest = raw.splitlines()[0].strip() if raw else ""
        if latest:
            info.latest = latest
            info.available = parse_version(latest) > parse_version(current_version)
            return info
        last_error = "Repo enthaelt keine lesbare VERSION-Datei"
    info.error = last_error or "Version nicht ermittelbar"
    return info


def apply(install_dir, reporter=None) -> bool:
    """Spielt den aktuellen Repo-Stand ein - auch bei geaenderter Struktur.

    Der Inhalt des Archivs ist massgeblich: Was dort liegt, wird eingespielt;
    was hier liegt und dort fehlt, wandert in den Sicherungsordner. Damit
    ueberleben Umbenennungen (z. B. eines Programmordners) und geloeschte
    Dateien ein Update, ohne Reste zu hinterlassen - die alte Fassung muss
    die neue Struktur dafuer nicht kennen.

    Unangetastet bleiben Nutzerdaten (Sicherungen, geladene Installer) sowie
    alles, was eine mitgelieferte update.json zusaetzlich schuetzt.
    """
    def log(msg, level="info"):
        if reporter is not None:
            getattr(reporter, {"ok": "ok", "warn": "warn", "err": "err"}.get(level, "log"))(msg)

    install_dir = Path(install_dir)
    log("Lade Update von GitHub ...")
    try:
        with _open(ZIP_URL) as resp:
            blob = resp.read(_MAX_ZIP + 1)
    except (urllib.error.URLError, ssl.SSLError) as e:
        if isinstance(e, ssl.SSLError) or isinstance(getattr(e, "reason", None), ssl.SSLError):
            # urllib verpackt einen TLS-Fehler in URLError.reason - hier gibt es
            # bewusst keinen Rueckfall auf eine ungepruefte Verbindung.
            log("Verbindung nicht vertrauenswuerdig - Update abgebrochen.", "err")
        else:
            log(f"Download fehlgeschlagen: {e}", "err")
        return False
    except Exception as e:
        log(f"Download fehlgeschlagen: {e}", "err")
        return False
    if len(blob) > _MAX_ZIP:
        log("Heruntergeladenes Archiv ist unplausibel gross - abgebrochen.", "err")
        return False
    if len(blob) < 2048:
        log("Heruntergeladenes Archiv ist unplausibel klein - abgebrochen.", "err")
        return False
    if not blob.startswith(b"PK"):
        # Captive Portals und Proxys antworten mit HTML statt mit dem Archiv.
        log("Heruntergeladene Datei ist kein Archiv - abgebrochen.", "err")
        return False

    # Der neue Stand wird auf demselben Datentraeger zwischengelagert, damit
    # das Einspielen nur noch Umbenennungen braucht: das Zeitfenster, in dem
    # der Stick weder Starter noch Programmordner hat, schrumpft von Sekunden
    # auf Millisekunden.
    tmp = install_dir / STAGING_DIR
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    backup = install_dir / BACKUP_DIR
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            root = _zip_root(zf)
            if root is None:
                log("Archivaufbau unerwartet - abgebrochen.", "err")
                return False
            _safe_extract(zf, tmp)
        src = tmp / root

        problem = _implausible(src)
        if problem:
            log(f"Archiv wirkt unvollstaendig ({problem}) - abgebrochen.", "err")
            return False

        protect = _protected_names(src)
        incoming = sorted(p.name for p in src.iterdir())
        obsolete = [p.name for p in install_dir.iterdir()
                    if p.name not in incoming and not _is_protected(p.name, protect)]

        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True, exist_ok=True)

        moved = []          # (Name, im Backup) - fuer den Rollback
        try:
            # 1) Alles beiseiteraeumen, was ersetzt oder ueberfluessig ist
            for name in incoming + obsolete:
                cur = install_dir / name
                if cur.exists() and not _is_protected(name, protect):
                    shutil.move(str(cur), str(backup / name))
                    moved.append(name)
            # 2) Neuen Stand einspielen
            for item in sorted(src.iterdir()):
                if _is_protected(item.name, protect):
                    # Schritt 1 hat Geschuetztes stehen lassen; es darf auch
                    # nicht aus dem Archiv ueberschrieben werden.
                    continue
                dst = install_dir / item.name
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
                os.replace(str(item), str(dst))
        except Exception as e:
            log(f"Einspielen fehlgeschlagen ({e}) - stelle die vorherige "
                f"Fassung wieder her.", "err")
            # Was Schritt 2 neu angelegt hat, steht nicht in 'moved' und wuerde
            # sich sonst unter die wiederhergestellte alte Fassung mischen.
            for name in incoming:
                if name in moved or _is_protected(name, protect):
                    continue
                fresh = install_dir / name
                try:
                    if fresh.is_dir():
                        shutil.rmtree(fresh, ignore_errors=True)
                    elif fresh.exists():
                        fresh.unlink()
                except Exception:
                    pass
            failed = []
            for name in moved:
                # Jeder Eintrag einzeln: ein blockierter Name (offene Datei,
                # Schreibschutz) darf die restliche Wiederherstellung nicht stoppen.
                try:
                    dst = install_dir / name
                    if dst.exists():
                        shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
                    shutil.move(str(backup / name), str(dst))
                except Exception:
                    failed.append(name)
            if failed:
                log("Wiederherstellung unvollstaendig: " + ", ".join(sorted(failed))
                    + f". Bitte den Inhalt von {backup} von Hand zurueckkopieren.", "err")
            return False

        if obsolete:
            log("Nicht mehr benoetigt und weggeraeumt: " + ", ".join(sorted(obsolete)))
        log(f"Update eingespielt ({len(incoming)} Eintraege).", "ok")
        log(f"Die vorherige Fassung liegt in: {backup}")
        return True
    except Exception as e:
        log(f"Update fehlgeschlagen: {e}", "err")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restart_app(install_dir, delay: int = 3) -> bool:
    """Startet Hoferium neu und gibt zurueck, ob das angestossen wurde.

    Der Neustart laeuft ueber den Starter (*.bat), nicht ueber den eigenen
    Interpreter: Nach einem Update kann der Programmordner anders heissen -
    nur der mitgelieferte Starter weiss, was zu starten ist. Er prueft
    ausserdem die Python-Umgebung und zieht neue Pakete nach.

    Der neue Vorgang wird abgekoppelt gestartet und wartet kurz, damit sich
    dieses Fenster vorher sauber schliessen kann.
    """
    install_dir = Path(install_dir)
    starters = sorted(install_dir.glob("*.bat"))
    if not starters:
        return False
    starter = starters[0]
    try:
        if os.name == "nt":
            DETACHED = 0x00000008 | 0x00000200      # DETACHED_PROCESS | NEW_GROUP
            subprocess.Popen(
                # ping statt timeout: timeout.exe bricht ohne Konsolen-Eingabe
                # ("Input redirection is not supported") sofort ab, und wegen
                # des einfachen & liefe start dann ohne jede Wartezeit los.
                f'ping -n {max(1, delay) + 1} 127.0.0.1 >nul & start "" "{starter.name}"',
                shell=True, cwd=str(install_dir), creationflags=DETACHED,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        else:                                        # nur fuer Tests ausserhalb Windows
            subprocess.Popen(["/bin/sh", "-c", f"sleep {delay}; echo {starter}"],
                             cwd=str(install_dir), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _implausible(src: Path) -> str:
    """Prueft grob, ob das Archiv ein brauchbares Programm enthaelt.

    Ohne diese Pruefung koennte ein leeres oder kaputtes Archiv die
    vorhandene Installation ersetzen.
    """
    if not any(src.glob("*.bat")):
        return "kein Starter enthalten"
    packages = [d for d in src.iterdir()
                if d.is_dir() and (d / "__main__.py").is_file()]
    if not packages:
        return "kein startbarer Programmordner enthalten"
    return ""


def _protected_names(src: Path) -> list:
    """Namen/Muster, die beim Update nicht angefasst werden.

    Die Grundliste steht hier; eine update.json im Archiv kann sie
    erweitern - so kann eine kuenftige Fassung mitteilen, was zusaetzlich
    erhalten bleiben muss, ohne dass diese Fassung sie kennt.
    """
    protect = list(KEEP_ALWAYS)
    manifest = src / "update.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            extra = data.get("protect") or []
            protect += [str(x) for x in extra if isinstance(x, str)]
        except Exception:
            pass
    return protect


def _is_protected(name: str, protect: list) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in protect)


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
        # Vergleich ueber die Pfadteile statt ueber den Text: ein Nachbarordner,
        # dessen Name mit dem Zielnamen beginnt, wuerde sonst durchgehen.
        if target != dest and dest not in target.parents:
            raise RuntimeError(f"Unerlaubter Pfad im Archiv: {member.filename}")
    zf.extractall(dest)
