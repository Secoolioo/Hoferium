"""Automatische Windows-Anmeldung einrichten und wieder abschalten.

Windows kennt zwei Wege dorthin:

  * Der verbreitete legt das Kennwort als Klartext nach
    HKLM\\...\\Winlogon\\DefaultPassword. Jeder, der den Schluessel lesen darf,
    hat damit das Kennwort. Diesen Weg geht Hoferium NICHT.

  * Der richtige legt es als LSA-Geheimnis ab (verschluesselt, nur fuer das
    System lesbar) - genau das macht auch Microsofts eigenes Werkzeug
    "Autologon" von Sysinternals.

Dieses Modul nimmt den zweiten Weg: zuerst direkt ueber die LSA-Schnittstelle,
und wenn das scheitert, ueber das Sysinternals-Werkzeug. Das Kennwort wandert
dabei ueber die Standardeingabe - nie als Befehlszeilenargument, wo andere
Prozesse es mitlesen koennten - und wird nirgends protokolliert.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .winutils import IS_WINDOWS, powershell, run

WINLOGON = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
SYSINTERNALS_ZIP = "https://download.sysinternals.com/files/AutoLogon.zip"


@dataclass
class LogonState:
    aktiv: bool = False
    benutzer: str = ""
    klartext_passwort: bool = False     # liegt ein DefaultPassword im Klartext?
    fehler: str = ""


def status() -> LogonState:
    """Liest den aktuellen Zustand aus der Registry."""
    if not IS_WINDOWS:
        return LogonState(fehler="nur unter Windows")
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WINLOGON)
        try:
            def hole(name):
                try:
                    return str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    return ""
            auto = hole("AutoAdminLogon")
            benutzer = hole("DefaultUserName")
            klartext = bool(hole("DefaultPassword"))
        finally:
            winreg.CloseKey(key)
        return LogonState(aktiv=(auto == "1"), benutzer=benutzer,
                          klartext_passwort=klartext)
    except OSError as e:
        return LogonState(fehler=str(e))


def aktuelle_kennung() -> tuple:
    """(Benutzername, Domaene bzw. Rechnername) des angemeldeten Kontos."""
    benutzer = os.environ.get("USERNAME", "")
    domaene = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME", "")
    return benutzer, domaene


def diagnose(reporter=None) -> dict:
    """Liest alles aus, was ueber die automatische Anmeldung entscheidet.

    Gedacht fuer den Fall "es passiert nichts": statt zu raten, zeigt das
    Programm den Ist-Zustand und benennt, was ihn blockiert.
    """
    def sag(msg, art="log"):
        if reporter is not None:
            getattr(reporter, art, reporter.log)(msg)

    werte: dict = {}
    if not IS_WINDOWS:
        sag("Diagnose nur unter Windows moeglich.", "warn")
        return werte

    script = r"""
