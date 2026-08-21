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

    def __init__(self, logfile: Path | None = None):
        self.q: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._logfile = Path(logfile) if logfile else None

    # ---- vom Worker aufgerufen ----
    def log(self, msg, level: str = "info") -> None:
        line = str(msg)
        self.q.put(("log", level, line))
        if self._logfile is not None:
            try:
                ts = datetime.now().strftime("%H:%M:%S")
                with self._logfile.open("a", encoding="utf-8") as fh:
                    fh.write(f"[{ts}] [{level.upper()}] {line}\n")
            except Exception:
                pass

    def head(self, msg) -> None:
        self.log(msg, "head")

    def ok(self, msg) -> None:
        self.log(msg, "ok")

    def warn(self, msg) -> None:
        self.log(msg, "warn")

    def err(self, msg) -> None:
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
