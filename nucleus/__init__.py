"""Hoferium - PC Toolkit fuer Windows.

Ein modulares Werkzeug fuer Windows, das vor einer Neuinstallation
Daten sichert, Software-Installer bereitstellt und Bloatware entfernt.

Aufbau (OOP):
    config      - Pfade, Theme, Konstanten
    winutils    - Windows-Helfer (Admin, Subprozesse, PowerShell, robocopy)
    registry    - Installierte Programme / AppX-Pakete auslesen
    backup      - BackupJob: Browser, Windows-Key, WLAN, Programme, Treiber ...
    downloader  - DownloadManager: offizielle Installer holen / per winget setzen
    uninstaller - Uninstaller: Bloatware auflisten und entfernen (mit Backup)
    ui          - customtkinter-Oberflaeche (Fenster, Seiten, Widgets)
"""

__version__ = "1.6.0"
APP_NAME = "Hoferium"
