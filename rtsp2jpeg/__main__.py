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
from .server import serve_forever

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
    parser.add_argument("--version", action="version", version=f"rtsp2jpeg {__version__}")
    return parser.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.server.log_level)

    if args.check:
        print(f"Konfiguration in Ordnung: {len(config.cameras)} Kamera(s)")
        for camera in config.cameras.values():
            print(f"  /cam/{camera.name}.jpg  <- {camera.redacted_url}")
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
