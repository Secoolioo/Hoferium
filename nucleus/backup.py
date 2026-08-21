"""BackupJob - sichert Browserdaten, Windows-Key, WLAN, Programme, Treiber u.v.m.

Jeder Sammelschritt ist gekapselt und faengt seine eigenen Fehler ab, damit
ein Fehlschlag nicht die restliche Sicherung abbricht. robocopy-Ergebnisse
werden EHRLICH ausgewertet (kein 'OK' bei fehlgeschlagener Kopie).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import registry
from .context import RunContext
from .winutils import (
    copy_path, dir_size_bytes, known_folders, onedrive_root,
    powershell, robocopy,
)

def ps_quote(path) -> str:
    """Pfad als einfach gequoteter PowerShell-String.

    In einfachen Anfuehrungszeichen expandiert PowerShell weder $ noch Backtick;
    ein enthaltenes Apostroph wird durch Verdoppeln maskiert. Ohne das bricht
    z. B. der Pfad "E:\\Peters' Stick" das Skript mitten entzwei.
    """
    return "'" + str(path).replace("'", "''") + "'"


CHROMIUM_CACHE_EXCLUDES = [
    "Cache", "Cache2", "cache2", "Code Cache", "GPUCache", "ShaderCache",
    "GrShaderCache", "Service Worker", "Crashpad", "component_crx_cache",
    "optimization_guide_model_store", "DawnCache", "DawnGraphiteCache",
]
FIREFOX_CACHE_EXCLUDES = ["cache2", "startupCache", "OfflineCache", "thumbnails"]


@dataclass
class BackupOptions:
    system: bool = True
    product_key: bool = True
    wifi: bool = True
    programs: bool = True
    browsers: bool = True
    sticky_notes: bool = True
    mail: bool = True
    drivers: bool = True
    misc: bool = True
    user_files: bool = False  # persoenliche Ordner mitkopieren (kann sehr gross sein)


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


# ------------------------------------------------------------------
#  Chromium-Lesezeichen -> importierbare Netscape-HTML
# ------------------------------------------------------------------
def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _chrome_time_to_unix(micro) -> str:
    try:
        v = int(micro)
        return str(int(v / 1_000_000 - 11644473600))
    except (TypeError, ValueError):
        return "0"


def _node_to_html(node, indent: int = 8) -> list:
    out: list = []
    pad = " " * indent
    for ch in (node.get("children") or []):
        if not isinstance(ch, dict):
            continue
        t = ch.get("type")
        if t == "url":
            out.append(
                f'{pad}<DT><A HREF="{_esc(ch.get("url", ""))}" '
                f'ADD_DATE="{_chrome_time_to_unix(ch.get("date_added", "0"))}">'
                f'{_esc(ch.get("name", ""))}</A>'
            )
        elif t == "folder":
            out.append(
                f'{pad}<DT><H3 ADD_DATE="{_chrome_time_to_unix(ch.get("date_added", "0"))}">'
                f'{_esc(ch.get("name", ""))}</H3>'
            )
            out.append(f"{pad}<DL><p>")
            out.extend(_node_to_html(ch, indent + 4))
            out.append(f"{pad}</DL><p>")
    return out


def bookmarks_json_to_html(data: dict) -> str:
    roots = (data or {}).get("roots", {}) or {}
    body: list = []
    # bookmark_bar bekommt PERSONAL_TOOLBAR_FOLDER, damit der Import die
    # Lesezeichenleiste korrekt zuordnet.
    order = [
        ("bookmark_bar", "Lesezeichenleiste", True),
        ("other", "Weitere Lesezeichen", False),
        ("synced", "Mobile Lesezeichen", False),
    ]
    for key, title, is_toolbar in order:
        node = roots.get(key)
        if not node:
            continue
        attr = ' PERSONAL_TOOLBAR_FOLDER="true"' if is_toolbar else ""
        body.append(f"    <DT><H3{attr}>{_esc(title)}</H3>")
        body.append("    <DL><p>")
        body.extend(_node_to_html(node))
        body.append("    </DL><p>")
    return (
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n"
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">\n'
        "<TITLE>Bookmarks</TITLE>\n<H1>Bookmarks</H1>\n<DL><p>\n"
        + "\n".join(body) + "\n</DL><p>\n"
    )


# Wo die Passwortverwaltung im jeweiligen Browser sitzt. Der Assistent oeffnet
# genau diese Seite - der Nutzer muss dann nur noch "Exportieren" klicken.
PASSWORD_PAGES = [
    ("Chrome", ["Google/Chrome/Application/chrome.exe"],
     "chrome://password-manager/settings"),
    ("Edge", ["Microsoft/Edge/Application/msedge.exe"],
     "edge://settings/passwords"),
    ("Brave", ["BraveSoftware/Brave-Browser/Application/brave.exe"],
     "brave://password-manager/settings"),
    ("Vivaldi", ["Vivaldi/Application/vivaldi.exe"],
     "vivaldi://password-manager/settings"),
    ("Opera", ["Opera/launcher.exe", "Opera/opera.exe"],
     "opera://settings/passwords"),
    ("Firefox", ["Mozilla Firefox/firefox.exe"], "about:logins"),
]


def find_browser_exe(rel_paths: list) -> str:
    """Sucht eine Browser-Programmdatei in den ueblichen Installationsorten."""
    roots = [os.environ.get(v, "") for v in
             ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    for root in roots:
        if not root:
            continue
        for rel in rel_paths:
            cand = os.path.join(root, *rel.split("/"))
            if os.path.isfile(cand):
                return cand
    return ""


def installed_password_browsers() -> list:
    """(Name, exe, url) fuer jeden installierten Browser."""
    found = []
    for name, rels, url in PASSWORD_PAGES:
        exe = find_browser_exe(rels)
        if exe:
            found.append((name, exe, url))
    return found


def _chromium_browsers() -> list:
    """(Name, User-Data-Pfad) fuer alle gefundenen Chromium-Browser -
    bekannte + dynamisch am Merkmal 'User Data\\Local State' erkannte."""
    la = os.environ.get("LOCALAPPDATA", "")
    ap = os.environ.get("APPDATA", "")
    known = [
        ("Chrome", os.path.join(la, "Google", "Chrome", "User Data")),
        ("Edge", os.path.join(la, "Microsoft", "Edge", "User Data")),
        ("Brave", os.path.join(la, "BraveSoftware", "Brave-Browser", "User Data")),
        ("Vivaldi", os.path.join(la, "Vivaldi", "User Data")),
        ("Chromium", os.path.join(la, "Chromium", "User Data")),
        ("Yandex", os.path.join(la, "Yandex", "YandexBrowser", "User Data")),
        ("Opera", os.path.join(ap, "Opera Software", "Opera Stable")),
        ("OperaGX", os.path.join(ap, "Opera Software", "Opera GX Stable")),
    ]
    found: dict = {}
    for name, path in known:
        if os.path.isdir(path):
            found.setdefault(os.path.normcase(path), (name, path))
    for base in (la, ap):
        if not base or not os.path.isdir(base):
            continue
        try:
            vendors = os.listdir(base)
        except OSError:
            continue
        for v in vendors:
            vp = os.path.join(base, v)
            if not os.path.isdir(vp):
                continue
            cand = os.path.join(vp, "User Data")
            if os.path.isfile(os.path.join(cand, "Local State")):
                found.setdefault(os.path.normcase(cand), (v, cand))
                continue
            try:
                subs = os.listdir(vp)
            except OSError:
                continue
            for s in subs:
                cand2 = os.path.join(vp, s, "User Data")
                if os.path.isfile(os.path.join(cand2, "Local State")):
                    found.setdefault(os.path.normcase(cand2), (f"{v}_{s}", cand2))
    return list(found.values())


# ------------------------------------------------------------------
#  BackupJob
# ------------------------------------------------------------------
class BackupJob:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.r = ctx.reporter
        self.results: list = []

    def run(self, opt: BackupOptions) -> dict:
        steps = []
        if opt.system:        steps.append(("System-Infos", self._system))
        if opt.product_key:   steps.append(("Windows-Key & Aktivierung", self._product_key))
        if opt.wifi:          steps.append(("WLAN-Profile & Passwoerter", self._wifi))
        if opt.programs:      steps.append(("Installierte Programme", self._programs))
        if opt.browsers:      steps.append(("Browser-Daten", self._browsers))
        if opt.sticky_notes:  steps.append(("Sticky Notes", self._sticky_notes))
        if opt.mail:          steps.append(("E-Mail-Profile", self._mail))
        if opt.misc:          steps.append(("Weitere Infos", self._misc))
        if opt.user_files:    steps.append(("Persoenliche Dateien", self._user_files))
        else:                 steps.append(("Persoenliche Dateien (nur Inventar)", self._user_inventory))
        if opt.drivers:       steps.append(("Treiber-Export", self._drivers))

        total = len(steps)
        self.r.head(f"Sicherung startet -> {self.ctx.output_dir}")
        for i, (name, fn) in enumerate(steps):
            if self.r.cancelled:
                self.r.warn("Abgebrochen.")
                break
            self.r.status(f"{name} ...")
            self.r.log(f"--- {name} ---", "head")
            try:
                fn()
                self.results.append(StepResult(name, True))
                self.r.ok(f"{name}: fertig")
            except Exception as e:  # pragma: no cover - defensiv
                self.results.append(StepResult(name, False, str(e)))
                self.r.err(f"{name}: FEHLER - {e}")
            self.r.progress((i + 1) / total)

        self._write_restore_guide()
        summary = self._write_summary()
        self.r.done(summary)
        return summary

    def _ps(self, script: str, timeout: int, what: str) -> str:
        """PowerShell ausfuehren und das Ergebnis EHRLICH bewerten.
        Eine leere Ausgabe wird gemeldet, statt still eine 0-Byte-Datei
        zu schreiben und den Schritt als erledigt zu verbuchen."""
        res = powershell(script, timeout=timeout)
        text = res.out or ""
        if res.rc != 0 or not text.strip():
            detail = (res.err or "").strip()[:150] or f"rc={res.rc}"
            self.r.warn(f"{what}: keine verwertbare Ausgabe ({detail})")
        return text

    # ---------- einzelne Schritte ----------
    def _system(self) -> None:
        d = self.ctx.sub("01_System")
        script = r"""
