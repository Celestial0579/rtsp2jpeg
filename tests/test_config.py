from __future__ import annotations

import textwrap

import pytest

from rtsp2jpeg.config import (
    CameraConfig,
    ConfigError,
    expand_env,
    load_config,
    redact_text,
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
            ("rtsp://u:ge@heim@host/s", "rtsp://u:***@host/s"),   # @ im Passwort
            ("rtsp://u:ge/heim@host/s", "rtsp://u:***@host/s"),   # / im Passwort
            ("rtsp://u:@host/s", "rtsp://u:***@host/s"),
            ("rtsp://nurbenutzer@host/s", "rtsp://nurbenutzer@host/s"),
        ],
    )
    def test_passwort_verschwindet(self, url, erwartet):
        assert redact_url(url) == erwartet


class TestRedactText:
    def test_ffmpeg_meldung_mit_url(self):
        url = "rtsp://admin:ge@h/eim@192.168.1.50:554/s"
        meldung = f"[rtsp @ 0x55] method DESCRIBE failed: 401 Unauthorized ({url})"
        ergebnis = redact_text(meldung, url)
        assert "ge@h/eim" not in ergebnis
        assert "401 Unauthorized" in ergebnis

    def test_passwort_auch_ohne_url_drumherum(self):
        url = "rtsp://admin:streng-geheim@host/s"
        assert "streng-geheim" not in redact_text("Passwort streng-geheim falsch", url)

    def test_kodiertes_passwort(self):
        url = "rtsp://admin:p%40ss@host/s"
        assert "p@ss" not in redact_text("anmelden mit p@ss", url)
        assert "p%40ss" not in redact_text(url, url)

    def test_fremde_urls_im_text(self):
        assert "geheim" not in redact_text("http://x:geheim@andere/pfad")

    def test_kamera_schwaerzt_sich_selbst(self):
        kamera = CameraConfig(name="t", url="rtsp://a:streng-geheim@h/s")
        assert "streng-geheim" not in kamera.redact(f"Fehler bei {kamera.url}")


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

    def test_kamera_wartezeit_hat_eine_obergrenze(self, tmp_path):
        with pytest.raises(ConfigError, match="'timeout'"):
            load_config(write(tmp_path, "cameras: {a: {url: 'rtsp://h/a', timeout: 3600}}"), {})

    def test_veraltetes_protect_health_stoert_nicht(self, tmp_path):
        """Alte Dateien mit protect_health sollen nicht am Start scheitern."""
        path = write(tmp_path, """
            server: {protect_health: true, token: "abc"}
            cameras: {a: {url: "rtsp://host/a"}}
        """)
        assert load_config(path, {}).server.token == "abc"

    @pytest.mark.parametrize(
        "eintrag, muster",
        [
            ("token: 12345", "server.token muss Text sein"),
            ("basic_auth: 12345", "server.basic_auth muss Text sein"),
            ("port: '8080'", "server.port muss eine ganze Zahl"),
            ("port: true", "server.port muss eine ganze Zahl"),
            ("port: 70000", "server.port muss zwischen"),
            ("trust_proxy: 'false'", "server.trust_proxy"),
            ("trust_proxy: true", "Adresse des eigenen Proxys"),
            ("trust_proxy: 12345", "server.trust_proxy muss eine IPv4-Adresse"),
            ("allow_from: [12345]", "server.allow_from muss eine IPv4-Adresse"),
            ("access_log: 1", "server.access_log muss true oder false"),
        ],
    )
    def test_servertypen_werden_streng_geprueft(self, tmp_path, eintrag, muster):
        path = write(tmp_path, f"server: {{{eintrag}}}\ncameras: {{a: {{url: 'rtsp://h/a'}}}}")
        with pytest.raises(ConfigError, match=muster) as fehler:
            load_config(path, {})
        assert "12345" not in str(fehler.value)

    @pytest.mark.parametrize("eintrag", [
        "input_args: '-rtsp_flags geheim-prefer_tcp'",
        "output_args: '-vf geheim'",
        "input_args: ['-timeout', 5000000]",
        "input_args: {a: geheim}",
    ])
    def test_argumente_nur_als_liste_von_texten(self, tmp_path, eintrag):
        path = write(tmp_path, f"cameras: {{a: {{url: 'rtsp://h/a', {eintrag}}}}}")
        with pytest.raises(ConfigError, match="muss eine Liste von Texten sein") as fehler:
            load_config(path, {})
        assert "geheim" not in str(fehler.value)
        assert "5000000" not in str(fehler.value)

    def test_argumente_als_liste_bleiben_erhalten(self, tmp_path):
        path = write(tmp_path, "cameras: {a: {url: 'rtsp://h/a', "
                               "input_args: ['-rtsp_flags', 'prefer_tcp']}}")
        assert load_config(path, {}).cameras["a"].input_args == ["-rtsp_flags", "prefer_tcp"]

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


