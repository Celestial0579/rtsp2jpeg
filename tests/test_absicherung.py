"""Alles, was zaehlt, sobald der Dienst aus dem Internet erreichbar ist."""

from __future__ import annotations

import base64
import threading
import time
import urllib.parse

import rtsp2jpeg.server
from rtsp2jpeg.config import CameraConfig, Config, ServerConfig
from rtsp2jpeg.server import Bremse, warn_ueber_die_absicherung

from .test_server import dienst, hole, hole_fehler, roh


class TestBremse:
    def test_erster_versuch_ohne_verzoegerung(self):
        bremse = Bremse()
        assert bremse.delay_for("1.2.3.4") == 0.0

    def test_unbekannte_gegenstelle_haengt_nicht_an_der_laufzeit(self, monkeypatch):
        """Auf einem eben gestarteten Rechner ist time.monotonic() winzig.

        Wer den Zeitstempel eines fehlenden Eintrags als 0.0 annimmt und
        gegen die Verfallsfrist prueft, bremst dort jeden Erstbesucher aus --
        auf einem seit Wochen laufenden Rechner faellt das nie auf. Deshalb
        wird die Uhr hier auf frisch gestartet gestellt.
        """
        monkeypatch.setattr(rtsp2jpeg.server.time, "monotonic", lambda: 12.5)
        bremse = Bremse(reset_after=900.0)
        assert bremse.delay_for("noch.nie.gesehen") == 0.0
        assert bremse.delay_for("auch.nicht.bekannt") == 0.0

    def test_alte_fehlversuche_verfallen(self):
        bremse = Bremse(reset_after=0.0)
        bremse.note_failure("1.2.3.4")
        time.sleep(0.01)
        assert bremse.delay_for("1.2.3.4") == 0.0

    def test_verzoegerung_waechst(self):
        bremse = Bremse()
        vorher = 0.0
        for _ in range(6):
            bremse.note_failure("1.2.3.4")
            jetzt = bremse.delay_for("1.2.3.4")
            assert jetzt >= vorher
            vorher = jetzt
        assert vorher > 0.0

    def test_verzoegerung_ist_gedeckelt(self):
        bremse = Bremse(max_delay=2.0)
        for _ in range(50):
            bremse.note_failure("1.2.3.4")
        assert bremse.delay_for("1.2.3.4") == 2.0

    def test_erfolg_loescht_die_strafe(self):
        bremse = Bremse()
        for _ in range(5):
            bremse.note_failure("1.2.3.4")
        bremse.note_success("1.2.3.4")
        assert bremse.delay_for("1.2.3.4") == 0.0

    def test_gegenstellen_stoeren_sich_nicht(self):
        bremse = Bremse()
        for _ in range(5):
            bremse.note_failure("1.2.3.4")
        assert bremse.delay_for("5.6.7.8") == 0.0

    def test_gleichzeitige_versuche_bekommen_nacheinander_liegende_fenster(self):
        bremse = Bremse(max_delay=2.0, max_wait=100.0)
        bremse.note_failure("1.2.3.4")
        wartezeiten = [bremse.reserve("1.2.3.4") for _ in range(5)]
        assert all(w is not None for w in wartezeiten)
        assert wartezeiten == sorted(wartezeiten)
        assert wartezeiten[-1] >= 5 * 0.2 - 0.01

    def test_volle_warteschlange_wird_nicht_geprueft(self):
        bremse = Bremse(max_delay=2.0, max_wait=1.0)
        for _ in range(10):
            bremse.note_failure("1.2.3.4")
        assert bremse.reserve("1.2.3.4") is None
        assert bremse.reserve("5.6.7.8") == 0.0

    def test_unbekannte_gegenstelle_reserviert_nichts(self):
        bremse = Bremse()
        assert bremse.reserve("1.2.3.4") == 0.0
        assert bremse.reserve("1.2.3.4") == 0.0

    def test_speicher_waechst_nicht_unbegrenzt(self):
        """Wer die Quell-IP wechselt, darf den Speicher nicht vollmuellen."""
        bremse = Bremse(max_entries=100)
        for i in range(1000):
            bremse.note_failure(f"10.0.{i // 256}.{i % 256}")
        assert len(bremse._fehlversuche) <= 101


