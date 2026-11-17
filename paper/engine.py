"""Paper-trading engine — simulates arb fills. Never places real orders.

TODO: Live trading is out of scope. Do not wire API keys with trade permission.
Do not call exchange.create_order / create_market_order / create_limit_order.
Any future live-trading module must live in a separate package with explicit
opt-in and must never be imported by default from this project.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from db import Database, utc_now_iso
from scanner.spreads import SpreadOpportunity

logger = logging.getLogger(__name__)


class PaperEngine:
    """Simulates round-trip arb fills at quoted prices minus slippage."""

    def __init__(self, config: dict[str, Any], db: Database) -> None:
        paper = config["paper"]
        self.db = db
        self.starting = float(paper["starting_balance_usd"])
        self.slippage = float(paper.get("slippage", 0.0005))
        self.trade_size = float(paper.get("trade_size_usd", 100.0))
        self.max_open = int(paper.get("max_open_positions", 3))
        self.taker_fees = dict(config["taker_fees"])
        self.withdrawal_cost = float(config.get("withdrawal_cost", 0.0005))
        self._lock = threading.Lock()
        self._open_count = 0

        self.db.ensure_paper_balance(self.starting)
        bal = self.db.get_balance()
        self.balance = float(bal["balance_usd"]) if bal else self.starting
        logger.info(
            "Paper engine ready: balance=$%.2f size=$%.2f slippage=%.4f%%",
            self.balance,
            self.trade_size,
            self.slippage * 100,
        )

    def on_opportunity(self, opp: SpreadOpportunity, opportunity_id: int) -> None:
        """Callback from scanner — attempt a simulated fill."""
        with self._lock:
            if self._open_count >= self.max_open:
                logger.debug("Skip paper trade: max open positions reached")
                return
            if self.balance < self.trade_size:
                logger.debug("Skip paper trade: insufficient paper balance")
                return
            self._open_count += 1
            try:
                self._simulate_fill(opp, opportunity_id)
            finally:
                self._open_count -= 1

    def _simulate_fill(self, opp: SpreadOpportunity, opportunity_id: int) -> None:
        # Buy side: pay ask * (1 + slippage)
        buy_price = opp.ask_price * (1.0 + self.slippage)
        # Sell side: receive bid * (1 - slippage)
        sell_price = opp.bid_price * (1.0 - self.slippage)

        size_usd = self.trade_size
        qty = size_usd / buy_price

        buy_fee_rate = float(self.taker_fees.get(opp.buy_exchange, 0.0))
        sell_fee_rate = float(self.taker_fees.get(opp.sell_exchange, 0.0))

        buy_notional = qty * buy_price
        sell_notional = qty * sell_price
        fees_usd = buy_notional * buy_fee_rate + sell_notional * sell_fee_rate
        withdrawal_usd = buy_notional * self.withdrawal_cost
        slippage_usd = abs(qty * opp.ask_price * self.slippage) + abs(
            qty * opp.bid_price * self.slippage
        )

        # Round-trip P&L: sell proceeds - buy cost - fees - withdrawal
        pnl = sell_notional - buy_notional - fees_usd - withdrawal_usd

        ts = utc_now_iso()
        trade = {
            "ts_open": ts,
            "ts_close": ts,  # instantaneous simulated arb
            "pair": opp.pair,
            "buy_exchange": opp.buy_exchange,
            "sell_exchange": opp.sell_exchange,
            "size_usd": size_usd,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "fees_usd": fees_usd + withdrawal_usd,
            "slippage_usd": slippage_usd,
            "pnl_usd": pnl,
            "status": "closed",
            "opportunity_id": opportunity_id,
        }
        self.db.insert_paper_trade(trade)
        self.balance += pnl
        self.db.update_balance(self.balance)

        logger.info(
            "PAPER FILL %s buy=%s sell=%s size=$%.2f pnl=$%+.4f balance=$%.2f",
            opp.pair,
            opp.buy_exchange,
            opp.sell_exchange,
            size_usd,
            pnl,
            self.balance,
        )
