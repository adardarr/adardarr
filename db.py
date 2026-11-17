"""SQLite persistence for opportunities, paper trades, and live spread snapshots."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thread-safe SQLite helper shared by scanner, paper engine, and dashboard."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    buy_exchange TEXT NOT NULL,
                    sell_exchange TEXT NOT NULL,
                    gross_pct REAL NOT NULL,
                    net_pct REAL NOT NULL,
                    bid_price REAL NOT NULL,
                    ask_price REAL NOT NULL,
                    bid_qty REAL,
                    ask_qty REAL,
                    gap_id TEXT
                );

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_open TEXT NOT NULL,
                    ts_close TEXT,
                    pair TEXT NOT NULL,
                    buy_exchange TEXT NOT NULL,
                    sell_exchange TEXT NOT NULL,
                    size_usd REAL NOT NULL,
                    buy_price REAL NOT NULL,
                    sell_price REAL NOT NULL,
                    fees_usd REAL NOT NULL,
                    slippage_usd REAL NOT NULL,
                    pnl_usd REAL,
                    status TEXT NOT NULL DEFAULT 'open',
                    opportunity_id INTEGER,
                    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
                );

                CREATE TABLE IF NOT EXISTS paper_balance (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance_usd REAL NOT NULL,
                    starting_balance_usd REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS spread_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    buy_exchange TEXT,
                    sell_exchange TEXT,
                    gross_pct REAL,
                    net_pct REAL,
                    bid_price REAL,
                    ask_price REAL
                );

                CREATE INDEX IF NOT EXISTS idx_opp_ts ON opportunities(ts);
                CREATE INDEX IF NOT EXISTS idx_opp_pair ON opportunities(pair);
                CREATE INDEX IF NOT EXISTS idx_snap_ts ON spread_snapshots(ts);
                CREATE INDEX IF NOT EXISTS idx_trades_ts ON paper_trades(ts_open);
                """
            )

    def ensure_paper_balance(self, starting: float) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM paper_balance WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO paper_balance (id, balance_usd, starting_balance_usd, updated_at)
                    VALUES (1, ?, ?, ?)
                    """,
                    (starting, starting, utc_now_iso()),
                )

    def insert_opportunity(self, data: dict[str, Any]) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO opportunities (
                    ts, pair, buy_exchange, sell_exchange, gross_pct, net_pct,
                    bid_price, ask_price, bid_qty, ask_qty, gap_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("ts", utc_now_iso()),
                    data["pair"],
                    data["buy_exchange"],
                    data["sell_exchange"],
                    data["gross_pct"],
                    data["net_pct"],
                    data["bid_price"],
                    data["ask_price"],
                    data.get("bid_qty"),
                    data.get("ask_qty"),
                    data.get("gap_id"),
                ),
            )
            return int(cur.lastrowid)

    def insert_spread_snapshot(self, data: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO spread_snapshots (
                    ts, pair, buy_exchange, sell_exchange, gross_pct, net_pct,
                    bid_price, ask_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("ts", utc_now_iso()),
                    data["pair"],
                    data.get("buy_exchange"),
                    data.get("sell_exchange"),
                    data.get("gross_pct"),
                    data.get("net_pct"),
                    data.get("bid_price"),
                    data.get("ask_price"),
                ),
            )

    def insert_paper_trade(self, data: dict[str, Any]) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO paper_trades (
                    ts_open, ts_close, pair, buy_exchange, sell_exchange,
                    size_usd, buy_price, sell_price, fees_usd, slippage_usd,
                    pnl_usd, status, opportunity_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("ts_open", utc_now_iso()),
                    data.get("ts_close"),
                    data["pair"],
                    data["buy_exchange"],
                    data["sell_exchange"],
                    data["size_usd"],
                    data["buy_price"],
                    data["sell_price"],
                    data["fees_usd"],
                    data["slippage_usd"],
                    data.get("pnl_usd"),
                    data.get("status", "closed"),
                    data.get("opportunity_id"),
                ),
            )
            return int(cur.lastrowid)

    def update_balance(self, new_balance: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE paper_balance SET balance_usd = ?, updated_at = ? WHERE id = 1
                """,
                (new_balance, utc_now_iso()),
            )

    def get_balance(self) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM paper_balance WHERE id = 1").fetchone()
            return dict(row) if row else None

    def recent_opportunities(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM opportunities ORDER BY ts DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def latest_spreads(self) -> list[dict[str, Any]]:
        """Most recent snapshot per pair."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.* FROM spread_snapshots s
                INNER JOIN (
                    SELECT pair, MAX(id) AS max_id FROM spread_snapshots GROUP BY pair
                ) t ON s.id = t.max_id
                ORDER BY s.pair
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def paper_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM paper_trades ORDER BY ts_open DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def paper_stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            bal = conn.execute("SELECT * FROM paper_balance WHERE id = 1").fetchone()
            closed = conn.execute(
                """
                SELECT
                    COUNT(*) AS n,
                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(pnl_usd), 0) AS total_pnl
                FROM paper_trades WHERE status = 'closed'
                """
            ).fetchone()
            equity = conn.execute(
                """
                SELECT ts_close AS ts, pnl_usd FROM paper_trades
                WHERE status = 'closed' AND ts_close IS NOT NULL
                ORDER BY ts_close ASC
                """
            ).fetchall()

            starting = float(bal["starting_balance_usd"]) if bal else 1000.0
            balance = float(bal["balance_usd"]) if bal else starting
            n = int(closed["n"] or 0)
            wins = int(closed["wins"] or 0)
            cumulative = []
            running = starting
            for row in equity:
                running += float(row["pnl_usd"] or 0)
                cumulative.append({"ts": row["ts"], "equity": running})

            return {
                "starting_balance_usd": starting,
                "balance_usd": balance,
                "total_pnl": float(closed["total_pnl"] or 0),
                "trades": n,
                "wins": wins,
                "losses": int(closed["losses"] or 0),
                "win_rate": (wins / n) if n else 0.0,
                "equity_curve": cumulative,
            }

    def opportunities_since(self, since_iso: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM opportunities WHERE ts >= ? ORDER BY ts ASC
                """,
                (since_iso,),
            ).fetchall()
            return [dict(r) for r in rows]

    def prune_old_snapshots(self, keep_hours: int = 48) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM spread_snapshots
                WHERE ts < datetime('now', ?)
                """,
                (f"-{keep_hours} hours",),
            )
