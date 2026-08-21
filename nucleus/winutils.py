"""Windows-Helfer: Admin-Check, Subprozesse (ohne Konsolenfenster),
PowerShell-Bruecke, robocopy, Known-Folder-Aufloesung, Freispeicher.

Alles ist so gekapselt, dass der Import auch unter Nicht-Windows
(z. B. beim Syntax-Check) nicht scheitert - Windows-spezifische Aufrufe
werden erst zur Laufzeit ausgefuehrt.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000


def _flags() -> int:
    return CREATE_NO_WINDOW if IS_WINDOWS else 0


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@dataclass
class ProcResult:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


# Eigene Fehlercodes, die sich NICHT mit den Bitflags von robocopy (0-7 Erfolg)
# ueberschneiden duerfen - sonst wuerde ein Timeout als geglueckte Kopie gelten.
RC_TIMEOUT = 16
RC_FAILED = 17
RC_NOT_FOUND = 127


# Laufende Kindprozesse - damit ein Sofort-Abbruch sie beenden kann.
_ACTIVE: set = set()
_ACTIVE_LOCK = threading.Lock()


def run(args, timeout: int = 600, cwd=None, input_text: str | None = None) -> ProcResult:
    """Startet ein Programm ohne sichtbares Konsolenfenster.

    `args` darf eine Liste sein oder (nur Windows) eine fertige Kommandozeile
    als String - Letzteres, wenn Quoting bereits stimmt (z. B. UninstallString).
    Ausgaben werden roh gelesen und tolerant dekodiert, weil Windows-Werkzeuge
    je nach Codepage OEM (cp850) statt UTF-8 liefern.

    Der Prozess wird registriert, solange er laeuft, damit kill_all() ihn im
    Notfall beenden kann.
    """
    proc = None
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_flags(),
        )
        with _ACTIVE_LOCK:
            _ACTIVE.add(proc)
        out, err = proc.communicate(
            input=input_text.encode("utf-8") if input_text is not None else None,
            timeout=timeout)
        return ProcResult(proc.returncode, _decode(out), _decode(err))
    except subprocess.TimeoutExpired:
        _terminate(proc)
        return ProcResult(RC_TIMEOUT, "", f"Timeout nach {timeout}s")
    except FileNotFoundError as e:
        return ProcResult(RC_NOT_FOUND, "", str(e))
    except Exception as e:  # pragma: no cover - defensive
        return ProcResult(RC_FAILED, "", str(e))
    finally:
        if proc is not None:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(proc)


def _terminate(proc) -> None:
    """Beendet einen Prozess samt Kindern (msiexec & Co. starten weitere)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=_flags(), timeout=20)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def kill_all() -> int:
    """Beendet alle laufenden Kindprozesse. Gibt deren Anzahl zurueck."""
    with _ACTIVE_LOCK:
        procs = [p for p in _ACTIVE if p.poll() is None]
    for p in procs:
        _terminate(p)
    return len(procs)


def _decode(raw) -> str:
    """Dekodiert Programmausgaben tolerant: erst UTF-8, sonst die
    Windows-Konsolen-Codepage (deutsches Windows liefert oft cp850)."""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "cp850", "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# Ohne diesen Vorspann gibt Windows PowerShell auf einem deutschen System in
# der OEM-Codepage (z. B. cp850) aus - wir lesen aber UTF-8. Ergebnis waeren
# zerstoerte Umlaute in WLAN-Namen, Programmnamen und System-Infos.
_PS_PREAMBLE = (
    "$OutputEncoding = [Console]::OutputEncoding = "
    "[System.Text.Encoding]::UTF8\n"
)


def powershell(script: str, timeout: int = 600) -> ProcResult:
    """Fuehrt PowerShell-Code aus.

    Uebergabe per -EncodedCommand (Base64 ueber UTF-16LE): damit gibt es weder
    Quoting- noch Codepage-Probleme, auch nicht bei Umlauten oder Apostrophen
    in eingesetzten Pfaden. Ueber stdin wuerde PowerShell 5.1 den Text in der
    OEM-Codepage lesen und Sonderzeichen verstuemmeln.
    """
    payload = (_PS_PREAMBLE + script).encode("utf-16-le")
    args = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-NonInteractive", "-EncodedCommand", base64.b64encode(payload).decode("ascii"),
    ]
    return run(args, timeout=timeout)


