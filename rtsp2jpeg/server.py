"""HTTP-Schnittstelle: liefert genau eines -- das Standbild fuer die Anlage.

Es gibt einen einzigen Weg: ``GET /cam/<name>.jpg``. Alles andere wird mit
einer leeren Fehlerantwort abgewiesen, jede Verbindung traegt genau eine
Anfrage und wird danach geschlossen.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hmac
import ipaddress
import logging
import re
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from .config import Config, im_netz
from .grabber import GrabberPool

log = logging.getLogger(__name__)

_BILDPFAD = re.compile(r"/cam/([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.jpg")
_METHODE = re.compile(r"[A-Z]{1,16}")

# Feste Grenzen. Sie stehen absichtlich nicht in der Konfiguration.
LESEFRIST = 5.0          # Sekunden je Lese- oder Schreibvorgang auf dem Socket
KOPFFRIST = 2.0          # Sekunden bis Anfragezeile und Kopfzeilen vollstaendig sind
MAX_VERBINDUNGEN = 32    # gleichzeitig offene Verbindungen insgesamt
MAX_JE_GEGENSTELLE = 8   # davon je Adresse (der eigene Proxy ausgenommen)
_MAX_ZEILE = 2048        # Anfragezeile
_MAX_KOPF = 8192         # Anfragezeile und Kopfzeilen zusammen
_MAX_PARAMETER = 8       # Abfrageparameter


class _KopfZuGrossError(Exception):
    """Anfragezeile und Kopfzeilen ueberschreiten das Budget."""


class _BegrenzterLeser:
    """Liest hoechstens ``budget`` Bytes; danach bricht die Anfrage ab.

    ``http.client.parse_headers`` liest zeilenweise -- ohne Obergrenze
    duerften Kopfzeilen bis zu 100 x 64 KiB gross werden.
    """

    def __init__(self, raw: Any, budget: int) -> None:
        self._raw = raw
        self._rest = budget

    def readline(self, size: int = -1) -> bytes:
        if self._rest <= 0:
            raise _KopfZuGrossError
        limit = self._rest + 1 if size is None or size < 0 else min(size, self._rest + 1)
        line = self._raw.readline(limit)
        self._rest -= len(line)
        if self._rest < 0:
            raise _KopfZuGrossError
        return line

    def read(self, size: int = -1) -> bytes:  # pragma: no cover - nie benutzt
        raise _KopfZuGrossError

    def close(self) -> None:
        self._raw.close()


class Bremse:
    """Sperrt Gegenstellen, die falsch lagen, fuer eine wachsende Frist.

    Waehrend der Sperre prueft der Dienst nichts, sondern weist sofort ab --
    kein Versuch wartet auf einem Verbindungsplatz. Gleichzeitige Versuche
    derselben Gegenstelle: der erste darf pruefen und belegt dabei schon die
    Frist, die uebrigen werden abgewiesen.
    """

    def __init__(self, max_delay: float = 2.0, reset_after: float = 900.0,
                 max_entries: int = 4096) -> None:
        self._max_delay = max_delay
        self._reset_after = reset_after
        self._max_entries = max_entries
        self._lock = threading.Lock()
        # Gegenstelle -> (Fehlversuche, letzter Fehlversuch, gesperrt bis)
        self._fehlversuche: dict[str, tuple[int, float, float]] = {}

    def _delay(self, count: int) -> float:
        return min(self._max_delay, 0.1 * (2 ** min(count, 8)))

    def darf_pruefen(self, client: str) -> bool:
        """Darf dieser Versuch geprueft werden?

        Ein unbekannter Absender darf immer. Der Nullpunkt von monotonic()
        ist beliebig gewaehlt; deshalb zaehlt "kein Eintrag", nicht ein
        Vergleich gegen 0.0.
        """
        now = time.monotonic()
        with self._lock:
            eintrag = self._fehlversuche.get(client)
            if eintrag is None:
                return True
            count, last, gesperrt_bis = eintrag
            if now - last > self._reset_after:
                del self._fehlversuche[client]
                return True
            if now < gesperrt_bis:
                return False
            self._fehlversuche[client] = (count, last, now + self._delay(count))
            return True

    def delay_for(self, client: str) -> float:
        """Sperrfrist dieser Gegenstelle nach dem naechsten Fehlversuch."""
        with self._lock:
            eintrag = self._fehlversuche.get(client)
            if eintrag is None or time.monotonic() - eintrag[1] > self._reset_after:
                return 0.0
            return self._delay(eintrag[0])

    def note_failure(self, client: str) -> int:
        now = time.monotonic()
        with self._lock:
            eintrag = self._fehlversuche.get(client)
            vergessen = eintrag is None or now - eintrag[1] > self._reset_after
            count = 1 if vergessen else eintrag[0] + 1  # type: ignore[index]
            self._fehlversuche[client] = (count, now, now + self._delay(count))
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
    # HTTP/1.0: keine wiederverwendeten Verbindungen, eine Anfrage je Verbindung.
    protocol_version = "HTTP/1.0"
    timeout = LESEFRIST

    config: Config
    pool: GrabberPool
    bremse: Bremse
    kopffrist: float
    verbindungsfrist: float

    # -- Verbindung --------------------------------------------------------

    def setup(self) -> None:
        super().setup()
        self.rfile = _BegrenzterLeser(self.rfile, _MAX_KOPF)  # type: ignore[assignment]
        # Harte Obergrenzen, unabhaengig davon, wie langsam die Gegenstelle
        # Byte fuer Byte liefert: eine kurze bis zum Ende der Kopfzeilen,
        # eine fuer die ganze Verbindung.
        self._kopfwecker = threading.Timer(self.kopffrist, self._abbrechen)
        self._wecker = threading.Timer(self.verbindungsfrist, self._abbrechen)
        for wecker in (self._kopfwecker, self._wecker):
            wecker.daemon = True
            wecker.start()

    def finish(self) -> None:
        self._kopfwecker.cancel()
        self._wecker.cancel()
        with contextlib.suppress(OSError):
            super().finish()

    def _abbrechen(self) -> None:
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def handle(self) -> None:
        self.close_connection = True
        self.handle_one_request()

    def handle_one_request(self) -> None:
        self.close_connection = True
        self.command = None  # type: ignore[assignment]
        self.request_version = "HTTP/1.0"
        try:
            try:
                self.raw_requestline = self.rfile.readline(_MAX_ZEILE + 1)
                if not self.raw_requestline:
                    return
                if len(self.raw_requestline) > _MAX_ZEILE:
                    return self._antwort(HTTPStatus.REQUEST_URI_TOO_LONG)
                if not self.parse_request():
                    return
            except _KopfZuGrossError:
                return self._antwort(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
            self._kopfwecker.cancel()
            if self.request_version not in ("HTTP/1.0", "HTTP/1.1"):
                return
            if self.command != "GET":
                return self._antwort(HTTPStatus.METHOD_NOT_ALLOWED, ("Allow", "GET"))
            if self._hat_rumpf():
                return self._antwort(HTTPStatus.BAD_REQUEST)
            self._bild()
            self.wfile.flush()
        except OSError:  # Zeitlimit, abgebrochene Verbindung
            log.debug("Verbindung von %s vorzeitig beendet", self.client_name())
        except Exception as exc:  # pragma: no cover - Notnagel
            log.error("Interner Fehler (%s)", type(exc).__name__)
            with contextlib.suppress(OSError):
                self._antwort(HTTPStatus.INTERNAL_SERVER_ERROR)

    def _hat_rumpf(self) -> bool:
        if self.headers.get_all("Transfer-Encoding"):
            return True
        return any(v.strip() != "0" for v in self.headers.get_all("Content-Length") or [])

    # -- Protokoll ---------------------------------------------------------

    def client_name(self) -> str:
        """Gegenstelle -- hinter einem Proxy die von ihm gemeldete Adresse.

        X-Forwarded-For zaehlt nur, wenn die Verbindung von einem der in
        ``trust_proxy`` genannten Proxys kommt. Massgeblich ist dann der
        letzte Eintrag: den hat der eigene Proxy angehaengt. Alles davor
        stammt vom Absender selbst.
        """
        if (im_netz(self.client_address[0], self.config.server.trust_proxy)
                and getattr(self, "headers", None) is not None):
            forwarded = self.headers.get_all("X-Forwarded-For") or []
            if forwarded:
                letzter = forwarded[-1].split(",")[-1].strip()
                with contextlib.suppress(ValueError):
                    return str(ipaddress.ip_address(letzter))
        return self.client_address[0]

    def _protokolliere(self, code: int) -> None:
        if not self.config.server.access_log:
            return
        methode = self.command if self.command and _METHODE.fullmatch(self.command) else "-"
        pfad = getattr(self, "path", "").partition("?")[0]
        pfad = pfad if _BILDPFAD.fullmatch(pfad) else "-"
        log.info('%s "%s %s" %d', self.client_name(), methode, pfad, code)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass  # eigenes Protokoll in _protokolliere

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # die Vorlage des Basismoduls enthielte die rohe Anfragezeile

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        """Fehler aus dem Basismodul -- ebenfalls ohne Rumpf und ohne Text."""
        self._antwort(HTTPStatus(code))

    # -- Antworten ---------------------------------------------------------

    def _antwort(self, code: HTTPStatus, *kopf: tuple[str, str]) -> None:
        """Leere Antwort: Statuszeile, wenige Kopfzeilen, kein Inhalt."""
        self.close_connection = True
        self._protokolliere(code)
        self.send_response_only(code)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for name, wert in kopf:
            self.send_header(name, wert)
        self.send_header("Connection", "close")
        self.end_headers()

    def _bild(self) -> None:
        pfad, _, abfrage = self.path.partition("?")
        client = self.client_name()

        if self._zugang_noetig():
            kopf = [("WWW-Authenticate", 'Basic realm="rtsp2jpeg"')] \
                if self.config.server.basic_auth else []
            if not self.bremse.darf_pruefen(client):
                log.debug("Abgewiesen ohne Pruefung: %s (gesperrt)", client)
                return self._antwort(HTTPStatus.UNAUTHORIZED, *kopf)
            if not self._authorized(abfrage):
                versuche = self.bremse.note_failure(client)
                log.warning("Abgewiesen: %s (%d. Fehlversuch)", client, versuche)
                return self._antwort(HTTPStatus.UNAUTHORIZED, *kopf)
            self.bremse.note_success(client)

        treffer = _BILDPFAD.fullmatch(pfad)
        if treffer is None or treffer.group(1) not in self.pool:
            return self._antwort(HTTPStatus.NOT_FOUND)

        grabber = self.pool[treffer.group(1)]
        try:
            frame = grabber.get_frame()
        except TimeoutError:
            log.warning("Kamera %s: kein aktuelles Bild", grabber.camera.name)
            return self._antwort(HTTPStatus.SERVICE_UNAVAILABLE)

        self._protokolliere(HTTPStatus.OK)
        self.send_response_only(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame.data)))
        # Die Anlage soll jedes Mal ein frisches Bild bekommen.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(frame.data)

    # -- Zugangsschutz -----------------------------------------------------

    def _zugang_noetig(self) -> bool:
        server = self.config.server
        return bool(server.token or server.basic_auth)

    def _authorized(self, abfrage: str) -> bool:
        """Prueft den Zugang -- genau ein Versuch je Weg und Anfrage.

        Der Login muss in die URL passen, weil die Anlage nur ein einziges
        URL-Feld anbietet: ``?token=...`` oder ``https://benutzer:passwort@host/...``
        (letzteres schickt der Client als Basic-Kopfzeile).
        """
        server = self.config.server

        if server.token:
            try:
                werte = parse_qs(abfrage, keep_blank_values=True,
                                 max_num_fields=_MAX_PARAMETER)
            except ValueError:
                return False
            tokens = werte.get("token", [])
            if len(tokens) == 1 and hmac.compare_digest(
                tokens[0].encode("utf-8"), server.token.encode("utf-8")
            ):
                return True

        if server.basic_auth:
            koepfe = self.headers.get_all("Authorization") or []
            if len(koepfe) == 1 and koepfe[0].startswith("Basic "):
                try:
                    gegeben = base64.b64decode(koepfe[0][len("Basic "):].strip(),
                                               validate=True)
                except (ValueError, binascii.Error):
                    return False
                if hmac.compare_digest(gegeben, server.basic_auth.encode("utf-8")):
                    return True

        return False


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET

    def __init__(self, address: tuple[str, int], handler: type[Handler],
                 max_verbindungen: int = MAX_VERBINDUNGEN,
                 max_je_gegenstelle: int = MAX_JE_GEGENSTELLE,
                 config: Config | None = None) -> None:
        self._max_verbindungen = max_verbindungen
        self._max_je_gegenstelle = max_je_gegenstelle
        self._erlaubt = config.server.allow_from if config else ()
        self._proxys = config.server.trust_proxy if config else ()
        self._platz_lock = threading.Lock()
        self._belegt = 0
        self._je_gegenstelle: dict[str, int] = {}
        super().__init__(address, handler)

    def _belegen(self, adresse: str) -> bool:
        with self._platz_lock:
            eigene = self._je_gegenstelle.get(adresse, 0)
            if self._belegt >= self._max_verbindungen:
                return False
            if eigene >= self._max_je_gegenstelle and not im_netz(adresse, self._proxys):
                return False
            self._belegt += 1
            self._je_gegenstelle[adresse] = eigene + 1
            return True

    def _freigeben(self, adresse: str) -> None:
        with self._platz_lock:
            self._belegt -= 1
            eigene = self._je_gegenstelle.get(adresse, 0) - 1
            if eigene > 0:
                self._je_gegenstelle[adresse] = eigene
            else:
                self._je_gegenstelle.pop(adresse, None)

    def process_request(self, request: Any, client_address: Any) -> None:
        adresse = client_address[0]
        if self._erlaubt and not im_netz(adresse, self._erlaubt):
            # Nicht in allow_from: schliessen, bevor irgendetwas gelesen wird.
            self.shutdown_request(request)
            return
        if not self._belegen(adresse):
            # Kein Platz frei: Verbindung sofort schliessen, ohne Antwort.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._freigeben(adresse)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._freigeben(client_address[0])

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("Verbindung mit Fehler beendet")


def make_server(config: Config, pool: GrabberPool, *,
                max_verbindungen: int = MAX_VERBINDUNGEN,
                max_je_gegenstelle: int = MAX_JE_GEGENSTELLE,
                lesefrist: float = LESEFRIST,
                kopffrist: float = KOPFFRIST,
                bremse: Bremse | None = None) -> Server:
    bremse = bremse or Bremse()
    laengste_wartezeit = max(c.timeout for c in config.cameras.values())
    handler = type(
        "BoundHandler", (Handler,),
        {
            "config": config,
            "pool": pool,
            "bremse": bremse,
            "timeout": lesefrist,
            "kopffrist": kopffrist,
            # Kopfzeilen, Warten auf das Bild, Schreiben der Antwort
            "verbindungsfrist": kopffrist + laengste_wartezeit + lesefrist,
        },
    )
    return Server((config.server.host, config.server.port), handler,
                  max_verbindungen, max_je_gegenstelle, config)


def token_erzeugen(laenge: int = 24) -> str:
    """Erzeugt ein Token, das sich gefahrlos in eine URL schreiben laesst."""
    return secrets.token_hex(laenge)


def fehlt_der_zugangsschutz(config: Config) -> str | None:
    """Begruendung, warum der Dienst nicht starten darf -- sonst None.

    Ohne Zugangsschutz zu starten hiesse, das Kamerabild der Haustuer jedem
    zu zeigen, der den Port findet. Das muss ausdruecklich gewollt sein, und
    zwar mit einem echten Wahrheitswert.
    """
    server = config.server
    if server.token or server.basic_auth or server.allow_anonymous is True:
        return None
    return (
        "Kein Zugangsschutz eingerichtet -- der Dienst startet nicht.\n"
        "\n"
        "Ohne Token saehe jeder das Kamerabild, der den Port erreicht.\n"
        "\n"
        "Token erzeugen und eintragen:\n"
        "    docker run --rm ghcr.io/celestial0579/rtsp2jpeg:latest --token-erzeugen\n"
        "  danach als Umgebungsvariable TOKEN oder als 'token' in der\n"
        "  Konfigurationsdatei setzen.\n"
        "\n"
        "Nur wenn der Dienst wirklich offen sein soll -- etwa in einem\n"
        "abgeschotteten Netz -- laesst sich das ausdruecklich abschalten:\n"
        "    ALLOW_ANONYMOUS=1   bzw.   server.allow_anonymous: true"
    )


def warn_ueber_die_absicherung(config: Config) -> list[str]:
    """Sammelt Hinweise, die vor dem Betrieb im offenen Netz wichtig sind."""
    hinweise = []
    server = config.server

    if not server.token and not server.basic_auth and server.allow_anonymous is True:
        hinweise.append(
            "Der Dienst laeuft ohne Zugangsschutz (allow_anonymous). Jeder, "
            "der den Port erreicht, sieht das Kamerabild. Aus dem Internet "
            "darf er so nicht erreichbar sein."
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


__all__ = ["Bremse", "Handler", "Server", "make_server", "serve_forever"]
