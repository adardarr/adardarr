# gap-scanner

Cross-exchange crypto **price-gap scanner** with **paper trading only**.

Polls public best bid/ask for `BTC/USDT`, `ETH/USDT`, and `SOL/USDT` on **Binance.US**, **Coinbase**, **Kraken**, and **OKX** via [`ccxt`](https://github.com/ccxt/ccxt), computes fee-adjusted spreads, logs opportunities to SQLite, simulates fills, and serves a small local dashboard.

> **TODO: Live trading is out of scope.**  
> This project never places real orders and must not use API keys with trade permission.  
> Do not add `create_order` / live-execution code here.

## Features

1. **Live scan** every 2s (configurable) of top-of-book across four venues  
2. **Net spread** = gross cross-exchange spread − taker fees − estimated withdrawal cost  
3. **Opportunity log** in SQLite when net spread ≥ threshold (default **0.15%**)  
4. **Paper engine** starting at **$1,000**, fills at quote ± **0.05%** slippage  
5. **Dashboard** (FastAPI + single HTML page, auto-refresh)  
6. **24h report** script: gap count, duration, profitable-after-fees count  

## Project layout

```
gap-scanner/
├── config.yaml          # pairs, fees, thresholds, paper settings
├── main.py              # run scanner + dashboard
├── backtest.py          # last-24h gap summary report
├── db.py                # SQLite helpers
├── requirements.txt
├── scanner/             # public market-data polling + spread math
├── paper/               # simulated fills only
├── dashboard/           # FastAPI app + HTML template
└── data/                # created at runtime (SQLite DB)
```

## Setup

```bash
cd gap-scanner
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

No exchange API keys are required (public endpoints only).

## Run

Start scanner + dashboard:

```bash
python main.py
```

Open **http://127.0.0.1:8000**

Useful flags:

```bash
python main.py --scanner-only      # log opportunities / paper fills, no UI
python main.py --dashboard-only    # UI against existing SQLite data
python main.py -v --port 8080
```

## 24h report

After the scanner has been running:

```bash
python backtest.py
python backtest.py --hours 24 --limit 100
```

## Configuration

Edit `config.yaml`:

| Key | Meaning |
|-----|---------|
| `spread_threshold` | Min **net** spread to log (default `0.0015` = 0.15%) |
| `withdrawal_cost` | Estimated transfer cost as fraction of notional |
| `taker_fees.*` | Per-exchange taker fee fractions |
| `paper.starting_balance_usd` | Fake starting cash (default `1000`) |
| `paper.slippage` | Simulated fill slippage (default `0.0005` = 0.05%) |
| `paper.trade_size_usd` | Notional per paper arb |
| `poll_interval_seconds` | Ticker poll cadence (default `2`) |

## Spread formula

For each pair, across exchanges:

```
gross = (highest_bid − lowest_ask) / lowest_ask
net   = gross − fee_buy − fee_sell − withdrawal_cost
```

Same-exchange quotes are ignored. Rate limits / network errors put a venue in a short cooldown instead of crashing the loop.

## Safety

- No order placement code exists in this repo.  
- Exchange clients are constructed **without** API keys.  
- Startup refuses clients that somehow have credentials set.  
- Dashboard footer and module docstrings restate: **paper only**.

## License

MIT — use at your own risk. This is a research / educational paper simulator, not financial advice.
