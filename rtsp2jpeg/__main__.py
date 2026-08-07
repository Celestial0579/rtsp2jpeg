"""Startpunkt: Konfiguration laden, Greifer aufsetzen, HTTP-Dienst starten."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from . import __version__
from .config import ConfigError, load_config
from .grabber import GrabberPool, find_ffmpeg
from .server import fehlt_der_zugangsschutz, serve_forever, token_erzeugen

log = logging.getLogger("rtsp2jpeg")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rtsp2jpeg",
        description="Stellt RTSP-Kameras als JPEG-Einzelbild ueber HTTP bereit "
                    "-- z. B. fuer das Feld 'Kamera-URL' einer "
                    "STARFACE-Tuersprechstelle.",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("CONFIG_FILE"),
        help="Pfad zur YAML-Konfiguration (Vorgabe: $CONFIG_FILE, sonst keine)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Konfiguration pruefen und beenden, ohne den Dienst zu starten",
    )
    parser.add_argument(
        "--token-erzeugen", action="store_true", dest="token_erzeugen",
        help="Ein neues Zugangstoken erzeugen, ausgeben und beenden",
    )
    parser.add_argument(
        "--host", default="tuerkamera.example.de",
        help="Nur mit --token-erzeugen: Name fuer die Beispiel-URL",
    )
    parser.add_argument(
        "--kamera", default="tuer",
        help="Nur mit --token-erzeugen: Kameraname fuer die Beispiel-URL",
    )
    parser.add_argument("--version", action="version", version=f"rtsp2jpeg {__version__}")
    return parser.parse_args(argv)


def zeige_neues_token(host: str, kamera: str) -> int:
    """Gibt ein frisches Token samt fertiger STARFACE-URL aus."""
    token = token_erzeugen()
    print(token)
    print()
    print("So wird es eingesetzt:")
    print()
    print("  1. Beim Start des Containers hinterlegen:")
    print(f"       -e TOKEN={token}")
    print("     oder in der Konfigurationsdatei unter server.token")
    print()
    print("  2. In der STARFACE als Kamera-URL eintragen")
    print("     (Telefonkonto der Tuersprechstelle, Erweiterte Einstellungen):")
    print()
    print(f"       https://{host}/cam/{kamera}.jpg?token={token}")
    print()
    print("Das Token steht damit in der URL -- STARFACE bietet kein eigenes")
    print("Feld fuer Benutzer und Passwort an. Wer die URL kennt, sieht das")
    print("Bild: nur ueber HTTPS uebertragen und nicht weitergeben.")
    return 0


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Braucht weder Konfiguration noch Kamera -- man erzeugt das Token ja,
    # bevor man es eintraegt.
    if args.token_erzeugen:
        return zeige_neues_token(args.host, args.kamera)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.server.log_level)

    grund = fehlt_der_zugangsschutz(config)
    if grund and not args.check:
        print(grund, file=sys.stderr)
        return 3

    if args.check:
        print(f"Konfiguration in Ordnung: {len(config.cameras)} Kamera(s)")
        for camera in config.cameras.values():
            print(f"  /cam/{camera.name}.jpg  <- {camera.redacted_url}")
        if grund:
            print()
            print(grund, file=sys.stderr)
            return 3
        return 0

    ffmpeg = find_ffmpeg()
    log.info("rtsp2jpeg %s, ffmpeg: %s", __version__, ffmpeg)

    pool = GrabberPool(config.cameras)
    pool.start()

    stop = threading.Event()

    def shutdown(signum: int, _frame: object) -> None:
        log.info("Signal %s erhalten, fahre herunter", signal.Signals(signum).name)
        stop.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        serve_forever(config, pool)
    except KeyboardInterrupt:
        pass
    finally:
        pool.stop()
        log.info("Beendet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