$ErrorActionPreference='SilentlyContinue'
$os=Get-CimInstance Win32_OperatingSystem
$cs=Get-CimInstance Win32_ComputerSystem
$bios=Get-CimInstance Win32_BIOS
$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1
$ram=[math]::Round($cs.TotalPhysicalMemory/1GB,1)
$o=@()
$o+="Computername   : $($cs.Name)"
$o+="Benutzer       : $env:USERNAME"
$o+="Windows        : $($os.Caption)  $($os.Version)  (Build $($os.BuildNumber))"
$o+="Architektur    : $($os.OSArchitecture)"
$o+="Installiert am : $($os.InstallDate)"
$o+="Hersteller     : $($cs.Manufacturer)"
$o+="Modell         : $($cs.Model)"
$o+="Seriennummer   : $($bios.SerialNumber)"
$o+="BIOS-Version   : $($bios.SMBIOSBIOSVersion)"
$o+="CPU            : $($cpu.Name)"
$o+="RAM (GB)       : $ram"
$o -join "`r`n"
"""
        self._write_text(d / "system-info.txt",
                         self._ps(script, 120, "System-Infos"))
        self._write_text(d / "systeminfo_voll.txt",
                         self._ps("systeminfo", 180, "systeminfo"))
        self._write_text(d / "netzwerk_ipconfig.txt",
                         self._ps("ipconfig /all", 60, "ipconfig"))

    def _product_key(self) -> None:
        d = self.ctx.sub("02_Windows-Key")
        script = r"""
