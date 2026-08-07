"""Gemeinsame Testhilfen.

Die Tests kommen ohne echte Kamera und ohne echtes ffmpeg aus: ``fake_ffmpeg``
ist ein kleines Python-Programm, das sich wie ffmpeg verhaelt und einen
MJPEG-Strom auf stdout schreibt.
"""

from __future__ import annotations

import base64
import struct
import sys
import textwrap

import pytest

# Gueltiges 1x1-JPEG (Standard-Quantisierungstabellen, Baseline).
MINIMAL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR"
    "CAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
    "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
    "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl"
    "5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
    "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE"
    "hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iiigD//2Q=="
)


def jpeg(marker: bytes = b"") -> bytes:
    """JPEG mit optionalem Erkennungsmerkmal in einem Kommentarsegment."""
    if not marker:
        return MINIMAL_JPEG
    comment = b"\xff\xfe" + struct.pack(">H", len(marker) + 2) + marker
    return MINIMAL_JPEG[:2] + comment + MINIMAL_JPEG[2:]


def is_jpeg(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")


# Verhaelt sich wie ffmpeg, gesteuert ueber Umgebungsvariablen.
FAKE_FFMPEG = textwrap.dedent(
    '''
    """Tut so, als waere es ffmpeg: schreibt einen MJPEG-Strom auf stdout."""
    import os, struct, sys, time

    JPEG = bytes.fromhex(os.environ["FAKE_JPEG_HEX"])
    fps = float(os.environ.get("FAKE_FPS", "20"))
    count = int(os.environ.get("FAKE_FRAMES", "0"))          # 0 = endlos
    exit_code = int(os.environ.get("FAKE_EXIT_CODE", "0"))
    delay = float(os.environ.get("FAKE_START_DELAY", "0"))
    garbage = os.environ.get("FAKE_GARBAGE", "")

    if os.environ.get("FAKE_LOG_ARGS"):
        with open(os.environ["FAKE_LOG_ARGS"], "a") as fh:
            fh.write("\\x00".join(sys.argv[1:]) + "\\n")

    if os.environ.get("FAKE_STDERR"):
        print(os.environ["FAKE_STDERR"], file=sys.stderr, flush=True)

    time.sleep(delay)

    if garbage:
        sys.stdout.buffer.write(garbage.encode())
        sys.stdout.buffer.flush()

    written = 0
    while count == 0 or written < count:
        # Laufende Nummer im Kommentarsegment, damit Tests Bilder unterscheiden.
        tag = b"frame-%d" % written
        comment = b"\\xff\\xfe" + struct.pack(">H", len(tag) + 2) + tag
        sys.stdout.buffer.write(JPEG[:2] + comment + JPEG[2:])
        sys.stdout.buffer.flush()
        written += 1
        time.sleep(1.0 / fps)

    sys.exit(exit_code)
    '''
)


@pytest.fixture(scope="session")
def fake_ffmpeg(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Pfad zu einem ausfuehrbaren Ersatz-ffmpeg."""
    directory = tmp_path_factory.mktemp("fake-ffmpeg")
    script = directory / "fake_ffmpeg.py"
    script.write_text(FAKE_FFMPEG, encoding="utf-8")

    launcher = directory / "ffmpeg"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)
    return str(launcher)


@pytest.fixture(autouse=True)
def fake_ffmpeg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setzt die Vorgaben, die das Ersatz-ffmpeg auswertet."""
    monkeypatch.setenv("FAKE_JPEG_HEX", MINIMAL_JPEG.hex())
    monkeypatch.setenv("FAKE_FPS", "25")
    for name in ("FAKE_FRAMES", "FAKE_EXIT_CODE", "FAKE_START_DELAY",
                 "FAKE_GARBAGE", "FAKE_STDERR", "FAKE_LOG_ARGS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def real_ffmpeg() -> str:
    """Echtes ffmpeg -- Tests damit werden ohne uebersprungen."""
    from shutil import which

    found = which("ffmpeg")
    if not found:
        pytest.skip("ffmpeg ist nicht installiert")
    return found