$ErrorActionPreference='SilentlyContinue'
$w = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$p = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
$o = [ordered]@{}
$o['AutoAdminLogon']    = (Get-ItemProperty $w).AutoAdminLogon
$o['DefaultUserName']   = (Get-ItemProperty $w).DefaultUserName
$o['DefaultDomainName'] = (Get-ItemProperty $w).DefaultDomainName
$o['AutoLogonCount']    = (Get-ItemProperty $w).AutoLogonCount
$o['KennwortImKlartext'] = if ((Get-ItemProperty $w).DefaultPassword) { 'ja' } else { 'nein' }
$o['HelloZwang']        = (Get-ItemProperty $p).DevicePasswordLessBuildVersion
$o['Angemeldet']        = whoami
$o['Kontotyp'] = try {
    if ($env:USERDOMAIN -and $env:USERDOMAIN -ne $env:COMPUTERNAME) { 'Domaenenkonto' }
    else {
        # -ErrorAction Stop ist noetig, weil $ErrorActionPreference oben den Fehler
        # sonst schluckt und der catch-Zweig nie greifen wuerde.
        $u = Get-LocalUser -Name $env:USERNAME -ErrorAction Stop
        if ($u.PrincipalSource -eq 'MicrosoftAccount') { 'Microsoft-Konto' } else { 'lokales Konto' }
    }
} catch { 'unbekannt' }
# Die Ngc-Container liegen im Profil von LocalService, nicht im LOCALAPPDATA des
# Benutzers - der Ordnerinhalt ist nur fuer SYSTEM lesbar, die Existenz genuegt.
$ngc = "$env:WINDIR\ServiceProfiles\LocalService\AppData\Local\Microsoft\Ngc"
$o['PIN_eingerichtet'] = if (Test-Path $ngc) { 'moeglich' } else { 'nein' }
$o | ConvertTo-Json -Compress
"""
    res = powershell(script, timeout=120)
    try:
        import json
        werte = json.loads((res.out or "").strip() or "{}")
    except Exception:
        sag(f"Diagnose nicht lesbar: {(res.err or res.out)[:120]}", "warn")
        return werte

    sag("--- Zustand der automatischen Anmeldung ---", "head")
    for name, wert in werte.items():
        sag(f"  {name:<20} {wert if wert not in (None, '') else '(nicht gesetzt)'}")

    # Die haeufigsten Blockierer benennen
    if str(werte.get("HelloZwang")) not in ("0",):
        sag("Blockiert: Windows erzwingt die Hello-Anmeldung. Der Wert "
            "DevicePasswordLessBuildVersion muss 0 sein - das setzt "
            "'EINRICHTEN' inzwischen selbst.", "warn")
    if str(werte.get("AutoAdminLogon")) != "1":
        sag("AutoAdminLogon steht nicht auf 1 - die automatische Anmeldung "
            "ist gar nicht eingeschaltet.", "warn")
    if werte.get("Kontotyp") == "Microsoft-Konto":
        sag("Microsoft-Konto erkannt: Als Benutzer muss der Name stehen, den "
            "'whoami' nach dem Backslash zeigt - nicht die E-Mail-Adresse.", "warn")
    if werte.get("AutoLogonCount") not in (None, ""):
        sag("AutoLogonCount ist gesetzt - die Anmeldung gilt dann nur "
            "begrenzt oft und schaltet sich danach ab.", "warn")
    if werte.get("PIN_eingerichtet") == "moeglich":
        sag("Es ist womoeglich eine Windows-Hello-PIN eingerichtet. Falls es "
            "weiter hakt: PIN unter Einstellungen > Konten > Anmeldeoptionen "
            "entfernen und erneut versuchen.", "warn")
    return werte


# ------------------------------------------------------------------
#  Einrichten
# ------------------------------------------------------------------
HOFERIUM_KEY = r"HKLM:\SOFTWARE\Hoferium\Autologon"

# Der C#-Teil wird von beiden Wegen gebraucht - Einrichten hinterlegt das
# Geheimnis, Abschalten loescht es. Deshalb steht er hier einmal als Baustein.
_CS_LSA = r"""
$code = @'
using System;
using System.Runtime.InteropServices;
public class HoferiumLsa {
    [StructLayout(LayoutKind.Sequential)]
    public struct LSA_UNICODE_STRING {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct LSA_OBJECT_ATTRIBUTES {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }
    [DllImport("advapi32.dll", SetLastError=true)]
    public static extern uint LsaOpenPolicy(IntPtr SystemName,
        ref LSA_OBJECT_ATTRIBUTES ObjectAttributes, uint DesiredAccess,
        out IntPtr PolicyHandle);
    [DllImport("advapi32.dll", SetLastError=true)]
    public static extern uint LsaStorePrivateData(IntPtr PolicyHandle,
        ref LSA_UNICODE_STRING KeyName, ref LSA_UNICODE_STRING PrivateData);
    // Zweite Deklaration mit IntPtr statt ref: nur so laesst sich NULL
    // uebergeben, und NULL ist laut MSDN das Signal, das Geheimnis samt
    // Schluessel zu loeschen.
    [DllImport("advapi32.dll", SetLastError=true)]
    public static extern uint LsaStorePrivateData(IntPtr PolicyHandle,
        ref LSA_UNICODE_STRING KeyName, IntPtr PrivateData);
    [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern bool LogonUser(string user, string domain,
        string password, int logonType, int logonProvider, out IntPtr token);
    [DllImport("advapi32.dll")]
    public static extern uint LsaClose(IntPtr PolicyHandle);
    [DllImport("advapi32.dll")]
    public static extern int LsaNtStatusToWinError(uint Status);
    [DllImport("kernel32.dll")]
    public static extern bool CloseHandle(IntPtr Handle);

    static LSA_UNICODE_STRING Str(string s) {
        LSA_UNICODE_STRING u = new LSA_UNICODE_STRING();
        u.Buffer = Marshal.StringToHGlobalUni(s);
        u.Length = (ushort)(s.Length * 2);
        u.MaximumLength = (ushort)((s.Length + 1) * 2);
        return u;
    }

    public static int Store(string key, string value) {
        LSA_OBJECT_ATTRIBUTES attr = new LSA_OBJECT_ATTRIBUTES();
        attr.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        uint st = LsaOpenPolicy(IntPtr.Zero, ref attr, 0x000007FF, out policy);
        if (st != 0) return LsaNtStatusToWinError(st);
        LSA_UNICODE_STRING k = Str(key);
        LSA_UNICODE_STRING v = Str(value);
        st = LsaStorePrivateData(policy, ref k, ref v);
        LsaClose(policy);
        Marshal.FreeHGlobal(k.Buffer);
        Marshal.ZeroFreeGlobalAllocUnicode(v.Buffer);   // Kennwort ueberschreiben
        return LsaNtStatusToWinError(st);
    }

    public static int Delete(string key) {
        LSA_OBJECT_ATTRIBUTES attr = new LSA_OBJECT_ATTRIBUTES();
        attr.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        uint st = LsaOpenPolicy(IntPtr.Zero, ref attr, 0x000007FF, out policy);
        if (st != 0) return LsaNtStatusToWinError(st);
        LSA_UNICODE_STRING k = Str(key);
        st = LsaStorePrivateData(policy, ref k, IntPtr.Zero);
        LsaClose(policy);
        Marshal.FreeHGlobal(k.Buffer);
        return LsaNtStatusToWinError(st);
    }

    // 0 = Kennwort stimmt, sonst der Windows-Fehlercode
    // (1326 = Benutzername oder Kennwort falsch).
    public static int Validate(string user, string domain, string password) {
        IntPtr token = IntPtr.Zero;
        if (LogonUser(user, domain, password, 2, 0, out token)) {
            CloseHandle(token);     // LOGON32_LOGON_INTERACTIVE, LOGON32_PROVIDER_DEFAULT
            return 0;
        }
        return Marshal.GetLastWin32Error();
    }
}
'@
Add-Type -TypeDefinition $code -Language CSharp | Out-Null
"""

# Stellt den beim Einrichten gemerkten Hello-Zwang wieder her. Der Wert wird
# als Text gesichert, damit der Fall "gab es vorher gar nicht" als leerer Text
# vom echten Wert 0 unterscheidbar bleibt.
_PS_HELLO_ZURUECK = r"""
$pl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
$hf = '__HOFERIUM_KEY__'
$merk = (Get-ItemProperty -Path $hf -Name 'HelloZwangVorher' -ErrorAction SilentlyContinue).HelloZwangVorher
if ($null -ne $merk) {
    if ($merk -eq '') {
        Remove-ItemProperty -Path $pl -Name 'DevicePasswordLessBuildVersion' -ErrorAction SilentlyContinue
    } else {
        Set-ItemProperty -Path $pl -Name 'DevicePasswordLessBuildVersion' -Value ([int]$merk) -Type DWord -ErrorAction SilentlyContinue
    }
    Remove-ItemProperty -Path $hf -Name 'HelloZwangVorher' -ErrorAction SilentlyContinue
    Write-Output 'HELLO_ZURUECK'
}
""".replace("__HOFERIUM_KEY__", HOFERIUM_KEY)

_PS_LSA = (r"""
$ErrorActionPreference = 'Stop'
$pw   = Read-HoferiumStdin      # Kennwort kommt Base64-kodiert ueber die Standardeingabe
$user = '__USER__'
$dom  = '__DOMAIN__'

# Ohne diesen Schritt bleibt alles Weitere wirkungslos: Solange Windows
# "Nur Windows Hello-Anmeldung fuer Microsoft-Konten zulassen" erzwingt
# (Standard auf Windows 11), ignoriert es AutoAdminLogon vollstaendig.
$pl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
$hf = '__HOFERIUM_KEY__'
if (-not (Test-Path $pl)) { New-Item -Path $pl -Force | Out-Null }
if (-not (Test-Path $hf)) { New-Item -Path $hf -Force | Out-Null }
$alt = (Get-ItemProperty -Path $pl -Name 'DevicePasswordLessBuildVersion' -ErrorAction SilentlyContinue).DevicePasswordLessBuildVersion
# Nur beim ersten Lauf sichern - ein zweiter Durchgang wuerde sonst die eigene
# 0 als Ausgangswert merken und das Zurueckstellen wertlos machen.
if ($null -eq (Get-ItemProperty -Path $hf -Name 'HelloZwangVorher' -ErrorAction SilentlyContinue).HelloZwangVorher) {
    $merk = if ($null -eq $alt) { '' } else { [string]$alt }
    Set-ItemProperty -Path $hf -Name 'HelloZwangVorher' -Value $merk -Type String
}
Set-ItemProperty -Path $pl -Name 'DevicePasswordLessBuildVersion' -Value 0 -Type DWord
""" + _CS_LSA + r"""
# Erst pruefen, dann speichern: ein Tippfehler wuerde sonst als eingerichtete
# Anmeldung gemeldet und faellt erst beim naechsten Start auf.
$vdom = if ($dom) { $dom } else { '.' }
$vrc = [HoferiumLsa]::Validate($user, $vdom, $pw)
# Zweiter Versuch als Microsoft-Konto. Windows haelt dafuer ein lokales
# Spiegelkonto, dessen Anmeldung ueber '.' normalerweise klappt - wenn nicht,
# darf ein gueltiges Kennwort nicht faelschlich als Tippfehler gelten.
if ($vrc -eq 1326 -and -not $dom) {
    $vrc = [HoferiumLsa]::Validate($user, 'MicrosoftAccount', $pw)
}
if ($vrc -eq 1326) {
    $pw = $null
""" + _PS_HELLO_ZURUECK + r"""
    Write-Output 'PW_FALSCH'
    exit 1
}
if ($vrc -ne 0) { Write-Output ("PW_UNGEPRUEFT:" + $vrc) }

$rc = [HoferiumLsa]::Store('DefaultPassword', $pw)
$pw = $null
if ($rc -ne 0) { Write-Output ("LSA_FEHLER:" + $rc); exit 1 }

$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -Path $k -Name 'AutoAdminLogon' -Value '1' -Type String
Set-ItemProperty -Path $k -Name 'DefaultUserName' -Value $user -Type String
Set-ItemProperty -Path $k -Name 'DefaultDomainName' -Value $dom -Type String
Remove-ItemProperty -Path $k -Name 'DefaultPassword' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $k -Name 'AutoLogonCount' -ErrorAction SilentlyContinue
Write-Output 'LSA_OK'
""").replace("__HOFERIUM_KEY__", HOFERIUM_KEY)


def einrichten(benutzer: str, domaene: str, kennwort: str, reporter=None) -> bool:
    """Schaltet die automatische Anmeldung ein.