class TestAllowAnonymous:
    """Nur echte Wahrheitswerte zaehlen -- im Zweifel startet der Dienst nicht."""

    def lade(self, tmp_path, wert: str, env: dict[str, str] | None = None):
        path = write(tmp_path, f"""
            server:
              allow_anonymous: {wert}
            cameras:
              a: {{url: "rtsp://host/a"}}
        """)
        return load_config(path, env or {})

    @pytest.mark.parametrize("wert, erwartet", [
        ("true", True), ("false", False), ("yes", True), ("no", False),
    ])
    def test_echte_wahrheitswerte(self, tmp_path, wert, erwartet):
        assert self.lade(tmp_path, wert).server.allow_anonymous is erwartet

    @pytest.mark.parametrize("wert", ['"false"', "'true'", '"0"', "1", "0", '""', "[]"])
    def test_text_und_zahlen_werden_abgelehnt(self, tmp_path, wert):
        with pytest.raises(ConfigError, match="startet nicht"):
            self.lade(tmp_path, wert)

    @pytest.mark.parametrize("inhalt", ["false", "true", "0", ""])
    def test_aus_einer_variablen_ist_es_immer_text(self, tmp_path, inhalt):
        with pytest.raises(ConfigError, match="allow_anonymous"):
            self.lade(tmp_path, "${OFFEN}", {"OFFEN": inhalt})

    @pytest.mark.parametrize("wert, erwartet", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("ja", True),
        ("0", False), ("false", False), ("no", False), ("nein", False), ("", False),
    ])
    def test_umgebung_eindeutig(self, wert, erwartet):
        config = load_config(None, {"CAMERA_URL": "rtsp://h/s", "ALLOW_ANONYMOUS": wert})
        assert config.server.allow_anonymous is erwartet

    @pytest.mark.parametrize("wert", ["vielleicht", "2", "on", "wahr"])
    def test_umgebung_mehrdeutig_ist_ein_fehler(self, wert):
        with pytest.raises(ConfigError, match="ALLOW_ANONYMOUS"):
            load_config(None, {"CAMERA_URL": "rtsp://h/s", "ALLOW_ANONYMOUS": wert})

    def test_trust_proxy_ebenso(self):
        with pytest.raises(ConfigError, match="TRUST_PROXY"):
            load_config(None, {"CAMERA_URL": "rtsp://h/s", "TRUST_PROXY": "vielleicht"})

    @pytest.mark.parametrize("wert", ["1", "true", "ja"])
    def test_trust_proxy_braucht_eine_adresse(self, wert):
        with pytest.raises(ConfigError, match=r"TRUST_PROXY.*Adresse des eigenen Proxys"):
            load_config(None, {"CAMERA_URL": "rtsp://h/s", "TRUST_PROXY": wert})

    @pytest.mark.parametrize("wert, erwartet", [
        ("0", ()), ("", ()), ("172.18.0.2", ("172.18.0.2/32",)),
        ("172.18.0.0/16, 10.0.0.1", ("172.18.0.0/16", "10.0.0.1/32")),
    ])
    def test_trust_proxy_aus_der_umgebung(self, wert, erwartet):
        config = load_config(None, {"CAMERA_URL": "rtsp://h/s", "TRUST_PROXY": wert})
        assert tuple(str(n) for n in config.server.trust_proxy) == erwartet

    @pytest.mark.parametrize("wert", ["0.0.0.0/0", "10.0.0.5/24", "::1", "proxy"])
    def test_trust_proxy_unzulaessig(self, wert):
        with pytest.raises(ConfigError, match="TRUST_PROXY"):
            load_config(None, {"CAMERA_URL": "rtsp://h/s", "TRUST_PROXY": wert})

    def test_allow_from_aus_datei_und_umgebung(self, tmp_path):
        path = write(tmp_path, "server: {allow_from: [192.168.1.10, 10.0.0.0/24]}\n"
                               "cameras: {a: {url: 'rtsp://h/a'}}")
        config = load_config(path, {})
        assert [str(n) for n in config.server.allow_from] == ["192.168.1.10/32",
                                                               "10.0.0.0/24"]
        config = load_config(None, {"CAMERA_URL": "rtsp://h/s", "ALLOW_FROM": "192.168.1.10"})
        assert [str(n) for n in config.server.allow_from] == ["192.168.1.10/32"]
        assert load_config(None, {"CAMERA_URL": "rtsp://h/s"}).server.allow_from == ()

    @pytest.mark.parametrize("wert", ["", " ", " , "])
    def test_leeres_allow_from_in_der_umgebung_startet_nicht(self, wert):
        with pytest.raises(ConfigError, match="ALLOW_FROM ist gesetzt, aber leer"):
            load_config(None, {"CAMERA_URL": "rtsp://h/s", "ALLOW_FROM": wert})

    @pytest.mark.parametrize("zeile", ["allow_from:", 'allow_from: ""', "allow_from: []",
                                       "allow_from: false"])
    def test_leeres_allow_from_in_der_datei_startet_nicht(self, tmp_path, zeile):
        path = write(tmp_path, f"server:\n  {zeile}\ncameras: {{t: {{url: rtsp://h/s}}}}\n")
        with pytest.raises(ConfigError, match="allow_from ist gesetzt, aber leer"):
            load_config(path, {})

    @pytest.mark.parametrize("feld", ["fps", "idle_timeout", "max_age", "timeout",
                                      "read_timeout"])
    @pytest.mark.parametrize("wert", [".nan", ".inf", "-.inf", "nan", "inf"])
    def test_zahlen_muessen_endlich_sein(self, tmp_path, feld, wert):
        path = write(tmp_path, f"cameras: {{t: {{url: rtsp://h/s, {feld}: {wert}}}}}\n")
        with pytest.raises(ConfigError, match="endliche Zahl"):
            load_config(path, {})

    def test_allow_from_unzulaessig(self):
        with pytest.raises(ConfigError, match="ALLOW_FROM"):
            load_config(None, {"CAMERA_URL": "rtsp://h/s", "ALLOW_FROM": "tuerkamera"})

    def test_kaputter_port_aus_der_umgebung(self):
        with pytest.raises(ConfigError, match="PORT"):
            load_config(None, {"CAMERA_URL": "rtsp://h/s", "PORT": "acht"})


def test_vorgabe_fuer_timeout_bleibt_unter_der_starface_grenze():
    """Die STARFACE wartet hoechstens 5 s auf das Bild (#19)."""
    assert CameraConfig(name="t", url="rtsp://host/s").timeout < 5
