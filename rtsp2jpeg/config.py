"""Konfiguration laden, validieren und mit Vorgabewerten auffuellen."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

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
        """URL ohne Passwort -- fuer das Protokoll."""
        return redact_url(self.url)

    def redact(self, text: str) -> str:
        """Entfernt die Zugangsdaten dieser Kamera aus beliebigem Text."""
        return redact_text(text, self.url)


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    token: str | None = None
    basic_auth: str | None = None       # "benutzer:passwort"
    # Adressen des eigenen Proxys: nur von dort zaehlt X-Forwarded-For.
    trust_proxy: tuple[ipaddress.IPv4Network, ...] = ()
    # Nur diese Gegenstellen werden bedient; leer heisst: alle.
    allow_from: tuple[ipaddress.IPv4Network, ...] = ()
    allow_anonymous: bool = False       # Betrieb ganz ohne Zugangsschutz zulassen
    log_level: str = "INFO"
    access_log: bool = True


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    cameras: dict[str, CameraConfig]


Netze = tuple[ipaddress.IPv4Network, ...]


def im_netz(adresse: str, netze: Netze) -> bool:
    """Liegt ``adresse`` in einem der Netze? Unlesbares gilt als nein."""
    try:
        ip = ipaddress.IPv4Address(adresse)
    except ValueError:
        return False
    return any(ip in netz for netz in netze)


def netze(value: Any, key: str, *, ganz_offen: bool = True) -> Netze:
    """Adressen und Netze aus Text (``"a, b"``) oder einer Liste davon.

    Ohne Praefix ist eine einzelne Adresse gemeint. Hostbits im Netz
    (``10.0.0.5/24``) gelten als Tippfehler und verhindern den Start.
    """
    if value is None or value is False:
        return ()
    if isinstance(value, str):
        teile = [t.strip() for t in value.split(",")]
        teile = [t for t in teile if t]
    elif isinstance(value, list) and all(isinstance(t, str) for t in value):
        teile = [t.strip() for t in value]
    else:
        raise ConfigError(
            f"{key} muss eine IPv4-Adresse, ein IPv4-Netz oder eine Liste davon "
            f"sein -- der Dienst startet nicht"
        )
    ergebnis = []
    for teil in teile:
        try:
            netz = ipaddress.IPv4Network(teil, strict=True)
        except ValueError as exc:
            raise ConfigError(
                f"{key}: {teil!r} ist weder IPv4-Adresse noch IPv4-Netz "
                f"(etwa 172.18.0.2 oder 10.0.0.0/24) -- der Dienst startet nicht"
            ) from exc
        if netz.prefixlen == 0 and not ganz_offen:
            raise ConfigError(
                f"{key}: {teil!r} umfasst alle Adressen -- der Dienst startet nicht"
            )
        ergebnis.append(netz)
    return tuple(ergebnis)


_TRUST_PROXY_TRUE = (
    "trust_proxy braucht die Adresse des eigenen Proxys statt true, etwa "
    "172.18.0.2 oder 172.18.0.0/16 -- der Dienst startet nicht"
)


def redact_url(url: str) -> str:
    """Ersetzt das Passwort in ``rtsp://user:pass@host/...`` durch ``***``.

    Massgeblich ist das letzte ``@`` -- ein Passwort darf selbst ``@`` oder
    ``/`` enthalten. Im Zweifel wird lieber zu viel geschwaerzt als zu wenig.
    """
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    userinfo, _, host = rest.rpartition("@")
    user, colon, _ = userinfo.partition(":")
    return f"{scheme}://{user}{':***' if colon else ''}@{host}"


def _secrets_of(url: str) -> list[str]:
    """Passwort einer URL, roh und dekodiert -- leer, wenn es keins gibt."""
    _, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return []
    userinfo = rest.rpartition("@")[0]
    _, colon, password = userinfo.partition(":")
    if not colon or not password:
        return []
    return sorted({password, unquote(password)}, key=len, reverse=True)


_USERINFO_IN_TEXT = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]*:)[^\s@]*@")


def redact_text(text: str, url: str | None = None) -> str:
    """Schwaerzt Zugangsdaten in Text, etwa in einer Meldung von ffmpeg."""
    if url:
        text = text.replace(url, redact_url(url))
        for secret in _secrets_of(url):
            text = text.replace(secret, "***")
    return _USERINFO_IN_TEXT.sub(r"\1***@", text)


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

_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

MAX_CAMERA_TIMEOUT = 30.0  # laenger wartet keine Anfrage auf ein Bild


def _coerce_camera(name: str, raw: dict[str, Any], defaults: dict[str, Any]) -> CameraConfig:
    if not _VALID_NAME.match(name):
        raise ConfigError(
            f"Kameraname {name!r} ist unzulaessig: erlaubt sind Buchstaben, "
            f"Ziffern, '-' und '_', beginnend mit Buchstabe oder Ziffer, "
            f"hoechstens 64 Zeichen"
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
        if want is list:
            # Text wuerde sonst in Einzelzeichen zerlegt. Den Wert nicht
            # nennen -- er kann Zugangsdaten enthalten.
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(
                    f"Kamera {name!r}: {key!r} muss eine Liste von Texten sein, "
                    f"etwa ['-rtsp_flags', 'prefer_tcp']"
                )
            values[key] = list(value)
            continue
        try:
            values[key] = want(value)
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

    timeout = values.get("timeout", 10.0)
    if not 0 < timeout <= MAX_CAMERA_TIMEOUT:
        raise ConfigError(
            f"Kamera {name!r}: 'timeout' muss groesser 0 und hoechstens "
            f"{MAX_CAMERA_TIMEOUT:g} sein, ist {timeout}"
        )

    if values.get("max_age", 5.0) < 0:
        raise ConfigError(
            f"Kamera {name!r}: 'max_age' darf nicht negativ sein, ist {values['max_age']}"
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


_SERVER_FIELDS: dict[str, type] = {
    "host": str,
    "port": int,
    "token": str,
    "basic_auth": str,
    "allow_anonymous": bool,
    "log_level": str,
    "access_log": bool,
}

# Frueher vorhanden, heute ohne Wirkung -- bestehende Dateien sollen
# deswegen nicht am Start scheitern.
_SERVER_OBSOLETE = {"protect_health"}


def _check_type(key: str, value: Any, want: type) -> None:
    if want is bool:
        if not isinstance(value, bool):
            raise ConfigError(
                f"server.{key} muss true oder false sein, ohne Anfuehrungszeichen "
                f"-- Text (auch aus ${{VARIABLE}}) wird nicht als Wahrheitswert "
                f"gelesen. Der Dienst startet nicht."
            )
    elif want is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"server.{key} muss eine ganze Zahl sein")
    elif not isinstance(value, want):
        # Den Wert nicht nennen -- es koennte ein Geheimnis sein.
        raise ConfigError(f"server.{key} muss Text sein (ist {type(value).__name__})")


_SERVER_NETZE = ("trust_proxy", "allow_from")


def _server_from(raw: dict[str, Any]) -> ServerConfig:
    raw = {k: v for k, v in raw.items() if k not in _SERVER_OBSOLETE}
    unknown = set(raw) - set(_SERVER_FIELDS) - set(_SERVER_NETZE)
    if unknown:
        raise ConfigError(f"server: unbekannte Einstellung(en) {sorted(unknown)}")

    values = {k: v for k, v in raw.items() if v is not None}
    for key, value in values.items():
        if key in _SERVER_NETZE:
            if isinstance(value, tuple):  # schon aus der Umgebung gelesen
                continue
            if key == "trust_proxy" and value is True:
                raise ConfigError(f"server.{_TRUST_PROXY_TRUE}")
            values[key] = netze(value, f"server.{key}",
                                ganz_offen=key != "trust_proxy")
            continue
        _check_type(key, value, _SERVER_FIELDS[key])

    basic = values.get("basic_auth")
    if basic is not None and ":" not in basic:
        raise ConfigError("server.basic_auth muss die Form 'benutzer:passwort' haben")

    port = values.get("port", 8080)
    if not 0 <= port <= 65535:
        raise ConfigError(f"server.port muss zwischen 0 und 65535 liegen, ist {port}")

    return ServerConfig(**values)


_TRUE = ("1", "true", "yes", "ja")
_FALSE = ("0", "false", "no", "nein", "")


def _env_switch(name: str, value: str) -> bool:
    """Schalter aus der Umgebung -- nur eindeutige Werte, sonst kein Start."""
    word = value.strip().lower()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    raise ConfigError(
        f"{name} muss 1/true/yes oder 0/false/no sein, ist {value!r} "
        f"-- der Dienst startet nicht"
    )


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
        try:
            server_raw["port"] = int(env["PORT"])
        except ValueError as exc:
            raise ConfigError(f"PORT muss eine ganze Zahl sein, ist {env['PORT']!r}") from exc
    if "TOKEN" in env:
        server_raw["token"] = env["TOKEN"] or None
    if "BASIC_AUTH" in env:
        server_raw["basic_auth"] = env["BASIC_AUTH"] or None
    if "TRUST_PROXY" in env:
        wert = env["TRUST_PROXY"]
        if wert.strip().lower() in _TRUE:
            raise ConfigError(f"TRUST_PROXY: {_TRUST_PROXY_TRUE}")
        if wert.strip().lower() in _FALSE:
            server_raw["trust_proxy"] = ()
        else:
            server_raw["trust_proxy"] = netze(wert, "TRUST_PROXY", ganz_offen=False)
    if "ALLOW_FROM" in env:
        server_raw["allow_from"] = netze(env["ALLOW_FROM"], "ALLOW_FROM")
    if "ALLOW_ANONYMOUS" in env:
        server_raw["allow_anonymous"] = _env_switch("ALLOW_ANONYMOUS", env["ALLOW_ANONYMOUS"])
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
