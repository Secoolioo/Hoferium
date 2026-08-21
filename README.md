<div align="center">

# Hoferium

**Datensicherung, Software-Bereitstellung und Systempflege für Windows – auf einem USB-Stick.**

[![Version](https://img.shields.io/badge/Version-1.0.0-ffce00?style=flat-square&labelColor=1e1e26)](VERSION)
[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-dd0000?style=flat-square&labelColor=1e1e26)](#voraussetzungen)
[![Python](https://img.shields.io/badge/Python-3.10%2B-1e1e26?style=flat-square&labelColor=1e1e26)](#voraussetzungen)
[![Lizenz](https://img.shields.io/badge/Lizenz-MIT-1e1e26?style=flat-square&labelColor=1e1e26)](LICENSE)

</div>

---

Wer einen Windows-PC neu aufsetzt, sammelt vorher immer dieselben Dinge ein:
Lesezeichen, WLAN-Passwörter, den Windows-Schlüssel, die Liste installierter
Programme. Hoferium erledigt das in einem Durchgang – und hilft anschließend
beim Einrichten des frischen Systems.

Das Programm liegt auf dem Stick, richtet sich beim ersten Start selbst ein und
schreibt alle Ergebnisse direkt daneben.

<div align="center">
  <img src="docs/screenshot-start.png" alt="Startseite mit Systemübersicht und Windows-Schlüssel" width="100%">
</div>

## Funktionen

| Bereich | Was es tut |
| --- | --- |
| **Datensicherung** | Browser-Profile (Chrome, Edge, Firefox, Brave, Opera, Vivaldi und weitere), Lesezeichen als importierbare HTML, WLAN-Profile samt Passwörtern, Windows-Schlüssel, Programmliste, Treiber, Sticky Notes, Thunderbird- und Outlook-Daten |
| **Zurückholen** | Spielt eine Sicherung auf Knopfdruck zurück: WLAN-Netze, Firefox- und Thunderbird-Profil, Kurznotizen, Outlook-Dateien, eigene Dateien, Treiber, Programme via `winget import` |
| **Software** | Installer von den offiziellen Quellen laden oder direkt per `winget` einspielen – die Programmliste wird aus diesem Repository aufgefrischt |
| **Deinstallieren** | Programme sauber entfernen, auf Wunsch samt Datei- und Registry-Resten |
| **Debloat** | Vorinstallierte Windows-Apps und Herstellerzugaben entfernen – automatisch oder gezielt ausgewählt |
| **Tweaks** | 41 umkehrbare Einstellungen zu Privatsphäre, Explorer, Taskleiste, Tempo, Updates und Gaming |
| **Cleaner** | Temporäre Dateien, Update-Zwischenspeicher und Papierkorb – mit Größenanzeige vor dem Löschen |
| **Werkzeuge** | Startet bewährte Fremdprogramme (WinUtil, O&O ShutUp10++, Sysinternals) aus den Originalquellen |

## Benutzung

1. Diesen Ordner auf einen USB-Stick kopieren.
2. Auf dem Windows-PC `hoferium.bat` doppelklicken und die Nachfrage nach
   Administratorrechten bestätigen.
3. Beim ersten Start richtet sich alles selbst ein: Falls Python fehlt, wird es
   installiert, danach die Oberfläche eingerichtet. Das dauert einmalig ein paar
   Minuten und braucht eine Internetverbindung.

Die Sicherung landet in einem Ordner `Sicherung_<PC>_<Datum>` direkt neben der
`.bat`. Darin liegt eine Datei `WIEDERHERSTELLEN.txt`, die Schritt für Schritt
beschreibt, wie alles auf dem neuen System zurückkommt.

<div align="center">
  <img src="docs/screenshot-tweaks.png" alt="Tweaks nach Kategorien geordnet" width="49%">
  <img src="docs/screenshot-cleaner.png" alt="Cleaner mit Größenaufstellung" width="49%">
</div>

## Zurückholen auf dem neuen System

Nach der Neuinstallation genügt es, Hoferium vom selben Stick zu starten und
auf *Zurückholen* zu gehen. Das Programm sucht die Sicherungsordner selbst,
erkennt am Rechnernamen die zu diesem Gerät passende und wählt sie vor.

<div align="center">
  <img src="docs/screenshot-restore.png" alt="Gefundene Sicherungen mit Auswahl der Bestandteile" width="100%">
</div>

Vollautomatisch laufen WLAN-Netze (`netsh`), das Firefox- und
Thunderbird-Profil, Kurznotizen, Outlook-Dateien, eigene Dateien, Treiber
(`pnputil`) und das Nachinstallieren der Programme (`winget import`). Was in
der gewählten Sicherung fehlt, wird ausgegraut statt es erfolglos zu
versuchen.

Nur die Lesezeichen der Chromium-Browser brauchen zwei Klicks von Hand – dafür
gibt es keine Schnittstelle. Hoferium öffnet dazu den passenden Ordner.

Bevor ein vorhandenes Profil überschrieben wird, sichert das Programm den
aktuellen Stand nach `%LOCALAPPDATA%\Hoferium\backups\vor_import_<Zeit>`.

## Zu den Browser-Passwörtern

Hier gibt es eine technische Grenze, die jedes ehrliche Werkzeug benennen muss:

- **Firefox** speichert Passwörter im Profil (`logins.json`, `key4.db`). Das
  Profil wird gesichert und lässt sich auf dem neuen System zurückkopieren –
  die Passwörter sind damit wieder da.
- **Chrome, Edge, Brave, Opera, Vivaldi** verschlüsseln ihre Passwörter mit dem
  Windows-Benutzerkonto (DPAPI). Nach einer Neuinstallation existiert dieses
  Konto nicht mehr, und die Daten sind **nicht wiederherstellbar** – auch nicht
  aus einer vollständigen Kopie des Profils.

Deshalb enthält die Seite *Datensicherung* einen Assistenten, der die
Passwortverwaltung des jeweiligen Browsers öffnet, solange das alte System noch
läuft. Der Export dauert zwei Klicks; die entstandene CSV-Datei gehört mit auf
den Stick und sollte nach dem Import wieder gelöscht werden.

Hoferium entschlüsselt keine Passwörter und liest keine Anmeldedaten aus.

## Aktualisierung

Beim Start prüft Hoferium, ob in diesem Repository eine neuere Fassung liegt.
Antwortet das Netz nicht, wird die Prüfung nach einer Minute stillschweigend
übersprungen – der Start hängt nie daran.

Ein Update spielt den Repository-Stand vollständig ein, statt einzelne Dateien
zu ersetzen. Dadurch überstehen auch **Strukturänderungen** ein Update: Wird
ein Ordner umbenannt oder eine Datei entfernt, verschwindet der alte Stand
mit – es bleiben keine Reste liegen, die eine ältere Fassung nicht kennen kann.

Dabei gilt:

- Unangetastet bleiben `Sicherung_*`, `Installer/` und Protokolldateien. Eine
  mitgelieferte `update.json` kann weitere Namen schützen.
- Alles Ersetzte wandert nach `_vorherige_version/` – gelöscht wird nichts.
- Vor dem Einspielen wird geprüft, ob das Archiv überhaupt ein startfähiges
  Programm enthält; andernfalls bricht der Vorgang ab und die vorhandene
  Installation bleibt unberührt.
- Schlägt das Einspielen mitten im Vorgang fehl, wird der vorherige Stand
  automatisch zurückgeholt.
- Der Starter merkt sich, für welche Version die Python-Umgebung eingerichtet
  wurde, und zieht nach einem Update die Pakete aus `requirements.txt` nach.

## Sicherheitsnetz

Alle eingreifenden Funktionen sind umkehrbar angelegt:

- Vor Registry-Änderungen wird der betroffene Zweig als `.reg` gesichert.
- Vor Debloat und Deinstallation wird – soweit der Systemschutz aktiv ist – ein
  Wiederherstellungspunkt gesetzt.
- Beim Entfernen von Resten werden Ordner **verschoben**, nicht gelöscht; sie
  liegen anschließend unter `%LOCALAPPDATA%\Hoferium\backups`.
- Tweaks lassen sich einzeln zurücksetzen.
- Ein zweiter Klick auf *Abbrechen* beendet laufende Installer sofort.

## Aufbau

Auf dem Stick sind nur zwei Dinge sichtbar:

```
hoferium.bat        ← starten
LIESMICH.txt        ← Kurzanleitung
```

Alles Übrige wird beim ersten Start ausgeblendet (`attrib +h`) und liegt
darunter:

```
nucleus/            Quellcode
  ui.py             Oberfläche und Animationen
  backup.py         Datensicherung
  sysinfo.py        Systemübersicht
  downloader.py     Software-Beschaffung
  uninstaller.py    Deinstallation und Debloat
  tweaks.py         Einstellungen, Cleaner, Fremdwerkzeuge
  registry.py       Installierte Programme auslesen
  winutils.py       Windows-Hilfsfunktionen
  updater.py        Versionsprüfung
apps.json           Programmliste (ohne Update pflegbar)
VERSION             Versionsstand für die Update-Prüfung
docs/               Bildmaterial für diese Seite
```

Das Ausblenden betrifft nur die Anzeige im Explorer – die Dateien
funktionieren unverändert weiter, und `git` arbeitet normal.

## Voraussetzungen

Windows 10 oder 11. Python wird bei Bedarf automatisch installiert; die
Oberfläche nutzt [customtkinter](https://github.com/TomSchimansky/CustomTkinter).
Für die Ersteinrichtung und zum Laden von Software wird eine Internetverbindung
benötigt – die Datensicherung selbst funktioniert offline.

## Lizenz

[MIT](LICENSE)