    Das Kennwort wird ausschliesslich ueber die Standardeingabe uebergeben und
    weder protokolliert noch in die Registry geschrieben.
    """
    def log(msg, art="log"):
        if reporter is not None:
            getattr(reporter, art, reporter.log)(msg)

    if not IS_WINDOWS:
        log("Nur unter Windows moeglich.", "err")
        return False
    if not benutzer or not kennwort:
        log("Benutzername und Kennwort werden benoetigt.", "err")
        return False

    log("Hinterlege das Kennwort verschluesselt (LSA) ...")
    script = (_PS_LSA
              .replace("__USER__", benutzer.replace("'", "''"))
              .replace("__DOMAIN__", (domaene or "").replace("'", "''")))
    res = powershell(script, timeout=180, stdin_text=kennwort + "\n")
    ausgabe = res.out or ""
    if "PW_FALSCH" in ausgabe:
        # Abbruch vor dem Speichern - sonst laege ein Tippfehler im System und
        # der Fehler fiele erst beim naechsten Start auf.
        # Windows-Code 1326 nennt Benutzername und Kennwort in einem Atemzug -
        # die Meldung darf deshalb nicht allein aufs Kennwort zeigen.
        log("Windows lehnt diese Angaben ab - Benutzername oder Kennwort "
            "stimmt nicht. Es wurde nichts geaendert. Bei einem "
            "Microsoft-Konto muss als Benutzer der kurze Name stehen, den "
            "'whoami' nach dem Backslash zeigt - nicht die E-Mail-Adresse.",
            "err")
        return False
    for zeile in ausgabe.splitlines():
        if zeile.startswith("PW_UNGEPRUEFT:"):
            log("Das Kennwort liess sich vorher nicht ueberpruefen "
                f"(Windows-Code {zeile.split(':', 1)[1].strip()}) - es wird "
                "trotzdem hinterlegt. Klappt die Anmeldung nicht, ist "
                "womoeglich das Kennwort falsch.", "warn")
    if "LSA_OK" in ausgabe:
        return _bestaetigen(benutzer, log)

    grund = (ausgabe or res.err or "").strip().splitlines()
    log(f"Direkter Weg nicht moeglich ({grund[-1][:110] if grund else res.rc}) - "
        f"versuche es mit dem Microsoft-Werkzeug.", "warn")
    if _via_sysinternals(benutzer, domaene, kennwort, log):
        return _bestaetigen(benutzer, log)
    if _hello_zwang_zurueck():
        log("Die Hello-Einstellung steht wieder auf ihrem Ausgangswert - ohne "
            "eingerichtete Anmeldung darf sie nicht abgeschaltet bleiben.")
    return False


def _hello_zwang_zurueck() -> bool:
    """Setzt DevicePasswordLessBuildVersion auf den gemerkten Ausgangswert.

