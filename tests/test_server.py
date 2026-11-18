from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest

from rtsp2jpeg.config import CameraConfig, Config, ServerConfig, netze
from rtsp2jpeg.grabber import GrabberPool
from rtsp2jpeg.server import make_server

from .conftest import is_jpeg


@contextmanager
def dienst(fake_ffmpeg, cameras=None, *, max_verbindungen=32, max_je_gegenstelle=8,
           lesefrist=5.0, kopffrist=2.0, bremse=None, protokollgrenze=None,
           **server_kwargs):
    """Startet den HTTP-Dienst auf einem freien Port."""
    cameras = cameras or {
        "tuer": CameraConfig(
            name="tuer", url="rtsp://host/s", fps=25,
            max_age=5.0, timeout=5.0, idle_timeout=0,
        )
    }
    einstellungen = {"host": "127.0.0.1", "port": 0, "access_log": False}
    einstellungen.update(server_kwargs)
    config = Config(server=ServerConfig(**einstellungen), cameras=cameras)
    pool = GrabberPool(cameras, ffmpeg=fake_ffmpeg)
    pool.start()
    server = make_server(config, pool, max_verbindungen=max_verbindungen,
                         max_je_gegenstelle=max_je_gegenstelle, lesefrist=lesefrist,
                         kopffrist=kopffrist, bremse=bremse,
                         protokollgrenze=protokollgrenze)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        pool.stop()
        thread.join(timeout=5)


def hole(url: str, timeout: float = 15.0, **kwargs):
    """Anfrage, die gelingen soll -- liefert (Status, Kopfzeilen, Rumpf)."""
    request = urllib.request.Request(url, **kwargs)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, dict(response.headers), response.read()


def hole_fehler(url: str, timeout: float = 15.0, **kwargs):
    """Anfrage, die scheitern soll -- liefert (Status, Kopfzeilen, Rumpf)."""
    request = urllib.request.Request(url, **kwargs)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as fehler:
        with fehler:
            return fehler.code, dict(fehler.headers), fehler.read()
    raise AssertionError(f"Anfrage {url} war unerwartet erfolgreich ({status})")


def verbinde(base: str, timeout: float = 10.0, quelle: str | None = None) -> socket.socket:
    """Verbindung zum Dienst, auf Wunsch von einer anderen Loopback-Adresse."""
    teile = urlsplit(base)
    return socket.create_connection((teile.hostname, teile.port), timeout=timeout,
                                    source_address=(quelle, 0) if quelle else None)


def roh(base: str, daten: bytes, timeout: float = 10.0, quelle: str | None = None) -> bytes:
    """Schickt Bytes unveraendert und liest, bis der Dienst schliesst."""
    with verbinde(base, timeout, quelle) as sock:
        sock.sendall(daten)
        antwort = b""
        while True:
            try:
                stueck = sock.recv(65536)
            except ConnectionResetError:
                break
            if not stueck:
                break
            antwort += stueck
    return antwort


def zerlege(antwort: bytes) -> tuple[int, dict[str, str], bytes]:
    kopf, _, rumpf = antwort.partition(b"\r\n\r\n")
    zeilen = kopf.decode("latin-1").split("\r\n")
    status = int(zeilen[0].split()[1])
    felder = {}
    for zeile in zeilen[1:]:
        name, _, wert = zeile.partition(":")
        felder[name.strip().lower()] = wert.strip()
    return status, felder, rumpf


