"""Main scan loop: poll exchanges, compute spreads, log opportunities."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional

from db import Database, utc_now_iso

from .exchanges import ExchangeHub
from .spreads import SpreadOpportunity, compute_best_spread

logger = logging.getLogger(__name__)

OpportunityHandler = Callable[[SpreadOpportunity, int], None]


class ScannerLoop:
    """Background scanner that writes snapshots / opportunities to SQLite."""

    def __init__(
        self,
        config: dict[str, Any],
        db: Database,
        on_opportunity: Optional[OpportunityHandler] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.on_opportunity = on_opportunity
        rl = config.get("rate_limit", {})
        self.hub = ExchangeHub(
            exchange_ids=list(config["exchanges"]),
            max_retries=int(rl.get("max_retries", 3)),
            backoff_seconds=float(rl.get("backoff_seconds", 1.0)),
            cooldown_seconds=float(rl.get("cooldown_seconds", 30.0)),
        )
        self.poll_interval = float(config.get("poll_interval_seconds", 2))
        self.threshold = float(config.get("spread_threshold", 0.0015))
        self.withdrawal_cost = float(config.get("withdrawal_cost", 0.0005))
        self.taker_fees = dict(config["taker_fees"])
        self.pairs = list(config["pairs"])

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Track active gap keys so we can attach a stable gap_id while a gap persists
        self._active_gaps: dict[str, str] = {}
        self.latest: dict[str, Optional[SpreadOpportunity]] = {p: None for p in self.pairs}
        self.last_cycle_at: Optional[str] = None
        self.cycle_count = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scanner-loop", daemon=True)
        self._thread.start()
        logger.info(
            "Scanner started: pairs=%s exchanges=%s interval=%.1fs threshold=%.4f%%",
            self.pairs,
            self.config["exchanges"],
            self.poll_interval,
            self.threshold * 100,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def _gap_key(self, opp: SpreadOpportunity) -> str:
        return f"{opp.pair}|{opp.buy_exchange}|{opp.sell_exchange}"

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._cycle()
            except Exception:
                logger.exception("Scanner cycle failed")
            elapsed = time.monotonic() - started
            wait = max(0.0, self.poll_interval - elapsed)
            self._stop.wait(wait)

    def _cycle(self) -> None:
        books_by_pair = self.hub.fetch_all(self.pairs)
        seen_keys: set[str] = set()
        ts = utc_now_iso()

        for pair, books in books_by_pair.items():
            opp = compute_best_spread(books, self.taker_fees, self.withdrawal_cost)
            self.latest[pair] = opp

            snap = {
                "ts": ts,
                "pair": pair,
                "buy_exchange": opp.buy_exchange if opp else None,
                "sell_exchange": opp.sell_exchange if opp else None,
                # Persist as percent for readable SQLite / dashboard-only mode
                "gross_pct": (opp.gross_pct * 100) if opp else None,
                "net_pct": (opp.net_pct * 100) if opp else None,
                "bid_price": opp.bid_price if opp else None,
                "ask_price": opp.ask_price if opp else None,
            }
            try:
                self.db.insert_spread_snapshot(snap)
            except Exception:
                logger.exception("Failed to persist spread snapshot for %s", pair)

            if opp is None:
                continue

            key = self._gap_key(opp)
            if opp.net_pct >= self.threshold:
                seen_keys.add(key)
                if key not in self._active_gaps:
                    self._active_gaps[key] = str(uuid.uuid4())
                gap_id = self._active_gaps[key]

                row = {
                    "ts": ts,
                    "pair": opp.pair,
                    "buy_exchange": opp.buy_exchange,
                    "sell_exchange": opp.sell_exchange,
                    "gross_pct": opp.gross_pct * 100,  # store as percent
                    "net_pct": opp.net_pct * 100,
                    "bid_price": opp.bid_price,
                    "ask_price": opp.ask_price,
                    "bid_qty": opp.bid_qty,
                    "ask_qty": opp.ask_qty,
                    "gap_id": gap_id,
                }
                try:
                    opp_id = self.db.insert_opportunity(row)
                    logger.info(
                        "OPPORTUNITY %s buy=%s @ %.4f sell=%s @ %.4f gross=%.4f%% net=%.4f%%",
                        opp.pair,
                        opp.buy_exchange,
                        opp.ask_price,
                        opp.sell_exchange,
                        opp.bid_price,
                        opp.gross_pct * 100,
                        opp.net_pct * 100,
                    )
                    if self.on_opportunity:
                        self.on_opportunity(opp, opp_id)
                except Exception:
                    logger.exception("Failed to log opportunity for %s", pair)

        # Clear gaps that fell below threshold this cycle
        for key in list(self._active_gaps):
            if key not in seen_keys:
                del self._active_gaps[key]

        self.cycle_count += 1
        self.last_cycle_at = ts
        if self.cycle_count % 30 == 0:
            try:
                self.db.prune_old_snapshots(keep_hours=48)
            except Exception:
                logger.exception("Snapshot prune failed")
