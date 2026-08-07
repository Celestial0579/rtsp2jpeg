"""RTSP-Strom mit ffmpeg anzapfen und das jeweils neueste JPEG vorhalten."""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from .config import CameraConfig

log = logging.getLogger(__name__)

SOI = b"\xff\xd8"  # Start of Image
EOI = b"\xff\xd9"  # End of Image

_READ_CHUNK = 65536
_BACKOFF_START = 1.0
_BACKOFF_MAX = 15.0     # eine Tuerklingel darf nicht minutenlang blind sein
_STDERR_KEEP = 20       # so viele Zeilen ffmpeg-Ausgabe fuer die Fehlersuche
_MAX_BUFFER = 32 << 20  # Reissleine, falls nie ein EOI kommt


def find_ffmpeg() -> str:
    """Pfad zum ffmpeg-Programm, per ``FFMPEG_BIN`` ueberschreibbar."""
    return os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"


def split_jpegs(buffer: bytearray) -> tuple[list[bytes], bytearray]:
    """Loest vollstaendige JPEGs aus dem Puffer heraus.

    Rueckgabe sind die gefundenen Bilder und der verbleibende Rest. Innerhalb
    der Bilddaten kann kein unmaskiertes ``FF D9`` auftreten -- JPEG maskiert
    jedes ``FF`` im Entropiestrom als ``FF 00`` --, weshalb die Suche nach dem
    Endmarker hier ausreicht.
    """
    frames: list[bytes] = []
    start = buffer.find(SOI)
    if start == -1:
        # Nur Muell im Puffer; das letzte Byte koennte ein halber Marker sein.
        return frames, buffer[-1:] if buffer else bytearray()

    while True:
        end = buffer.find(EOI, start + 2)
        if end == -1:
            break
        frames.append(bytes(buffer[start : end + 2]))
        next_start = buffer.find(SOI, end + 2)
        if next_start == -1:
            return frames, bytearray(buffer[end + 2 :])
        start = next_start

    return frames, bytearray(buffer[start:])


def build_command(camera: CameraConfig, ffmpeg: str | None = None) -> list[str]:
    """Baut die ffmpeg-Befehlszeile fuer eine Kamera."""
    filters = [f"fps={camera.fps:g}"]
    if camera.rotate in (90, 270):
        filters.append("transpose=1" if camera.rotate == 90 else "transpose=2")
    elif camera.rotate == 180:
        filters.append("transpose=1,transpose=1")
    if camera.width or camera.height:
        width = camera.width or -2   # -2 haelt das Seitenverhaeltnis und
        height = camera.height or -2  # rundet auf gerade Pixelzahlen
        filters.append(f"scale={width}:{height}")

    cmd = [
        ffmpeg or find_ffmpeg(),
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
    ]
    # Haengende Verbindungen abbrechen, statt ewig zu warten. Die Option
    # heisst je nach Quelle anders: der RTSP-Demuxer kennt '-timeout', bei
    # HTTP wirkt '-rw_timeout' auf der Protokollebene. '-rw_timeout' an einen
    # RTSP-Eingang zu haengen laesst ffmpeg mit "Option not found" abbrechen.
    read_timeout_us = str(int(camera.read_timeout * 1_000_000))
    if camera.url.startswith(("rtsp://", "rtsps://")):
        cmd += ["-rtsp_transport", camera.transport, "-timeout", read_timeout_us]
    else:
        cmd += ["-rw_timeout", read_timeout_us]
    cmd += camera.input_args
    cmd += [
        "-i", camera.url,
        "-an", "-sn", "-dn",
        "-vf", ",".join(filters),
        "-q:v", str(camera.quality),
    ]
    cmd += camera.output_args
    cmd += ["-f", "mjpeg", "pipe:1"]
    return cmd


@dataclass(frozen=True)
class Frame:
    data: bytes
    timestamp: float

    @property
    def age(self) -> float:
        return max(0.0, time.monotonic() - self.timestamp)


