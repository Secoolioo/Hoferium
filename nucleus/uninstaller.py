"""Uninstaller - Programme/AppX entfernen, inkl. Deep-Uninstall (Reste-Scan),
kuratierter Bloatware-Liste, Edge-Entfernung, Registry-Backup und
Wiederherstellungspunkt. Destruktiv - deshalb wird VOR jedem Eingriff
gesichert und alles protokolliert.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .context import Reporter
from .registry import InstalledApp, list_installed
from .winutils import IS_WINDOWS, is_admin, powershell, run

# ------------------------------------------------------------------
#  Bloatware-Katalog (AppX-Namensfragmente + Win32-Namensfragmente)
# ------------------------------------------------------------------
BLOAT_APPX = [
    "Microsoft.BingNews", "Microsoft.BingWeather", "Microsoft.BingSearch",
    "Microsoft.BingFinance", "Microsoft.BingSports",
    "Microsoft.GetHelp", "Microsoft.Getstarted", "Microsoft.MicrosoftOfficeHub",
    "Microsoft.MicrosoftSolitaireCollection", "Microsoft.People",
    "Microsoft.WindowsFeedbackHub", "Microsoft.WindowsMaps",
    "Microsoft.ZuneMusic", "Microsoft.ZuneVideo", "Microsoft.YourPhone",
    "Microsoft.MicrosoftStickyNotes",  # nur wenn NICHT gesichert werden soll
    "Microsoft.Todos", "Microsoft.PowerAutomateDesktop",
    "Microsoft.Clipchamp", "Clipchamp.Clipchamp",
    "MicrosoftTeams", "MSTeams", "Microsoft.Teams",
    "Microsoft.549981C3F5F10",  # Cortana
    "Microsoft.GamingApp", "Microsoft.XboxGamingOverlay",
    "Microsoft.XboxGameOverlay", "Microsoft.XboxSpeechToTextOverlay",
    "Microsoft.XboxIdentityProvider", "Microsoft.Xbox.TCUI",
    "Microsoft.MixedReality.Portal", "Microsoft.OneConnect",
    "Microsoft.WindowsCommunicationsApps",  # Mail & Kalender
    "Microsoft.SkypeApp", "Microsoft.Wallet", "Microsoft.3DBuilder",
    "Microsoft.Microsoft3DViewer", "Microsoft.Print3D",
    "Microsoft.OutlookForWindows", "Microsoft.Copilot",
    "Disney", "SpotifyAB.SpotifyMusic", "Facebook", "Instagram",
    "king.com", "king.com.CandyCrush", "PandoraMediaInc",
    "Microsoft.MicrosoftJournal", "Microsoft.PowerBIforWindows",
]
# StickyNotes standardmaessig NICHT als Bloat behandeln (Notizen!)
BLOAT_APPX_SAFE = [p for p in BLOAT_APPX if "StickyNotes" not in p]

# Nur ab Werk vorinstallierter Herstellerkram und Testversionen.
# BEWUSST NICHT enthalten: Programme, die der Nutzer selbst installiert haben
# koennte und die eigene Daten halten (Spotify, CCleaner, Amazon, Disney+ ...).
# Solche Programme gehoeren auf die Seite "Deinstallieren" - dort waehlt der
# Nutzer sie einzeln aus.
BLOAT_WIN32 = [
    "McAfee", "Norton Security", "Norton 360", "Avast Free", "AVG Protection",
    "WildTangent", "Booking.com", "ExpressVPN Trial",
    "HP Documentation", "HP Support Assistant", "HP JumpStart", "HP Wolf Security",
    "Lenovo Vantage", "Lenovo Now", "Lenovo Welcome",
    "Dell SupportAssist", "Dell Customer Connect", "Dell Digital Delivery",
    "ASUS GiftBox", "Nahimic", "Acer Jumpstart", "Acer Product Registration",
]


def _looks_bloat(app: InstalledApp) -> bool:
    n = app.name.lower()
    if app.kind == "appx":
        return any(frag.lower() in app.name.lower() for frag in BLOAT_APPX_SAFE)
    return any(frag.lower() in n for frag in BLOAT_WIN32)


def mark_bloatware(apps: list) -> list:
    for a in apps:
        a.is_bloatware = _looks_bloat(a)
    return apps


# ------------------------------------------------------------------
#  Uninstaller
# ------------------------------------------------------------------
class Uninstaller:
    def __init__(self, reporter: Reporter, backup_dir: Path):
        self.r = reporter
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.admin = is_admin()

    # ---------- Sicherheitsnetz ----------
    def prepare_safety(self) -> None:
        """Restore-Point-Drossel abschalten + Wiederherstellungspunkt anlegen."""
        self.r.log("Lege Wiederherstellungspunkt an (falls Systemschutz aktiv) ...")
        # Erfolg am tatsaechlich entstandenen Punkt pruefen - der Exitcode ist
        # wegen ErrorActionPreference='SilentlyContinue' immer 0.
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$before = (Get-ComputerRestorePoint | Select-Object -Last 1).SequenceNumber;"
            "New-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\SystemRestore' "
            "-Name 'SystemRestorePointCreationFrequency' -Value 0 -PropertyType DWord -Force | Out-Null;"
            "try { Enable-ComputerRestore -Drive $env:SystemDrive -EA SilentlyContinue } catch {};"
            "Checkpoint-Computer -Description 'Hoferium vor Debloat' -RestorePointType MODIFY_SETTINGS;"
            "$after = (Get-ComputerRestorePoint | Select-Object -Last 1).SequenceNumber;"
            "if ($after -and $after -ne $before) { Write-Output 'RP_OK' } "
            "else { Write-Output 'RP_NONE' }"
        )
        res = powershell(script, timeout=300)
        if "RP_OK" in (res.out or ""):
            self.r.ok("Wiederherstellungspunkt erstellt.")
        else:
            self.r.warn("KEIN Wiederherstellungspunkt moeglich (Systemschutz ist "
                        "auf den meisten Systemen ab Werk aus). Es greifen nur die "
                        "Registry-Backups und die Ordner-Quarantaene.")

    def _backup_reg(self, reg_path: str, tag: str) -> bool:
        """Sichert einen Registry-Zweig als .reg. Gibt True nur zurueck, wenn
        wirklich eine nicht-leere Datei entstanden ist - der Aufrufer darf sonst
        nichts loeschen. Der Dateiname enthaelt den Schluesselpfad, damit sich
        mehrere Treffer nicht gegenseitig ueberschreiben."""
        if not reg_path or not IS_WINDOWS:
            return False
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in tag)[:40]
        keyid = "".join(c if c.isalnum() else "_" for c in reg_path)[-70:]
        stamp = datetime.now().strftime("%H%M%S")
        out = self.backup_dir / f"{safe}_{keyid}_{stamp}.reg"
        res = run(["reg", "export", reg_path, str(out), "/y"], timeout=60)
        try:
            return res.rc == 0 and out.exists() and out.stat().st_size > 0
        except OSError:
            return False

    # ---------- Einzeldeinstallation ----------
    def uninstall(self, app: InstalledApp, deep: bool = False, silent: bool = True) -> bool:
        self.r.log(f"Entferne: {app.name} [{app.kind}]")
        install_location = self._install_location(app) if deep else ""
        try:
            if app.kind == "appx":
                ok = self._uninstall_appx(app)
            else:
                self._backup_reg(app.reg_path, app.name)
                ok = self._uninstall_win32(app, silent)
        except Exception as e:
            self.r.err(f"{app.name}: {e}")
            return False
        if ok and deep:
            self._deep_clean(app, install_location)
        if ok:
            self.r.ok(f"{app.name} entfernt.")
        else:
            self.r.warn(f"{app.name}: Uninstaller meldete Fehler/kein Ergebnis.")
        return ok

    def _uninstall_win32(self, app: InstalledApp, silent: bool) -> bool:
        cmd = app.quiet_uninstall or app.uninstall
        if not cmd:
            self.r.warn(f"{app.name}: keine UninstallString hinterlegt.")
            return False
        # MSI-Deinstallation still machen
        low = cmd.lower()
        if silent and "msiexec" in low:
            if "/i" in low:
                cmd = cmd.replace("/I", "/X").replace("/i", "/x")
            if "/quiet" not in low and "/qn" not in low:
                cmd += " /quiet /norestart"
        # Als STRING uebergeben: bei einer Liste wuerde subprocess die im
        # UninstallString enthaltenen Anfuehrungszeichen escapen ("\"C:\...")
        # und der Aufruf schluege fehl. /s laesst cmd die aeusseren Quotes
        # unveraendert stehen.
        res = run(f'cmd /s /c "{cmd}"', timeout=1200)
        return res.rc in (0, 1605, 3010)  # 1605=nicht installiert, 3010=Neustart noetig

    def _uninstall_appx(self, app: InstalledApp) -> bool:
        allusers = "-AllUsers" if self.admin else ""
        name = app.name.replace("'", "''")
        # Erfolg am ZUSTAND messen: PowerShell endet wegen
        # ErrorActionPreference='SilentlyContinue' auch dann mit 0, wenn das
        # Paket gar nicht entfernt werden konnte.
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"Get-AppxPackage {allusers} -Name '{name}' | Remove-AppxPackage {allusers};"
            "Get-AppxProvisionedPackage -Online | "
            f"Where-Object {{ $_.DisplayName -eq '{name}' }} | "
            "Remove-AppxProvisionedPackage -Online -AllUsers | Out-Null;"
            f"if (Get-AppxPackage {allusers} -Name '{name}') "
            "{ Write-Output 'STILL_PRESENT' } else { Write-Output 'REMOVED' }"
        )
        res = powershell(script, timeout=300)
        return "REMOVED" in (res.out or "")

    # ---------- Deep-Clean (Revo-Stil) ----------
    def _install_location(self, app: InstalledApp) -> str:
        if app.kind != "win32" or not app.reg_path:
            return ""
        # InstallLocation aus dem Uninstall-Key lesen
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"(Get-ItemProperty -Path 'Registry::{app.reg_path}').InstallLocation"
        )
        return (powershell(script, timeout=30).out or "").strip()

    def _deep_clean(self, app: InstalledApp, install_location: str) -> None:
        self.r.log(f"Suche Reste von {app.name} ...")
        token = _norm_token(app.name)
        removed = 0
        # 1) uebrig gebliebener Installationsordner - wird NICHT geloescht,
        #    sondern in den Backup-Ordner verschoben. So bleibt jeder Fehl-
        #    treffer umkehrbar (rmtree waere endgueltig).
        quarantine = self.backup_dir / "verschobene_ordner"
        for folder in self._leftover_folders(app, install_location, token):
            self.r.log(f"  Rest-Ordner: {folder}")
            try:
                quarantine.mkdir(parents=True, exist_ok=True)
                name = os.path.basename(folder.rstrip("\\/"))
                target = quarantine / name
                n = 1
                while target.exists():
                    target = quarantine / f"{name}_{n}"
                    n += 1
                shutil.move(folder, str(target))
                self.r.ok(f"  -> verschoben nach {target}")
                removed += 1
            except Exception as e:
                self.r.warn(f"  konnte {folder} nicht verschieben: {e}")
        # 2) uebrig gebliebene Registry-Schluessel (mit Backup vor dem Loeschen)
        for reg_path in self._leftover_regkeys(token):
            self.r.log(f"  Rest-Registry: {reg_path}")
            if not self._backup_reg(reg_path, f"leftover_{token}"):
                self.r.warn(f"  Backup fehlgeschlagen - {reg_path} bleibt bestehen.")
                continue
            run(["reg", "delete", reg_path, "/f"], timeout=30)
            removed += 1
        self.r.log(f"  {removed} Reste bereinigt (Backups in {self.backup_dir}).")

    def _leftover_folders(self, app: InstalledApp, install_location: str, token: str) -> list:
        found = []
        if install_location and os.path.isdir(install_location):
            # InstallLocation nur akzeptieren, wenn der Ordnername auch zum
            # Programm passt - sonst zeigt er evtl. auf einen Sammelordner.
            base_name = _norm_token(os.path.basename(install_location.rstrip("\\/")))
            if _safe_to_delete(install_location) and base_name == token:
                found.append(install_location)
        roots = [os.environ.get(v, "") for v in
                 ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ProgramFiles", "ProgramFiles(x86)")]
        for base in roots:
            if not base or not os.path.isdir(base):
                continue
            try:
                entries = os.listdir(base)
            except OSError:
                continue
            for entry in entries:
                # EXAKTER Token-Vergleich. Ein Teilstring-Match wuerde fremde,
                # noch installierte Produkte treffen (Discord -> DiscordCanary,
                # Brave -> BraveSoftware) und deren Daten vernichten.
                if not token or _norm_token(entry) != token:
                    continue
                full = os.path.join(base, entry)
                if os.path.isdir(full) and _safe_to_delete(full) and full not in found:
                    found.append(full)
        return found

    def _leftover_regkeys(self, token: str) -> list:
        if not token or len(token) < 4 or not IS_WINDOWS:
            return []
        import winreg
        found = []
        bases = [
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
        ]
        for hive, path in bases:
            hive_name = "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM"
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                    i += 1
                except OSError:
                    break
                if _norm_token(sub) == token:  # exakter Token-Treffer, keine Teilstrings
                    found.append(f"{hive_name}\\{path}\\{sub}")
            winreg.CloseKey(key)
        return found

    # ---------- Batch / Auto-Debloat ----------
    def uninstall_many(self, apps: list, deep: bool = False, silent: bool = True,
                       emit_done: bool = True) -> dict:
        self.prepare_safety()
        ok = 0
        fail = 0
        total = len(apps)
        for i, app in enumerate(apps):
            if self.r.cancelled:
                self.r.warn("Abgebrochen.")
                break
            self.r.status(f"Entferne {app.name} ...")
            if self.uninstall(app, deep=deep, silent=silent):
                ok += 1
            else:
                fail += 1
            self.r.progress((i + 1) / max(total, 1))
        result = {"ok": ok, "fail": fail, "backup": str(self.backup_dir)}
        if emit_done:
            self.r.done(result)
        return result

    def debloat_auto(self, deep: bool = False, remove_edge: bool = False) -> dict:
        """Ein-Klick: kuratierte Bloatware automatisch entfernen."""
        apps = mark_bloatware(list_installed(include_appx=True, reporter=self.r))
        targets = [a for a in apps if a.is_bloatware]
        self.r.head(f"Auto-Debloat: {len(targets)} Bloatware-Eintraege gefunden.")
        for t in targets:
            self.r.log(f"  - {t.name} [{t.kind}]")
        # done erst NACH dem Edge-Schritt melden - sonst zeigt die Oberflaeche
        # "Fertig", waehrend die Edge-Entfernung noch minutenlang laeuft.
        result = (self.uninstall_many(targets, deep=deep, emit_done=False)
                  if targets else {"ok": 0, "fail": 0})
        if remove_edge and not self.r.cancelled:
            self.r.status("Entferne Microsoft Edge ...")
            if self.remove_edge():
                result["ok"] = result.get("ok", 0) + 1
            else:
                result["fail"] = result.get("fail", 0) + 1
        self.r.done(result)
        return result

    # ---------- Microsoft Edge ----------
    def remove_edge(self) -> bool:
        self.r.head("Entferne Microsoft Edge (best effort) ...")
        # 1) winget-Versuch (klappt in manchen Regionen)
        res = run(["winget", "uninstall", "--id", "Microsoft.Edge", "-e",
                   "--accept-source-agreements", "--force"], timeout=600)
        if res.rc == 0:
            self.r.ok("Edge via winget entfernt.")
            return True
        # 2) Setup.exe des Edge-Installers mit --force-uninstall
        script = r"""
