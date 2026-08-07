"""Alles, was zaehlt, sobald der Dienst aus dem Internet erreichbar ist."""

from __future__ import annotations

import base64
import time
import urllib.parse

import rtsp2jpeg.server
from rtsp2jpeg.config import CameraConfig, Config, ServerConfig
from rtsp2jpeg.server import Bremse, redact_query, warn_ueber_die_absicherung

from .test_server import dienst, hole, hole_fehler


class TestRedactQuery:
    def test_token_wird_unkenntlich(self):
        assert redact_query("/cam/t.jpg?token=geheim") == "/cam/t.jpg?token=***"

    def test_auch_zwischen_anderen_parametern(self):
        ergebnis = redact_query("/cam/t.jpg?max_age=1&token=geheim&stale=1")
        assert "geheim" not in ergebnis
        assert "max_age=1" in ergebnis
        assert "stale=1" in ergebnis

    def test_ohne_token_unveraendert(self):
        assert redact_query("/cam/t.jpg?max_age=1") == "/cam/t.jpg?max_age=1"


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

    def test_kurzes_token_wird_bemaengelt(self):
        hinweise = warn_ueber_die_absicherung(self.baue(token="kurz"))
        assert any("kurz" in h for h in hinweise)

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

    def test_token_taucht_im_zugriffsprotokoll_nicht_auf(self, fake_ffmpeg, caplog):
        token = "streng-geheimes-token-1234"
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, token=token,
                                             access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg?token={token}")
        assert token not in caplog.text
        assert "token=***" in caplog.text

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

    def test_richtiges_token_bleibt_schnell(self, fake_ffmpeg):
        token = "a" * 32
        with dienst(fake_ffmpeg, token=token) as base:
            hole(f"{base}/cam/tuer.jpg?token={token}")   # Bild schon im Speicher
            for _ in range(6):
                hole_fehler(f"{base}/cam/tuer.jpg?token=falsch")
            beginn = time.monotonic()
            status, _, _ = hole(f"{base}/cam/tuer.jpg?token={token}")
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

    def test_mehrere_proxys_erster_eintrag_zaehlt(self, fake_ffmpeg, caplog):
        with caplog.at_level("INFO"), dienst(fake_ffmpeg, trust_proxy=True,
                                             access_log=True) as base:
            hole(f"{base}/cam/tuer.jpg",
                 headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
        assert "203.0.113.9" in caplog.text
