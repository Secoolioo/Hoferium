"""Reporter (thread-sichere Bruecke Worker -> UI) und RunContext."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class Reporter:
    """Ein Worker-Thread meldet Log/Fortschritt hierher; die UI pollt die Queue.

    Nachrichtenformat in der Queue: (kind, a, b)
        kind == 'log'      -> a=level('info'|'ok'|'warn'|'err'|'head'), b=text
        kind == 'progress' -> a=float 0..1, b=optional text
        kind == 'status'   -> a=text, b=None
        kind == 'done'     -> a=summary(dict|None), b=None
    """

    # Ab dieser Groesse wird das Protokoll einmal zur Seite gelegt (.1) und neu
    # begonnen. Ein Stick laeuft ueber Jahre auf vielen Rechnern - ohne diese
    # Grenze waechst die Datei unbegrenzt.
    MAX_LOG_BYTES = 2 * 1024 * 1024

    def __init__(self, logfile: Path | None = None):
        self.q: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._logfiles: list[Path] = []
        # Zaehler statt blosser Anzeige: Der Aufrufer kann damit entscheiden, ob
        # ein Lauf wirklich sauber war. Ohne das galt jeder Schritt, der nicht
        # abgestuerzt ist, als erfolgreich - auch wenn er nur gewarnt hat.
        self.warnings = 0
        self.errors = 0
        if logfile:
            self.add_logfile(logfile)

    def add_logfile(self, path) -> None:
        """Haengt ein weiteres Protokollziel an. Der Sicherungsordner bekommt so
        eine eigene Kopie, die eine Neuinstallation ueberlebt - das Protokoll
        unter %LOCALAPPDATA% wird dabei geloescht."""
        p = Path(path)
        if p not in self._logfiles:
            self._logfiles.append(p)

    # ---- vom Worker aufgerufen ----
    def log(self, msg, level: str = "info") -> None:
        line = str(msg)
        self.q.put(("log", level, line))
        ts = datetime.now().strftime("%H:%M:%S")
        for f in self._logfiles:
            try:
                if f.exists() and f.stat().st_size > self.MAX_LOG_BYTES:
                    f.replace(f.with_suffix(f.suffix + ".1"))
                with f.open("a", encoding="utf-8") as fh:
                    fh.write(f"[{ts}] [{level.upper()}] {line}\n")
            except Exception:
                pass

    def head(self, msg) -> None:
        self.log(msg, "head")

    def ok(self, msg) -> None:
        self.log(msg, "ok")

    def warn(self, msg) -> None:
        self.warnings += 1
        self.log(msg, "warn")

    def err(self, msg) -> None:
        self.errors += 1
        self.log(msg, "err")

    def progress(self, frac: float, text: str | None = None) -> None:
        try:
            frac = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            frac = 0.0
        self.q.put(("progress", frac, text))

    def status(self, text: str) -> None:
        self.q.put(("status", text, None))

    def done(self, summary=None) -> None:
        """Meldet das Ende. Idempotent: Nur die ERSTE Meldung zaehlt, damit ein
        zusaetzliches done() aus dem Task-Runner nichts doppelt abschliesst."""
        if self._done.is_set():
            return
        self._done.set()
        self.q.put(("done", summary, None))

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    # ---- Steuerung ----
    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


@dataclass
class RunContext:
    output_dir: Path
    reporter: Reporter
    admin: bool = False

    def sub(self, name: str) -> Path:
        p = self.output_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p
