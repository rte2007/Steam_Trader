"""Pure candidate selection for Rust LIS-SKINS -> DMarket buy orders."""
from __future__ import annotations

import math
import time


def build_skins_table_snapshot(
    ls_rows: dict,
    order_rows: dict,
    *,
    min_buy_usd: float,
    max_buy_usd: float,
    min_orders: int,
    min_profit_pct: float,
    dmarket_fee: float,
    max_age_sec: int,
    now: float | None = None,
) -> tuple[set[str], dict[str, dict]]:
    """Build a prefilter from the exact skins-table ``DMARKET ORDER`` side."""
    now = time.time() if now is None else now
    names: set[str] = set()
    dm_prices: dict[str, dict] = {}
    for name, ls_row in (ls_rows or {}).items():
        order_row = (order_rows or {}).get(name)
        if not isinstance(ls_row, dict) or not isinstance(order_row, dict):
            continue
        try:
            buy_price = float(ls_row.get("p") or 0)
            order_price = float(order_row.get("p") or 0)
            order_count = int(order_row.get("c") or order_row.get("m") or 0)
        except (TypeError, ValueError):
            continue
        if buy_price <= 0 or order_price <= 0 or order_count < min_orders:
            continue
        if buy_price < min_buy_usd or buy_price > max_buy_usd:
            continue
        timestamps = [row.get("t") for row in (ls_row, order_row) if row.get("t")]
        stale = False
        for raw_ts in timestamps:
            try:
                ts = float(raw_ts)
                if ts > 10_000_000_000:
                    ts /= 1000.0
                stale = stale or now - ts > max_age_sec
            except (TypeError, ValueError):
                stale = True
        if stale:
            continue
        order_cents = int(round(order_price * 100))
        fee_cents = int(math.ceil(order_cents * dmarket_fee - 1e-12))
        net = (order_cents - fee_cents) / 100
        if (net - buy_price) / buy_price * 100 < min_profit_pct:
            continue
        names.add(name)
        dm_prices[name] = {
            "order": order_price,
            "order_cnt": order_count,
            "offer": 0.0,
            "offer_cnt": 0,
        }
    return names, dm_prices


def register_live_confirmation(checks: dict, name: str, live_profit_pct: float,
                               *, minimum_profit_pct: float, required: int,
                               window_sec: int, now: float | None = None) -> bool:
    """Require repeated live checks before allowing a purchase."""
    now = time.time() if now is None else now
    if live_profit_pct < minimum_profit_pct:
        checks.pop(name, None)
        return False
    previous = checks.get(name) or {}
    count = int(previous.get("count", 0)) + 1 if now - float(previous.get("ts", 0)) <= window_sec else 1
    checks[name] = {"count": count, "ts": now, "profit_pct": round(live_profit_pct, 3)}
    return count >= max(1, required)
