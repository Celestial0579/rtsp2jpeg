from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from rtsp2jpeg.config import CameraConfig, Config, ServerConfig
from rtsp2jpeg.grabber import GrabberPool
from rtsp2jpeg.server import make_server

from .conftest import is_jpeg


@contextmanager
def dienst(fake_ffmpeg, cameras=None, **server_kwargs):
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
    server = make_server(config, pool)
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


def status_von(url: str, timeout: float = 15.0, **kwargs):
    """Anfrage, deren Ausgang offen ist -- liefert (Status, Rumpf)."""
    request = urllib.request.Request(url, **kwargs)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as fehler:
        with fehler:
            return fehler.code, fehler.read()


class TestSnapshot:
    def test_liefert_ein_jpeg(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            status, headers, body = hole(f"{base}/cam/tuer.jpg")
        assert status == 200
        assert headers["Content-Type"] == "image/jpeg"
        assert int(headers["Content-Length"]) == len(body)
        assert is_jpeg(body)

    def test_wird_nicht_zwischengespeichert(self, fake_ffmpeg):
        """STARFACE und die Apps sollen jedes Mal ein frisches Bild sehen."""
        with dienst(fake_ffmpeg) as base:
            _, headers, _ = hole(f"{base}/cam/tuer.jpg")
        assert "no-store" in headers["Cache-Control"]
        assert headers["Pragma"] == "no-cache"

    def test_alter_steht_im_kopf(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            _, headers, _ = hole(f"{base}/cam/tuer.jpg")
        assert float(headers["X-Frame-Age"]) < 5.0

    def test_spaeterer_abruf_liefert_ein_neueres_bild(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            _, _, erst = hole(f"{base}/cam/tuer.jpg")
            time.sleep(0.3)
            _, _, zweit = hole(f"{base}/cam/tuer.jpg?max_age=0.2")
        assert erst != zweit

    def test_kurz_aufeinander_folgende_abrufe_teilen_sich_ein_bild(self, fake_ffmpeg):
        """Zehn Callmanager sollen nicht zehn Kameraverbindungen ausloesen."""
        with dienst(fake_ffmpeg) as base:
            _, _, erst = hole(f"{base}/cam/tuer.jpg?max_age=5")
            _, _, zweit = hole(f"{base}/cam/tuer.jpg?max_age=5")
        assert erst == zweit

    def test_unbekannte_kamera_ist_404(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            status, _, body = hole_fehler(f"{base}/cam/garten.jpg")
        assert status == 404
        assert "tuer" in json.loads(body)["error"]

    def test_stumme_kamera_ergibt_503(self, fake_ffmpeg, monkeypatch):
        monkeypatch.setenv("FAKE_START_DELAY", "30")
        with dienst(fake_ffmpeg) as base:
            status, _, _ = hole_fehler(f"{base}/cam/tuer.jpg?timeout=1")
        assert status == 503

    def test_stale_erlaubt_ein_altes_bild(self, fake_ffmpeg):
        """Lieber ein altes Bild als ein kaputtes Symbol im Callmanager."""
        with dienst(fake_ffmpeg) as base:
            hole(f"{base}/cam/tuer.jpg")               # erst ein Bild besorgen
            status, _, body = hole(f"{base}/cam/tuer.jpg?max_age=0&timeout=0.5&stale=1")
        assert status == 200
        assert is_jpeg(body)

    def test_ohne_stale_kein_altes_bild(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            hole(f"{base}/cam/tuer.jpg")
            status, _, _ = hole_fehler(f"{base}/cam/tuer.jpg?max_age=0&timeout=0.5")
        assert status == 503

    def test_unsinnige_parameter_werden_ignoriert(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            status, _, body = hole(f"{base}/cam/tuer.jpg?timeout=abc&max_age=xyz")
        assert status == 200
        assert is_jpeg(body)

    def test_head_ohne_rumpf(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            status, headers, body = hole(f"{base}/cam/tuer.jpg", method="HEAD")
        assert status == 200
        assert headers["Content-Type"] == "image/jpeg"
        assert body == b""


class TestZugangsschutz:
    def test_ohne_token_offen(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            assert hole(f"{base}/cam/tuer.jpg")[0] == 200

    def test_token_in_der_url(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, token="geheim") as base:
            assert hole(f"{base}/cam/tuer.jpg?token=geheim")[0] == 200

    def test_token_als_kopfzeile(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, token="geheim") as base:
            bearer = hole(f"{base}/cam/tuer.jpg",
                          headers={"Authorization": "Bearer geheim"})
            eigen = hole(f"{base}/cam/tuer.jpg", headers={"X-Auth-Token": "geheim"})
        assert bearer[0] == 200
        assert eigen[0] == 200

    @pytest.mark.parametrize(
        "pfad", ["/cam/tuer.jpg", "/cam/tuer.jpg?token=falsch", "/", "/status",
                 "/cam/tuer.mjpg"],
    )
    def test_ohne_gueltiges_token_401(self, fake_ffmpeg, pfad):
        with dienst(fake_ffmpeg, token="geheim") as base:
            status, _, _ = hole_fehler(f"{base}{pfad}")
        assert status == 401

    def test_basic_auth(self, fake_ffmpeg):
        auth = base64.b64encode(b"starface:geheim").decode()
        with dienst(fake_ffmpeg, basic_auth="starface:geheim") as base:
            gut = hole(f"{base}/cam/tuer.jpg",
                       headers={"Authorization": f"Basic {auth}"})
            status, headers, _ = hole_fehler(f"{base}/cam/tuer.jpg")
        assert gut[0] == 200
        assert status == 401
        assert "Basic" in headers["WWW-Authenticate"]

    def test_kaputter_basic_kopf_wird_abgelehnt(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, basic_auth="a:b") as base:
            status, _, _ = hole_fehler(
                f"{base}/cam/tuer.jpg", headers={"Authorization": "Basic !!!kein-base64"}
            )
        assert status == 401

    def test_healthz_bleibt_offen(self, fake_ffmpeg):
        """Docker und Ueberwachung sollen das Geheimnis nicht kennen muessen."""
        with dienst(fake_ffmpeg, token="geheim") as base:
            assert hole(f"{base}/healthz")[0] == 200

    def test_healthz_kann_geschuetzt_werden(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, token="geheim", protect_health=True) as base:
            status, _, _ = hole_fehler(f"{base}/healthz")
        assert status == 401


class TestWeitereEndpunkte:
    def test_healthz(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            status, headers, body = hole(f"{base}/healthz")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(body)["status"] == "ok"

    def test_healthz_meldet_stoerung(self, fake_ffmpeg, monkeypatch):
        """Antwortet die Kamera dauerhaft nicht, muss /healthz das zeigen."""
        monkeypatch.setenv("FAKE_START_DELAY", "60")
        cameras = {
            "tuer": CameraConfig(name="tuer", url="rtsp://host/s", max_age=0.2,
                                 timeout=0.5, idle_timeout=0)
        }
        with dienst(fake_ffmpeg, cameras=cameras) as base:
            frist = time.monotonic() + 6.0
            while time.monotonic() < frist:
                status, body = status_von(f"{base}/healthz")
                if status != 200:
                    break
                time.sleep(0.2)
        assert status == 503
        assert json.loads(body)["status"] == "degraded"
        assert json.loads(body)["cameras"]["tuer"]["frame_age"] is None

    def test_status_nennt_kein_passwort(self, fake_ffmpeg):
        cameras = {
            "tuer": CameraConfig(name="tuer", url="rtsp://admin:streng-geheim@host/s",
                                 idle_timeout=0)
        }
        with dienst(fake_ffmpeg, cameras=cameras) as base:
            _, _, body = hole(f"{base}/status")
        assert b"streng-geheim" not in body
        assert b"***" in body

    def test_startseite_listet_die_kameras(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            status, headers, body = hole(f"{base}/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"/cam/tuer.jpg" in body

    def test_unbekannter_pfad_ist_404(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            assert hole_fehler(f"{base}/gibtsnicht")[0] == 404

    def test_mjpeg_liefert_mehrere_teile(self, fake_ffmpeg):
        with dienst(fake_ffmpeg) as base:
            request = urllib.request.Request(f"{base}/cam/tuer.mjpg")
            with urllib.request.urlopen(request, timeout=15) as response:
                assert response.headers["Content-Type"].startswith(
                    "multipart/x-mixed-replace"
                )
                daten = response.read(20000)
        assert daten.count(b"--rtsp2jpeg-frame") >= 2


class TestMehrereKameras:
    def test_jede_kamera_eigener_pfad(self, fake_ffmpeg):
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

            threads = [threading.Thread(target=abrufen) for _ in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        assert len(ergebnisse) == 10
        assert all(status == 200 and gueltig for status, gueltig in ergebnisse)