class Grabber:
    """Haelt fuer eine Kamera das neueste JPEG bereit.

    ffmpeg laeuft nur, solange Bilder abgerufen werden: die erste Anfrage
    startet den Prozess, nach ``idle_timeout`` ohne Anfrage wird er wieder
    beendet. Bei ``idle_timeout = 0`` laeuft er dauerhaft.
    """

    def __init__(self, camera: CameraConfig, ffmpeg: str | None = None) -> None:
        self.camera = camera
        self._ffmpeg = ffmpeg or find_ffmpeg()
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._last_request = time.monotonic()
        self._wanted = camera.idle_timeout <= 0
        self._wanted_since = time.monotonic()
        self._running = False
        self._stopping = False
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=_STDERR_KEEP)
        self._frames_total = 0
        self._restarts = 0
        self._last_error: str | None = None

    # -- Steuerung ---------------------------------------------------------

    def start(self) -> None:
        """Startet den Hintergrund-Thread (bei Lazy-Betrieb noch ohne ffmpeg)."""
        with self._cond:
            if self._thread is not None:
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name=f"grabber-{self.camera.name}", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopping = True
            self._wanted = False
            self._cond.notify_all()
        self._terminate_process()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def touch(self) -> None:
        """Meldet Bedarf an -- startet ffmpeg bei Bedarf."""
        with self._cond:
            self._last_request = time.monotonic()
            if not self._wanted:
                self._wanted = True
                self._wanted_since = time.monotonic()
                self._cond.notify_all()

    # -- Abruf -------------------------------------------------------------

    def latest(self) -> Frame | None:
        with self._cond:
            return self._frame

    def get_frame(self, max_age: float | None = None, timeout: float | None = None) -> Frame:
        """Liefert ein Bild, das hoechstens ``max_age`` Sekunden alt ist.

        Wirft ``TimeoutError``, wenn innerhalb von ``timeout`` kein
        ausreichend frisches Bild eintrifft.
        """
        max_age = self.camera.max_age if max_age is None else max_age
        timeout = self.camera.timeout if timeout is None else timeout

        self.touch()
        self.start()

        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                frame = self._frame
                if frame is not None and frame.age <= max_age:
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)

        detail = self._last_error or "keine Antwort von der Kamera"
        raise TimeoutError(
            f"Kamera {self.camera.name!r}: innerhalb von {timeout:g}s kein "
            f"Bild juenger als {max_age:g}s -- {detail}"
        )

    def stream(self, timeout: float | None = None):
        """Generator ueber neu eintreffende Bilder (fuer MJPEG-Ausgabe)."""
        timeout = self.camera.timeout if timeout is None else timeout
        self.start()
        last_ts = 0.0
        while True:
            self.touch()
            deadline = time.monotonic() + timeout
            with self._cond:
                while True:
                    frame = self._frame
                    if frame is not None and frame.timestamp > last_ts:
                        last_ts = frame.timestamp
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._stopping:
                        return
                    self._cond.wait(remaining)
            yield frame

    # -- Status ------------------------------------------------------------

    def status(self) -> dict[str, object]:
        with self._cond:
            frame = self._frame
            return {
                "name": self.camera.name,
                "url": self.camera.redacted_url,
                "running": self._running,
                "wanted": self._wanted,
                "frames_total": self._frames_total,
                "restarts": self._restarts,
                "frame_age": round(frame.age, 3) if frame else None,
                "frame_bytes": len(frame.data) if frame else None,
                "last_error": self._last_error,
                "ffmpeg_log": list(self._stderr),
            }

    def is_healthy(self) -> bool:
        """Gesund heisst: nicht angefordert, oder ein frisches Bild vorhanden.

        Direkt nach dem Start gibt es naturgemaess noch kein Bild -- bis die
        Nachsichtsfrist abgelaufen ist, gilt das nicht als Stoerung.
        """
        with self._cond:
            if not self._wanted:
                return True
            grace = max(self.camera.max_age * 3, self.camera.timeout)
            frame = self._frame
            if frame is None:
                return time.monotonic() - self._wanted_since <= grace
            return frame.age <= grace

    # -- Innenleben --------------------------------------------------------

    def _run(self) -> None:
        backoff = _BACKOFF_START
        while True:
            with self._cond:
                while not self._wanted and not self._stopping:
                    self._cond.wait(1.0)
                if self._stopping:
                    return

            started = time.monotonic()
            try:
                clean = self._pump()
            except Exception as exc:  # pragma: no cover - Notnagel
                clean = False
                self._note_error(f"unerwarteter Fehler: {exc}")
                log.exception("Kamera %s: Greifer abgestuerzt", self.camera.name)

            with self._cond:
                self._running = False
                if self._stopping:
                    return
                if not self._wanted:
                    # Regulaer wegen Leerlauf beendet -- kein Fehlerfall.
                    backoff = _BACKOFF_START
                    continue
                self._restarts += 1

            # Lief der Prozess laenger als eine Minute, war es kein
            # Startproblem -- dann sofort wieder versuchen.
            if clean or time.monotonic() - started > 60:
                backoff = _BACKOFF_START
            log.warning(
                "Kamera %s: ffmpeg beendet, neuer Versuch in %.0fs",
                self.camera.name, backoff,
            )
            with self._cond:
                self._cond.wait(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    def _pump(self) -> bool:
        """Startet ffmpeg und liest Bilder, bis der Prozess endet."""
        cmd = build_command(self.camera, self._ffmpeg)
        log.info("Kamera %s: starte ffmpeg fuer %s",
                 self.camera.name, self.camera.redacted_url)
        log.debug("Kamera %s: %s", self.camera.name, " ".join(cmd))

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            self._note_error(f"ffmpeg nicht startbar: {exc}")
            log.error("Kamera %s: ffmpeg nicht startbar: %s", self.camera.name, exc)
            return False

        with self._cond:
            self._process = process
            self._running = True

        stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(process,),
            name=f"ffmpeg-log-{self.camera.name}", daemon=True,
        )
        stderr_thread.start()

        idle_thread: threading.Thread | None = None
        if self.camera.idle_timeout > 0:
            idle_thread = threading.Thread(
                target=self._watch_idle, args=(process,),
                name=f"idle-{self.camera.name}", daemon=True,
            )
            idle_thread.start()

        buffer = bytearray()
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                buffer.extend(chunk)
                frames, buffer = split_jpegs(buffer)
                if frames:
                    self._publish(frames[-1])
                if len(buffer) > _MAX_BUFFER:
                    self._note_error("Datenstrom ohne gueltiges JPEG-Ende")
                    log.error("Kamera %s: Puffer laeuft ueber, breche ab",
                              self.camera.name)
                    break
        except OSError as exc:
            self._note_error(f"Lesefehler: {exc}")
        finally:
            self._terminate_process()
            code = process.wait()
            stderr_thread.join(timeout=2.0)
            if idle_thread is not None:
                idle_thread.join(timeout=2.0)
            # Ohne das sammeln sich ueber viele Neustarts hinweg offene
            # Dateideskriptoren an.
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()
            with self._cond:
                self._process = None
                self._running = False

        if code not in (0, -signal.SIGTERM, -signal.SIGKILL, 255):
            tail = "; ".join(list(self._stderr)[-3:]) or f"Exit-Code {code}"
            self._note_error(tail)
            return False
        return True

    def _publish(self, data: bytes) -> None:
        with self._cond:
            self._frame = Frame(data=data, timestamp=time.monotonic())
            self._frames_total += 1
            self._last_error = None
            self._cond.notify_all()

    def _note_error(self, message: str) -> None:
        with self._cond:
            self._last_error = message
            self._cond.notify_all()

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self._stderr.append(line)
            log.debug("Kamera %s: ffmpeg: %s", self.camera.name, line)

    def _watch_idle(self, process: subprocess.Popen[bytes]) -> None:
        """Beendet ffmpeg, wenn eine Weile niemand Bilder abgerufen hat."""
        timeout = self.camera.idle_timeout
        while process.poll() is None:
            with self._cond:
                idle = time.monotonic() - self._last_request
                if self._stopping:
                    return
                if idle < timeout:
                    self._cond.wait(min(timeout - idle, 5.0))
                    continue
                self._wanted = False
            log.info("Kamera %s: %.0fs ohne Abruf, stoppe ffmpeg",
                     self.camera.name, timeout)
            self._terminate_process()
            return

    def _terminate_process(self) -> None:
        with self._cond:
            process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            log.warning("Kamera %s: ffmpeg reagiert nicht, sende SIGKILL",
                        self.camera.name)
            process.kill()


class GrabberPool:
    """Alle Kameras zusammen."""

    def __init__(self, cameras: dict[str, CameraConfig], ffmpeg: str | None = None) -> None:
        self.grabbers = {
            name: Grabber(camera, ffmpeg) for name, camera in cameras.items()
        }

    def __contains__(self, name: object) -> bool:
        return name in self.grabbers

    def __getitem__(self, name: str) -> Grabber:
        return self.grabbers[name]

    def start(self) -> None:
        for grabber in self.grabbers.values():
            grabber.start()

    def stop(self) -> None:
        for grabber in self.grabbers.values():
            grabber.stop()

    def status(self) -> list[dict[str, object]]:
        return [grabber.status() for grabber in self.grabbers.values()]

    def is_healthy(self) -> bool:
        return all(grabber.is_healthy() for grabber in self.grabbers.values())
