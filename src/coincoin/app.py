"""Entry point for the browser audio service."""

import argparse
import logging
import os

from aiohttp import web

from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.web.server import create_app


def main(argv: list[str] | None = None) -> None:
    """Load configuration and start the local HTTP/audio service on port 8765."""
    parser = argparse.ArgumentParser(prog="coincoin")
    parser.add_argument("--echo", action="store_true", help="echo microphone audio to playback")
    parser.add_argument(
        "--tone", action="store_true", help="play a 1 kHz tone and measure round-trip latency"
    )
    args = parser.parse_args(argv)
    echo = args.echo or os.environ.get("ECHO", "").strip().lower() in {"1", "true", "yes", "on"}
    if echo and args.tone:
        parser.error("--echo and --tone are mutually exclusive")

    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    journal = Journal(retention_days=settings.log_retention_days)
    journal.write("state", service="starting")
    web.run_app(
        create_app(echo=echo, tone=args.tone, journal=journal),
        host="0.0.0.0",
        port=8765,
    )


if __name__ == "__main__":
    main()
