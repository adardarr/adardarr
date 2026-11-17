"""Public-only ccxt exchange clients with rate-limit / error handling.

TODO: Live trading is out of scope. This module must never call create_order,
create_market_order, or any authenticated trading endpoint. Only public
fetch_ticker / fetch_order_book style reads are permitted.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

import ccxt

logger = logging.getLogger(__name__)

# Map config ids → ccxt class names / constructor kwargs (public endpoints only).
EXCHANGE_FACTORIES: dict[str, tuple[type, dict[str, Any]]] = {
    "binanceus": (ccxt.binanceus, {"enableRateLimit": True}),
    "coinbase": (ccxt.coinbase, {"enableRateLimit": True}),
    "kraken": (ccxt.kraken, {"enableRateLimit": True}),
    "okx": (ccxt.okx, {"enableRateLimit": True}),
}


@dataclass
class TopOfBook:
    exchange: str
    pair: str
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    ts: float


@dataclass
class ExchangeState:
    client: Any
    cooldown_until: float = 0.0
    consecutive_errors: int = 0
    last_error: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class ExchangeHub:
    """Fetches best bid/ask from multiple public exchange endpoints."""

    exchange_ids: list[str]
    max_retries: int = 3
    backoff_seconds: float = 1.0
    cooldown_seconds: float = 30.0
    states: dict[str, ExchangeState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for eid in self.exchange_ids:
            if eid not in EXCHANGE_FACTORIES:
                raise ValueError(f"Unsupported exchange: {eid}")
            cls, kwargs = EXCHANGE_FACTORIES[eid]
            # Explicitly no API keys — public market data only.
            client = cls({**kwargs})
            # Defense in depth: refuse if credentials somehow appear.
            if getattr(client, "apiKey", None) or getattr(client, "secret", None):
                raise RuntimeError(
                    f"Refusing to run {eid} with API credentials. "
                    "gap-scanner is paper-trading only."
                )
            self.states[eid] = ExchangeState(client=client)

    def _in_cooldown(self, eid: str) -> bool:
        with self.states[eid].lock:
            return time.time() < self.states[eid].cooldown_until

    def _record_success(self, eid: str) -> None:
        with self.states[eid].lock:
            st = self.states[eid]
            st.consecutive_errors = 0
            st.last_error = None

    def _record_error(self, eid: str, err: Exception) -> None:
        with self.states[eid].lock:
            st = self.states[eid]
            st.consecutive_errors += 1
            st.last_error = str(err)
            logger.warning("%s fetch error (%s): %s", eid, st.consecutive_errors, err)
            if st.consecutive_errors >= self.max_retries:
                st.cooldown_until = time.time() + self.cooldown_seconds
                st.consecutive_errors = 0
                logger.error(
                    "%s entering cooldown for %.0fs after repeated failures",
                    eid,
                    self.cooldown_seconds,
                )

    def fetch_top_of_book(self, eid: str, pair: str) -> Optional[TopOfBook]:
        """Fetch best bid/ask. Prefer ticker; fall back to order book depth 1."""
        if self._in_cooldown(eid):
            return None

        st = self.states[eid]
        client = st.client
        last_err: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                # Public ticker — no auth
                ticker = client.fetch_ticker(pair)
                bid = ticker.get("bid")
                ask = ticker.get("ask")
                bid_qty = float(ticker.get("bidVolume") or 0) or 0.0
                ask_qty = float(ticker.get("askVolume") or 0) or 0.0

                # Some venues omit qty on ticker — enrich from top of book
                if bid is None or ask is None or (bid_qty == 0 and ask_qty == 0):
                    book = client.fetch_order_book(pair, limit=1)
                    if bid is None and book.get("bids"):
                        bid = book["bids"][0][0]
                        bid_qty = float(book["bids"][0][1])
                    if ask is None and book.get("asks"):
                        ask = book["asks"][0][0]
                        ask_qty = float(book["asks"][0][1])
                    if bid_qty == 0 and book.get("bids"):
                        bid_qty = float(book["bids"][0][1])
                    if ask_qty == 0 and book.get("asks"):
                        ask_qty = float(book["asks"][0][1])

                if bid is None or ask is None or float(bid) <= 0 or float(ask) <= 0:
                    raise ValueError(f"incomplete book for {pair}: bid={bid} ask={ask}")

                self._record_success(eid)
                return TopOfBook(
                    exchange=eid,
                    pair=pair,
                    bid=float(bid),
                    ask=float(ask),
                    bid_qty=float(bid_qty),
                    ask_qty=float(ask_qty),
                    ts=time.time(),
                )
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
                last_err = e
                time.sleep(self.backoff_seconds * (attempt + 1))
            except ccxt.RateLimitExceeded as e:
                last_err = e
                time.sleep(self.backoff_seconds * (2 ** attempt))
            except ccxt.BadSymbol as e:
                # Pair not listed on this venue — don't burn cooldown aggressively
                logger.debug("%s does not list %s: %s", eid, pair, e)
                with st.lock:
                    st.last_error = str(e)
                return None
            except Exception as e:
                last_err = e
                break

        if last_err is not None:
            self._record_error(eid, last_err)
        return None

    def fetch_all(self, pairs: list[str]) -> dict[str, dict[str, TopOfBook]]:
        """Return {pair: {exchange: TopOfBook}} for all reachable books.

        Fetches exchange/pair combinations in parallel so the scan loop can
        stay close to the configured poll interval despite network latency.
        """
        out: dict[str, dict[str, TopOfBook]] = {p: {} for p in pairs}
        jobs = [(eid, pair) for pair in pairs for eid in self.exchange_ids]
        workers = min(8, max(1, len(jobs)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tob") as pool:
            futures = {
                pool.submit(self.fetch_top_of_book, eid, pair): (eid, pair)
                for eid, pair in jobs
            }
            for fut in as_completed(futures):
                eid, pair = futures[fut]
                try:
                    tob = fut.result()
                except Exception as e:
                    logger.warning("parallel fetch failed %s %s: %s", eid, pair, e)
                    continue
                if tob is not None:
                    out[pair][eid] = tob
        return out
