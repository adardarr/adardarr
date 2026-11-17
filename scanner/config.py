"""Load and validate config.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    required = ["pairs", "exchanges", "taker_fees", "spread_threshold", "paper", "database"]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"config.yaml missing keys: {missing}")

    for ex in data["exchanges"]:
        if ex not in data["taker_fees"]:
            raise ValueError(f"taker_fees missing entry for exchange '{ex}'")

    # Resolve relative DB path against project root
    db_path = Path(data["database"]["path"])
    if not db_path.is_absolute():
        data["database"]["path"] = str(ROOT / db_path)

    return data
