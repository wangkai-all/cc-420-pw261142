"""Small shared helpers: stable JSON hashing, canonical serialisation, actions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

ACTIONS = ["fix_upstream", "reprocess_affected", "recalc_report", "modify_rule", "quantify_impact"]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def short_hash(value: Any, n: int = 8) -> str:
    return digest(value)[:n]


def pct(part: float, total: float) -> float:
    if not total:
        return 0.0
    return round(100.0 * part / total, 2)


def as_float(value: Any, default: float | None = None) -> float | None:
    """Best-effort numeric coercion used by rule evaluators on dirty inputs."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))
