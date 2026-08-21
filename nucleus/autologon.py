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


# ------------------------------------------------------------------
#  Einrichten
# ------------------------------------------------------------------
_PS_LSA = r"""
$ErrorActionPreference = 'Stop'
$pw   = [Console]::In.ReadLine()      # Kennwort kommt ueber die Standardeingabe
$user = '__USER__'
$dom  = '__DOMAIN__'

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
    [DllImport("advapi32.dll")]
    public static extern uint LsaClose(IntPtr PolicyHandle);
    [DllImport("advapi32.dll")]
    public static extern int LsaNtStatusToWinError(uint Status);

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
}
'@
Add-Type -TypeDefinition $code -Language CSharp | Out-Null

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
"""


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
    if "LSA_OK" in (res.out or ""):
        return _bestaetigen(benutzer, log)

    grund = (res.out or res.err or "").strip().splitlines()
    log(f"Direkter Weg nicht moeglich ({grund[-1][:110] if grund else res.rc}) - "
        f"versuche es mit dem Microsoft-Werkzeug.", "warn")
    if _via_sysinternals(benutzer, domaene, kennwort, log):
        return _bestaetigen(benutzer, log)
    return False


def _bestaetigen(benutzer: str, log) -> bool:
    """Nach dem Einrichten pruefen, ob es wirklich steht."""
    st = status()
    if st.aktiv and st.benutzer.lower() == benutzer.lower():
        log("Automatische Anmeldung ist eingerichtet.", "ok")
        if st.klartext_passwort:
            log("Achtung: In der Registry liegt noch ein Kennwort im Klartext "
                "(von einem anderen Werkzeug). Es wurde nicht von Hoferium "
                "geschrieben.", "warn")
        log("Beim naechsten Start meldet Windows sich ohne Kennworteingabe an.")
        return True
    log("Die Einstellung liess sich nicht bestaetigen.", "err")
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
Write-Output 'AUS_OK'
"""
    res = powershell(script, timeout=120)
    if "AUS_OK" not in (res.out or ""):
        log("Abschalten fehlgeschlagen.", "err")
        return False
    if status().aktiv:
        log("Die Einstellung steht noch - bitte in netplwiz nachsehen.", "warn")
        return False
    log("Automatische Anmeldung ist abgeschaltet - beim naechsten Start wird "
        "wieder nach dem Kennwort gefragt.", "ok")
    return True