class TestBild:
    def test_liefert_ein_jpeg(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            status, headers, body = hole(f"{base}/cam/tuer.jpg")
        assert status == 200
        assert headers["Content-Type"] == "image/jpeg"
        assert int(headers["Content-Length"]) == len(body)
        assert is_jpeg(body)

    def test_wird_nicht_zwischengespeichert(self, fake_ffmpeg):
        """Die Anlage soll jedes Mal ein frisches Bild sehen."""
        with dienst(fake_ffmpeg) as base:
            _, headers, _ = hole(f"{base}/cam/tuer.jpg")
        assert "no-store" in headers["Cache-Control"]
        assert headers["Pragma"] == "no-cache"

    def test_verraet_nichts_ueber_sich(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            _, headers, _ = hole(f"{base}/cam/tuer.jpg")
        assert "Server" not in headers
        assert "X-Frame-Age" not in headers

    def test_spaeterer_abruf_liefert_ein_neueres_bild(self, fake_ffmpeg):
        cameras = {"tuer": CameraConfig(name="tuer", url="rtsp://host/s", fps=25,
                                        max_age=0.2, timeout=5.0, idle_timeout=0)}
        with dienst(fake_ffmpeg, cameras=cameras) as base:
            _, _, erst = hole(f"{base}/cam/tuer.jpg")
            time.sleep(0.3)
            _, _, zweit = hole(f"{base}/cam/tuer.jpg")
        assert erst != zweit

    def test_abfrageparameter_aendern_nichts(self, fake_ffmpeg):
        """max_age, timeout, stale gibt es nicht mehr -- sie bleiben wirkungslos."""
        with dienst(fake_ffmpeg) as base:
            status, _, body = hole(f"{base}/cam/tuer.jpg?timeout=abc&max_age=0&stale=1")
        assert status == 200
        assert is_jpeg(body)

    def test_mehrere_kameras_je_eigener_pfad(self, fake_ffmpeg):
        cameras = {
            name: CameraConfig(name=name, url=f"rtsp://host/{name}", fps=25,
                               timeout=5.0, idle_timeout=0)
            for name in ("tuer", "hof")
        }
        with dienst(fake_ffmpeg, cameras=cameras) as base:
            for name in cameras:
                status, _, body = hole(f"{base}/cam/{name}.jpg")
                assert status == 200
                assert is_jpeg(body)

    def test_gleichzeitige_abrufe(self, fake_ffmpeg):
        """Mehrere Callmanager fragen dasselbe Bild gleichzeitig ab."""
        ergebnisse: list[tuple[int, bool]] = []
        sperre = threading.Lock()

        with dienst(fake_ffmpeg) as base:
            def abrufen() -> None:
                status, _, body = hole(f"{base}/cam/tuer.jpg")
                with sperre:
                    ergebnisse.append((status, is_jpeg(body)))

            threads = [threading.Thread(target=abrufen) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        assert len(ergebnisse) == 8
        assert all(status == 200 and gueltig for status, gueltig in ergebnisse)

    def test_rumpf_null_ist_erlaubt(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            antwort = roh(base, b"GET /cam/tuer.jpg HTTP/1.1\r\nHost: x\r\n"
                                b"Content-Length: 0\r\n\r\n")
        status, _, rumpf = zerlege(antwort)
        assert status == 200
        assert is_jpeg(rumpf)


ENTFALLENE_PFADE = [
    "/", "/healthz", "/health", "/status", "/index.html", "/cam/tuer.mjpg",
    "/cam/tuer.jpeg", "/cam/tuer.jpg/", "/cam/tuer", "/cam/", "/cam/../cam/tuer.jpg",
    "/cam/t%75er.jpg", "/CAM/tuer.jpg", "/gibtsnicht",
]


class TestNurEinWeg:
    @pytest.mark.parametrize("pfad", ENTFALLENE_PFADE)
    def test_alles_andere_gibt_es_nicht(self, fake_ffmpeg, pfad):
        with dienst(fake_ffmpeg, allow_anonymous=True) as base:
            status, headers, body = hole_fehler(f"{base}{pfad}")
        assert status == 404
        assert body == b""
        assert headers["Content-Length"] == "0"

    @pytest.mark.parametrize("pfad", ENTFALLENE_PFADE)
    def test_auch_mit_gueltigem_token_nicht(self, fake_ffmpeg, pfad):
        token = "a" * 48
        trenner = "&" if "?" in pfad else "?"
        with dienst(fake_ffmpeg, token=token) as base:
            status, _, body = hole_fehler(f"{base}{pfad}{trenner}token={token}")
        assert status == 404
        assert body == b""

    @pytest.mark.parametrize("pfad", ENTFALLENE_PFADE)
    def test_ohne_token_nur_401(self, fake_ffmpeg, pfad):
        """Ohne Zugang erfaehrt niemand, welche Pfade es gibt."""
        with dienst(fake_ffmpeg, token="a" * 48) as base:
            status, _, body = hole_fehler(f"{base}{pfad}")
        assert status == 401
        assert body == b""

    def test_unbekannte_kamera_nennt_keine_anderen(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            status, _, body = hole_fehler(f"{base}/cam/garten.jpg")
        assert status == 404
        assert body == b""


class TestMethodenUndRumpf:
    @pytest.mark.parametrize(
        "methode", ["HEAD", "POST", "PUT", "DELETE", "OPTIONS", "TRACE", "PATCH",
                    "CONNECT", "PROPFIND", "get"],
    )
    def test_nur_get(self, fake_ffmpeg, methode):
        with dienst(fake_ffmpeg) as base:
            antwort = roh(base, f"{methode} /cam/tuer.jpg HTTP/1.1\r\nHost: x\r\n\r\n"
                          .encode())
        status, kopf, rumpf = zerlege(antwort)
        assert status == 405
        assert kopf["content-length"] == "0"
        assert kopf["connection"] == "close"
        assert rumpf == b""

    @pytest.mark.parametrize(
        "kopf, rumpf",
        [
            (b"Content-Length: 5\r\n", b"hallo"),
            (b"Content-Length: 5\r\nContent-Length: 0\r\n", b"hallo"),
            (b"Transfer-Encoding: chunked\r\n", b"5\r\nhallo\r\n0\r\n\r\n"),
        ],
    )
    def test_anfrage_mit_rumpf_wird_abgewiesen(self, fake_ffmpeg, kopf, rumpf):
        with dienst(fake_ffmpeg) as base:
            antwort = roh(base, b"GET /cam/tuer.jpg HTTP/1.1\r\nHost: x\r\n"
                                + kopf + b"\r\n" + rumpf)
        status, felder, inhalt = zerlege(antwort)
        assert status == 400
        assert felder["connection"] == "close"
        assert inhalt == b""

    def test_rumpf_wird_nie_als_anfrage_gelesen(self, fake_ffmpeg):
        """Was nach der ersten Anfrage kommt, beantwortet der Dienst nicht."""
        zweite = b"GET /cam/tuer.jpg HTTP/1.1\r\nHost: x\r\n\r\n"
        with dienst(fake_ffmpeg) as base:
            antwort = roh(base, b"POST /cam/tuer.jpg HTTP/1.1\r\nHost: x\r\n"
                                b"Content-Length: %d\r\n\r\n" % len(zweite) + zweite)
        assert antwort.count(b"HTTP/1.0 ") == 1
        assert zerlege(antwort)[0] == 405

    def test_eine_anfrage_je_verbindung(self, fake_ffmpeg):
        anfrage = b"GET /cam/tuer.jpg HTTP/1.1\r\nHost: x\r\n\r\n"
        with dienst(fake_ffmpeg) as base:
            antwort = roh(base, anfrage * 2)
        assert antwort.count(b"HTTP/1.0 200") == 1
        assert zerlege(antwort)[1]["connection"] == "close"

    @pytest.mark.parametrize(
        "anfrage",
        [
            b"KAPUTT\r\n\r\n",
            b"GET /cam/tuer.jpg HTTP/9.9\r\n\r\n",
            b"GET /cam/tuer.jpg HTTP/1.1\r\n" + b"X-A: " + b"a" * 9000 + b"\r\n\r\n",
            b"GET /cam/tuer.jpg HTTP/1.1\r\n" + b"X-A: b\r\n" * 2000 + b"\r\n",
            b"GET /" + b"a" * 5000 + b" HTTP/1.1\r\n\r\n",
        ],
    )
    def test_kaputte_anfragen_ohne_inhalt(self, fake_ffmpeg, anfrage):
        with dienst(fake_ffmpeg) as base:
            antwort = roh(base, anfrage)
        if antwort:  # eine leere Antwort ist ebenfalls in Ordnung
            status, felder, rumpf = zerlege(antwort)
            assert status >= 400
            assert rumpf == b""
            assert felder["content-length"] == "0"
        assert b"KAPUTT" not in antwort
        assert b"9.9" not in antwort


class TestFehlerantworten:
    def test_stumme_kamera_ergibt_leeres_503(self, fake_ffmpeg, monkeypatch):
        monkeypatch.setenv("FAKE_START_DELAY", "30")
        monkeypatch.setenv("FAKE_STDERR", "rtsp://admin:streng-geheim@host/s: 401")
        cameras = {"tuer": CameraConfig(name="tuer", url="rtsp://admin:streng-geheim@host/s",
                                        timeout=1.0, idle_timeout=0)}
        with dienst(fake_ffmpeg, cameras=cameras) as base:
            status, headers, body = hole_fehler(f"{base}/cam/tuer.jpg")
        assert status == 503
        assert body == b""
        assert headers["Content-Length"] == "0"
        assert "streng-geheim" not in str(headers)

    def test_401_ohne_inhalt(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, token="a" * 48) as base:
            status, headers, body = hole_fehler(f"{base}/cam/tuer.jpg?token=falsch")
        assert status == 401
        assert body == b""
        assert headers["Content-Length"] == "0"
        assert "WWW-Authenticate" not in headers


class TestZugangsschutz:
    def test_token_in_der_url(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, token="geheim") as base:
            assert hole(f"{base}/cam/tuer.jpg?token=geheim")[0] == 200

    def test_token_nur_in_der_url(self, fake_ffmpeg):
        """Bearer und X-Auth-Token sind entfallen -- die Anlage nutzt sie nicht."""
        with dienst(fake_ffmpeg, token="geheim") as base:
            bearer = hole_fehler(f"{base}/cam/tuer.jpg",
                                 headers={"Authorization": "Bearer geheim"})
            eigen = hole_fehler(f"{base}/cam/tuer.jpg", headers={"X-Auth-Token": "geheim"})
        assert bearer[0] == 401
        assert eigen[0] == 401

    def test_ein_versuch_je_anfrage(self, fake_ffmpeg):
        """Mehrere token-Parameter sind mehrere Versuche -- das zaehlt nicht."""
        with dienst(fake_ffmpeg, token="geheim") as base:
            status, _, _ = hole_fehler(f"{base}/cam/tuer.jpg?token=falsch&token=geheim")
        assert status == 401

    def test_zu_viele_parameter(self, fake_ffmpeg):
        abfrage = "&".join(f"p{i}=1" for i in range(20))
        with dienst(fake_ffmpeg, token="geheim") as base:
            status, _, _ = hole_fehler(f"{base}/cam/tuer.jpg?{abfrage}&token=geheim")
        assert status == 401

    def test_nicht_ascii_im_token_ist_kein_absturz(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, token="geheim") as base:
            status, _, _ = hole_fehler(f"{base}/cam/tuer.jpg?token=%C3%A4%00")
        assert status == 401

    def test_basic_auth(self, fake_ffmpeg):
        import base64

        auth = base64.b64encode(b"starface:geheim").decode()
        with dienst(fake_ffmpeg, basic_auth="starface:geheim") as base:
            gut = hole(f"{base}/cam/tuer.jpg",
                       headers={"Authorization": f"Basic {auth}"})
            status, headers, _ = hole_fehler(f"{base}/cam/tuer.jpg")
        assert gut[0] == 200
        assert status == 401
        assert "Basic" in headers["WWW-Authenticate"]

    @pytest.mark.parametrize("kopf", ["Basic !!!kein-base64", "Basic", "Basic w6Q="])
    def test_kaputter_basic_kopf_wird_abgelehnt(self, fake_ffmpeg, kopf):
        with dienst(fake_ffmpeg, basic_auth="a:b") as base:
            status, _, _ = hole_fehler(f"{base}/cam/tuer.jpg",
                                       headers={"Authorization": kopf})
        assert status == 401


class TestGrenzen:
    def test_stumme_verbindung_wird_geschlossen(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, lesefrist=0.5) as base, verbinde(base) as sock:
            beginn = time.monotonic()
            assert sock.recv(100) == b""
            dauer = time.monotonic() - beginn
        assert dauer < 3.0, f"Verbindung blieb {dauer:.1f}s offen"

    def test_tropfende_anfrage_hat_eine_obergrenze(self, fake_ffmpeg):
        """Wer Byte fuer Byte knapp unter der Lesefrist liefert, fliegt trotzdem."""
        cameras = {"tuer": CameraConfig(name="tuer", url="rtsp://host/s",
                                        timeout=1.0, idle_timeout=0)}
        with dienst(fake_ffmpeg, cameras=cameras, lesefrist=0.5,
                    kopffrist=10.0) as base, verbinde(base) as sock:
            # Frist: 10 + 1.0 + 0.5 = 11.5 s -- die Kopffrist greift hier nicht
            beginn = time.monotonic()
            sock.sendall(b"GET /cam/tuer.jpg HTTP/1.1\r\n")
            geschlossen = False
            while time.monotonic() - beginn < 15.0:
                try:
                    sock.sendall(b"X")
                except OSError:
                    geschlossen = True
                    break
                time.sleep(0.3)
            dauer = time.monotonic() - beginn
        assert geschlossen
        assert dauer < 14.0, f"Verbindung blieb {dauer:.1f}s offen"

    def test_kopfzeilen_haben_eine_kurze_frist(self, fake_ffmpeg):
        """Bis zum Ende der Kopfzeilen gilt die kurze Frist, nicht die Gesamtfrist."""
        with dienst(fake_ffmpeg, lesefrist=5.0, kopffrist=0.5) as base, \
                verbinde(base) as sock:
            beginn = time.monotonic()
            sock.sendall(b"GET /cam/tuer.jpg HTTP/1.1\r\n")
            geschlossen = False
            while time.monotonic() - beginn < 8.0:
                try:
                    sock.sendall(b"X")
                except OSError:
                    geschlossen = True
                    break
                time.sleep(0.2)
            dauer = time.monotonic() - beginn
        assert geschlossen
        assert dauer < 2.5, f"Verbindung blieb {dauer:.1f}s offen"

    def test_kopffrist_endet_mit_den_kopfzeilen(self, fake_ffmpeg, monkeypatch):
        """Wartet die Anfrage danach auf die Kamera, bricht die Kopffrist sie nicht ab."""
        monkeypatch.setenv("FAKE_START_DELAY", "1.0")
        cameras = {"tuer": CameraConfig(name="tuer", url="rtsp://host/s",
                                        timeout=5.0, idle_timeout=0)}
        with dienst(fake_ffmpeg, cameras=cameras, kopffrist=0.3) as base:
            status, _, _ = hole(f"{base}/cam/tuer.jpg")
        assert status == 200

    def test_obergrenze_gleichzeitiger_verbindungen(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, max_verbindungen=2) as base:
            belegt = [verbinde(base), verbinde(base)]
            try:
                time.sleep(0.3)
                with verbinde(base) as dritte:
                    beginn = time.monotonic()
                    try:
                        dritte.sendall(b"GET /cam/tuer.jpg HTTP/1.1\r\n\r\n")
                        antwort = dritte.recv(100)
                    except ConnectionResetError:
                        antwort = b""
                    assert antwort == b""
                    assert time.monotonic() - beginn < 2.0
            finally:
                for sock in belegt:
                    sock.close()
            time.sleep(0.3)
            # Plaetze werden wieder frei
            assert hole(f"{base}/cam/tuer.jpg")[0] == 200

    def test_obergrenze_je_gegenstelle(self, fake_ffmpeg):
        """Eine einzelne Adresse kann nicht alle Plaetze belegen."""
        with dienst(fake_ffmpeg, max_je_gegenstelle=2) as base:
            belegt = [verbinde(base), verbinde(base)]
            try:
                time.sleep(0.3)
                antwort = roh(base, b"GET /cam/tuer.jpg HTTP/1.1\r\n\r\n")
                assert antwort == b""
                andere = roh(base, b"GET /cam/tuer.jpg HTTP/1.1\r\n\r\n",
                             quelle="127.0.0.2")
                assert zerlege(andere)[0] == 200
            finally:
                for sock in belegt:
                    sock.close()

    def test_eigener_proxy_ist_von_der_grenze_je_gegenstelle_ausgenommen(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, max_je_gegenstelle=2,
                    trust_proxy=netze("127.0.0.1", "test")) as base:
            belegt = [verbinde(base), verbinde(base)]
            try:
                time.sleep(0.3)
                antwort = roh(base, b"GET /cam/tuer.jpg HTTP/1.1\r\n\r\n")
                assert zerlege(antwort)[0] == 200
            finally:
                for sock in belegt:
                    sock.close()

    def test_allow_from_schliesst_fremde_sofort(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, allow_from=netze("127.0.0.2", "test")) as base:
            beginn = time.monotonic()
            fremd = roh(base, b"GET /cam/tuer.jpg HTTP/1.1\r\n\r\n")
            dauer = time.monotonic() - beginn
            erlaubt = roh(base, b"GET /cam/tuer.jpg HTTP/1.1\r\n\r\n",
                          quelle="127.0.0.2")
        assert fremd == b""
        assert dauer < 1.0
        assert zerlege(erlaubt)[0] == 200