$ErrorActionPreference='SilentlyContinue'
$oem=(Get-CimInstance -ClassName SoftwareLicensingService).OA3xOriginalProductKey
if([string]::IsNullOrWhiteSpace($oem)){$oem='(kein Key im BIOS/UEFI hinterlegt)'}
$o=@()
$o+="OEM/BIOS-Produktschluessel : $oem"
$o+=""
$lic=Get-CimInstance SoftwareLicensingProduct -Filter "ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f' AND PartialProductKey IS NOT NULL"
foreach($p in $lic){
  $st=switch($p.LicenseStatus){0{'Nicht lizenziert'}1{'Aktiviert'}2{'Out-of-Box Grace'}3{'Out-of-Tolerance Grace'}4{'Non-Genuine Grace'}5{'Benachrichtigung'}6{'Extended Grace'}default{"Status $($p.LicenseStatus)"}}
  $o+="Produkt        : $($p.Name)"
  $o+="Teil-Key       : $($p.PartialProductKey)"
  $o+="Status         : $st"
  $o+=""
}
$o+="Hinweis: Bei vorinstalliertem Windows steckt der echte Key im BIOS und"
$o+="wird bei einer Neuinstallation automatisch uebernommen."
$o -join "`r`n"
"""
        self._write_text(d / "windows-key.txt",
                         self._ps(script, 120, "Windows-Key"))
        self._write_text(d / "aktivierung_detail.txt", self._ps(
            r'cscript //nologo "$env:windir\System32\slmgr.vbs" /dlv', 120, "Aktivierung"))

    def _wifi(self) -> None:
        d = self.ctx.sub("03_WLAN")
        xml_dir = d / "profile_xml"
        xml_dir.mkdir(parents=True, exist_ok=True)
        # XML-Export (fuer Re-Import) inkl. Klartext-Passwort
        exp = powershell(
            f"netsh wlan export profile key=clear folder={ps_quote(xml_dir)} | Out-Null",
            timeout=120)
        if exp.rc != 0:
            self.r.warn(f"WLAN-Export meldete einen Fehler (rc={exp.rc}).")
        rows = []
        import xml.etree.ElementTree as ET
        for xf in sorted(xml_dir.glob("*.xml")):
            try:
                root = ET.parse(str(xf)).getroot()
                ssid = None
                pw = None
                protected = None
                for el in root.iter():
                    local = el.tag.split("}")[-1]  # Namespace ignorieren
                    if local == "name" and ssid is None:
                        ssid = (el.text or "").strip()
                    elif local == "keyMaterial" and pw is None:
                        pw = (el.text or "").strip()
                    elif local == "protected" and protected is None:
                        protected = (el.text or "").strip().lower()
                ssid = ssid or xf.stem
                # Bei protected=true steht dort KEIN Passwort, sondern ein
                # verschluesselter DPAPI-Blob - den duerfen wir nicht als
                # Passwort ausgeben.
                if protected == "true":
                    pw = "(verschluesselt - Klartext-Export nicht moeglich)"
                    self.r.warn(f"{ssid}: Passwort nicht im Klartext exportierbar.")
                elif not pw:
                    pw = "(offen / kein Passwort)"
                rows.append((ssid, pw))
            except Exception:
                continue
        lines = ["WLAN-Profile und Passwoerter", "=" * 30, ""]
        if rows:
            for ssid, pw in rows:
                lines.append(f"SSID     : {ssid}")
                lines.append(f"Passwort : {pw}")
                lines.append("")
        else:
            lines.append("Keine WLAN-Profile gefunden.")
        self._write_text(d / "wlan-passwoerter.txt", "\r\n".join(lines))

    def _programs(self) -> None:
        d = self.ctx.sub("04_Programme")
        apps = registry.list_installed(include_appx=False)
        # Textliste
        lines = [f"{'Name':<55}{'Version':<20}{'Herausgeber'}", "-" * 100]
        for a in apps:
            lines.append(f"{a.name[:54]:<55}{a.version[:19]:<20}{a.publisher}")
        self._write_text(d / "programme.txt", "\r\n".join(lines))
        # CSV
        import csv
        try:
            with (d / "programme.csv").open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh, delimiter=";")
                w.writerow(["Name", "Version", "Herausgeber", "GroesseMB"])
                for a in apps:
                    w.writerow([a.name, a.version, a.publisher, a.est_size_mb])
        except Exception as e:
            self.r.warn(f"CSV-Export: {e}")
        # winget-Export (fuer 1-Klick-Wiederherstellung), falls vorhanden
        wg = powershell(
            f'if(Get-Command winget -EA SilentlyContinue){{ '
            f'winget export -o {ps_quote(d / "winget-programme.json")} '
            f'--accept-source-agreements | Out-Null }}',
            timeout=180,
        )
        self.r.log(f"{len(apps)} Programme erfasst.")

    def _browsers(self) -> None:
        base = self.ctx.sub("05_Browser")
        browsers = _chromium_browsers()
        bm_dir = base / "_Lesezeichen_HTML"
        bm_dir.mkdir(parents=True, exist_ok=True)
        n = len(browsers)
        for idx, (name, path) in enumerate(browsers):
            self.r.log(f"Sichere {name} ...")
            res = robocopy(path, base / name, exclude_dirs=CHROMIUM_CACHE_EXCLUDES)
            if not res.ok:
                self.r.warn(f"{name}: Kopie unvollstaendig (robocopy-Code {res.rc}) - evtl. Browser offen?")
            self._export_bookmarks(name, path, bm_dir)
        # Firefox: kompletten Ordner (inkl. profiles.ini/installs.ini) sichern
        ff_root = os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox")
        if os.path.isdir(ff_root):
            self.r.log("Sichere Firefox ...")
            res = robocopy(ff_root, base / "Firefox", exclude_dirs=FIREFOX_CACHE_EXCLUDES)
            if not res.ok:
                self.r.warn(f"Firefox: Kopie unvollstaendig (Code {res.rc}) - evtl. Browser offen?")
        self._write_password_hint(base)

    def _export_bookmarks(self, name: str, user_data: str, bm_dir: Path) -> None:
        # Bookmarks liegen je Profil (Default, Profile 1, ...) oder direkt (Opera)
        candidates = []
        root_bm = os.path.join(user_data, "Bookmarks")
        if os.path.isfile(root_bm):
            candidates.append(("Default", root_bm))
        try:
            for entry in os.listdir(user_data):
                p = os.path.join(user_data, entry, "Bookmarks")
                if os.path.isfile(p):
                    candidates.append((entry, p))
        except OSError:
            pass
        for prof, bmfile in candidates:
            safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in prof)
            try:
                with open(bmfile, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                html = bookmarks_json_to_html(data)
                self._write_text(bm_dir / f"{name}_{safe}_Lesezeichen.html", html)
                self.r.ok(f"Lesezeichen-HTML: {name} / {prof}")
            except Exception as e:
                self.r.warn(f"Lesezeichen {name}/{prof} nicht lesbar: {e}")

    def _sticky_notes(self) -> None:
        d = self.ctx.sub("09_Sticky-Notes")
        src = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Packages",
            "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe", "LocalState",
        )
        if os.path.isdir(src):
            res = copy_path(src, d)
            if res.ok:
                self.r.ok("Sticky Notes (plum.sqlite) gesichert.")
            else:
                self.r.warn(f"Sticky Notes: {res.err or res.rc}")
        else:
            self.r.log("Keine Sticky Notes gefunden.")

    def _mail(self) -> None:
        d = self.ctx.sub("10_EMail")
        ap = os.environ.get("APPDATA", "")
        la = os.environ.get("LOCALAPPDATA", "")
        tb = os.path.join(ap, "Thunderbird")
        if os.path.isdir(tb):
            self.r.log("Sichere Thunderbird ...")
            res = robocopy(tb, d / "Thunderbird",
                           exclude_dirs=["Cache", "cache2", "startupCache"])
            if not res.ok:
                self.r.warn(f"Thunderbird: Code {res.rc} - evtl. offen?")
        # Outlook: nur portable .pst kopieren (.ost ist nicht wiederherstellbar)
        pst_found = 0
        search_roots = [
            os.path.join(la, "Microsoft", "Outlook"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "Outlook-Dateien"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "Outlook Files"),
        ]
        pst_dir = d / "Outlook_PST"
        for rootp in search_roots:
            if not os.path.isdir(rootp):
                continue
            try:
                for f in os.listdir(rootp):
                    if not f.lower().endswith(".pst"):
                        continue
                    res = copy_path(os.path.join(rootp, f), pst_dir)
                    if res.ok:
                        pst_found += 1
                    else:
                        self.r.warn(f"PST {f} NICHT gesichert "
                                    f"({res.err or res.rc}) - Outlook geschlossen?")
            except OSError:
                pass
        if pst_found:
            self.r.ok(f"{pst_found} Outlook-PST-Datei(en) gesichert.")
        if not os.path.isdir(tb) and not pst_found:
            self.r.log("Keine lokalen Mail-Profile gefunden.")

    def _misc(self) -> None:
        d = self.ctx.sub("08_Sonstiges")
        # hosts
        copy_path(os.path.join(os.environ.get("windir", r"C:\Windows"),
                               "System32", "drivers", "etc", "hosts"), d)
        # __DIR__ wird durch den Zielordner ersetzt (kein $args ueber stdin noetig)
        script = r"""
