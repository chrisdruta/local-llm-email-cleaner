"""Central stdlib-logging configuration for the CLI."""

from __future__ import annotations

import logging


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party chatter stays at WARNING unless --verbose.
    for noisy in ("googleapiclient", "google", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)
