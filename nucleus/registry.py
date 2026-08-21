"""Installierte Programme (Win32 via Registry) und AppX-Pakete (via PowerShell)."""
from __future__ import annotations

from dataclasses import dataclass

from .winutils import IS_WINDOWS, ps_json


@dataclass
class InstalledApp:
    name: str
    kind: str = "win32"            # 'win32' | 'appx'
    version: str = ""
    publisher: str = ""
    uninstall: str = ""            # UninstallString (win32)
    quiet_uninstall: str = ""      # QuietUninstallString (win32)
    package_full_name: str = ""    # PackageFullName (appx)
    package_family: str = ""       # PackageFamilyName (appx)
    reg_path: str = ""             # z.B. HKLM\...\Uninstall\<key>  (fuer .reg-Backup)
    est_size_mb: float = 0.0
    is_bloatware: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}:{self.version}".lower()


def _win32_apps() -> list:
    if not IS_WINDOWS:
        return []
    import winreg

    UNINST = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    UNINST32 = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
    roots = [
        # (Hive, Oeffnungs-Pfad, View, Anzeige-Pfad fuer reg export)
        (winreg.HKEY_LOCAL_MACHINE, UNINST, winreg.KEY_WOW64_64KEY, f"HKLM\\{UNINST}"),
        (winreg.HKEY_LOCAL_MACHINE, UNINST, winreg.KEY_WOW64_32KEY, f"HKLM\\{UNINST32}"),
        (winreg.HKEY_CURRENT_USER,  UNINST, 0, f"HKCU\\{UNINST}"),
    ]
    found: dict = {}
    for hive, path, view, disp_path in roots:
        try:
            base = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(base, i)
                i += 1
            except OSError:
                break
            try:
                sk = winreg.OpenKey(base, sub)
            except OSError:
                continue

            def gv(vn):
                try:
                    return winreg.QueryValueEx(sk, vn)[0]
                except OSError:
                    return None

            name = gv("DisplayName")
            if not name:
                continue
            if gv("SystemComponent") == 1:
                continue
            if gv("ParentKeyName"):  # Updates/Patches ausblenden
                continue
            if gv("ReleaseType") in ("Security Update", "Update", "Hotfix"):
                continue
            try:
                size_kb = float(gv("EstimatedSize") or 0)
            except (TypeError, ValueError):
                size_kb = 0.0
            app = InstalledApp(
                name=str(name).strip(),
                kind="win32",
                version=str(gv("DisplayVersion") or ""),
                publisher=str(gv("Publisher") or ""),
                uninstall=str(gv("UninstallString") or ""),
                quiet_uninstall=str(gv("QuietUninstallString") or ""),
                reg_path=f"{disp_path}\\{sub}",
                est_size_mb=round(size_kb / 1024, 1),
            )
            found[(app.name.lower(), app.version)] = app
    return list(found.values())


def _appx_apps(reporter=None) -> list:
    """AppX-Pakete auslesen. Meldet ueber den Reporter, wenn PowerShell nichts
    liefert - sonst sieht ein Fehler aus wie 'keine Bloatware vorhanden'."""
    if not IS_WINDOWS:
        return []
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-AppxPackage | Where-Object { $_.NonRemovable -ne $true -and $_.IsFramework -ne $true } |"
        "Select-Object Name, PackageFullName, PackageFamilyName, Publisher, Version |"
        "ConvertTo-Json -Depth 3 -Compress"
    )
    raw = ps_json(script, timeout=120)
    if not raw and reporter is not None:
        reporter.warn("Konnte die Windows-Apps (AppX) nicht auslesen - die "
                      "Liste zeigt daher nur klassische Programme.")
    apps = []
    for it in raw:
        nm = str(it.get("Name") or "").strip()
        if not nm:
            continue
        apps.append(InstalledApp(
            name=nm,
            kind="appx",
            version=str(it.get("Version") or ""),
            publisher=str(it.get("Publisher") or ""),
            package_full_name=str(it.get("PackageFullName") or ""),
            package_family=str(it.get("PackageFamilyName") or ""),
        ))
    return apps


def list_installed(include_appx: bool = True, reporter=None) -> list:
    """Alle deinstallierbaren Programme + optional AppX-Pakete, alphabetisch."""
    apps = _win32_apps()
    if include_appx:
        apps += _appx_apps(reporter)
    apps.sort(key=lambda a: a.name.lower())
    return apps