$ErrorActionPreference='SilentlyContinue'
$f = '__DIR__\windows-anmeldeinfos.txt'
'=== Gespeicherte Anmeldeinfos (nur Namen) ===' | Out-File $f -Encoding UTF8
cmdkey /list | Out-File $f -Append -Encoding UTF8
if(Get-Command Get-LocalUser -EA SilentlyContinue){
  '' | Out-File $f -Append -Encoding UTF8
  '=== Lokale Benutzerkonten ===' | Out-File $f -Append -Encoding UTF8
  Get-LocalUser | Select-Object Name,Enabled,LastLogon | Format-Table -Auto | Out-File $f -Append -Encoding UTF8
}
Get-CimInstance Win32_Printer | Select-Object Name,DriverName,PortName | Format-Table -Auto | Out-File '__DIR__\drucker.txt' -Encoding UTF8 -Width 400
Get-CimInstance Win32_MappedLogicalDisk | Select-Object DeviceID,ProviderName | Format-Table -Auto | Out-File '__DIR__\netzlaufwerke.txt' -Encoding UTF8 -Width 400
Get-ChildItem Env: | Sort-Object Name | Format-Table -Auto | Out-File '__DIR__\umgebungsvariablen.txt' -Encoding UTF8 -Width 400
Get-Service | Select-Object Status,Name,DisplayName | Sort-Object Name | Format-Table -Auto | Out-File '__DIR__\dienste.txt' -Encoding UTF8 -Width 400
"""
        # Apostrophe im Zielpfad verdoppeln, sonst bricht der PS-String auf
        powershell(script.replace("__DIR__", str(d).replace("'", "''")), timeout=180)

    def _user_inventory(self) -> None:
        d = self.ctx.sub("06_Eigene-Dateien")
        self._write_text(d / "inventar.txt", self._build_inventory(copy=False))

    def _user_files(self) -> None:
        d = self.ctx.sub("06_Eigene-Dateien")
        folders = known_folders()
        out_abs = os.path.normcase(os.path.abspath(str(self.ctx.output_dir)))
        inv_lines = ["Persoenliche Ordner - kopiert", "=" * 40, ""]
        for name, src in folders.items():
            # Liegt das Sicherungsziel INNERHALB der Quelle, wuerde robocopy
            # sich selbst mitkopieren - endlos, bis der Datentraeger voll ist.
            src_abs = os.path.normcase(os.path.abspath(src))
            if out_abs == src_abs or out_abs.startswith(src_abs + os.sep):
                self.r.warn(f"{name} uebersprungen - das Sicherungsziel liegt "
                            f"in diesem Ordner ({src}).")
                inv_lines.append(f"{name:<12} {'':>8}     [UEBERSPRUNGEN]  {src}")
                continue
            size_gb = round(dir_size_bytes(src) / (1024 ** 3), 2)
            self.r.log(f"Kopiere {name} ({size_gb} GB) ...")
            res = robocopy(src, d / name)
            state = "OK" if res.ok else f"FEHLER (Code {res.rc})"
            if not res.ok:
                self.r.warn(f"{name}: Kopie fehlgeschlagen (Code {res.rc})")
            inv_lines.append(f"{name:<12} {size_gb:>8} GB  [{state}]  {src}")
        od = onedrive_root()
        if od:
            inv_lines.append("")
            inv_lines.append(f"OneDrive-Ordner (cloud-synchron): {od}")
            inv_lines.append("Nicht mitkopiert - liegt bereits in der Cloud.")
        self._write_text(d / "inventar.txt", "\r\n".join(inv_lines))

    def _build_inventory(self, copy: bool) -> str:
        folders = known_folders()
        lines = ["Persoenliche Ordner - Inventar", "=" * 40, ""]
        for name, src in folders.items():
            size_gb = round(dir_size_bytes(src) / (1024 ** 3), 2)
            lines.append(f"{name:<12} {size_gb:>8} GB   {src}")
        od = onedrive_root()
        if od:
            lines.append("")
            lines.append(f"OneDrive (cloud-synchron): {od}")
        lines.append("")
        lines.append("Hinweis: Diese Ordner wurden NUR aufgelistet, nicht kopiert.")
        lines.append("Zum Mitkopieren im Programm 'Persoenliche Dateien mitkopieren' aktivieren.")
        return "\r\n".join(lines)

    def _drivers(self) -> None:
        d = self.ctx.sub("07_Treiber")
        if not self.ctx.admin:
            self.r.warn("Treiber-Export braucht Administrator-Rechte - uebersprungen.")
            return
        self.r.log("Exportiere Treiber (kann etwas dauern) ...")
        res = powershell(
            f"Export-WindowsDriver -Online -Destination {ps_quote(d)} | Out-Null",
            timeout=1800)
        if res.rc != 0:
            raise RuntimeError(res.err or "Export-WindowsDriver fehlgeschlagen")
        if not any(d.iterdir()):
            raise RuntimeError("Treiber-Export lieferte keine Dateien")

    # ---------- Abschluss ----------
    def _write_summary(self) -> dict:
        ok = [r for r in self.results if r.ok]
        fail = [r for r in self.results if not r.ok]
        lines = [
            "SICHERUNG ABGESCHLOSSEN",
            "=" * 40,
            f"PC       : {os.environ.get('COMPUTERNAME', '')}",
            f"Benutzer : {os.environ.get('USERNAME', '')}",
            f"Ordner   : {self.ctx.output_dir}",
            f"Zeit     : {datetime.now():%Y-%m-%d %H:%M:%S}",
            "",
            "Ergebnisse:",
        ]
        for r in self.results:
            mark = "[OK]  " if r.ok else "[FEHL]"
            extra = f"  ({r.detail})" if r.detail else ""
            lines.append(f"  {mark} {r.name}{extra}")
        self._write_text(self.ctx.output_dir / "ZUSAMMENFASSUNG.txt", "\r\n".join(lines))
        return {"ok": len(ok), "fail": len(fail),
                "failed": [r.name for r in fail], "dir": str(self.ctx.output_dir)}

    # ---------- Helfer ----------
    def _write_text(self, path: Path, text: str) -> None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text or "", encoding="utf-8")
        except Exception as e:
            self.r.warn(f"Schreiben {path.name}: {e}")

    def _write_restore_guide(self) -> None:
        """Schritt-fuer-Schritt-Anleitung fuer den frisch aufgesetzten PC."""
        root = self.ctx.output_dir
        csvs = list((root / "05_Browser").glob("**/*.csv")) if (root / "05_Browser").exists() else []
        pw_state = (f"{len(csvs)} Passwort-Datei(en) gefunden."
                    if csvs else
                    "ACHTUNG: Es wurde KEINE Passwort-Datei (CSV) gefunden.")
        text = f"""