def ps_json(script: str, timeout: int = 600):
    """PowerShell-Ausgabe als JSON parsen. Das Skript muss selbst
    `ConvertTo-Json` am Ende aufrufen. Liefert immer eine Liste."""
    import json
    res = powershell(script, timeout=timeout)
    text = (res.out or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return []


# ------------------------------------------------------------------
#  robocopy: robustes Kopieren, Exitcode wird EHRLICH ausgewertet
# ------------------------------------------------------------------
# robocopy-Exitcodes: 0-7 = Erfolg/Info (Bitflags), >=8 = mindestens ein Fehler.
def robocopy(src, dst, exclude_dirs=None, exclude_files=None,
             mirror: bool = False, timeout: int = 3600) -> ProcResult:
    args = ["robocopy", str(src), str(dst)]
    args += ["/MIR"] if mirror else ["/E"]
    args += ["/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XJ", "/DCOPY:DAT"]
    if exclude_dirs:
        args += ["/XD"] + list(exclude_dirs)
    if exclude_files:
        args += ["/XF"] + list(exclude_files)
    res = run(args, timeout=timeout)
    # rc>=8 = echter Fehler; darunter als Erfolg werten.
    res_ok = res.rc < 8
    return ProcResult(0 if res_ok else res.rc, res.out, res.err)


def copy_path(src, dst, exclude_dirs=None) -> ProcResult:
    """Kopiert eine Datei ODER einen Ordner nach dst.
    Ordner via robocopy, Einzeldatei via shutil."""
    src = Path(src)
    if not src.exists():
        return ProcResult(1, "", f"Quelle fehlt: {src}")
    if src.is_dir():
        return robocopy(src, dst, exclude_dirs=exclude_dirs)
    try:
        Path(dst).mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(Path(dst) / src.name))
        return ProcResult(0)
    except Exception as e:
        return ProcResult(1, "", str(e))


def free_space_mb(path) -> float:
    try:
        total, used, free = shutil.disk_usage(str(path))
        return round(free / (1024 * 1024), 1)
    except Exception:
        return -1.0


def dir_size_bytes(path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(str(path)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except Exception:
        pass
    return total


# ------------------------------------------------------------------
#  Known Folders (loest OneDrive-Ordnerumleitung korrekt auf)
# ------------------------------------------------------------------
# Wir lesen die bereits aufgeloesten Pfade aus der Registry ("Shell Folders"),
# denn diese spiegeln OneDrive-KnownFolderMove wider. Fallback: %USERPROFILE%.
_SHELL_FOLDER_KEYS = {
    "Desktop":   "Desktop",
    "Documents": "Personal",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Pictures":  "My Pictures",
    "Music":     "My Music",
    "Videos":    "My Video",
    "Favorites": "Favorites",
}


def known_folders() -> dict:
    """Gibt {AnzeigeName: Pfad} fuer die wichtigen Benutzerordner zurueck
    - mit OneDrive-Umleitung, falls aktiv."""
    result: dict = {}
    up = os.environ.get("USERPROFILE", str(Path.home()))
    if IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            for name, valname in _SHELL_FOLDER_KEYS.items():
                try:
                    val, _ = winreg.QueryValueEx(key, valname)
                    if val and os.path.isdir(val):
                        result[name] = val
                except OSError:
                    pass
            winreg.CloseKey(key)
        except OSError:
            pass
    # Fallback fuer alles, was nicht aufgeloest wurde
    for name in _SHELL_FOLDER_KEYS:
        if name not in result:
            cand = os.path.join(up, name)
            if os.path.isdir(cand):
                result[name] = cand
    return result


def onedrive_root() -> str | None:
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        p = os.environ.get(var)
        if p and os.path.isdir(p):
            return p
    return None