    Liefert True, wenn es etwas zurueckzustellen gab.
    """
    res = powershell(_PS_HELLO_ZURUECK, timeout=60)
    return "HELLO_ZURUECK" in (res.out or "")


def _bestaetigen(benutzer: str, log) -> bool:
    """Nach dem Einrichten pruefen, ob wirklich alles Noetige steht.

    Geprueft wird nicht nur AutoAdminLogon: Ohne abgeschalteten Hello-Zwang
    ignoriert Windows die Einstellung, und dann waere eine Erfolgsmeldung
    schlicht gelogen.
    """
    st = status()
    if not (st.aktiv and st.benutzer.lower() == benutzer.lower()):
        log("Die Einstellung liess sich nicht bestaetigen.", "err")
        return False

    hello_frei = _hello_zwang_aus()
    if not hello_frei:
        log("AutoAdminLogon steht, aber Windows erzwingt weiterhin die "
            "Hello-Anmeldung - dann greift die automatische Anmeldung nicht. "
            "Bitte 'WARUM GEHT ES NICHT?' druecken.", "err")
        return False

    log("Automatische Anmeldung ist eingerichtet.", "ok")
    if st.klartext_passwort:
        log("Achtung: In der Registry liegt noch ein Kennwort im Klartext "
            "(von einem anderen Werkzeug). Es wurde nicht von Hoferium "
            "geschrieben - du kannst es dort loeschen.", "warn")
    log("Beim naechsten Start meldet Windows sich ohne Kennworteingabe an. "
        "Klappt es nicht, hilft der Knopf 'WARUM GEHT ES NICHT?'.")
    return True


def _hello_zwang_aus() -> bool:
    """True, wenn Windows die Hello-Anmeldung NICHT mehr erzwingt."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device",
            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        try:
            wert, _ = winreg.QueryValueEx(key, "DevicePasswordLessBuildVersion")
        finally:
            winreg.CloseKey(key)
        return int(wert) == 0
    except OSError:
        return False


def _via_sysinternals(benutzer: str, domaene: str, kennwort: str, log) -> bool:
    """Rueckfallweg ueber Microsofts Autologon-Werkzeug.

    Das Kennwort steht dabei kurzzeitig in der Befehlszeile des Kindprozesses -
    deshalb wird dieser Weg nur genommen, wenn der direkte scheitert.
    """
    import shutil
    import tempfile
    import urllib.request
    import zipfile
    from pathlib import Path

