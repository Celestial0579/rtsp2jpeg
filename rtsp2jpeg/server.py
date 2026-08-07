"""HTTP-Schnittstelle: liefert Standbilder, die STARFACE abholen kann."""

from __future__ import annotations

import base64
import contextlib
import hmac
import json
import logging
import re
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import Config
from .grabber import GrabberPool

log = logging.getLogger(__name__)

BOUNDARY = "rtsp2jpeg-frame"
_MAX_WAIT = 60.0  # Obergrenze fuer per Anfrage gesetzte Wartezeiten


_TOKEN_IM_PFAD = re.compile(r"(?i)\btoken=[^&]*")


def redact_query(path: str) -> str:
    """Entfernt das Token aus einem Pfad, bevor er ins Protokoll geht."""
    return _TOKEN_IM_PFAD.sub("token=***", path)


def _clamp(value: str | None, fallback: float, low: float, high: float) -> float:
    if value is None:
        return fallback
    try:
        return max(low, min(high, float(value)))
    except ValueError:
        return fallback


class Bremse:
    """Verzoegert Anfragen von Gegenstellen, die das Token raten.

    Der Dienst steht bei einer Cloud-Anlage im offenen Netz. Ein Token laesst
    sich nicht in wenigen Versuchen erraten, aber ungebremst darf niemand
    tausende Versuche pro Sekunde machen.
    """

    def __init__(self, max_delay: float = 2.0, reset_after: float = 900.0,
                 max_entries: int = 4096) -> None:
        self._max_delay = max_delay
        self._reset_after = reset_after
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._fehlversuche: dict[str, tuple[int, float]] = {}

    def delay_for(self, client: str) -> float:
        """Wartezeit, die dieser Gegenstelle vor der Antwort zusteht.

        Ein unbekannter Absender wartet nie. Wichtig ist die Unterscheidung
        "kein Eintrag" gegen "alter Eintrag": der Nullpunkt von monotonic()
        ist beliebig gewaehlt, ein Vergleich gegen 0.0 waere auf einem eben
        erst gestarteten Rechner etwas voellig anderes als auf einem, der
        seit Wochen laeuft.
        """
        with self._lock:
            eintrag = self._fehlversuche.get(client)
            if eintrag is None:
                return 0.0
            count, last = eintrag
            if time.monotonic() - last > self._reset_after:
                return 0.0
            return min(self._max_delay, 0.1 * (2 ** min(count, 8)))

    def note_failure(self, client: str) -> int:
        now = time.monotonic()
        with self._lock:
            eintrag = self._fehlversuche.get(client)
            count = 0 if eintrag is None or now - eintrag[1] > self._reset_after \
                else eintrag[0]
            count += 1
            self._fehlversuche[client] = (count, now)
            if len(self._fehlversuche) > self._max_entries:
                # Aeltesten Eintrag verwerfen -- der Speicher darf nicht
                # unbegrenzt wachsen, nur weil jemand die Quell-IP wechselt.
                aeltester = min(self._fehlversuche.items(), key=lambda kv: kv[1][1])[0]
                del self._fehlversuche[aeltester]
            return count

    def note_success(self, client: str) -> None:
        with self._lock:
            self._fehlversuche.pop(client, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "rtsp2jpeg"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    config: Config
    pool: GrabberPool
    bremse: Bremse

    # -- Geruest -----------------------------------------------------------

    def client_name(self) -> str:
        """Gegenstelle -- hinter einem Proxy die echte Absender-Adresse."""
        if self.config.server.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                # Der erste Eintrag ist der urspruengliche Absender.
                return forwarded.split(",")[0].strip()[:64]
        return self.address_string()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if self.config.server.access_log:
            log.info('%s "%s %s" %s', self.client_name(), self.command,
                     redact_query(self.path), code)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.config.server.access_log:
            log.info("%s %s", self.client_name(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:
        log.debug("%s %s", self.client_name(), fmt % args)

    def do_GET(self) -> None:
        self._dispatch(body=True)

    def do_HEAD(self) -> None:
        self._dispatch(body=False)

    def _dispatch(self, body: bool) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        try:
            if path in ("/healthz", "/health"):
                if self.config.server.protect_health and not self._authorized(query):
                    return self._deny()
                return self._health(body)

            if not self._authorized(query):
                return self._deny()

            if path == "/":
                return self._index(body)
            if path == "/status":
                return self._status(body)
            if path.startswith("/cam/") and path.endswith(".jpg"):
                return self._snapshot(path[len("/cam/") : -len(".jpg")], query, body)
            if path.startswith("/cam/") and path.endswith(".mjpg"):
                return self._mjpeg(path[len("/cam/") : -len(".mjpg")], query, body)

            self._error(HTTPStatus.NOT_FOUND, f"Unbekannter Pfad: {path}")
        except (BrokenPipeError, ConnectionResetError):
            log.debug("Gegenstelle hat die Verbindung getrennt: %s", path)
        except Exception:  # pragma: no cover - Notnagel
            log.exception("Fehler bei der Anfrage %s", path)
            with contextlib.suppress(OSError):
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Interner Fehler")

    # -- Zugangsschutz -----------------------------------------------------

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        """Prueft den Zugang.

        Der Login muss sich vollstaendig in der URL unterbringen lassen, weil
        STARFACE nur ein einziges URL-Feld anbietet. Deshalb zaehlt sowohl
        ``?token=...`` als auch ``https://benutzer:passwort@host/...`` --
        letzteres schickt der abrufende Client als Basic-Kopfzeile.
        """
        server = self.config.server
        if not server.token and not server.basic_auth:
            return True

        header = self.headers.get("Authorization", "")

        if server.token:
            candidates = [
                *query.get("token", []),
                self.headers.get("X-Auth-Token", ""),
            ]
            if header.startswith("Bearer "):
                candidates.append(header[len("Bearer ") :].strip())
            if any(hmac.compare_digest(c, server.token) for c in candidates if c):
                self.bremse.note_success(self.client_name())
                return True

        if server.basic_auth and header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[len("Basic ") :].strip()).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            if hmac.compare_digest(decoded, server.basic_auth):
                self.bremse.note_success(self.client_name())
                return True

        return False

    def _deny(self) -> None:
        client = self.client_name()
        verzoegerung = self.bremse.delay_for(client)
        versuche = self.bremse.note_failure(client)
        if verzoegerung:
            time.sleep(verzoegerung)
        # Nur den Pfad protokollieren -- ein fast richtig geratenes Token
        # hat im Protokoll nichts verloren.
        log.warning("Abgewiesen: %s (%d. Fehlversuch) %s",
                    client, versuche, urlparse(self.path).path)

        payload = json.dumps({"error": "nicht autorisiert"}).encode()
        self.send_response(HTTPStatus.UNAUTHORIZED)
        if self.config.server.basic_auth:
            self.send_header("WWW-Authenticate", 'Basic realm="rtsp2jpeg"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # -- Endpunkte ---------------------------------------------------------

    def _snapshot(self, name: str, query: dict[str, list[str]], body: bool) -> None:
        if name not in self.pool:
            return self._error(
                HTTPStatus.NOT_FOUND,
                f"Kamera {name!r} ist nicht konfiguriert -- bekannt sind: "
                f"{', '.join(sorted(self.pool.grabbers))}",
            )

        grabber = self.pool[name]
        camera = grabber.camera

        if not body:  # HEAD -- nur ankuendigen, kein Bild holen
            self.send_response(HTTPStatus.OK)
            self._no_cache_headers("image/jpeg")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        first = lambda key: query.get(key, [None])[0]  # noqa: E731
        max_age = _clamp(first("max_age"), camera.max_age, 0.0, _MAX_WAIT)
        timeout = _clamp(first("timeout"), camera.timeout, 0.5, _MAX_WAIT)
        allow_stale = first("stale") in ("1", "true", "ja", "yes")

        try:
            frame = grabber.get_frame(max_age=max_age, timeout=timeout)
        except TimeoutError as exc:
            stale = grabber.latest() if allow_stale else None
            if stale is None:
                return self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            frame = stale

        self.send_response(HTTPStatus.OK)
        self._no_cache_headers("image/jpeg")
        self.send_header("Content-Length", str(len(frame.data)))
        self.send_header("X-Frame-Age", f"{frame.age:.3f}")
        self.end_headers()
        self.wfile.write(frame.data)

    def _mjpeg(self, name: str, query: dict[str, list[str]], body: bool) -> None:
        if name not in self.pool:
            return self._error(HTTPStatus.NOT_FOUND, f"Kamera {name!r} ist nicht konfiguriert")

        grabber = self.pool[name]
        self.send_response(HTTPStatus.OK)
        self._no_cache_headers(f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.end_headers()
        if not body:
            return

        # Bei laufendem Strom ist die Laenge unbekannt -- Verbindung nicht
        # wiederverwenden, sonst verhaspelt sich HTTP/1.1 keep-alive.
        self.close_connection = True

        try:
            for frame in grabber.stream():
                header = (
                    f"--{BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame.data)}\r\n\r\n"
                ).encode()
                self.wfile.write(header)
                self.wfile.write(frame.data)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            log.debug("MJPEG-Abnehmer fuer %s hat aufgelegt", name)

    def _health(self, body: bool) -> None:
        healthy = self.pool.is_healthy()
        payload = {
            "status": "ok" if healthy else "degraded",
            "cameras": {
                s["name"]: {
                    "running": s["running"],
                    "frame_age": s["frame_age"],
                    "last_error": s["last_error"],
                }
                for s in self.pool.status()
            },
        }
        code = HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE
        self._json(code, payload, body)

    def _status(self, body: bool) -> None:
        self._json(HTTPStatus.OK, {"cameras": self.pool.status()}, body)

    def _index(self, body: bool) -> None:
        rows = []
        for status in self.pool.status():
            name = status["name"]
            state = "laeuft" if status["running"] else "im Leerlauf"
            age = status["frame_age"]
            age_text = f"{age:.1f}s alt" if isinstance(age, float) else "noch kein Bild"
            rows.append(
                f"<tr><td><code>{name}</code></td>"
                f"<td><a href='/cam/{name}.jpg'>/cam/{name}.jpg</a></td>"
                f"<td><a href='/cam/{name}.mjpg'>/cam/{name}.mjpg</a></td>"
                f"<td>{state}, {age_text}</td></tr>"
            )
        html = f"""<!doctype html>
<html lang="de"><meta charset="utf-8"><title>rtsp2jpeg</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 60rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td, th {{ text-align: left; padding: .4rem .8rem; border-bottom: 1px solid #ddd; }}
 code {{ background: #f4f4f4; padding: .1rem .3rem; }}
</style>
<h1>rtsp2jpeg</h1>
<p>Standbilder aus RTSP-Kameras, abrufbar als JPEG &ndash; gedacht fuer das
Feld <em>Kamera-URL</em> einer STARFACE-Tuersprechstelle.</p>
<table>
<tr><th>Kamera</th><th>Einzelbild</th><th>Bewegtbild</th><th>Zustand</th></tr>
{"".join(rows)}
</table>
<p><a href="/healthz">/healthz</a> &middot; <a href="/status">/status</a></p>
</html>"""
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8", body)

    # -- Hilfen ------------------------------------------------------------

    def _no_cache_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        # STARFACE und die Apps sollen jedes Mal ein frisches Bild bekommen.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _json(self, code: HTTPStatus, payload: dict[str, Any], body: bool) -> None:
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8", body)

    def _error(self, code: HTTPStatus, message: str) -> None:
        log.warning("%s: %s", code.value, message)
        data = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8", True)

    def _send(self, code: HTTPStatus, data: bytes, content_type: str, body: bool) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if code >= 400:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET


def make_server(config: Config, pool: GrabberPool) -> Server:
    handler = type(
        "BoundHandler", (Handler,),
        {"config": config, "pool": pool, "bremse": Bremse()},
    )
    return Server((config.server.host, config.server.port), handler)


def warn_ueber_die_absicherung(config: Config) -> list[str]:
    """Sammelt Hinweise, die vor dem Betrieb im offenen Netz wichtig sind."""
    hinweise = []
    server = config.server

    if not server.token and not server.basic_auth:
        hinweise.append(
            "Kein Zugangsschutz eingerichtet (weder 'token' noch 'basic_auth'). "
            "Jeder, der den Port erreicht, sieht das Kamerabild. Im offenen "
            "Netz ist das nicht vertretbar."
        )
    if server.token and len(server.token) < 24:
        hinweise.append(
            f"Das Token ist mit {len(server.token)} Zeichen kurz. Fuer einen "
            f"aus dem Internet erreichbaren Dienst mindestens 24 Zeichen "
            f"verwenden: openssl rand -hex 24"
        )
    return hinweise


def serve_forever(config: Config, pool: GrabberPool) -> None:  # pragma: no cover
    server = make_server(config, pool)
    for hinweis in warn_ueber_die_absicherung(config):
        log.warning("%s", hinweis)
    log.info("Hoere auf http://%s:%d", config.server.host, config.server.port)
    for camera in config.cameras.values():
        mode = ("dauerhaft" if camera.idle_timeout <= 0
                else f"bei Bedarf, Leerlauf {camera.idle_timeout:g}s")
        log.info("  /cam/%s.jpg  <- %s  (%s)",
                 camera.name, camera.redacted_url, mode)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.shutdown()
        server.server_close()


__all__ = ["Handler", "Server", "make_server", "serve_forever"]
