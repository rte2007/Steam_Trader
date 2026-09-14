"""Pure rules for the Rust DMarket -> MARKET ORDER route."""
from __future__ import annotations

import math
import time
from urllib.parse import unquote
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Limits:
    min_profit_pct: float = 1.0
    market_fee_pct: float = 10.0
    min_buy_usd: float = 0.30
    max_buy_usd: float = 10.0
    max_snapshot_age_sec: int = 24 * 3600
    order_undercut_usd: float = 0.0


def money_mills(value: Any) -> int:
    """Market USD prices use thousandths; never round revenue upward."""
    return int(math.floor(float(value) * 1000 + 1e-9))


def net_after_market_fee(sell_usd: float, fee_pct: float) -> float:
    gross = money_mills(sell_usd)
    net = int(math.floor(gross * (1 - fee_pct / 100) + 1e-9))
    return net / 1000


def profit_pct(buy_usd: float, net_usd: float) -> float:
    return -100.0 if buy_usd <= 0 else (net_usd - buy_usd) * 100 / buy_usd


def executable_price(order_usd: float, undercut_usd: float = 0.0) -> float:
    return max(0.0, round(float(order_usd) - undercut_usd, 3))


def market_order_is_executable(lowest_listing: Any, buy_order: Any) -> bool:
    """Reject stale/broken bids that sit above an already cheaper listing."""
    try:
        listing = float(lowest_listing or 0)
        order = float(buy_order or 0)
    except (TypeError, ValueError):
        return False
    return listing > 0 and order > 0 and order <= listing + 1e-9


def snapshot_ok(row: dict, now_ms: float, max_age_sec: int) -> bool:
    try:
        price = float(row.get("p") or 0)
        count = int(row.get("c") or 0)
        ts = float(row.get("t") or 0)
        return price > 0 and count > 0 and ts > 0 and 0 <= (now_ms - ts) / 1000 <= max_age_sec
    except (TypeError, ValueError):
        return False


def build_candidates(dmarket: dict[str, dict], market_orders: dict[str, dict],
                     limits: Limits, *, now_ms: float | None = None) -> list[dict]:
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    rows = []
    for name, ask in dmarket.items():
        order = market_orders.get(name)
        if not order or not snapshot_ok(ask, now_ms, limits.max_snapshot_age_sec) or not snapshot_ok(order, now_ms, limits.max_snapshot_age_sec):
            continue
        buy = float(ask["p"])
        if buy < limits.min_buy_usd or (limits.max_buy_usd > 0 and buy > limits.max_buy_usd):
            continue
        live_exit = executable_price(float(order["p"]), limits.order_undercut_usd)
        net = net_after_market_fee(live_exit, limits.market_fee_pct)
        pct = profit_pct(buy, net)
        if pct + 1e-9 < limits.min_profit_pct:
            continue
        rows.append({"name": name, "table_buy": buy, "table_order": float(order["p"]),
                     "exit_price": live_exit, "projected_net": net, "profit_pct": pct})
    return sorted(rows, key=lambda row: (row["profit_pct"], row["projected_net"]), reverse=True)


def choose_dmarket_offer(offers: Iterable[dict], table_buy: float,
                         max_slippage_pct: float = 3.0) -> dict | None:
    ceiling = int(math.floor(table_buy * 100 * (1 + max_slippage_pct / 100) + 1e-9))
    valid = []
    for offer in offers:
        attrs = offer.get("attributes") or {}
        try:
            price = int(offer.get("priceCents") or 0)
            lock_days = int(attrs.get("tradeLockDays") or 0)
        except (TypeError, ValueError):
            continue
        if (0 < price <= ceiling and attrs.get("withdrawable") is True and lock_days == 0
                and attrs.get("id") and attrs.get("classId") and offer.get("offerId")):
            valid.append(offer)
    return min(valid, key=lambda row: int(row["priceCents"]), default=None)


def exact_market_offer(offers: Iterable[dict], asset_id: str) -> dict | None:
    matches = []
    for offer in offers:
        ids = [str(item.get("assetid")) for item in (offer.get("items") or [])]
        if ids == [str(asset_id)]:
            matches.append(offer)
    return matches[0] if len(matches) == 1 else None


def exact_inventory_asset_id(name: str, known_asset_ids: Iterable[str],
                             *inventories: Iterable[dict],
                             preferred_asset_id: str | None = None) -> str | None:
    """Resolve one new Steam asset across marketplace and direct inventories.

    rust.tm can temporarily omit a freshly received item from ``my-inventory``
    even though Steam already exposes its exact asset id.  Both representations
    are accepted here, but ambiguity is rejected so another copy is never sold.
    """
    known = {str(value) for value in known_asset_ids if value is not None}
    candidates: set[str] = set()
    for inventory in inventories:
        for item in inventory or []:
            if item.get("market_hash_name") != name:
                continue
            asset_id = item.get("id") or item.get("assetid")
            raw = str(asset_id or "")
            # CS.TRADE represents Rust ids as "assetid_appid".
            if "_" in raw and all(part.isdigit() for part in raw.rsplit("_", 1)):
                raw = raw.rsplit("_", 1)[0]
            if raw and raw not in known:
                candidates.add(raw)
    preferred = str(preferred_asset_id) if preferred_asset_id else ""
    if preferred and preferred in candidates:
        return preferred
    return next(iter(candidates)) if len(candidates) == 1 else None


def extract_steam_access_token(steam_login_secure: str) -> str | None:
    """Extract the Steam JWT from the URL-encoded steamLoginSecure cookie."""
    decoded = unquote(str(steam_login_secure or ""))
    token = decoded.split("||", 1)[1] if "||" in decoded else ""
    return token if token.count(".") == 2 else None