$ErrorActionPreference='SilentlyContinue'
$base = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application'
$setup = Get-ChildItem -Path $base -Recurse -Filter setup.exe -EA SilentlyContinue |
         Where-Object { $_.FullName -match 'Installer' } | Select-Object -First 1
if($setup){
  & $setup.FullName --uninstall --msedge --system-level --verbose-logging --force-uninstall
  Write-Output "EDGE_SETUP_RUN"
} else { Write-Output "EDGE_SETUP_NOT_FOUND" }
"""
        out = powershell(script, timeout=300)
        if "EDGE_SETUP_RUN" in (out.out or ""):
            self.r.ok("Edge-Deinstallation ausgeloest (Neustart empfohlen).")
            return True
        self.r.warn("Edge liess sich nicht automatisch entfernen "
                    "(von Microsoft teils gesperrt). Manuell pruefen.")
        return False


# ------------------------------------------------------------------
#  Hilfsfunktionen
# ------------------------------------------------------------------
_GENERIC = {"microsoft", "windows", "common", "update", "temp", "data",
            "app", "apps", "software", "program", "programs", "system"}


def _norm_token(name: str) -> str:
    """Vergleichs-Token: nur Buchstaben/Ziffern, kleingeschrieben.
    Liefert '' fuer generische/zu kurze Namen (verhindert Fehltreffer)."""
    t = "".join(c.lower() for c in str(name) if c.isalnum())
    if len(t) < 4 or t in _GENERIC:
        return ""
    return t


_ALLOWED_ROOTS = None

# Direkt unter diesen Wurzeln darf NICHT geloescht werden - dort liegen
# Sammelordner, die vielen Programmen gehoeren.
_NEVER_DELETE_NAMES = {
    "common files", "windows", "system32", "syswow64", "microsoft",
    "microsoft shared", "windowsapps", "packages", "temp", "microsoft.net",
    "internet explorer", "windows defender", "windows nt", "programdata",
    "start menu", "desktop", "documents", "downloads", "pictures", "music",
    "videos", "favorites",
}


def _allowed_roots() -> list:
    """Wurzeln, UNTER denen Programmordner liegen duerfen.

    Wichtig: SystemDrive ("C:") gehoert NICHT dazu - damit waere sonst jeder
    Pfad auf C: erlaubt, und der Schutz waere wirkungslos.
    """
    global _ALLOWED_ROOTS
    if _ALLOWED_ROOTS is None:
        vals = []
        for v in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                  "APPDATA", "LOCALAPPDATA"):
            p = os.environ.get(v)
            if p:
                vals.append(os.path.normcase(os.path.normpath(p)))
        _ALLOWED_ROOTS = vals
    return _ALLOWED_ROOTS


def _safe_to_delete(path: str) -> bool:
    """True nur fuer einen echten Programm-Unterordner einer bekannten Wurzel.

    Regeln (bewusst streng - hier wird unwiderruflich geloescht):
      * muss unterhalb von Program Files / ProgramData / AppData liegen
      * die Wurzel selbst und Sammelordner wie "Common Files" sind tabu
      * hoechstens zwei Ebenen tief (Hersteller\\Produkt), damit nicht
        versehentlich ein ganzer Herstellerbaum verschwindet
    """
    try:
        norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except Exception:
        return False
    if len(norm) < 4 or norm.endswith(os.sep):
        return False
    windir = os.path.normcase(os.path.normpath(os.environ.get("windir", r"C:\Windows")))
    if norm == windir or norm.startswith(windir + os.sep):
        return False          # niemals im Windows-Verzeichnis
    for root in _allowed_roots():
        if not norm.startswith(root + os.sep):
            continue
        rel = norm[len(root) + 1:]
        parts = [p for p in rel.split(os.sep) if p]
        if not parts or len(parts) > 2:
            return False      # Wurzel selbst bzw. zu tief verschachtelt
        if any(p in _NEVER_DELETE_NAMES for p in parts):
            return False
        return True
    return False