class TestStartwarnungen:
    def baue(self, **server_kwargs) -> Config:
        return Config(
            server=ServerConfig(**server_kwargs),
            cameras={"t": CameraConfig(name="t", url="rtsp://h/s")},
        )

    def test_ohne_zugangsschutz_wird_nicht_nur_gewarnt(self):
        """Fehlender Schutz ist kein Hinweis mehr, sondern ein Startabbruch.

        Eine Warnzeile im Protokoll uebersieht man; siehe tests/test_token.py
        fuer die Verweigerung selbst.
        """
        from rtsp2jpeg.server import fehlt_der_zugangsschutz

        assert fehlt_der_zugangsschutz(self.baue()) is not None

    def test_ausdruecklich_offener_betrieb_wird_gewarnt(self):
        hinweise = warn_ueber_die_absicherung(self.baue(allow_anonymous=True))
        assert any("ohne Zugangsschutz" in h for h in hinweise)

    def test_langes_token_ist_in_ordnung(self):
        assert warn_ueber_die_absicherung(self.baue(token="a" * 48)) == []

    def test_basic_auth_gilt_als_schutz(self):
        hinweise = warn_ueber_die_absicherung(self.baue(basic_auth="u:p"))
        assert not any("Kein Zugangsschutz" in h for h in hinweise)


