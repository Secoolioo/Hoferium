"""Einstiegspunkt:  python -m nucleus

Faengt Startfehler ab, schreibt einen Crash-Log und zeigt eine
Fehlermeldung, damit auch beim Start ohne Konsole (pythonw) etwas sichtbar ist.
"""
from __future__ import annotations

import os
import sys
import traceback


def _write_crash(tb: str) -> None:
    try:
        from pathlib import Path
        logdir = Path(os.environ.get("LOCALAPPDATA", ".")) / "Hoferium"
        logdir.mkdir(parents=True, exist_ok=True)
        (logdir / "crash.log").write_text(tb, encoding="utf-8")
    except Exception:
        pass


def _show_error(tb: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Hoferium - Fehler", tb[-1600:])
        root.destroy()
        return
    except Exception:
        pass
    try:                      # ohne Tk: Windows-Messagebox direkt aufrufen
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, tb[-1600:], "Hoferium - Fehler", 0x10)
    except Exception:
        try:
            print(tb, file=sys.stderr)
        except Exception:
            pass              # unter pythonw.exe gibt es kein stderr


def main() -> int:
    try:
        from nucleus.ui import HoferiumApp
        app = HoferiumApp()

        # Fehler aus Tk-Callbacks (also aus jedem Button-Klick) faengt Tkinter
        # sonst ab und schreibt sie nach stderr - unter pythonw.exe gibt es das
        # nicht, der Fehler verschwaende spurlos.
        def on_callback_error(*_args):
            tb = traceback.format_exc()
            _write_crash(tb)
            _show_error(tb)

        app.report_callback_exception = on_callback_error
        app.mainloop()
        return 0
    except Exception:
        tb = traceback.format_exc()
        _write_crash(tb)
        _show_error(tb)
        return 1


if __name__ == "__main__":
    sys.exit(main())
