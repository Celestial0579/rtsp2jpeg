"""Token erzeugen und die Weigerung, ungeschuetzt zu starten."""

from __future__ import annotations

import re

import pytest

from rtsp2jpeg.__main__ import main
from rtsp2jpeg.config import CameraConfig, Config, ServerConfig
from rtsp2jpeg.server import (
    fehlt_der_zugangsschutz,
    token_erzeugen,
    warn_ueber_die_absicherung,
)


def baue(**server_kwargs) -> Config:
    return Config(
        server=ServerConfig(**server_kwargs),
        cameras={"tuer": CameraConfig(name="tuer", url="rtsp://h/s")},
    )


class TestTokenErzeugen:
    def test_lang_genug_und_zufaellig(self):
        einer, anderer = token_erzeugen(), token_erzeugen()
        assert einer != anderer
        assert len(einer) >= 48          # 24 Byte als Hex
        assert warn_ueber_die_absicherung(baue(token=einer)) == []

    def test_url_sicher(self):
        """Das Token landet in einer URL -- es darf nichts kodiert werden."""
        for _ in range(20):
            assert re.fullmatch(r"[0-9a-f]+", token_erzeugen())

    def test_laenge_einstellbar(self):
        assert len(token_erzeugen(8)) == 16


class TestFehltDerZugangsschutz:
    def test_ohne_alles_verweigert(self):
        grund = fehlt_der_zugangsschutz(baue())
        assert grund is not None
        assert "startet nicht" in grund
        assert "--token-erzeugen" in grund      # der Ausweg wird genannt
        assert "ALLOW_ANONYMOUS" in grund       # und die Ausnahme auch

    def test_mit_token_in_ordnung(self):
        assert fehlt_der_zugangsschutz(baue(token="a" * 48)) is None

    def test_mit_basic_auth_in_ordnung(self):
        assert fehlt_der_zugangsschutz(baue(basic_auth="u:p")) is None

    def test_ausdruecklich_offen_ist_erlaubt(self):
        assert fehlt_der_zugangsschutz(baue(allow_anonymous=True)) is None

    def test_ausdruecklich_offen_wird_trotzdem_angemahnt(self):
        hinweise = warn_ueber_die_absicherung(baue(allow_anonymous=True))
        assert any("ohne Zugangsschutz" in h for h in hinweise)


class TestKommandozeile:
    def test_token_erzeugen_braucht_keine_konfiguration(self, capsys):
        """Das Token entsteht, bevor es irgendwo eingetragen ist."""
        assert main(["--token-erzeugen"]) == 0
        ausgabe = capsys.readouterr().out
        token = ausgabe.splitlines()[0].strip()
        assert re.fullmatch(r"[0-9a-f]{48}", token)
        assert f"?token={token}" in ausgabe          # fertige STARFACE-URL
        assert "/cam/tuer.jpg" in ausgabe

    def test_beispiel_url_ist_anpassbar(self, capsys):
        assert main(["--token-erzeugen", "--host", "kamera.example.org",
                     "--kamera", "hof"]) == 0
        assert "https://kamera.example.org/cam/hof.jpg?token=" in capsys.readouterr().out

    def test_start_ohne_zugangsschutz_wird_verweigert(self, monkeypatch, capsys):
        monkeypatch.setenv("CAMERA_URL", "rtsp://host/s")
        monkeypatch.delenv("TOKEN", raising=False)
        monkeypatch.delenv("ALLOW_ANONYMOUS", raising=False)
        assert main([]) == 3
        assert "startet nicht" in capsys.readouterr().err

    def test_check_meldet_fehlenden_zugangsschutz(self, monkeypatch, capsys):
        monkeypatch.setenv("CAMERA_URL", "rtsp://host/s")
        monkeypatch.delenv("TOKEN", raising=False)
        assert main(["--check"]) == 3
        gefangen = capsys.readouterr()
        assert "Konfiguration in Ordnung" in gefangen.out
        assert "startet nicht" in gefangen.err

    def test_check_mit_token_ist_zufrieden(self, monkeypatch, capsys):
        monkeypatch.setenv("CAMERA_URL", "rtsp://host/s")
        monkeypatch.setenv("TOKEN", "a" * 48)
        assert main(["--check"]) == 0
        assert "/cam/kamera.jpg" in capsys.readouterr().out

    @pytest.mark.parametrize("wert", ["1", "true", "ja", "yes"])
    def test_ausnahme_ueber_die_umgebung(self, monkeypatch, capsys, wert):
        monkeypatch.setenv("CAMERA_URL", "rtsp://host/s")
        monkeypatch.delenv("TOKEN", raising=False)
        monkeypatch.setenv("ALLOW_ANONYMOUS", wert)
        assert main(["--check"]) == 0

    def test_ausnahme_muss_ausdruecklich_sein(self, monkeypatch):
        monkeypatch.setenv("CAMERA_URL", "rtsp://host/s")
        monkeypatch.delenv("TOKEN", raising=False)
        monkeypatch.setenv("ALLOW_ANONYMOUS", "0")
        assert main(["--check"]) == 3
