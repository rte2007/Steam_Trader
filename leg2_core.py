"""Pure decision logic for the Rust DMarket -> CS.TRADE leg."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Limits:
    min_profit_pct: float = 10.0
    min_buy_usd: float = 0.30
    max_buy_usd: float = 10.00
    max_snapshot_age_sec: int = 24 * 3600
    deposit_safety_pct: float = 0.0
    fixed_cost_cents: int = 0


def cents(value: Any) -> int:
    """Convert a dollar value to cents without optimistic float rounding."""
    return int(math.floor(float(value) * 100 + 1e-9))


def projected_net_cents(deposit_usd: float, limits: Limits) -> int:
    """Conservative projected CS.TRADE credit after a configurable haircut."""
    gross = cents(deposit_usd)
    haircut = int(math.ceil(gross * limits.deposit_safety_pct / 100 - 1e-12))
    return max(0, gross - haircut - limits.fixed_cost_cents)


def profit_pct(buy_cents: int, net_cents: int) -> float:
    if buy_cents <= 0:
        return -100.0
    return (net_cents - buy_cents) * 100.0 / buy_cents


def snapshot_is_sellable(row: dict, now_ms: float, max_age_sec: int) -> bool:
    """Mirror skins-table's default availability/overstock/freshness filters."""
    try:
        if float(row.get("p") or 0) <= 0:
            return False
        if int(row.get("o") or 0) != 0:
            return False
        if int(row.get("c") or 0) <= 0:
            return False
        ts = float(row.get("t") or 0)
        return ts > 0 and 0 <= (now_ms - ts) / 1000 <= max_age_sec
    except (TypeError, ValueError):
        return False


def snapshot_is_buyable(row: dict, now_ms: float, max_age_sec: int) -> bool:
    try:
        if float(row.get("p") or 0) <= 0 or int(row.get("c") or 0) <= 0:
            return False
        ts = float(row.get("t") or 0)
        return ts > 0 and 0 <= (now_ms - ts) / 1000 <= max_age_sec
    except (TypeError, ValueError):
        return False


def build_candidates(
    dmarket: dict[str, dict],
    deposits: dict[str, dict],
    limits: Limits,
    *,
    now_ms: float | None = None,
) -> list[dict]:
    """Build candidates from the exact DMARKET / CS.TRADE DEPOSIT table pair."""
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    rows: list[dict] = []
    for name, dep in deposits.items():
        ask = dmarket.get(name)
        if (not ask
                or not snapshot_is_buyable(ask, now_ms, limits.max_snapshot_age_sec)
                or not snapshot_is_sellable(dep, now_ms, limits.max_snapshot_age_sec)):
            continue
        try:
            buy = cents(ask.get("p") or 0)
            dep_gross = cents(dep.get("p") or 0)
        except (TypeError, ValueError):
            continue
        if buy < cents(limits.min_buy_usd) or buy > cents(limits.max_buy_usd):
            continue
        if buy <= 0 or dep_gross <= 0:
            continue
        net = projected_net_cents(dep_gross / 100, limits)
        pct = profit_pct(buy, net)
        if pct + 1e-9 < limits.min_profit_pct:
            continue
        rows.append({
            "name": name,
            "table_buy_cents": buy,
            "table_deposit_cents": dep_gross,
            "projected_net_cents": net,
            "profit_pct": pct,
            "deposit_snapshot": dep,
            "dmarket_snapshot": ask,
        })
    return sorted(rows, key=lambda row: (row["profit_pct"], row["projected_net_cents"]), reverse=True)


def choose_live_offer(offers: Iterable[dict], table_buy_cents: int, *, max_slippage_pct: float = 3.0) -> dict | None:
    """Return the cheapest immediately withdrawable, unlocked DMarket offer."""
    ceiling = int(math.floor(table_buy_cents * (1 + max_slippage_pct / 100) + 1e-9))
    valid = []
    for offer in offers:
        attrs = offer.get("attributes") or {}
        try:
            price = int(offer.get("priceCents") or 0)
            lock_days = int(attrs.get("tradeLockDays") or 0)
        except (TypeError, ValueError):
            continue
        if not offer.get("offerId") or price <= 0 or price > ceiling:
            continue
        if attrs.get("withdrawable") is not True or lock_days != 0:
            continue
        if not attrs.get("id") or not attrs.get("classId"):
            continue
        valid.append((price, offer))
    return min(valid, key=lambda pair: pair[0])[1] if valid else None


def steam_asset_id(cs_item: dict) -> str:
    """CS.TRADE commonly suffixes its user-inventory id with the app id."""
    raw = str(cs_item.get("id") or cs_item.get("assetid") or "")
    return raw.rsplit("_", 1)[0]


def find_new_item(items: Iterable[dict], name: str, known_asset_ids: Iterable[str]) -> dict | None:
    known = {str(x) for x in known_asset_ids}
    matches = [
        item for item in items
        if item.get("market_hash_name") == name and steam_asset_id(item) not in known
    ]
    return matches[0] if len(matches) == 1 else None


def eligible_user_item(item: dict) -> tuple[bool, str]:
    """Validate the live CS.TRADE item before asking it to create an offer."""
    if not item:
        return False, "not_found"
    if item.get("status") == "unavailable":
        return False, "unavailable"
    if not item.get("price"):
        return False, "no_deposit_price"
    if item.get("tradable") in (False, 0, "0") or item.get("tradable_bool") in (False, 0, "0"):
        return False, "steam_trade_protected"
    return True, "ok"


def match_incoming_asset_offer(offers: Iterable[dict], asset_id: str) -> dict | None:
    """Match only a request taking the exact expected asset and giving nothing back."""
    asset_id = str(asset_id)
    matches = []
    for offer in offers:
        if int(offer.get("trade_offer_state") or 0) != 2:
            continue
        if offer.get("items_to_receive"):
            continue
        gives = offer.get("items_to_give") or []
        if len(gives) != 1 or str(gives[0].get("assetid")) != asset_id:
            continue
        matches.append(offer)
    return matches[0] if len(matches) == 1 else None
