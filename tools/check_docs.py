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

    # ---------- Version an allen drei Stellen ----------
    version_datei = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    init = (ROOT / "nucleus" / "__init__.py").read_text(encoding="utf-8")
    version_code = re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)
    badge = re.search(r"Version-([\d.]+)-", readme)
    pruefe("VERSION-Datei = __version__ im Code",
           version_datei == version_code,
           f"VERSION={version_datei}, Code={version_code}")
    pruefe("Badge im README = VERSION-Datei",
           bool(badge) and badge.group(1) == version_datei,
           f"Badge={badge.group(1) if badge else '?'}, VERSION={version_datei}")

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
    fehlende_kat = [k for k in kategorien if k.replace("ae", "ä") not in readme
                    and k not in readme]
    pruefe("alle Tweak-Kategorien im README genannt", not fehlende_kat,
           f"fehlen: {fehlende_kat}")

    sysinternals = [t for t in EXT_TOOLS if "Sysinternals" in t.name]
    zahlwort = {1: "ein", 2: "zwei", 3: "drei", 4: "vier", 5: "fuenf"}
    pruefe(f"Sysinternals-Anzahl ({len(sysinternals)})",
           zahlwort.get(len(sysinternals), "?") in readme.lower()
           or str(len(sysinternals)) in readme,
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
    ui = (ROOT / "nucleus" / "ui.py").read_text(encoding="utf-8")
    seiten = re.findall(r'self\._pages\["(\w+)"\] = self\._build_', ui)
    # "info" und "log" sind Nebenseiten und brauchen keinen eigenen Abschnitt
    wichtig = {"backup": "Datensicherung", "restore": "Zurückholen",
               "software": "Software", "uninstall": "Deinstallieren",
               "debloat": "Debloat", "tweaks": "Tweaks", "cleaner": "Cleaner",
               "tools": "Werkzeuge"}
    fehlende_seiten = [b for a, b in wichtig.items()
                       if a in seiten and b not in readme]
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
        kern = muster.strip("*_.")
        pruefe(f"Update-Schutz '{muster}' im README erklaert",
               kern in readme or muster in readme)

    # ---------- Zeilenenden fuer Windows ----------
    for name in ("hoferium.bat", "LIESMICH.txt", "requirements.txt"):
        roh = (ROOT / name).read_bytes()
        pruefe(f"{name} hat Windows-Zeilenenden (CRLF)",
               b"\r\n" in roh and b"\n" not in roh.replace(b"\r\n", b""),
               "Ohne CRLF kann die Batch-Datei stolpern: unix2dos ausfuehren.")

    # ---------- Herkunftshinweise ----------
    pruefe("keine Hinweise auf Werkzeuge der Erstellung",
           not re.search(r"(?i)claude|anthropic|copilot|chatgpt|generated by",
                         beide))

    # ---------- winget-Katalog ----------
    apps, _quelle = downloader.load_catalog()
    pruefe("apps.json ist lesbar und nicht leer", len(apps) > 0)

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