SO HOLST DU ALLES ZURUECK
=========================
Sicherung vom {datetime.now():%d.%m.%Y %H:%M} - PC: {os.environ.get('COMPUTERNAME', '')}

Reihenfolge: erst Windows einrichten, dann Browser installieren, dann diese
Schritte durchgehen.


1) LESEZEICHEN
   Ordner: 05_Browser\\_Lesezeichen_HTML\\
   Fuer jeden Browser liegt dort eine .html-Datei.
   Chrome/Edge/Brave:  Einstellungen -> Lesezeichen -> Lesezeichen importieren
                       -> "HTML-Datei" waehlen -> die passende Datei oeffnen.
   Firefox:            Lesezeichen verwalten (Strg+Umschalt+O) ->
                       Importieren und Sichern -> Lesezeichen aus HTML importieren.


2) PASSWOERTER
   {pw_state}

   Chrome/Edge/Brave/Opera/Vivaldi:
     Die Passwoerter aus dem Profil lassen sich NICHT uebertragen - sie waren
     an das alte Windows-Konto gebunden. Nutzbar ist nur eine CSV-Datei, die
     VOR der Neuinstallation exportiert wurde (Hoferium bietet dafuer den
     Passwort-Assistenten auf der Seite Datensicherung).
     Import: Einstellungen -> Passwoerter -> Drei-Punkte-Menue -> Importieren.
     Danach die CSV-Datei loeschen - darin stehen alle Passwoerter im Klartext!

   Firefox:
     Hier geht es ohne Export: den gesicherten Ordner
        05_Browser\\Firefox\\
     nach  %APPDATA%\\Mozilla\\Firefox\\  zurueckkopieren (Firefox vorher
     schliessen). Danach sind Passwoerter, Lesezeichen und Verlauf wieder da.

   Am bequemsten fuer die Zukunft: im Browser die Synchronisierung mit einem
   Konto einschalten - dann sind die Passwoerter nach jeder Neuinstallation
   von allein wieder da.


