"""Caddy im Beispiel und im Testaufbau ersetzt das Token in seinen Protokollen."""

from __future__ import annotations

from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
DATEIEN = ["beispiele/internet/Caddyfile", "tests/proxy/Caddyfile"]


def log_block(datei: str) -> str:
    """Der Block ``log default { ... }`` aus den globalen Optionen."""
    text = (WURZEL / datei).read_text(encoding="utf-8")
    zeilen = [z for z in text.splitlines() if not z.strip().startswith("#")]
    text = "\n".join(zeilen)
    beginn = text.index("log default {")
    tiefe = 0
    for i in range(beginn, len(text)):
        if text[i] == "{":
            tiefe += 1
        elif text[i] == "}":
            tiefe -= 1
            if tiefe == 0:
                return text[beginn : i + 1]
    raise AssertionError(f"{datei}: Block nicht geschlossen")


@pytest.mark.parametrize("datei", DATEIEN)
def test_token_wird_ersetzt(datei):
    block = " ".join(log_block(datei).split())
    assert "format filter {" in block
    assert "request>uri query { replace token REDACTED }" in block


def test_beispiel_und_testaufbau_gleich():
    assert log_block(DATEIEN[0]) == log_block(DATEIEN[1])
