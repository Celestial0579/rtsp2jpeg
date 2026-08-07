from __future__ import annotations

import time

import pytest

from rtsp2jpeg.config import CameraConfig
from rtsp2jpeg.grabber import Grabber, build_command, split_jpegs

from .conftest import MINIMAL_JPEG, is_jpeg, jpeg


class TestSplitJpegs:
    def test_ein_vollstaendiges_bild(self):
        frames, rest = split_jpegs(bytearray(MINIMAL_JPEG))
        assert frames == [MINIMAL_JPEG]
        assert rest == b""

    def test_mehrere_bilder_am_stueck(self):
        a, b = jpeg(b"a"), jpeg(b"b")
        frames, rest = split_jpegs(bytearray(a + b))
        assert frames == [a, b]
        assert rest == b""

    def test_angefangenes_bild_bleibt_im_puffer(self):
        a = jpeg(b"a")
        halb = MINIMAL_JPEG[:20]
        frames, rest = split_jpegs(bytearray(a + halb))
        assert frames == [a]
        assert bytes(rest) == halb

    def test_muell_vor_dem_ersten_bild_wird_verworfen(self):
        frames, rest = split_jpegs(bytearray(b"lauter unsinn" + MINIMAL_JPEG))
        assert frames == [MINIMAL_JPEG]
        assert rest == b""

    def test_reiner_muell_waechst_nicht_unbegrenzt(self):
        frames, rest = split_jpegs(bytearray(b"x" * 10_000))
        assert frames == []
        assert len(rest) <= 1  # nur ein moegliches halbes Markerbyte

    def test_leerer_puffer(self):
        assert split_jpegs(bytearray()) == ([], bytearray())

    def test_bildweise_haeppchen_ergeben_das_ganze_bild(self):
        """So kommen die Daten in Wirklichkeit an: in beliebigen Stuecken."""
        strom = jpeg(b"eins") + jpeg(b"zwei") + jpeg(b"drei")
        puffer = bytearray()
        gefunden = []
        for i in range(0, len(strom), 7):
            puffer.extend(strom[i : i + 7])
            neue, puffer = split_jpegs(puffer)
            gefunden.extend(neue)
        assert gefunden == [jpeg(b"eins"), jpeg(b"zwei"), jpeg(b"drei")]
        assert all(is_jpeg(f) for f in gefunden)


class TestBuildCommand:
    def make(self, **kwargs) -> CameraConfig:
        return CameraConfig(name="t", url="rtsp://host/s", **kwargs)

    def test_grundgeruest(self):
        cmd = build_command(self.make(), ffmpeg="/bin/ffmpeg")
        assert cmd[0] == "/bin/ffmpeg"
        assert cmd[-3:] == ["-f", "mjpeg", "pipe:1"]
        assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "rtsp://host/s"

    def test_rtsp_transport_nur_bei_rtsp(self):
        assert "-rtsp_transport" in build_command(self.make(transport="udp"))
        http = CameraConfig(name="t", url="http://host/s.mjpg")
        assert "-rtsp_transport" not in build_command(http)

    def test_rtsp_bekommt_timeout_und_kein_rw_timeout(self):
        """ffmpeg bricht mit 'Option not found' ab, wenn hier -rw_timeout steht.

        Der RTSP-Demuxer kennt nur -timeout; -rw_timeout wirkt auf der
        Protokollebene und gilt fuer HTTP.
        """
        cmd = build_command(self.make(read_timeout=7.0))
        assert "-rw_timeout" not in cmd
        assert cmd[cmd.index("-timeout") + 1] == "7000000"
        assert cmd.index("-timeout") < cmd.index("-i")

    def test_http_bekommt_rw_timeout(self):
        cmd = build_command(CameraConfig(name="t", url="http://host/s.mjpg",
                                         read_timeout=3.0))
        assert "-timeout" not in cmd
        assert cmd[cmd.index("-rw_timeout") + 1] == "3000000"

    def test_skalierung_haelt_seitenverhaeltnis(self):
        cmd = build_command(self.make(width=640))
        filters = cmd[cmd.index("-vf") + 1]
        assert "scale=640:-2" in filters

    def test_feste_hoehe(self):
        filters = build_command(self.make(height=480))[
            build_command(self.make(height=480)).index("-vf") + 1
        ]
        assert "scale=-2:480" in filters

    @pytest.mark.parametrize(
        "grad, erwartet",
        [(90, "transpose=1"), (270, "transpose=2"), (180, "transpose=1,transpose=1")],
    )
    def test_drehung(self, grad, erwartet):
        cmd = build_command(self.make(rotate=grad))
        assert erwartet in cmd[cmd.index("-vf") + 1]

    def test_bildrate_und_qualitaet(self):
        cmd = build_command(self.make(fps=1.5, quality=7))
        assert "fps=1.5" in cmd[cmd.index("-vf") + 1]
        assert cmd[cmd.index("-q:v") + 1] == "7"

    def test_eigene_argumente_landen_an_der_richtigen_stelle(self):
        cmd = build_command(self.make(
            input_args=["-probesize", "32"], output_args=["-pix_fmt", "yuvj420p"]
        ))
        assert cmd.index("-probesize") < cmd.index("-i")
        assert cmd.index("-pix_fmt") > cmd.index("-i")