class TestLoginInDerUrl:
    """STARFACE bietet nur ein einziges URL-Feld -- der Login muss hinein."""

    def test_token_als_abfrageparameter(self, fake_ffmpeg):
        with dienst(fake_ffmpeg, token="a" * 32) as base:
            status, _, _ = hole(f"{base}/cam/tuer.jpg?token={'a' * 32}")
        assert status == 200

    def test_benutzer_und_passwort_im_url_vorspann(self, fake_ffmpeg):
        """https://benutzer:passwort@host/... -- so, wie ein Browser es sendet."""
        with dienst(fake_ffmpeg, basic_auth="starface:geheim") as base:
            # urllib wertet den Vorspann nicht selbst aus; wir bilden nach,
            # was ein Client daraus macht.
            zerlegt = urllib.parse.urlsplit(base)
            angemeldet = f"{zerlegt.scheme}://starface:geheim@{zerlegt.netloc}/cam/tuer.jpg"
            benutzer_teil = urllib.parse.urlsplit(angemeldet).netloc.split("@")[0]
            kopf = base64.b64encode(benutzer_teil.encode()).decode()
            status, _, _ = hole(f"{base}/cam/tuer.jpg",
                                headers={"Authorization": f"Basic {kopf}"})
        assert status == 200

    def test_sonderzeichen_im_token(self, fake_ffmpeg):
        token = "a+b/c=d&e"
        with dienst(fake_ffmpeg, token=token) as base:
            status, _, _ = hole(
                f"{base}/cam/tuer.jpg?token={urllib.parse.quote(token, safe='')}"
            )
        assert status == 200

    def test_token_taucht_im_protokoll_nicht_auf(self, fake_ffmpeg, caplog):
        token = "streng-geheimes-token-1234"
        with caplog.at_level("DEBUG"), dienst(fake_ffmpeg, token=token,
                                              access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg?token={token}")
            hole_fehler(f"{base}/cam/tuer.jpg?token={token}x")
            hole_fehler(f"{base}/status?token={token}")
            roh(base, f"GET /cam/tuer.jpg?token={token} HTTP/9.9\r\n\r\n".encode())
            roh(base, f"POST /cam/tuer.jpg?token={token} HTTP/1.1\r\n\r\n".encode())
        assert token not in caplog.text
        assert '"GET /cam/tuer.jpg" 200' in caplog.text
        assert '"GET -" 404' in caplog.text

    def test_kamera_passwort_taucht_im_protokoll_nicht_auf(self, fake_ffmpeg, monkeypatch,
                                                           caplog):
        url = "rtsp://admin:pa@ss/wort@host/s"
        monkeypatch.setenv("FAKE_STDERR", f"{url}: Connection refused")
        monkeypatch.setenv("FAKE_FRAMES", "1")
        monkeypatch.setenv("FAKE_EXIT_CODE", "1")
        cameras = {"tuer": CameraConfig(name="tuer", url=url, timeout=1.0, idle_timeout=0)}
        with caplog.at_level("DEBUG"), dienst(fake_ffmpeg, cameras=cameras,
                                              access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg")
            time.sleep(1.0)  # ffmpeg endet mit Fehler, der Greifer meldet es
        assert "Connection refused" in caplog.text
        assert "pa@ss" not in caplog.text
        assert "ss/wort" not in caplog.text

    def test_falsches_token_wird_gebremst(self, fake_ffmpeg):
        """Mehrere Fehlversuche muessen spuerbar langsamer werden."""
        with dienst(fake_ffmpeg, token="a" * 32) as base:
            for _ in range(6):
                hole_fehler(f"{base}/cam/tuer.jpg?token=falsch")
            beginn = time.monotonic()
            status, _, _ = hole_fehler(f"{base}/cam/tuer.jpg?token=falsch")
            dauer = time.monotonic() - beginn
        assert status == 401
        assert dauer > 0.2, f"Fehlversuch kam nach {dauer:.3f}s zurueck"

    def test_gleichzeitige_fehlversuche_werden_ebenfalls_gebremst(self, fake_ffmpeg):
        """Parallel geschickte Versuche duerfen nicht zusammen durchrutschen."""
        with dienst(fake_ffmpeg, token="a" * 32) as base:
            hole_fehler(f"{base}/cam/tuer.jpg?token=falsch")
            dauern: list[float] = []
            sperre = threading.Lock()

            def versuch() -> None:
                beginn = time.monotonic()
                hole_fehler(f"{base}/cam/tuer.jpg?token=falsch", timeout=30)
                with sperre:
                    dauern.append(time.monotonic() - beginn)

            threads = [threading.Thread(target=versuch) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        assert len(dauern) == 6
        # Einzeln je mindestens 0,2 s -- nacheinander also mindestens 1,2 s.
        assert max(dauern) >= 1.1, f"laengster Versuch {max(dauern):.2f}s"

    def test_auch_das_richtige_token_wartet_nach_fehlversuchen(self, fake_ffmpeg):
        """Sonst verriete die schnelle Antwort den Treffer unter vielen Versuchen."""
        token = "a" * 32
        with dienst(fake_ffmpeg, token=token) as base:
            hole(f"{base}/cam/tuer.jpg?token={token}")   # Bild schon im Speicher
            for _ in range(3):
                hole_fehler(f"{base}/cam/tuer.jpg?token=falsch")
            beginn = time.monotonic()
            status, _, _ = hole(f"{base}/cam/tuer.jpg?token={token}")
            dauer = time.monotonic() - beginn
            # danach ist die Strafe vergessen
            beginn = time.monotonic()
            hole(f"{base}/cam/tuer.jpg?token={token}")
            danach = time.monotonic() - beginn
        assert status == 200
        assert dauer >= 0.3, f"gueltiger Abruf kam nach {dauer:.3f}s"
        assert danach < 0.5

    def test_andere_gegenstelle_bleibt_schnell(self, fake_ffmpeg):
        token = "a" * 32
        with dienst(fake_ffmpeg, token=token, trust_proxy=True) as base:
            hole(f"{base}/cam/tuer.jpg?token={token}")
            for _ in range(6):
                hole_fehler(f"{base}/cam/tuer.jpg?token=falsch",
                            headers={"X-Forwarded-For": "203.0.113.66"})
            beginn = time.monotonic()
            status, _, _ = hole(f"{base}/cam/tuer.jpg?token={token}",
                                headers={"X-Forwarded-For": "198.51.100.7"})
            dauer = time.monotonic() - beginn
        assert status == 200
        assert dauer < 1.0, f"gueltiger Abruf dauerte {dauer:.3f}s"


class TestHinterEinemProxy:
    def test_ohne_vertrauen_zaehlt_die_proxy_adresse(self, fake_ffmpeg, caplog):
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg", headers={"X-Forwarded-For": "203.0.113.9"})
        assert "203.0.113.9" not in caplog.text

    def test_mit_vertrauen_steht_der_echte_absender_im_protokoll(self, fake_ffmpeg, caplog):
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, trust_proxy=True,
                                             access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg", headers={"X-Forwarded-For": "203.0.113.9"})
        assert "203.0.113.9" in caplog.text

    def test_letzter_eintrag_zaehlt(self, fake_ffmpeg, caplog):
        """Den letzten Eintrag hat der eigene Proxy angehaengt -- der Rest ist frei erfunden."""
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, trust_proxy=True,
                                             access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg",
                 headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
        assert "10.0.0.1" in caplog.text
        assert "203.0.113.9" not in caplog.text

    def test_unsinn_im_kopf_landet_nicht_im_protokoll(self, fake_ffmpeg, caplog):
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, trust_proxy=True,
                                             access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg",
                 headers={"X-Forwarded-For": "erfunden-und-sehr-lang"})
        assert "erfunden" not in caplog.text
        assert "127.0.0.1" in caplog.text
