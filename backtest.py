#!/usr/bin/env python3
"""Summarize last 24h of logged gaps: count, duration, fee-aware profitability.

Usage:
  python backtest.py
  python backtest.py --hours 24 --config config.yaml
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from db import Database
from scanner.config import load_config


def parse_ts(value: str) -> datetime:
    # Stored as ISO; tolerate Z suffix
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def cluster_gaps(rows: list[dict[str, Any]], poll_seconds: float) -> list[dict[str, Any]]:
    """
    Group opportunity rows into gap episodes.

    Prefer gap_id when present; otherwise cluster by (pair, buy, sell) with
    gaps between observations larger than 2× poll interval starting a new episode.
    """
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = r.get("gap_id") or f"{r['pair']}|{r['buy_exchange']}|{r['sell_exchange']}|ungrouped"
        by_key[str(key)].append(r)

    max_gap = timedelta(seconds=max(poll_seconds * 2.5, 5.0))
    episodes: list[dict[str, Any]] = []

    for key, items in by_key.items():
        items.sort(key=lambda x: x["ts"])
        current: list[dict[str, Any]] = []

        def flush(group: list[dict[str, Any]]) -> None:
            if not group:
                return
            start = parse_ts(group[0]["ts"])
            end = parse_ts(group[-1]["ts"])
            duration = max((end - start).total_seconds(), poll_seconds)
            nets = [float(g["net_pct"]) for g in group]
            grosses = [float(g["gross_pct"]) for g in group]
            # net_pct already fee-adjusted when logged; profitable if any observation > 0
            profitable = max(nets) > 0
            episodes.append(
                {
                    "gap_id": group[0].get("gap_id") or key,
                    "pair": group[0]["pair"],
                    "buy_exchange": group[0]["buy_exchange"],
                    "sell_exchange": group[0]["sell_exchange"],
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "duration_seconds": duration,
                    "observations": len(group),
                    "max_net_pct": max(nets),
                    "max_gross_pct": max(grosses),
                    "avg_net_pct": sum(nets) / len(nets),
                    "profitable_after_fees": profitable,
                }
            )

        for row in items:
            if not current:
                current = [row]
                continue
            prev = parse_ts(current[-1]["ts"])
            cur = parse_ts(row["ts"])
            # Ungrouped keys (no gap_id) need time-based splitting
            if row.get("gap_id"):
                current.append(row)
            elif cur - prev <= max_gap:
                current.append(row)
            else:
                flush(current)
                current = [row]
        flush(current)

    episodes.sort(key=lambda e: e["start"])
    return episodes


def summarize(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    if not episodes:
        return {
            "gap_count": 0,
            "profitable_count": 0,
            "avg_duration_seconds": 0.0,
            "median_duration_seconds": 0.0,
            "max_duration_seconds": 0.0,
            "by_pair": {},
        }

    durations = sorted(e["duration_seconds"] for e in episodes)
    mid = len(durations) // 2
    if len(durations) % 2:
        median = durations[mid]
    else:
        median = (durations[mid - 1] + durations[mid]) / 2

    by_pair: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"gaps": 0, "profitable": 0, "total_duration": 0.0}
    )
    for e in episodes:
        b = by_pair[e["pair"]]
        b["gaps"] += 1
        b["total_duration"] += e["duration_seconds"]
        if e["profitable_after_fees"]:
            b["profitable"] += 1

    return {
        "gap_count": len(episodes),
        "profitable_count": sum(1 for e in episodes if e["profitable_after_fees"]),
        "avg_duration_seconds": sum(durations) / len(durations),
        "median_duration_seconds": median,
        "max_duration_seconds": max(durations),
        "by_pair": dict(by_pair),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gap-scanner 24h gap report (paper data)")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    parser.add_argument("--limit", type=int, default=50, help="Max episode rows to print")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    db = Database(config["database"]["path"])
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    rows = db.opportunities_since(since.isoformat())
    poll = float(config.get("poll_interval_seconds", 2))
    episodes = cluster_gaps(rows, poll)
    stats = summarize(episodes)

    print("=" * 64)
    print(f"gap-scanner report — last {args.hours:g}h (paper / logged opportunities)")
    print("=" * 64)
    print(f"Opportunity ticks logged : {len(rows)}")
    print(f"Distinct gap episodes    : {stats['gap_count']}")
    print(f"Profitable after fees    : {stats['profitable_count']}")
    if stats["gap_count"]:
        print(f"Avg duration             : {stats['avg_duration_seconds']:.1f}s")
        print(f"Median duration          : {stats['median_duration_seconds']:.1f}s")
        print(f"Max duration             : {stats['max_duration_seconds']:.1f}s")
        print()
        print("By pair:")
        for pair, info in sorted(stats["by_pair"].items()):
            print(
                f"  {pair:10} gaps={info['gaps']:4d}  "
                f"profitable={info['profitable']:4d}  "
                f"total_open={info['total_duration']:.0f}s"
            )
        print()
        print(f"Recent episodes (up to {args.limit}):")
        print("-" * 64)
        for e in episodes[-args.limit :]:
            flag = "YES" if e["profitable_after_fees"] else "no"
            print(
                f"{e['start'][:19]}  {e['pair']:10}  "
                f"{e['buy_exchange']}->{e['sell_exchange']:10}  "
                f"dur={e['duration_seconds']:6.1f}s  "
                f"max_net={e['max_net_pct']:7.4f}%  profitable={flag}"
            )
    else:
        print()
        print("No opportunities in this window. Run the scanner first:")
        print("  python main.py")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
