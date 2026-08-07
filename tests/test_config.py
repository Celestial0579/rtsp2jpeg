from __future__ import annotations

import textwrap

import pytest

from rtsp2jpeg.config import (
    ConfigError,
    expand_env,
    load_config,
    redact_url,
)


def write(tmp_path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


class TestEnvExpansion:
    def test_ersetzt_variable(self):
        assert expand_env("a${X}b", {"X": "1"}) == "a1b"

    def test_fallback_greift_nur_bei_fehlender_variable(self):
        assert expand_env("${X:-standard}", {}) == "standard"
        assert expand_env("${X:-standard}", {"X": ""}) == ""

    def test_fehlende_variable_ohne_fallback_ist_ein_fehler(self):
        with pytest.raises(ConfigError, match="X"):
            expand_env("${X}", {})


class TestRedactUrl:
    @pytest.mark.parametrize(
        "url, erwartet",
        [
            ("rtsp://u:geheim@host/s", "rtsp://u:***@host/s"),
            ("rtsp://host:554/s", "rtsp://host:554/s"),   # Port ist kein Passwort
            ("rtsp://host/s", "rtsp://host/s"),
        ],
    )
    def test_passwort_verschwindet(self, url, erwartet):
        assert redact_url(url) == erwartet


class TestLoadConfig:
    def test_minimal_ueber_umgebung(self):
        config = load_config(None, {"CAMERA_URL": "rtsp://host/s"})
        assert list(config.cameras) == ["kamera"]
        assert config.cameras["kamera"].url == "rtsp://host/s"
        assert config.server.port == 8080

    def test_kameraname_aus_umgebung(self):
        config = load_config(None, {"CAMERA_URL": "rtsp://host/s", "CAMERA_NAME": "tuer"})
        assert list(config.cameras) == ["tuer"]

    def test_ohne_kamera_ist_fehler(self):
        with pytest.raises(ConfigError, match="Keine Kamera"):
            load_config(None, {})

    def test_defaults_werden_vererbt_und_ueberschrieben(self, tmp_path):
        path = write(tmp_path, """
            defaults:
              fps: 4
              width: 640
            cameras:
              a:
                url: rtsp://host/a
              b:
                url: rtsp://host/b
                fps: 1
        """)
        config = load_config(path, {})
        assert config.cameras["a"].fps == 4
        assert config.cameras["a"].width == 640
        assert config.cameras["b"].fps == 1
        assert config.cameras["b"].width == 640

    def test_umgebung_wird_in_yaml_ersetzt(self, tmp_path):
        path = write(tmp_path, """
            cameras:
              tuer:
                url: rtsp://${U}:${P}@host/s
        """)
        config = load_config(path, {"U": "admin", "P": "geheim"})
        assert config.cameras["tuer"].url == "rtsp://admin:geheim@host/s"
        assert config.cameras["tuer"].redacted_url == "rtsp://admin:***@host/s"

    def test_port_und_token_aus_umgebung_ueberschreiben_datei(self, tmp_path):
        path = write(tmp_path, """
            server:
              port: 9000
              token: aus-datei
            cameras:
              a: {url: "rtsp://host/a"}
        """)
        config = load_config(path, {"PORT": "9999", "TOKEN": "aus-umgebung"})
        assert config.server.port == 9999
        assert config.server.token == "aus-umgebung"

    def test_leeres_token_aus_umgebung_schaltet_schutz_ab(self, tmp_path):
        path = write(tmp_path, """
            server: {token: "abc"}
            cameras: {a: {url: "rtsp://host/a"}}
        """)
        assert load_config(path, {"TOKEN": ""}).server.token is None

    @pytest.mark.parametrize(
        "yaml_text, muster",
        [
            ("cameras: {a: {}}", "'url' fehlt"),
            ("cameras: {a: {url: 'ftp://host/a'}}", "muss mit rtsp://"),
            ("cameras: {a: {url: 'rtsp://h/a', fps: 0}}", "'fps'"),
            ("cameras: {a: {url: 'rtsp://h/a', quality: 99}}", "'quality'"),
            ("cameras: {a: {url: 'rtsp://h/a', rotate: 45}}", "'rotate'"),
            ("cameras: {a: {url: 'rtsp://h/a', transport: sctp}}", "'transport'"),
            ("cameras: {a: {url: 'rtsp://h/a', width: 0}}", "'width'"),
            ("cameras: {a: {url: 'rtsp://h/a', quatsch: 1}}", "unbekannte Einstellung"),
            ("server: {quatsch: 1}\ncameras: {a: {url: 'rtsp://h/a'}}", "unbekannte Einstellung"),
            ("server: {basic_auth: 'ohnedoppelpunkt'}\ncameras: {a: {url: 'rtsp://h/a'}}",
             "benutzer:passwort"),
            ("quatsch: 1\ncameras: {a: {url: 'rtsp://h/a'}}", "Unbekannter Abschnitt"),
            ("cameras: {'boese/name': {url: 'rtsp://h/a'}}", "unzulaessig"),
            ("cameras: {'../etc': {url: 'rtsp://h/a'}}", "unzulaessig"),
        ],
    )
    def test_fehlerhafte_werte_werden_abgelehnt(self, tmp_path, yaml_text, muster):
        with pytest.raises(ConfigError, match=muster):
            load_config(write(tmp_path, yaml_text), {})

    def test_fehlende_datei(self, tmp_path):
        with pytest.raises(ConfigError, match="nicht gefunden"):
            load_config(str(tmp_path / "gibtsnicht.yaml"), {})

    def test_beispielkonfiguration_ist_gueltig(self):
        """Das mitgelieferte Beispiel muss durchlaufen."""
        config = load_config(
            "config.example.yaml",
            {"KAMERA_USER": "admin", "KAMERA_PASS": "geheim",
             "RTSP2JPEG_TOKEN": "t" * 48},
        )
        assert "tuer" in config.cameras
        assert config.cameras["tuer"].width == 800
        assert config.server.token == "t" * 48

    def test_beispielkonfiguration_verlangt_ein_token(self):
        """Wer das Beispiel ohne Token benutzt, soll das sofort merken."""
        with pytest.raises(ConfigError, match="RTSP2JPEG_TOKEN"):
            load_config("config.example.yaml",
                        {"KAMERA_USER": "a", "KAMERA_PASS": "b"})
