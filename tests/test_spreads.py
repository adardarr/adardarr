"""Unit tests for fee-aware spread math (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scanner.exchanges import TopOfBook
from scanner.spreads import compute_best_spread


def _book(ex: str, pair: str, bid: float, ask: float) -> TopOfBook:
    return TopOfBook(
        exchange=ex,
        pair=pair,
        bid=bid,
        ask=ask,
        bid_qty=1.0,
        ask_qty=1.0,
        ts=0.0,
    )


def test_cross_exchange_net_spread() -> None:
    books = {
        "binanceus": _book("binanceus", "BTC/USDT", bid=100.0, ask=100.2),
        "okx": _book("okx", "BTC/USDT", bid=101.0, ask=101.2),
    }
    fees = {"binanceus": 0.001, "okx": 0.001}
    # Buy binanceus ask 100.2, sell okx bid 101.0
    # gross = (101 - 100.2) / 100.2 ≈ 0.007984
    # net = gross - 0.001 - 0.001 - 0.0005 ≈ 0.005484
    opp = compute_best_spread(books, fees, withdrawal_cost=0.0005)
    assert opp is not None
    assert opp.buy_exchange == "binanceus"
    assert opp.sell_exchange == "okx"
    assert abs(opp.gross_pct - ((101.0 - 100.2) / 100.2)) < 1e-9
    assert abs(opp.net_pct - (opp.gross_pct - 0.0025)) < 1e-9


def test_ignores_same_exchange() -> None:
    books = {"kraken": _book("kraken", "ETH/USDT", bid=50.0, ask=40.0)}
    assert compute_best_spread(books, {"kraken": 0.001}, 0.0005) is None


def test_needs_two_venues() -> None:
    assert compute_best_spread({}, {}, 0.0) is None


if __name__ == "__main__":
    test_cross_exchange_net_spread()
    test_ignores_same_exchange()
    test_needs_two_venues()
    print("ok")
