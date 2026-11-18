"""Die Compose-Dateien schraenken jeden Dienst ein, der von aussen erreichbar ist."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WURZEL = Path(__file__).resolve().parent.parent


def dienst(datei: str, name: str) -> dict:
    with open(WURZEL / datei, encoding="utf-8") as handle:
        return yaml.safe_load(handle)["services"][name]


@pytest.mark.parametrize("datei, name", [
    ("beispiele/internet/docker-compose.yml", "caddy"),
    ("beispiele/internet/docker-compose.yml", "rtsp2jpeg"),
    ("docker-compose.test.yml", "proxy"),
])
def test_dienst_ist_eingeschraenkt(datei, name):
    d = dienst(datei, name)
    assert d.get("read_only") is True
    assert d.get("cap_drop") == ["ALL"]
    assert set(d.get("cap_add") or []) <= {"NET_BIND_SERVICE"}
    assert "no-new-privileges:true" in (d.get("security_opt") or [])


@pytest.mark.parametrize("datei, name", [
    ("docker-compose.yml", "rtsp2jpeg"),
    ("beispiele/internet/docker-compose.yml", "caddy"),
    ("beispiele/internet/docker-compose.yml", "rtsp2jpeg"),
])
def test_protokoll_ist_begrenzt(datei, name):
    """Ohne max-size rotiert Dockers json-file nie (GHSA-42jm-5fpq-65j5)."""
    optionen = (dienst(datei, name).get("logging") or {}).get("options") or {}
    assert optionen.get("max-size") and optionen.get("max-file")
