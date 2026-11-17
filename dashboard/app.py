"""FastAPI dashboard for live spreads, opportunities, and paper P&L."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import Database
from scanner.loop import ScannerLoop

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(
    db: Database,
    scanner: Optional[ScannerLoop] = None,
    config: Optional[dict[str, Any]] = None,
) -> FastAPI:
    app = FastAPI(title="gap-scanner", description="Paper-trading crypto price-gap scanner")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    refresh = 2
    if config:
        refresh = int(config.get("dashboard", {}).get("refresh_seconds", 2))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        context = {
            "refresh_seconds": refresh,
            "threshold_pct": float(config.get("spread_threshold", 0.0015)) * 100
            if config
            else 0.15,
        }
        # Starlette >=0.37: TemplateResponse(request, name, context)
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "mode": "paper",
                "scanner_cycles": scanner.cycle_count if scanner else 0,
                "last_cycle_at": scanner.last_cycle_at if scanner else None,
            }
        )

    @app.get("/api/spreads")
    async def spreads() -> JSONResponse:
        rows = db.latest_spreads()
        # Enrich with in-memory latest if scanner is running
        if scanner:
            live = []
            for pair, opp in scanner.latest.items():
                if opp is None:
                    live.append(
                        {
                            "pair": pair,
                            "buy_exchange": None,
                            "sell_exchange": None,
                            "gross_pct": None,
                            "net_pct": None,
                            "bid_price": None,
                            "ask_price": None,
                            "source": "live",
                        }
                    )
                else:
                    live.append(
                        {
                            "pair": pair,
                            "buy_exchange": opp.buy_exchange,
                            "sell_exchange": opp.sell_exchange,
                            "gross_pct": opp.gross_pct * 100,
                            "net_pct": opp.net_pct * 100,
                            "bid_price": opp.bid_price,
                            "ask_price": opp.ask_price,
                            "bid_qty": opp.bid_qty,
                            "ask_qty": opp.ask_qty,
                            "source": "live",
                        }
                    )
            return JSONResponse({"spreads": live, "persisted": rows})
        return JSONResponse({"spreads": rows, "persisted": rows})

    @app.get("/api/opportunities")
    async def opportunities(limit: int = 50) -> JSONResponse:
        return JSONResponse({"opportunities": db.recent_opportunities(limit=limit)})

    @app.get("/api/paper")
    async def paper() -> JSONResponse:
        stats = db.paper_stats()
        trades = db.paper_trades(limit=100)
        return JSONResponse({"stats": stats, "trades": trades})

    return app