    ordner = Path(tempfile.mkdtemp(prefix="hoferium_al_"))
    try:
        log("Lade Autologon von Sysinternals ...")
        archiv = ordner / "autologon.zip"
        req = urllib.request.Request(SYSINTERNALS_ZIP,
                                     headers={"User-Agent": "Hoferium"})
        with urllib.request.urlopen(req, timeout=90) as resp, open(archiv, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        with zipfile.ZipFile(archiv) as zf:
            zf.extractall(ordner)
        exe = next((p for p in ordner.glob("*.exe")
                    if "autologon" in p.name.lower() and "64" in p.name), None)
        if exe is None:
            exe = next((p for p in ordner.glob("*.exe")
                        if "autologon" in p.name.lower()), None)
        if exe is None:
            log("Im Archiv war kein Autologon-Programm.", "err")
            return False
        res = run([str(exe), "-accepteula", benutzer, domaene or ".", kennwort],
                  timeout=120)
        if res.rc != 0:
            log(f"Autologon meldete Code {res.rc}.", "warn")
        return res.rc == 0
    except Exception as e:
        log(f"Rueckfallweg fehlgeschlagen: {e}", "err")
        return False
    finally:
        shutil.rmtree(ordner, ignore_errors=True)


def abschalten(reporter=None) -> bool:
    """Schaltet die automatische Anmeldung wieder aus und raeumt auf."""
    def log(msg, art="log"):
        if reporter is not None:
            getattr(reporter, art, reporter.log)(msg)

    if not IS_WINDOWS:
        return False
    script = r"""
$ErrorActionPreference='SilentlyContinue'
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -Path $k -Name 'AutoAdminLogon' -Value '0' -Type String
Remove-ItemProperty -Path $k -Name 'DefaultPassword'
Remove-ItemProperty -Path $k -Name 'AutoLogonCount'
""" + _CS_LSA + r"""
# Das Kennwort liegt nicht in der Registry, sondern als LSA-Geheimnis - ohne
# dieses Loeschen bliebe es auf einem weitergegebenen Rechner abrufbar.
# Der Aufruf steht in try/catch, weil ein fehlgeschlagenes Add-Type oben nur
# eine Meldung erzeugt und der Typ dann schlicht fehlt.
$rc = -1
try { $rc = [HoferiumLsa]::Delete('DefaultPassword') } catch { $rc = -1 }
# Code 2 heisst: es lag gar kein Geheimnis vor - das ist der gewuenschte Zustand.
if ($rc -ne 0 -and $rc -ne 2) { Write-Output ('LSA_REST:' + $rc) }
Write-Output 'AUS_OK'
"""
    res = powershell(script, timeout=120)
    ausgabe = res.out or ""
    if "AUS_OK" not in ausgabe:
        log("Abschalten fehlgeschlagen.", "err")
        return False
    if _hello_zwang_zurueck():
        log("Die beim Einrichten geaenderte Hello-Einstellung steht wieder auf "
            "ihrem Ausgangswert.")
    if status().aktiv:
        log("Die Einstellung steht noch - bitte in netplwiz nachsehen.", "warn")
        return False
    for zeile in ausgabe.splitlines():
        if zeile.startswith("LSA_REST:"):
            log("Die automatische Anmeldung ist aus, aber das hinterlegte "
                f"Kennwort liess sich nicht loeschen (Code {zeile.split(':', 1)[1].strip()}). "
                "Es liegt verschluesselt weiter im System - bitte Hoferium als "
                "Administrator starten und noch einmal abschalten.", "err")
            return False
    log("Automatische Anmeldung ist abgeschaltet - beim naechsten Start wird "
        "wieder nach dem Kennwort gefragt.", "ok")
    return True
