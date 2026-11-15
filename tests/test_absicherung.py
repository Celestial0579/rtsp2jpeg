"""Alles, was zaehlt, sobald der Dienst aus dem Internet erreichbar ist."""

from __future__ import annotations

import base64
import threading
import time
import urllib.parse

import rtsp2jpeg.server
from rtsp2jpeg.config import CameraConfig, Config, ServerConfig, netze
from rtsp2jpeg.server import Bremse, warn_ueber_die_absicherung

from .test_server import dienst, hole, hole_fehler, roh

PROXY = netze("127.0.0.1", "test")


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

    def test_waehrend_der_sperre_wird_nicht_geprueft(self):
        bremse = Bremse()
        bremse.note_failure("1.2.3.4")
        assert bremse.darf_pruefen("1.2.3.4") is False
        assert bremse.darf_pruefen("5.6.7.8") is True

    def test_nach_der_sperre_genau_ein_versuch(self):
        """Gleichzeitige Versuche: einer wird geprueft, die anderen nicht."""
        bremse = Bremse(max_delay=0.05)
        bremse.note_failure("1.2.3.4")
        time.sleep(0.08)
        assert [bremse.darf_pruefen("1.2.3.4") for _ in range(5)] == \
            [True, False, False, False, False]

    def test_unbekannte_gegenstelle_darf_immer(self):
        bremse = Bremse()
        assert bremse.darf_pruefen("1.2.3.4") is True
        assert bremse.darf_pruefen("1.2.3.4") is True

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
        with caplog.at_level("DEBUG"), dienst(fake_ffmpeg, token=token, access_log=True,
                                              bremse=Bremse(max_delay=0.0)) as base:
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

    def test_nach_einem_fehlversuch_wird_nicht_geprueft(self, fake_ffmpeg):
        """Auch das richtige Token zaehlt waehrend der Sperre nicht."""
        token = "a" * 32
        with dienst(fake_ffmpeg, token=token) as base:
            hole(f"{base}/cam/tuer.jpg?token={token}")   # Bild schon im Speicher
            hole_fehler(f"{base}/cam/tuer.jpg?token=falsch")
            gesperrt, _, rumpf = hole_fehler(f"{base}/cam/tuer.jpg?token={token}")
            time.sleep(0.3)
            danach, _, _ = hole(f"{base}/cam/tuer.jpg?token={token}")
        assert gesperrt == 401
        assert rumpf == b""
        assert danach == 200

    def test_gesperrte_versuche_warten_nicht(self, fake_ffmpeg):
        """Gleichzeitige Versuche einer gesperrten Gegenstelle: sofort abgewiesen."""
        token = "a" * 32
        bremse = Bremse(max_delay=5.0)
        for _ in range(6):
            bremse.note_failure("127.0.0.1")
        with dienst(fake_ffmpeg, token=token, bremse=bremse) as base:
            ergebnisse: list[tuple[int, float]] = []
            sperre = threading.Lock()

            def versuch() -> None:
                beginn = time.monotonic()
                status, _, _ = hole_fehler(f"{base}/cam/tuer.jpg?token={token}")
                with sperre:
                    ergebnisse.append((status, time.monotonic() - beginn))

            threads = [threading.Thread(target=versuch) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        assert len(ergebnisse) == 6
        assert all(status == 401 for status, _ in ergebnisse)
        assert max(d for _, d in ergebnisse) < 1.0

    def test_andere_gegenstelle_bleibt_schnell(self, fake_ffmpeg):
        token = "a" * 32
        with dienst(fake_ffmpeg, token=token, trust_proxy=PROXY) as base:
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
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, trust_proxy=PROXY,
                                             access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg", headers={"X-Forwarded-For": "203.0.113.9"})
        assert "203.0.113.9" in caplog.text

    def test_letzter_eintrag_zaehlt(self, fake_ffmpeg, caplog):
        """Den letzten Eintrag hat der eigene Proxy angehaengt -- der Rest ist frei erfunden."""
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, trust_proxy=PROXY,
                                             access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg",
                 headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
        assert "10.0.0.1" in caplog.text
        assert "203.0.113.9" not in caplog.text

    def test_xff_zaehlt_nur_vom_eigenen_proxy(self, fake_ffmpeg, caplog):
        """Wer am Proxy vorbei direkt kommt, bestimmt seine Adresse nicht selbst."""
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, trust_proxy=PROXY,
                                             access_log=True) as base:
            roh(base, b"GET /cam/tuer.jpg HTTP/1.1\r\nX-Forwarded-For: 203.0.113.9\r\n\r\n",
                quelle="127.0.0.2")
        assert "203.0.113.9" not in caplog.text
        assert "127.0.0.2" in caplog.text

    def test_unsinn_im_kopf_landet_nicht_im_protokoll(self, fake_ffmpeg, caplog):
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, trust_proxy=PROXY,
                                             access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg",
                 headers={"X-Forwarded-For": "erfunden-und-sehr-lang"})
        assert "erfunden" not in caplog.text
        assert "127.0.0.1" in caplog.text
