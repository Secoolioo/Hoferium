"""Sammelt die wichtigsten Eckdaten des PCs fuer die Startseite.

Alles laeuft in EINEM PowerShell-Aufruf und kommt als JSON zurueck - das ist
deutlich schneller als ein Dutzend Einzelabfragen und blockiert die
Oberflaeche nicht, weil der Aufruf in einem Hintergrund-Thread passiert.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .winutils import IS_WINDOWS, powershell

_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$os   = Get-CimInstance Win32_OperatingSystem
$cs   = Get-CimInstance Win32_ComputerSystem
$bios = Get-CimInstance Win32_BIOS
$bb   = Get-CimInstance Win32_BaseBoard
$cpu  = Get-CimInstance Win32_Processor | Select-Object -First 1
$gpus = @(Get-CimInstance Win32_VideoController | Where-Object { $_.Name } |
          ForEach-Object { "$($_.Name) ($([math]::Round($_.AdapterRAM/1GB,1)) GB)" })
$ramMods = @(Get-CimInstance Win32_PhysicalMemory)
$ramSum  = ($ramMods | Measure-Object -Property Capacity -Sum).Sum
$speed   = ($ramMods | Select-Object -First 1).Speed
$disks = @(Get-CimInstance Win32_DiskDrive | ForEach-Object {
    "$($_.Model) - $([math]::Round($_.Size/1GB,0)) GB"
})
$vols = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    "$($_.DeviceID) $([math]::Round($_.FreeSpace/1GB,1)) von $([math]::Round($_.Size/1GB,1)) GB frei"
})
$key = (Get-CimInstance -ClassName SoftwareLicensingService).OA3xOriginalProductKey
$lic = Get-CimInstance SoftwareLicensingProduct -Filter "ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f' AND PartialProductKey IS NOT NULL" |
       Select-Object -First 1
$act = switch ($lic.LicenseStatus) {
    0 {'Nicht lizenziert'} 1 {'Aktiviert'} 2 {'Testzeitraum'} 3 {'Toleranzzeitraum'}
    4 {'Nicht echt'} 5 {'Benachrichtigung'} 6 {'Verlaengerter Zeitraum'} default {'Unbekannt'}
}
$net = @(Get-CimInstance Win32_NetworkAdapterConfiguration -Filter "IPEnabled=True" |
         ForEach-Object { "$($_.Description): $($_.IPAddress -join ', ')" })

[pscustomobject]@{
    Computer     = $cs.Name
    Benutzer     = $env:USERNAME
    Windows      = $os.Caption
    Version      = $os.Version
    Build        = $os.BuildNumber
    Architektur  = $os.OSArchitecture
    Installiert  = if ($os.InstallDate) { $os.InstallDate.ToString('dd.MM.yyyy HH:mm') } else { '' }
    LetzterStart = if ($os.LastBootUpTime) { $os.LastBootUpTime.ToString('dd.MM.yyyy HH:mm') } else { '' }
    Hersteller   = $cs.Manufacturer
    Modell       = $cs.Model
    Mainboard    = "$($bb.Manufacturer) $($bb.Product)"
    Seriennummer = $bios.SerialNumber
    BiosVersion  = $bios.SMBIOSBIOSVersion
    BiosDatum    = if ($bios.ReleaseDate) { $bios.ReleaseDate.ToString('dd.MM.yyyy') } else { '' }
    CPU          = $cpu.Name
    CPUKerne     = "$($cpu.NumberOfCores) Kerne / $($cpu.NumberOfLogicalProcessors) Threads"
    CPUTakt      = "$([math]::Round($cpu.MaxClockSpeed/1000,2)) GHz"
    RAM          = "$([math]::Round($ramSum/1GB,0)) GB"
    RAMDetail    = "$($ramMods.Count) Riegel" + $(if ($speed) { " @ $speed MHz" } else { "" })
    GPU          = ($gpus -join ' | ')
    Datentraeger = ($disks -join ' | ')
    Laufwerke    = ($vols -join ' | ')
    WindowsKey   = $(if ($key) { $key } else { '' })
    KeyTeil      = $lic.PartialProductKey
    Aktivierung  = $act
    Netzwerk     = ($net -join ' | ')
} | ConvertTo-Json -Compress
"""

# Reihenfolge und Gruppierung fuer die Anzeige
GROUPS = [
    ("WINDOWS", ["Windows", "Version", "Build", "Architektur",
                 "Installiert", "LetzterStart", "Aktivierung"]),
    ("HARDWARE", ["CPU", "CPUKerne", "CPUTakt", "RAM", "RAMDetail", "GPU"]),
    ("SYSTEM", ["Computer", "Benutzer", "Hersteller", "Modell", "Mainboard",
                "Seriennummer", "BiosVersion", "BiosDatum"]),
    ("DATENTRAEGER", ["Datentraeger", "Laufwerke"]),
    ("NETZWERK", ["Netzwerk"]),
]

LABELS = {
    "Windows": "Ausgabe", "Version": "Version", "Build": "Build",
    "Architektur": "Architektur", "Installiert": "Installiert am",
    "LetzterStart": "Letzter Start", "Aktivierung": "Aktivierung",
    "CPU": "Prozessor", "CPUKerne": "Kerne", "CPUTakt": "Takt",
    "RAM": "Arbeitsspeicher", "RAMDetail": "Bestueckung", "GPU": "Grafikkarte",
    "Computer": "Computername", "Benutzer": "Benutzer",
    "Hersteller": "Hersteller", "Modell": "Modell", "Mainboard": "Mainboard",
    "Seriennummer": "Seriennummer", "BiosVersion": "BIOS", "BiosDatum": "BIOS-Datum",
    "Datentraeger": "Festplatten", "Laufwerke": "Belegung",
    "Netzwerk": "Adapter",
}


@dataclass
class SysInfo:
    data: dict = field(default_factory=dict)
    error: str = ""

    def get(self, key: str) -> str:
        val = self.data.get(key)
        return "" if val is None else str(val).strip()

    @property
    def windows_key(self) -> str:
        """Der im BIOS/UEFI hinterlegte Schluessel - oder leer."""
        return self.get("WindowsKey")

    @property
    def ok(self) -> bool:
        return bool(self.data) and not self.error


def collect() -> SysInfo:
    """Liest die Systemdaten aus. Wirft nie."""
    if not IS_WINDOWS:
        # Damit sich die Oberflaeche auch ausserhalb von Windows testen laesst.
        return SysInfo(data={
            "Computer": os.environ.get("HOSTNAME", "-"),
            "Benutzer": os.environ.get("USER", "-"),
            "Windows": "(kein Windows - Testbetrieb)",
        })
    res = powershell(_SCRIPT, timeout=90)
    text = (res.out or "").strip()
    if not text:
        return SysInfo(error=(res.err or "").strip()[:200] or "keine Ausgabe")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return SysInfo(error=f"Antwort nicht lesbar: {e}")
    if isinstance(data, list):
        data = data[0] if data else {}
    return SysInfo(data=data)
