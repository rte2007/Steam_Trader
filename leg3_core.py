"""Pure selection and pricing rules for CS.TRADE -> Market CS2."""
from __future__ import annotations

import re
from datetime import datetime, timezone


WEARS = {
    "Factory New": "FN", "Minimal Wear": "MW", "Field-Tested": "FT",
    "Well-Worn": "WW", "Battle-Scarred": "BS",
}


def wear_of(name: str) -> str | None:
    match = re.search(r"\((Factory New|Minimal Wear|Field-Tested|Well-Worn|Battle-Scarred)\)$", name)
    return WEARS.get(match.group(1)) if match else None


def is_regular_weapon_name(name: str) -> bool:
    return bool(name and not name.startswith(("StatTrak™ ", "Souvenir ")) and wear_of(name))


def exposure_key(name: str) -> str:
    """One position per exact weapon skin and wear grade."""
    return name.strip().casefold()


def listing_price(reference_price: float, live_price: float,
                  buy_price: float, market_fee_pct: float = 0.0) -> float:
    """Never list below the pre-buy quote or conservative break-even."""
    fee = max(0.0, min(float(market_fee_pct), 99.0)) / 100.0
    break_even = buy_price / (1.0 - fee)
    live_undercut = max(0.0, live_price - 0.01)
    return round(max(reference_price, live_undercut, break_even), 2)


def order_exit_price(reference_order: float, live_highest_order: float,
                     buy_price: float) -> float:
    """Use the highest live order, but never fall below the pre-buy quote/break-even."""
    return round(max(reference_order, live_highest_order, buy_price), 3)


def profit_pct(buy: float, sell: float, market_fee_pct: float = 0.0) -> float:
    if buy <= 0:
        return -100.0
    net = sell * (1.0 - market_fee_pct / 100.0)
    return (net - buy) / buy * 100.0


def iso_timestamp(value: object) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def dmarket_unlock_at(attributes: dict, now: int) -> int:
    """Prefer DMarket's exact unlockDate; fall back to its remaining duration."""
    if attributes.get("withdrawable") is True and attributes.get("tradable") is True:
        return now
    exact = iso_timestamp(attributes.get("unlockDate"))
    if exact:
        return max(now, exact)
    duration = str(attributes.get("tradeLockDuration") or "").rstrip("s")
    try:
        return now + max(0, int(float(duration)))
    except ValueError:
        return now + max(0, int(attributes.get("tradeLockDays") or 0)) * 86400


def steam_unlock_at(accepted_at: int, explicit_unlock: int | None = None) -> int:
    """Use an explicit Steam timestamp, otherwise the safe CS2 eight-day window."""
    return max(accepted_at, int(explicit_unlock)) if explicit_unlock else accepted_at + 8 * 86400


def choose_cs2_dmarket_offer(offers: list[dict], table_price: float,
                             max_slippage_pct: float) -> dict | None:
    """Choose the cheapest exact CS2 asset, including a disclosed locked asset."""
    valid = []
    for offer in offers:
        attrs = offer.get("attributes") or {}
        cents = int(offer.get("priceCents") or 0)
        if cents > 0 and offer.get("offerId") and attrs.get("id") and attrs.get("classId"):
            valid.append(offer)
    if not valid:
        return None
    best = min(valid, key=lambda row: int(row.get("priceCents") or 0))
    if table_price > 0 and int(best["priceCents"]) / 100 > table_price * (1 + max_slippage_pct / 100):
        return None
    return best
