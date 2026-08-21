"""Tweaks (Privacy/Debloat/Performance), System-Cleaner und externe Tools.

Alle Registry-Tweaks sind umkehrbar (An/Aus-Wert) und werden VOR dem Setzen
als .reg gesichert. Der Cleaner zeigt Groessen an, bevor geloescht wird.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .context import Reporter
from .winutils import IS_WINDOWS, dir_size_bytes, powershell, run

CREATE_NEW_CONSOLE = 0x00000010


# ==================================================================
#  Tweaks
# ==================================================================
@dataclass
class Tweak:
    key: str
    name: str
    category: str
    description: str
    ops: list           # {hive, path, name, type('DWORD'|'SZ'|'BINARY'), on, off}
    recommended: bool = False
    hint: str = ""      # wird nach dem Anwenden ins Protokoll geschrieben


TWEAKS: list = [
    Tweak("telemetry", "Telemetrie minimieren", "Privatsphaere",
          "Setzt die Windows-Datensammlung auf das Minimum (AllowTelemetry=0).",
          [{"hive": "HKLM", "path": r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
            "name": "AllowTelemetry", "type": "DWORD", "on": 0, "off": None}],
          recommended=True),
    Tweak("advertising_id", "Werbe-ID deaktivieren", "Privatsphaere",
          "Schaltet die personalisierte Werbe-ID ab.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\AdvertisingInfo",
            "name": "Enabled", "type": "DWORD", "on": 0, "off": 1}],
          recommended=True),
    Tweak("start_suggestions", "Start-Vorschlaege/Werbung aus", "Privatsphaere",
          "Deaktiviert vorgeschlagene Apps und Tipps im Startmenue.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
            "name": "SystemPaneSuggestionEnabled", "type": "DWORD", "on": 0, "off": 1},
           {"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
            "name": "SubscribedContent-338389Enabled", "type": "DWORD", "on": 0, "off": 1}],
          recommended=True),
    Tweak("bing_search", "Bing/Web-Suche im Startmenue aus", "Privatsphaere",
          "Entfernt Web-/Bing-Ergebnisse aus der Startmenue-Suche.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Policies\Microsoft\Windows\Explorer",
            "name": "DisableSearchBoxSuggestions", "type": "DWORD", "on": 1, "off": None}],
          recommended=True),
    Tweak("show_ext", "Dateiendungen anzeigen", "Explorer",
          "Blendet bekannte Dateiendungen im Explorer ein.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "HideFileExt", "type": "DWORD", "on": 0, "off": 1}],
          recommended=True),
    Tweak("show_hidden", "Versteckte Dateien anzeigen", "Explorer",
          "Zeigt versteckte Dateien und Ordner an.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "Hidden", "type": "DWORD", "on": 1, "off": 2}]),
    # Der Trick wirkt dadurch, dass der Schluessel InprocServer32 mit leerem
    # Standardwert EXISTIERT. Zum Zuruecksetzen muss deshalb der SCHLUESSEL
    # weg - nur den Wert zu loeschen genuegt nicht.
    Tweak("classic_menu", "Klassisches Rechtsklick-Menue (Win11)", "Explorer",
          "Stellt das vollstaendige Kontextmenue von Windows 10 wieder her.",
          [{"hive": "HKCU",
            "path": r"SOFTWARE\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32",
            "name": "", "type": "SZ", "on": "", "off": None, "delete_key": True}]),
    Tweak("taskbar_widgets", "Widgets aus Taskleiste (Win11)", "Taskleiste",
          "Entfernt den Widgets-Button aus der Taskleiste.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "TaskbarDa", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("taskbar_chat", "Chat/Teams aus Taskleiste (Win11)", "Taskleiste",
          "Entfernt den Chat-Button aus der Taskleiste.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "TaskbarMn", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("copilot_off", "Copilot deaktivieren", "Privatsphaere",
          "Schaltet Windows Copilot ab.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot",
            "name": "TurnOffWindowsCopilot", "type": "DWORD", "on": 1, "off": 0}]),
    Tweak("lockscreen_tips", "Sperrbildschirm-Tipps aus", "Privatsphaere",
          "Deaktiviert Fun-Facts/Werbung auf dem Sperrbildschirm.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
            "name": "RotatingLockScreenOverlayEnabled", "type": "DWORD", "on": 0, "off": 1}]),

    # ---------------- Privatsphaere ----------------
    Tweak("tailored_experiences", "Personalisierte Erlebnisse aus", "Privatsphaere",
          "Verhindert, dass Windows aus deinen Diagnosedaten Vorschlaege ableitet.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Privacy",
            "name": "TailoredExperiencesWithDiagnosticDataEnabled", "type": "DWORD",
            "on": 0, "off": 1}],
          recommended=True),
    Tweak("activity_history", "Aktivitaetsverlauf aus", "Privatsphaere",
          "Windows sammelt keine Zeitleiste deiner Aktivitaeten mehr.",
          [{"hive": "HKLM", "path": r"SOFTWARE\Policies\Microsoft\Windows\System",
            "name": "PublishUserActivities", "type": "DWORD", "on": 0, "off": 1},
           {"hive": "HKLM", "path": r"SOFTWARE\Policies\Microsoft\Windows\System",
            "name": "UploadUserActivities", "type": "DWORD", "on": 0, "off": 1}],
          recommended=True),
    Tweak("location_off", "Standortermittlung aus", "Privatsphaere",
          "Sperrt den Zugriff auf den Geraetestandort systemweit.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location",
            "name": "Value", "type": "SZ", "on": "Deny", "off": "Allow"}]),
    Tweak("app_diagnostics", "App-Diagnosezugriff aus", "Privatsphaere",
          "Apps duerfen keine Diagnosedaten anderer Apps auslesen.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\appDiagnostics",
            "name": "Value", "type": "SZ", "on": "Deny", "off": "Allow"}]),
    Tweak("feedback_off", "Feedback-Anfragen aus", "Privatsphaere",
          "Windows fragt nicht mehr nach Feedback.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Siuf\Rules",
            "name": "NumberOfSIUFInPeriod", "type": "DWORD", "on": 0, "off": None}],
          recommended=True),
    Tweak("cortana_off", "Cortana deaktivieren", "Privatsphaere",
          "Schaltet die Sprachassistentin ab.",
          [{"hive": "HKLM", "path": r"SOFTWARE\Policies\Microsoft\Windows\Windows Search",
            "name": "AllowCortana", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("recall_off", "Recall/Snapshots aus (Win11)", "Privatsphaere",
          "Verhindert die automatische Bildschirmaufzeichnung von Recall.",
          [{"hive": "HKLM", "path": r"SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
            "name": "DisableAIDataAnalysis", "type": "DWORD", "on": 1, "off": 0}],
          recommended=True),
    Tweak("wifi_sense", "WLAN-Freigabe (WiFi Sense) aus", "Privatsphaere",
          "Teilt WLAN-Zugaenge nicht mehr automatisch mit Kontakten.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Microsoft\PolicyManager\default\WiFi\AllowWiFiHotSpotReporting",
            "name": "Value", "type": "DWORD", "on": 0, "off": 1}]),

    # ---------------- Explorer ----------------
    Tweak("open_thispc", "Explorer startet mit 'Dieser PC'", "Explorer",
          "Statt Schnellzugriff oeffnet der Explorer die Laufwerksuebersicht.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "LaunchTo", "type": "DWORD", "on": 1, "off": 2}],
          recommended=True),
    Tweak("full_path", "Vollstaendigen Pfad im Titel", "Explorer",
          "Zeigt den kompletten Ordnerpfad in der Titelleiste.",
          [{"hive": "HKCU",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\CabinetState",
            "name": "FullPath", "type": "DWORD", "on": 1, "off": 0}]),
    Tweak("no_shortcut_suffix", "Kein '- Verknuepfung' im Namen", "Explorer",
          "Neue Verknuepfungen bekommen keinen Namenszusatz mehr.",
          [{"hive": "HKCU",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer",
            "name": "link", "type": "BINARY", "on": "00000000", "off": "16000000"}]),
    Tweak("compact_view", "Kompakte Ansicht (Win11)", "Explorer",
          "Engere Zeilenabstaende - mehr Eintraege auf einen Blick.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "UseCompactMode", "type": "DWORD", "on": 1, "off": 0}]),
    Tweak("no_sync_provider", "Keine OneDrive-Werbung im Explorer", "Explorer",
          "Blendet Hinweise auf Synchronisierungsanbieter aus.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "ShowSyncProviderNotifications", "type": "DWORD", "on": 0, "off": 1}],
          recommended=True),
    Tweak("no_gallery", "Galerie aus Navigationsleiste (Win11)", "Explorer",
          "Entfernt den Galerie-Eintrag aus der linken Spalte.",
          [{"hive": "HKCU",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\CLSID\{e88865ea-0e1c-4e20-9aa6-edcd0212c87c}",
            "name": "System.IsPinnedToNameSpaceTree", "type": "DWORD", "on": 0, "off": 1}]),

    # ---------------- Taskleiste & Startmenue ----------------
    Tweak("taskbar_left", "Taskleiste linksbuendig (Win11)", "Taskleiste",
          "Symbole starten links statt in der Mitte.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "TaskbarAl", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("taskbar_search", "Suchfeld aus Taskleiste", "Taskleiste",
          "Entfernt das breite Suchfeld neben dem Startknopf.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Search",
            "name": "SearchboxTaskbarMode", "type": "DWORD", "on": 0, "off": 1}],
          recommended=True),
    Tweak("taskbar_taskview", "Taskansicht-Button aus", "Taskleiste",
          "Entfernt den Button fuer virtuelle Desktops.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "ShowTaskViewButton", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("taskbar_seconds", "Sekunden in der Uhr", "Taskleiste",
          "Zeigt Sekunden in der Taskleisten-Uhr an.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            "name": "ShowSecondsInSystemClock", "type": "DWORD", "on": 1, "off": 0}]),
    Tweak("end_task", "'Task beenden' im Rechtsklick (Win11)", "Taskleiste",
          "Erlaubt das Beenden haengender Programme direkt aus der Taskleiste.",
          [{"hive": "HKCU",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced\TaskbarDeveloperSettings",
            "name": "TaskbarEndTask", "type": "DWORD", "on": 1, "off": 0}],
          recommended=True),

    # ---------------- System & Tempo ----------------
    Tweak("fast_menus", "Menues schneller einblenden", "Tempo",
          "Verkuerzt die Verzoegerung beim Oeffnen von Menues (400 -> 20 ms).",
          [{"hive": "HKCU", "path": r"Control Panel\Desktop",
            "name": "MenuShowDelay", "type": "SZ", "on": "20", "off": "400"}],
          recommended=True),
    Tweak("no_startup_delay", "Autostart ohne Verzoegerung", "Tempo",
          "Startprogramme laufen sofort statt nach ~10 Sekunden.",
          [{"hive": "HKCU",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Serialize",
            "name": "StartupDelayInMSec", "type": "DWORD", "on": 0, "off": None}]),
    Tweak("hibernate_off", "Ruhezustandsdatei entfernen", "Tempo",
          "Gibt mehrere GB frei (hiberfil.sys). Schnellstart entfaellt dadurch.",
          [{"hive": "HKLM", "path": r"SYSTEM\CurrentControlSet\Control\Power",
            "name": "HibernateEnabled", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("verbose_status", "Ausfuehrliche Statusmeldungen", "Tempo",
          "Zeigt beim Start/Herunterfahren, worauf Windows gerade wartet.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
            "name": "VerboseStatus", "type": "DWORD", "on": 1, "off": 0}]),
    Tweak("no_web_widgets", "Widgets-Dienst abschalten (Win11)", "Tempo",
          "Verhindert, dass der Widgets-Prozess im Hintergrund laeuft.",
          [{"hive": "HKLM", "path": r"SOFTWARE\Policies\Microsoft\Dsh",
            "name": "AllowNewsAndInterests", "type": "DWORD", "on": 0, "off": 1}],
          recommended=True),

    # ---------------- Updates ----------------
    Tweak("no_auto_restart", "Kein Neustart bei aktiver Nutzung", "Updates",
          "Windows startet nicht neu, solange du angemeldet bist.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU",
            "name": "NoAutoRebootWithLoggedOnUsers", "type": "DWORD", "on": 1, "off": 0}],
          recommended=True),
    Tweak("no_driver_updates", "Keine Treiber ueber Windows Update", "Updates",
          "Verhindert, dass Windows eigenmaechtig Treiber ersetzt.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching",
            "name": "SearchOrderConfig", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("delivery_optimization", "Update-Weitergabe an andere PCs aus", "Updates",
          "Dein PC verteilt keine Update-Pakete mehr im Internet.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization",
            "name": "DODownloadMode", "type": "DWORD", "on": 0, "off": 1}],
          recommended=True),

    # ---------------- Gaming ----------------
    Tweak("game_bar_off", "Xbox Game Bar aus", "Gaming",
          "Deaktiviert die Spielleiste und ihre Aufnahmefunktion.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\GameDVR",
            "name": "AppCaptureEnabled", "type": "DWORD", "on": 0, "off": 1},
           {"hive": "HKCU", "path": r"System\GameConfigStore",
            "name": "GameDVR_Enabled", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("game_mode_on", "Spielmodus einschalten", "Gaming",
          "Priorisiert das laufende Spiel gegenueber Hintergrunddiensten.",
          [{"hive": "HKCU", "path": r"SOFTWARE\Microsoft\GameBar",
            "name": "AutoGameModeEnabled", "type": "DWORD", "on": 1, "off": 0}]),
    Tweak("gpu_scheduling", "Hardware-GPU-Planung", "Gaming",
          "Laesst die Grafikkarte ihren Speicher selbst verwalten (Neustart noetig).",
          [{"hive": "HKLM",
            "path": r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers",
            "name": "HwSchMode", "type": "DWORD", "on": 2, "off": 1}]),

    # ---------------- Anmeldung ----------------
    # Hinweis zum ersten Eintrag: Es gibt zwei Wege zur automatischen
    # Anmeldung. Der verbreitete traegt Benutzername und Passwort IM KLARTEXT
    # in die Registry (Winlogon\DefaultPassword) - jeder Leseberechtigte kann
    # es dort abholen. Deshalb wird hier nur die netplwiz-Option
    # freigeschaltet: Windows legt das Passwort danach verschluesselt als
    # LSA-Geheimnis ab, nicht im Klartext.
    Tweak("passwordless_option", "Automatische Anmeldung freischalten", "Anmeldung",
          "Blendet in netplwiz die Option 'Benutzer muessen Benutzernamen und "
          "Kennwort eingeben' wieder ein - bei Microsoft-Konten ist sie ab Werk "
          "versteckt. Das Passwort wird dabei verschluesselt hinterlegt, nicht "
          "im Klartext.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device",
            "name": "DevicePasswordLessBuildVersion", "type": "DWORD",
            "on": 0, "off": 2}],
          hint="Jetzt Windows-Taste+R druecken, 'netplwiz' eingeben, den Haken "
               "bei 'Benutzer muessen Benutzernamen und Kennwort eingeben' "
               "entfernen und das Passwort einmal bestaetigen."),
    Tweak("no_wake_password", "Kein Passwort nach dem Ruhezustand", "Anmeldung",
          "Nach Standby oder Bildschirmsperre wird nicht mehr nach dem Kennwort "
          "gefragt - weder im Akku- noch im Netzbetrieb.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Policies\Microsoft\Power\PowerSettings"
                    r"\0e796bdb-100d-47d6-a2d5-f7d2daa51f51",
            "name": "ACSettingIndex", "type": "DWORD", "on": 0, "off": 1},
           {"hive": "HKLM",
            "path": r"SOFTWARE\Policies\Microsoft\Power\PowerSettings"
                    r"\0e796bdb-100d-47d6-a2d5-f7d2daa51f51",
            "name": "DCSettingIndex", "type": "DWORD", "on": 0, "off": 1}]),
    Tweak("no_lockscreen", "Sperrbildschirm ueberspringen", "Anmeldung",
          "Beim Start erscheint direkt die Anmeldung statt des Bildes, das man "
          "erst wegklicken muss.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Policies\Microsoft\Windows\Personalization",
            "name": "NoLockScreen", "type": "DWORD", "on": 1, "off": 0}]),
    Tweak("no_ctrl_alt_del", "Kein Strg+Alt+Entf zum Anmelden", "Anmeldung",
          "Entfernt den zusaetzlichen Tastendruck vor der Kennworteingabe.",
          [{"hive": "HKLM",
            "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
            "name": "DisableCAD", "type": "DWORD", "on": 1, "off": 0}]),
]


class TweakEngine:
    def __init__(self, reporter: Reporter, backup_dir: Path):
        self.r = reporter
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def apply(self, tweaks: list, enable: bool = True) -> dict:
        ok = 0
        fail = 0
        total = len(tweaks)
        for i, tw in enumerate(tweaks):
            if self.r.cancelled:
                self.r.warn("Abgebrochen.")
                break
            self.r.status(f"{'Aktiviere' if enable else 'Widerrufe'} {tw.name} ...")
            try:
                for op in tw.ops:
                    self._backup(op["hive"], op["path"], tw.key)
                    value = op["on"] if enable else op["off"]
                    if value is None:
                        if op.get("delete_key"):
                            self._del_key(op["hive"], op["path"])
                        else:
                            self._del(op["hive"], op["path"], op["name"])
                    else:
                        self._set(op["hive"], op["path"], op["name"], op["type"], value)
                ok += 1
                self.r.ok(f"{tw.name}: {'aktiviert' if enable else 'zurueckgesetzt'}")
                if enable and tw.hint:
                    self.r.warn(f"  Noch zu tun: {tw.hint}")
            except Exception as e:
                fail += 1
                self.r.err(f"{tw.name}: {e}")
            self.r.progress((i + 1) / max(total, 1))
        self.r.log("Hinweis: Fuer Explorer/Taskleisten-Tweaks Explorer neu starten "
                   "(Abmelden/Anmelden).")
        self.r.done({"ok": ok, "fail": fail, "backup": str(self.backup_dir)})
        return {"ok": ok, "fail": fail}

    # ---- Registry-Helfer ----
    def _hive(self, name: str):
        import winreg
        return winreg.HKEY_CURRENT_USER if name == "HKCU" else winreg.HKEY_LOCAL_MACHINE

    def _backup(self, hive: str, path: str, tag: str) -> None:
        if not IS_WINDOWS:
            return
        stamp = datetime.now().strftime("%H%M%S")
        safe = "".join(c if c.isalnum() else "_" for c in tag)[:40]
        out = self.backup_dir / f"tweak_{safe}_{stamp}.reg"
        run(["reg", "export", f"{hive}\\{path}", str(out), "/y"], timeout=60)

    def _set(self, hive: str, path: str, name: str, typ: str, value) -> None:
        import winreg
        key = winreg.CreateKeyEx(self._hive(hive), path, 0, winreg.KEY_SET_VALUE)
        try:
            if typ == "DWORD":
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
            elif typ == "BINARY":
                winreg.SetValueEx(key, name, 0, winreg.REG_BINARY,
                                  bytes.fromhex(str(value)))
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
        finally:
            winreg.CloseKey(key)

    def _del(self, hive: str, path: str, name: str) -> None:
        import winreg
        try:
            key = winreg.OpenKey(self._hive(hive), path, 0, winreg.KEY_SET_VALUE)
        except OSError:
            return
        try:
            winreg.DeleteValue(key, name)
        except OSError:
            pass
        finally:
            winreg.CloseKey(key)

    def _del_key(self, hive: str, path: str) -> None:
        """Entfernt den Schluessel selbst (samt uebergeordnetem CLSID-Knoten,
        falls dieser danach leer ist) - noetig fuer Tweaks, die allein durch
        die EXISTENZ eines Schluessels wirken."""
        import winreg
        try:
            winreg.DeleteKey(self._hive(hive), path)
        except OSError:
            return
        parent = path.rsplit("\\", 1)[0]
        try:
            key = winreg.OpenKey(self._hive(hive), parent)
            try:
                winreg.EnumKey(key, 0)        # noch Unterschluessel? -> stehen lassen
                return
            except OSError:
                pass
            finally:
                winreg.CloseKey(key)
            winreg.DeleteKey(self._hive(hive), parent)
        except OSError:
            pass


# ==================================================================
#  Cleaner
# ==================================================================
@dataclass
class CleanTarget:
    key: str
    name: str
    description: str
    kind: str            # 'files' | 'recycle' | 'updatecache' | 'prefetch'
    paths: list = field(default_factory=list)


def _p(*parts) -> str:
    return os.path.join(*[p for p in parts if p])


def clean_targets() -> list:
    win = os.environ.get("windir", r"C:\Windows")
    la = os.environ.get("LOCALAPPDATA", "")
    tmp = os.environ.get("TEMP", "")
    return [
        CleanTarget("user_temp", "Temp-Dateien (Benutzer)",
                    "Temporaere Dateien deines Benutzerkontos.", "files", [tmp]),
        CleanTarget("win_temp", "Temp-Dateien (Windows)",
                    "Systemweite Temp-Dateien.", "files", [_p(win, "Temp")]),
        CleanTarget("update_cache", "Windows-Update-Cache",
                    "Heruntergeladene Update-Pakete (SoftwareDistribution).",
                    "updatecache", [_p(win, "SoftwareDistribution", "Download")]),
        CleanTarget("prefetch", "Prefetch",
                    "Prefetch-Dateien (werden neu aufgebaut).", "prefetch",
                    [_p(win, "Prefetch")]),
        CleanTarget("thumb_cache", "Thumbnail-Cache",
                    "Miniaturansichten-Cache des Explorers.", "files",
                    [_p(la, "Microsoft", "Windows", "Explorer")]),
        CleanTarget("recycle", "Papierkorb leeren",
                    "Leert den Papierkorb aller Laufwerke.", "recycle", []),
    ]


class CleanupEngine:
    def __init__(self, reporter: Reporter):
        self.r = reporter

    def scan(self, targets: list, reporter=None) -> dict:
        """Ermittelt je Ziel die belegte Groesse und die groessten Posten.

        Rueckgabe: {key: {"size": Bytes, "items": [(Name, Bytes), ...],
                          "files": Anzahl}}
        'items' ist die nach Groesse sortierte Aufschluesselung der obersten
        Ebene - so sieht man, was den Platz tatsaechlich belegt.
        """
        result = {}
        for t in targets:
            if reporter is not None:
                if reporter.cancelled:
                    break
                reporter.status(f"Berechne {t.name} ...")
            if t.kind == "recycle":
                size, items, files = self._recycle_size()
                result[t.key] = {"size": size, "items": items, "files": files}
                continue
            total = 0
            files = 0
            items = []
            for base in t.paths:
                if not base or not os.path.isabs(base) or not os.path.isdir(base):
                    continue
                try:
                    entries = os.listdir(base)
                except OSError:
                    continue
                for name in entries:
                    full = os.path.join(base, name)
                    try:
                        if os.path.isfile(full):
                            sz = os.path.getsize(full)
                            files += 1
                        elif os.path.isdir(full):
                            sz = dir_size_bytes(full)
                            files += sum(len(f) for _r, _d, f in os.walk(full))
                        else:
                            continue
                    except OSError:
                        continue
                    total += sz
                    items.append((name, sz))
            items.sort(key=lambda x: x[1], reverse=True)
            result[t.key] = {"size": total, "items": items[:12], "files": files}
        return result

    def _recycle_size(self):
        """Papierkorb ueber $Recycle.Bin aller Laufwerke messen."""
        total, files, items = 0, 0, []
        for drive in "CDEFGHIJKL":
            root = f"{drive}:\\$Recycle.Bin"
            if not os.path.isdir(root):
                continue
            sz = dir_size_bytes(root)
            if sz:
                total += sz
                items.append((f"{drive}:", sz))
                files += sum(len(f) for _r, _d, f in os.walk(root))
        items.sort(key=lambda x: x[1], reverse=True)
        return total, items, files

    def clean(self, targets: list) -> dict:
        freed = 0
        total = len(targets)
        for i, t in enumerate(targets):
            if self.r.cancelled:
                self.r.warn("Abgebrochen.")
                break
            self.r.status(f"Bereinige {t.name} ...")
            try:
                if t.kind == "recycle":
                    powershell("Clear-RecycleBin -Force -EA SilentlyContinue", timeout=120)
                    self.r.ok("Papierkorb geleert.")
                elif t.kind == "updatecache":
                    freed += self._clean_update_cache(t)
                else:
                    freed += self._clean_files(t)
            except Exception as e:
                self.r.err(f"{t.name}: {e}")
            self.r.progress((i + 1) / max(total, 1))
        self.r.done({"freed_mb": round(freed / 1048576, 1)})
        return {"freed_mb": round(freed / 1048576, 1)}

    def _clean_files(self, t: CleanTarget) -> int:
        freed = 0
        for base in t.paths:
            # Nur absolute, existierende Pfade. Fehlt eine Umgebungsvariable,
            # entstuende sonst ein RELATIVER Pfad - der zeigt auf das
            # Arbeitsverzeichnis, also auf den USB-Stick.
            if not base or not os.path.isabs(base) or not os.path.isdir(base):
                continue
            for entry in os.listdir(base):
                full = os.path.join(base, entry)
                try:
                    if os.path.isfile(full) or os.path.islink(full):
                        sz = os.path.getsize(full)
                        os.remove(full)
                        freed += sz
                    elif os.path.isdir(full):
                        sz = dir_size_bytes(full)
                        shutil.rmtree(full, ignore_errors=True)
                        freed += sz
                except OSError:
                    pass  # in Benutzung -> ueberspringen
        self.r.ok(f"{t.name}: {round(freed / 1048576, 1)} MB frei.")
        return freed

    def _clean_update_cache(self, t: CleanTarget) -> int:
        run(["net", "stop", "wuauserv"], timeout=60)
        run(["net", "stop", "bits"], timeout=60)
        freed = self._clean_files(t)
        run(["net", "start", "bits"], timeout=60)
        run(["net", "start", "wuauserv"], timeout=60)
        return freed


# ==================================================================
#  Externe Tools (transparent, sichtbar, nach Bestaetigung in der UI)
# ==================================================================
@dataclass
class ExtTool:
    key: str
    name: str
    description: str
    kind: str                 # 'ps' | 'download' | 'zip'
    source: str               # PowerShell-Befehl oder URL
    exe: str = ""             # bei 'zip': zu startende Datei im Archiv
    filename: str = ""        # bei 'download': Zieldateiname
    note: str = ""


# Nur offizielle Quellen. Sysinternals- und ShutUp10-Adressen sind seit
# Jahren stabil; schlaegt eine fehl, meldet das Programm das deutlich,
# statt so zu tun, als sei alles in Ordnung.
EXT_TOOLS: list = [
    ExtTool("winutil", "Chris Titus WinUtil",
            "Bekanntes Open-Source-Werkzeug zum Entschlacken und Einstellen von "
            "Windows. Oeffnet sich als eigenes Fenster.",
            "ps", "irm 'https://christitus.com/win' | iex",
            note="Laedt ein Skript von christitus.com und fuehrt es aus."),
    ExtTool("shutup10", "O&O ShutUp10++",
            "Privatsphaere-Einstellungen von O&O - portabel, keine Installation.",
            "download", "https://dl5.oo-software.com/files/ooshutup10/OOSU10.exe",
            filename="OOSU10.exe"),
    ExtTool("autoruns", "Autoruns (Microsoft Sysinternals)",
            "Zeigt alles, was beim Start automatisch mitlaeuft - und schaltet es ab.",
            "zip", "https://download.sysinternals.com/files/Autoruns.zip",
            exe="Autoruns64.exe"),
    ExtTool("procexp", "Process Explorer (Microsoft Sysinternals)",
            "Taskmanager-Ersatz: zeigt, welcher Prozess wirklich bremst.",
            "zip", "https://download.sysinternals.com/files/ProcessExplorer.zip",
            exe="procexp64.exe"),
    ExtTool("tcpview", "TCPView (Microsoft Sysinternals)",
            "Zeigt live, welches Programm ins Internet funkt.",
            "zip", "https://download.sysinternals.com/files/TCPView.zip",
            exe="tcpview64.exe"),
]


class ExternalTools:
    """Startet bewaehrte Fremdwerkzeuge - sichtbar und nach Bestaetigung."""

    def __init__(self, reporter: Reporter):
        self.r = reporter

    def run(self, tool: ExtTool, dest) -> bool:
        self.r.head(f"Starte {tool.name} ...")
        try:
            if tool.kind == "ps":
                ok = self._run_ps(tool)
            elif tool.kind == "download":
                ok = self._run_download(tool, Path(dest))
            elif tool.kind == "zip":
                ok = self._run_zip(tool, Path(dest))
            else:
                self.r.err(f"Unbekannte Art: {tool.kind}")
                ok = False
        except Exception as e:
            self.r.err(f"{tool.name}: {e}")
            ok = False
        self.r.done({"ok": 1 if ok else 0, "fail": 0 if ok else 1})
        return ok

    # ---------- Startarten ----------
    def _run_ps(self, tool: ExtTool) -> bool:
        # TLS 1.2 erzwingen: aeltere Windows-Stände scheitern sonst still beim
        # Herunterladen des Skripts.
        cmd = ("[Net.ServicePointManager]::SecurityProtocol="
               "[Net.SecurityProtocolType]::Tls12; " + tool.source)
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit",
             "-Command", cmd],
            creationflags=CREATE_NEW_CONSOLE)
        return self._confirm_started(proc, tool)

    def _run_download(self, tool: ExtTool, dest: Path) -> bool:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / (tool.filename or "tool.exe")
        if not self._fetch(tool.source, target):
            return False
        proc = subprocess.Popen([str(target)], cwd=str(dest),
                                creationflags=CREATE_NEW_CONSOLE)
        return self._confirm_started(proc, tool)

    def _run_zip(self, tool: ExtTool, dest: Path) -> bool:
        import zipfile
        folder = dest / tool.key
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / "download.zip"
        if not self._fetch(tool.source, archive):
            return False
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(folder)
        except zipfile.BadZipFile:
            self.r.err("Archiv ist beschaedigt - Download abgebrochen?")
            return False
        finally:
            archive.unlink(missing_ok=True)

        exe = folder / tool.exe
        if not exe.exists():                 # 32-Bit-Variante als Rueckfall
            alt = [p for p in folder.glob("*.exe") if p.name.lower().endswith(".exe")]
            if not alt:
                self.r.err(f"Im Archiv fand sich keine ausfuehrbare Datei.")
                return False
            exe = sorted(alt, key=lambda p: len(p.name))[0]
        proc = subprocess.Popen([str(exe)], cwd=str(folder),
                                creationflags=CREATE_NEW_CONSOLE)
        self.r.log(f"Abgelegt unter: {folder}")
        return self._confirm_started(proc, tool)

    # ---------- Helfer ----------
    def _fetch(self, url: str, target: Path) -> bool:
        import urllib.request
        self.r.log(f"Lade {url} ...")
        tmp = target.with_name(target.name + ".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            if tmp.stat().st_size < 4096:
                raise RuntimeError("Datei unplausibel klein")
            tmp.replace(target)
            self.r.ok(f"Geladen ({target.stat().st_size // 1024} KB).")
            return True
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self.r.err(f"Download fehlgeschlagen: {e}")
            self.r.warn("Die Adresse kann sich geaendert haben - dann das Werkzeug "
                        "bitte direkt beim Hersteller laden.")
            return False

    def _confirm_started(self, proc, tool: ExtTool) -> bool:
        """Kurz nachsehen, ob der Prozess wirklich laeuft - frueher wurde
        jeder Startversuch als Erfolg gemeldet."""
        time.sleep(1.2)
        if proc.poll() is None:
            self.r.ok(f"{tool.name} laeuft (eigenes Fenster).")
            return True
        code = proc.returncode
        if code == 0:
            self.r.ok(f"{tool.name} wurde beendet (Code 0).")
            return True
        self.r.err(f"{tool.name} hat sich sofort beendet (Code {code}).")
        return False
