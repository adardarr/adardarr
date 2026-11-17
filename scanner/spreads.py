"""Cross-exchange spread math with fee and withdrawal adjustments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .exchanges import TopOfBook


@dataclass
class SpreadOpportunity:
    pair: str
    buy_exchange: str
    sell_exchange: str
    ask_price: float  # buy at (lowest ask)
    bid_price: float  # sell at (highest bid)
    ask_qty: float
    bid_qty: float
    gross_pct: float  # (bid - ask) / ask
    net_pct: float  # gross minus taker fees and withdrawal cost
    buy_fee: float
    sell_fee: float
    withdrawal_cost: float


def compute_best_spread(
    books: dict[str, TopOfBook],
    taker_fees: dict[str, float],
    withdrawal_cost: float,
) -> Optional[SpreadOpportunity]:
    """
    For a single pair's exchange books, find the max net cross-exchange spread:
        gross = (highest_bid - lowest_ask) / lowest_ask
        net   = gross - buy_taker_fee - sell_taker_fee - withdrawal_cost

    Same-exchange quotes are ignored (not a cross-exchange gap).
    """
    if len(books) < 2:
        return None

    best: Optional[SpreadOpportunity] = None

    for buy_ex, buy_book in books.items():
        for sell_ex, sell_book in books.items():
            if buy_ex == sell_ex:
                continue
            ask = buy_book.ask
            bid = sell_book.bid
            if ask <= 0 or bid <= 0:
                continue
            gross = (bid - ask) / ask
            buy_fee = float(taker_fees.get(buy_ex, 0.0))
            sell_fee = float(taker_fees.get(sell_ex, 0.0))
            net = gross - buy_fee - sell_fee - withdrawal_cost

            cand = SpreadOpportunity(
                pair=buy_book.pair,
                buy_exchange=buy_ex,
                sell_exchange=sell_ex,
                ask_price=ask,
                bid_price=bid,
                ask_qty=buy_book.ask_qty,
                bid_qty=sell_book.bid_qty,
                gross_pct=gross,
                net_pct=net,
                buy_fee=buy_fee,
                sell_fee=sell_fee,
                withdrawal_cost=withdrawal_cost,
            )
            if best is None or cand.net_pct > best.net_pct:
                best = cand

    return best