3) WLAN
   Ordner: 03_WLAN\\
   Die Passwoerter stehen lesbar in wlan-passwoerter.txt.
   Alle Netze auf einmal zurueckspielen (eingabeaufforderung als Admin):
       netsh wlan add profile filename="PFAD\\profile_xml\\NAME.xml" user=all


4) WINDOWS-SCHLUESSEL
   Ordner: 02_Windows-Key\\windows-key.txt
   Bei vorinstalliertem Windows wird der Schluessel automatisch aus dem BIOS
   uebernommen - meist ist gar keine Eingabe noetig.


5) PROGRAMME
   Ordner: 04_Programme\\
   programme.txt / programme.csv  = Liste dessen, was installiert war.
   winget-programme.json          = falls vorhanden, alles auf einmal setzen:
       winget import -i winget-programme.json --accept-package-agreements


6) EIGENE DATEIEN
   Ordner: 06_Eigene-Dateien\\
   Enthaelt die Kopien (oder in inventar.txt die Liste, was wo lag).


7) WEITERES
   09_Sticky-Notes\\  -> Inhalt nach
        %LOCALAPPDATA%\\Packages\\Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe\\LocalState\\
   10_EMail\\Thunderbird\\ -> nach %APPDATA%\\Thunderbird\\
   10_EMail\\Outlook_PST\\ -> .pst in Outlook oeffnen (Datei -> Oeffnen)
   07_Treiber\\ -> nur noetig, wenn ein Geraet nach der Installation fehlt:
        pnputil /add-driver "PFAD\\*.inf" /subdirs /install