def camera(**kwargs) -> CameraConfig:
    base = {
        "name": "test", "url": "rtsp://host/s", "fps": 25,
        "max_age": 5.0, "timeout": 5.0, "idle_timeout": 0,
    }
    base.update(kwargs)
    return CameraConfig(**base)


class TestGrabber:
    def test_liefert_ein_bild(self, fake_ffmpeg):
        grabber = Grabber(camera(), ffmpeg=fake_ffmpeg)
        try:
            frame = grabber.get_frame()
            assert is_jpeg(frame.data)
            assert frame.age < 1.0
        finally:
            grabber.stop()

    def test_bilder_werden_frischer(self, fake_ffmpeg):
        grabber = Grabber(camera(), ffmpeg=fake_ffmpeg)
        try:
            erst = grabber.get_frame()
            time.sleep(0.3)
            spaeter = grabber.get_frame(max_age=0.2)
            assert spaeter.timestamp > erst.timestamp
            assert spaeter.data != erst.data  # laufende Nummer im Kommentar
        finally:
            grabber.stop()

    def test_zeitueberschreitung_wenn_ffmpeg_nichts_liefert(self, fake_ffmpeg, monkeypatch):
        monkeypatch.setenv("FAKE_START_DELAY", "30")
        grabber = Grabber(camera(timeout=0.6), ffmpeg=fake_ffmpeg)
        try:
            with pytest.raises(TimeoutError, match="kein Bild"):
                grabber.get_frame()
        finally:
            grabber.stop()

    def test_fehlendes_ffmpeg_wird_gemeldet(self):
        grabber = Grabber(camera(timeout=1.0), ffmpeg="/gibt/es/nicht/ffmpeg")
        try:
            with pytest.raises(TimeoutError):
                grabber.get_frame()
            assert "nicht startbar" in (grabber.status()["last_error"] or "")
        finally:
            grabber.stop()

    def test_neustart_nach_abbruch(self, fake_ffmpeg, monkeypatch):
        """Bricht ffmpeg weg, verbindet sich der Greifer neu."""
        monkeypatch.setenv("FAKE_FRAMES", "2")
        monkeypatch.setenv("FAKE_FPS", "50")
        grabber = Grabber(camera(timeout=8.0, max_age=0.5), ffmpeg=fake_ffmpeg)
        try:
            grabber.get_frame()
            deadline = time.monotonic() + 8.0
            while grabber.status()["restarts"] == 0 and time.monotonic() < deadline:
                time.sleep(0.1)
            assert grabber.status()["restarts"] >= 1
            assert is_jpeg(grabber.get_frame(max_age=10.0).data)
        finally:
            grabber.stop()

    def test_muell_im_strom_stoert_nicht(self, fake_ffmpeg, monkeypatch):
        monkeypatch.setenv("FAKE_GARBAGE", "unsinn vor dem ersten bild")
        grabber = Grabber(camera(), ffmpeg=fake_ffmpeg)
        try:
            assert is_jpeg(grabber.get_frame().data)
        finally:
            grabber.stop()

    def test_ffmpeg_meldungen_landen_im_status(self, fake_ffmpeg, monkeypatch):
        monkeypatch.setenv("FAKE_STDERR", "Connection refused")
        grabber = Grabber(camera(), ffmpeg=fake_ffmpeg)
        try:
            grabber.get_frame()
            deadline = time.monotonic() + 2.0
            while not grabber.status()["ffmpeg_log"] and time.monotonic() < deadline:
                time.sleep(0.05)
            assert "Connection refused" in " ".join(grabber.status()["ffmpeg_log"])
        finally:
            grabber.stop()

    def test_laeuft_erst_auf_anfrage_und_schlaeft_wieder_ein(self, fake_ffmpeg):
        grabber = Grabber(camera(idle_timeout=1.0), ffmpeg=fake_ffmpeg)
        try:
            grabber.start()
            time.sleep(0.4)
            assert grabber.status()["running"] is False, "ohne Abruf kein ffmpeg"

            grabber.get_frame()
            assert grabber.status()["running"] is True

            deadline = time.monotonic() + 6.0
            while grabber.status()["running"] and time.monotonic() < deadline:
                time.sleep(0.1)
            assert grabber.status()["running"] is False, "Leerlauf beendet ffmpeg nicht"

            # und auf Anfrage wieder aufwachen
            assert is_jpeg(grabber.get_frame().data)
        finally:
            grabber.stop()

    def test_leerlauf_zaehlt_nicht_als_fehler(self, fake_ffmpeg):
        grabber = Grabber(camera(idle_timeout=1.0), ffmpeg=fake_ffmpeg)
        try:
            grabber.get_frame()
            deadline = time.monotonic() + 6.0
            while grabber.status()["running"] and time.monotonic() < deadline:
                time.sleep(0.1)
            assert grabber.status()["restarts"] == 0
        finally:
            grabber.stop()

    def test_gesundheit_ohne_anfrage_ist_in_ordnung(self, fake_ffmpeg):
        grabber = Grabber(camera(idle_timeout=30), ffmpeg=fake_ffmpeg)
        assert grabber.is_healthy(), "eine schlafende Kamera ist nicht krank"

    def test_stream_liefert_fortlaufend_neue_bilder(self, fake_ffmpeg):
        grabber = Grabber(camera(), ffmpeg=fake_ffmpeg)
        try:
            gesehen = []
            for frame in grabber.stream(timeout=5.0):
                gesehen.append(frame.data)
                if len(gesehen) == 3:
                    break
            assert len(set(gesehen)) == 3
            assert all(is_jpeg(f) for f in gesehen)
        finally:
            grabber.stop()

    def test_stop_beendet_den_ffmpeg_prozess(self, fake_ffmpeg):
        grabber = Grabber(camera(), ffmpeg=fake_ffmpeg)
        grabber.get_frame()
        process = grabber._process
        assert process is not None and process.poll() is None
        grabber.stop()
        assert process.poll() is not None, "ffmpeg laeuft nach stop() weiter"

    def test_passwort_taucht_im_status_nicht_auf(self, fake_ffmpeg):
        grabber = Grabber(
            camera(url="rtsp://admin:streng-geheim@host/s"), ffmpeg=fake_ffmpeg
        )
        assert "streng-geheim" not in str(grabber.status())
