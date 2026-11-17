#!/usr/bin/env python3
"""gap-scanner entrypoint — public market data + paper trading only.

TODO: Live trading is out of scope. This process never places real orders,
never loads trade-permission API keys, and must not import any live-execution
module. A future live trader would require a separate opt-in package.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

import uvicorn

from dashboard.app import create_app
from db import Database
from paper.engine import PaperEngine
from scanner.config import load_config
from scanner.loop import ScannerLoop


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy HTTP libs a bit
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("ccxt.base.exchange").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gap-scanner (paper trading only)")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--host", default=None, help="Dashboard bind host")
    parser.add_argument("--port", type=int, default=None, help="Dashboard bind port")
    parser.add_argument("--scanner-only", action="store_true", help="Run scanner without dashboard")
    parser.add_argument("--dashboard-only", action="store_true", help="Serve dashboard without scanner")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    log = logging.getLogger("main")

    config = load_config(args.config)
    db = Database(config["database"]["path"])

    paper = PaperEngine(config, db)
    scanner: ScannerLoop | None = None

    if not args.dashboard_only:
        scanner = ScannerLoop(config, db, on_opportunity=paper.on_opportunity)
        scanner.start()

    if args.scanner_only:
        log.info("Running scanner only (Ctrl+C to stop)")
        stop = False

        def _stop(signum, frame) -> None:  # noqa: ANN001
            nonlocal stop
            log.info("Signal %s received — shutting down", signum)
            stop = True
            if scanner:
                scanner.stop()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        while not stop:
            if hasattr(signal, "pause"):
                signal.pause()
            else:
                time.sleep(1)
        return 0

    app = create_app(db=db, scanner=scanner, config=config)
    host = args.host or config.get("dashboard", {}).get("host", "127.0.0.1")
    port = args.port or int(config.get("dashboard", {}).get("port", 8000))
    log.info("Dashboard at http://%s:%s  (paper mode)", host, port)

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if scanner:
            scanner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
