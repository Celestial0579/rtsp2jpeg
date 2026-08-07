"""Konfiguration laden, validieren und mit Vorgabewerten auffuellen."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

# ${VAR} und ${VAR:-fallback}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Fehlerhafte oder unvollstaendige Konfiguration."""


def expand_env(value: str, environ: dict[str, str] | None = None) -> str:
    """Ersetzt ``${VAR}`` und ``${VAR:-fallback}`` durch Umgebungsvariablen.

    Damit stehen Kamera-Passwoerter in der Umgebung statt in der YAML-Datei.
    Eine gesetzte, aber leere Variable gilt als gesetzt -- der Fallback greift
    also nur, wenn die Variable gar nicht existiert.
    """
    env = os.environ if environ is None else environ

    def replace(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if fallback is not None:
            return fallback
        raise ConfigError(
            f"Umgebungsvariable {name!r} ist in der Konfiguration referenziert, "
            f"aber nicht gesetzt"
        )

    return _ENV_PATTERN.sub(replace, value)


def _expand_tree(node: Any, environ: dict[str, str] | None = None) -> Any:
    if isinstance(node, str):
        return expand_env(node, environ)
    if isinstance(node, dict):
        return {k: _expand_tree(v, environ) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_tree(v, environ) for v in node]
    return node


@dataclass(frozen=True)
class CameraConfig:
    """Eine Kamera: RTSP-Quelle und wie daraus JPEGs werden."""

    name: str
    url: str
    fps: float = 2.0
    width: int | None = None
    height: int | None = None
    quality: int = 5           # ffmpeg -q:v, 2 = beste, 31 = schlechteste
    transport: str = "tcp"     # tcp ist bei Tuersprechstellen deutlich stabiler
    idle_timeout: float = 60.0  # 0 = ffmpeg laeuft dauerhaft
    max_age: float = 5.0       # aelteres Standbild gilt als abgestanden
    timeout: float = 10.0      # so lange wartet eine Anfrage auf ein Bild
    read_timeout: float = 10.0  # so lange wartet ffmpeg auf Daten der Kamera
    rotate: int = 0            # 0, 90, 180 oder 270 Grad
    input_args: list[str] = field(default_factory=list)
    output_args: list[str] = field(default_factory=list)

    @property
    def redacted_url(self) -> str:
        """URL ohne Passwort -- fuer Logs und die Statusseite."""
        return redact_url(self.url)


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    token: str | None = None
    basic_auth: str | None = None       # "benutzer:passwort"
    protect_health: bool = False        # /healthz standardmaessig ohne Token
    trust_proxy: bool = False           # X-Forwarded-For fuers Protokoll auswerten
    log_level: str = "INFO"
    access_log: bool = True


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    cameras: dict[str, CameraConfig]


def redact_url(url: str) -> str:
    """Ersetzt das Passwort in ``rtsp://user:pass@host/...`` durch ``***``."""
    return re.sub(r"(://[^/:@\s]+:)[^@/\s]*(@)", r"\1***\2", url)


_CAMERA_FIELDS = {
    "url": str,
    "fps": float,
    "width": int,
    "height": int,
    "quality": int,
    "transport": str,
    "idle_timeout": float,
    "max_age": float,
    "timeout": float,
    "read_timeout": float,
    "rotate": int,
    "input_args": list,
    "output_args": list,
}

_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _coerce_camera(name: str, raw: dict[str, Any], defaults: dict[str, Any]) -> CameraConfig:
    if not _VALID_NAME.match(name):
        raise ConfigError(
            f"Kameraname {name!r} ist unzulaessig: erlaubt sind Buchstaben, "
            f"Ziffern, '-' und '_', beginnend mit Buchstabe oder Ziffer"
        )

    merged: dict[str, Any] = {**defaults, **raw}
    unknown = set(merged) - set(_CAMERA_FIELDS)
    if unknown:
        raise ConfigError(
            f"Kamera {name!r}: unbekannte Einstellung(en) {sorted(unknown)}"
        )

    if not merged.get("url"):
        raise ConfigError(f"Kamera {name!r}: 'url' fehlt")

    values: dict[str, Any] = {}
    for key, value in merged.items():
        if value is None:
            continue
        want = _CAMERA_FIELDS[key]
        try:
            values[key] = want(value) if want is not list else list(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Kamera {name!r}: {key!r} erwartet {want.__name__}, "
                f"bekam {value!r}"
            ) from exc

    url = values["url"]
    if not url.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        raise ConfigError(
            f"Kamera {name!r}: 'url' muss mit rtsp://, rtsps://, http:// oder "
            f"https:// beginnen (ist: {redact_url(url)!r})"
        )

    transport = values.get("transport", "tcp").lower()
    if transport not in ("tcp", "udp"):
        raise ConfigError(
            f"Kamera {name!r}: 'transport' muss 'tcp' oder 'udp' sein, "
            f"ist {transport!r}"
        )
    values["transport"] = transport

    fps = values.get("fps", 2.0)
    if fps <= 0:
        raise ConfigError(f"Kamera {name!r}: 'fps' muss groesser 0 sein, ist {fps}")

    quality = values.get("quality", 5)
    if not 2 <= quality <= 31:
        raise ConfigError(
            f"Kamera {name!r}: 'quality' muss zwischen 2 und 31 liegen, ist {quality}"
        )

    rotate = values.get("rotate", 0)
    if rotate not in (0, 90, 180, 270):
        raise ConfigError(
            f"Kamera {name!r}: 'rotate' muss 0, 90, 180 oder 270 sein, ist {rotate}"
        )

    if values.get("read_timeout", 10.0) <= 0:
        raise ConfigError(
            f"Kamera {name!r}: 'read_timeout' muss groesser 0 sein, "
            f"ist {values['read_timeout']}"
        )

    for key in ("width", "height"):
        if key in values and values[key] <= 0:
            raise ConfigError(
                f"Kamera {name!r}: {key!r} muss groesser 0 sein, ist {values[key]}"
            )

    return CameraConfig(name=name, **values)


def _server_from(raw: dict[str, Any]) -> ServerConfig:
    known = set(ServerConfig.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"server: unbekannte Einstellung(en) {sorted(unknown)}")

    basic = raw.get("basic_auth")
    if basic is not None and ":" not in basic:
        raise ConfigError("server.basic_auth muss die Form 'benutzer:passwort' haben")

    values = {k: v for k, v in raw.items() if v is not None}
    try:
        return ServerConfig(**values)
    except TypeError as exc:  # pragma: no cover - von der Pruefung oben abgedeckt
        raise ConfigError(f"server: {exc}") from exc


def load_config(path: str | None = None, environ: dict[str, str] | None = None) -> Config:
    """Laedt die Konfiguration aus YAML-Datei und/oder Umgebung.

    Ohne Datei genuegt ``CAMERA_URL`` fuer den Ein-Kamera-Betrieb -- das ist der
    typische Fall einer einzelnen Tuerklingel.
    """
    env = dict(os.environ if environ is None else environ)
    raw: dict[str, Any] = {}

    if path:
        if not os.path.exists(path):
            raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}")
        with open(path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(
                f"{path}: erwartet wird eine YAML-Zuordnung auf oberster Ebene"
            )
        raw = _expand_tree(loaded, env)

    unknown_top = set(raw) - {"server", "defaults", "cameras"}
    if unknown_top:
        raise ConfigError(
            f"Unbekannter Abschnitt {sorted(unknown_top)} -- erlaubt sind "
            f"'server', 'defaults' und 'cameras'"
        )

    server_raw: dict[str, Any] = dict(raw.get("server") or {})
    if "PORT" in env:
        server_raw["port"] = int(env["PORT"])
    if "TOKEN" in env:
        server_raw["token"] = env["TOKEN"] or None
    if "BASIC_AUTH" in env:
        server_raw["basic_auth"] = env["BASIC_AUTH"] or None
    if "TRUST_PROXY" in env:
        server_raw["trust_proxy"] = env["TRUST_PROXY"].lower() in ("1", "true", "ja", "yes")
    if "LOG_LEVEL" in env:
        server_raw["log_level"] = env["LOG_LEVEL"]
    server = _server_from(server_raw)

    defaults: dict[str, Any] = dict(raw.get("defaults") or {})
    cameras_raw: dict[str, Any] = dict(raw.get("cameras") or {})

    # Ein-Kamera-Betrieb ohne YAML-Datei
    if env.get("CAMERA_URL"):
        name = env.get("CAMERA_NAME", "kamera")
        entry = dict(cameras_raw.get(name) or {})
        entry["url"] = env["CAMERA_URL"]
        cameras_raw[name] = entry

    if not cameras_raw:
        raise ConfigError(
            "Keine Kamera konfiguriert -- entweder 'cameras' in der "
            "Konfigurationsdatei fuellen oder CAMERA_URL setzen"
        )

    cameras = {
        name: _coerce_camera(name, dict(entry or {}), defaults)
        for name, entry in cameras_raw.items()
    }
    return Config(server=server, cameras=cameras)
