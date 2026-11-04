"""Entry point for the credential-free S0 service."""

import logging

from aiohttp import web

from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.web.server import create_app


def main() -> None:
    """Load configuration and start the local HTTP service on port 8765."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    journal = Journal(retention_days=settings.log_retention_days)
    journal.write("state", service="starting")
    web.run_app(create_app(), host="0.0.0.0", port=8765)


if __name__ == "__main__":
    main()
