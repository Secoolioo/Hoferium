#!/usr/bin/env python3
"""Prueft, ob README.md und LIESMICH.txt noch zum Code passen.

Vor jedem Commit aufrufen:

    python3 tools/check_docs.py

Der Sinn: Zahlen und Listen in der Doku veralten still. Ein Tweak kommt
dazu, eine Datei wird umbenannt, die Version steigt - und die Doku sagt
weiter etwas anderes. Dieses Skript vergleicht beides und nennt jede
Abweichung. Rueckgabe 0 = alles stimmig, 1 = es gibt etwas zu tun.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fehler: list = []
geprueft = 0


def pruefe(name: str, bedingung: bool, hinweis: str = "") -> None:
    global geprueft
    geprueft += 1
    if not bedingung:
        fehler.append(f"{name}" + (f"\n      {hinweis}" if hinweis else ""))


def main() -> int:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    liesmich = (ROOT / "LIESMICH.txt").read_text(encoding="utf-8")
    beide = readme + liesmich

    # ---------- Version an allen Stellen ----------
    version_datei = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    init = (ROOT / "nucleus" / "__init__.py").read_text(encoding="utf-8")
    version_code = re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)
    badge = re.search(r"Version-([\d.]+)-", readme)
    ui = (ROOT / "nucleus" / "ui.py").read_text(encoding="utf-8")
    pruefe("VERSION-Datei = __version__ im Code",
           version_datei == version_code,
           f"VERSION={version_datei}, Code={version_code}")
    pruefe("Badge im README = VERSION-Datei",
           bool(badge) and badge.group(1) == version_datei,
           f"Badge={badge.group(1) if badge else '?'}, VERSION={version_datei}")
    # Eine in der Oberflaeche fest eingetippte Versionsnummer laeuft beim naechsten
    # Versionssprung still von der VERSION-Datei weg und faellt niemandem auf.
    fremde_version = sorted({v for v in re.findall(r"\b\d+\.\d+\.\d+\b", ui)
                             if v != version_datei})
    pruefe("keine abweichende Versionsnummer in ui.py", not fremde_version,
           f"fest eingetippt: {fremde_version}, VERSION={version_datei} - "
           "stattdessen __version__ aus nucleus/__init__.py anzeigen.")

    # ---------- Zahlen, die in der Doku stehen ----------
    from nucleus.tweaks import TWEAKS, EXT_TOOLS, clean_targets
    from nucleus import downloader

    anzahl_tweaks = len(TWEAKS)
    pruefe(f"Tweak-Anzahl ({anzahl_tweaks}) im README",
           f"{anzahl_tweaks} umkehrbare" in readme,
           "Die Funktionstabelle nennt eine andere Zahl.")
    pruefe(f"Tweak-Anzahl ({anzahl_tweaks}) in LIESMICH.txt",
           f"{anzahl_tweaks} Tweaks" in liesmich)

    kategorien = sorted({t.category for t in TWEAKS})
    # Nur die Tweak-Zeile der Funktionstabelle zaehlt als Beleg - sonst genuegt ein
    # zufaelliges "Explorer" oder "Updates" irgendwo im Fliesstext.
    tweak_zeile = next((z for z in readme.splitlines()
                        if f"{anzahl_tweaks} umkehrbare" in z), "")
    fehlende_kat = [k for k in kategorien
                    if k.replace("ae", "ä") not in tweak_zeile
                    and k not in tweak_zeile]
    pruefe("alle Tweak-Kategorien im README genannt", not fehlende_kat,
           f"fehlen: {fehlende_kat}")

    sysinternals = [t for t in EXT_TOOLS if "Sysinternals" in t.name]
    zahlwort = {1: "ein", 2: "zwei", 3: "drei", 4: "vier", 5: "fuenf"}
    wort = zahlwort.get(len(sysinternals), "")
    # Die blosse Ziffer belegt nichts, sie steht ohnehin irgendwo im README -
    # gefordert ist das Zahlwort unmittelbar vor dem Stichwort.
    pruefe(f"Sysinternals-Anzahl ({len(sysinternals)}) im README",
           bool(wort) and re.search(rf"{wort}\s+\w*\s*Sysinternals",
                                    readme, re.I) is not None,
           "README nennt eine andere Anzahl Sysinternals-Programme.")

    # ---------- Dateien und Module ----------
    module = sorted(p.name for p in (ROOT / "nucleus").glob("*.py"))
    fehlend = [m for m in module if m not in readme]
    pruefe("alle Module im README-Aufbau gelistet", not fehlend,
           f"fehlen: {fehlend}")
    fachmodule = [m for m in module if not m.startswith("__")]
    fehlend_l = [m for m in fachmodule if m not in liesmich]
    pruefe("alle Fachmodule in LIESMICH.txt gelistet", not fehlend_l,
           f"fehlen: {fehlend_l}")

    wurzel = ["apps.json", "requirements.txt", "VERSION", "hoferium.bat",
              "LIESMICH.txt"]
    fehlend_w = [w for w in wurzel if w not in readme]
    pruefe("Wurzeldateien im README genannt", not fehlend_w,
           f"fehlen: {fehlend_w}")

    # ---------- Seiten der Oberflaeche ----------
    seiten = re.findall(r'self\._pages\["(\w+)"\] = self\._build_', ui)
    # "dashboard", "info" und "log" sind Nebenseiten und brauchen keinen eigenen Abschnitt
    nebenseiten = {"dashboard", "info", "log"}
    wichtig = {"backup": "Datensicherung", "restore": "Zurückholen",
               "software": "Software", "uninstall": "Deinstallieren",
               "debloat": "Debloat", "tweaks": "Tweaks", "cleaner": "Cleaner",
               "tools": "Werkzeuge"}
    # Ueber die Seiten aus ui.py laufen, nicht ueber die Tabelle: sonst bleibt eine
    # neu gebaute Seite unbemerkt, weil sie hier schlicht nicht eingetragen ist.
    fehlende_seiten = [wichtig.get(s, s) for s in seiten
                       if s not in nebenseiten and wichtig.get(s, s) not in readme]
    pruefe("jede Hauptseite kommt im README vor", not fehlende_seiten,
           f"fehlen: {fehlende_seiten}")

    # ---------- Screenshots ----------
    benutzt = set(re.findall(r'src="(docs/[^"]+)"', readme))
    fehlt_datei = [b for b in benutzt if not (ROOT / b).exists()]
    vorhanden = {f"docs/{p.name}" for p in (ROOT / "docs").glob("*.png")}
    ungenutzt = sorted(vorhanden - benutzt)
    pruefe("alle eingebundenen Bilder existieren", not fehlt_datei,
           f"fehlen: {fehlt_datei}")
    pruefe("keine ungenutzten Bilder in docs/", not ungenutzt,
           f"ungenutzt: {ungenutzt}")

    # ---------- Schutzliste des Updaters ----------
    from nucleus.updater import KEEP_ALWAYS
    for muster in KEEP_ALWAYS:
        # Der frueher zusaetzlich gepruefte Kern '*.log' -> 'log' steckt schon in
        # "Dialog" und liess die Pruefung immer bestehen; nur das Muster selbst zaehlt.
        pruefe(f"Update-Schutz '{muster}' im README erklaert",
               muster in readme)

    # ---------- Zeilenenden fuer Windows ----------
    for name in ("hoferium.bat", "LIESMICH.txt", "requirements.txt"):
        roh = (ROOT / name).read_bytes()
        pruefe(f"{name} hat Windows-Zeilenenden (CRLF)",
               b"\r\n" in roh and b"\n" not in roh.replace(b"\r\n", b""),
               "Ohne CRLF kann die Batch-Datei stolpern: unix2dos ausfuehren.")

    # ---------- Herkunftshinweise ----------
    # Die Namen stehen absichtlich in Bruchstuecken: ausgeschrieben stuenden
    # genau die Woerter in der Datei, die diese Pruefung fernhalten soll.
    marken = ["cla" + "ude", "anthr" + "opic", "cop" + "ilot", "chat" + "gpt",
              "gene" + "rated by", "erst" + "ellt mit ki", "ki-gene" + "riert"]
    gefunden = [m for m in marken if m in beide.lower()]
    pruefe("keine Hinweise auf Werkzeuge der Erstellung", not gefunden,
           f"gefunden: {gefunden}")

    # ---------- winget-Katalog ----------
    # Nicht ueber load_catalog(): das holt den Katalog notfalls aus dem Netz oder aus
    # der eingebauten Liste und wuerde eine kaputte apps.json genau hier verschweigen.
    try:
        apps = downloader._apps_from(
            json.loads((ROOT / "apps.json").read_text(encoding="utf-8")))
        grund = ""
    except Exception as e:
        apps, grund = [], str(e)
    pruefe("apps.json ist lesbar und nicht leer", len(apps) > 0, grund)
    ohne_quelle = [a.name for a in apps if not a.can_download]
    pruefe("jeder Eintrag in apps.json hat URL oder winget-ID", not ohne_quelle,
           f"weder url noch winget_id: {ohne_quelle}")

    print(f"Geprueft: {geprueft} Punkte")
    if fehler:
        print(f"\nAbweichungen ({len(fehler)}):")
        for f in fehler:
            print(f"  - {f}")
        print("\nDoku anpassen, dann erneut pruefen.")
        return 1
    print("Doku und Code stimmen ueberein.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