"""
        self._write_text(root / "WIEDERHERSTELLEN.txt", text.lstrip())

    def _write_password_hint(self, base: Path) -> None:
        txt = (
            "PASSWOERTER - BITTE LESEN\n"
            "=========================\n"
            "Diese Sicherung enthaelt die KOMPLETTEN Browser-Profile.\n\n"
            "* Firefox: Passwoerter im Profil (logins.json + key4.db), direkt\n"
            "  wiederherstellbar durch Zurueckkopieren des Firefox-Ordners.\n\n"
            "* Chrome/Edge/Brave/Opera/Vivaldi: Passwoerter sind mit dem\n"
            "  Windows-Konto (DPAPI) verschluesselt und nach einer NEUEN\n"
            "  Windows-Installation nicht mehr entschluesselbar. Deshalb VOR\n"
            "  dem Plattmachen zusaetzlich einmal manuell exportieren:\n"
            "     Einstellungen -> Passwoerter -> Passwoerter exportieren (CSV)\n"
            "  oder Browser-Sync/Konto aktivieren.\n\n"
            "Die Lesezeichen liegen zusaetzlich als importierbare HTML im\n"
            "Ordner _Lesezeichen_HTML.\n"
        )
        self._write_text(base / "_PASSWOERTER_LIESMICH.txt", txt)
