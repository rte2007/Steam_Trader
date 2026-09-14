"""Automated CS2 DMarket -> market.csgo.com MARKET ORDER worker."""
from __future__ import annotations

import json
import os
import socket
import time
import traceback
import uuid
import base64
from datetime import datetime, timezone
from pathlib import Path

import requests


# Some VPS resolvers return an unreachable Akamai edge for steamcommunity.com.
# Pin only this process/hostname to a verified edge; leave server DNS untouched.
_STEAM_HOST_IPS = {
    "steamcommunity.com": os.getenv("DMCS_STEAM_COMMUNITY_IP", "").strip(),
    "api.steampowered.com": os.getenv("DMCS_STEAM_API_IP", "").strip(),
    "login.steampowered.com": os.getenv("DMCS_STEAM_LOGIN_IP", "").strip(),
    "store.steampowered.com": os.getenv("DMCS_STEAM_STORE_IP", "").strip(),
    "help.steampowered.com": os.getenv("DMCS_STEAM_HELP_IP", "").strip(),
}
_STEAM_HOST_IPS = {host: ip for host, ip in _STEAM_HOST_IPS.items() if ip}
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
if _STEAM_HOST_IPS:
    def _route_steam_community(host, port, *args, **kwargs):
        target = _STEAM_HOST_IPS.get(str(host).lower(), host)
        return _ORIGINAL_GETADDRINFO(target, port, *args, **kwargs)

    socket.getaddrinfo = _route_steam_community

from bootstrap_config import load_shared_config

load_shared_config()

import dmarket_api as dm
import cs_trade_api as cs
import market_cs2_api as market
import skinstable_api as skins
import steam_login as steam
import telegram_bot as tg
import cs2_item_types
from dm_market_core import (Limits, build_candidates, choose_dmarket_offer,
                            exact_inventory_asset_id, exact_market_offer, executable_price,
                            extract_steam_access_token, market_order_is_executable,
                            net_after_market_fee,
                            profit_pct)
from leg3_core import (choose_cs2_dmarket_offer, dmarket_unlock_at, exposure_key,
                       is_regular_weapon_name, steam_unlock_at)


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "dm_market_cs2_state.json"
CONTROL_PATH = ROOT / "dm_market_cs2_control.json"
APP_ID = 730
GAME = "a8db"
LIMITS = Limits(
    min_profit_pct=float(os.getenv("DMCS_MIN_PROFIT_PCT", "1")),
    market_fee_pct=float(os.getenv("DMCS_MARKET_FEE_PCT", "10")),
    min_buy_usd=float(os.getenv("DMCS_MIN_BUY_USD", "0.30")),
    max_buy_usd=float(os.getenv("DMCS_MAX_BUY_USD", "0")),
    max_snapshot_age_sec=int(os.getenv("DMCS_SNAPSHOT_MAX_AGE_SEC", str(24 * 3600))),
    order_undercut_usd=float(os.getenv("DMCS_ORDER_UNDERCUT_USD", "0")),
)
MAX_ACTIVE = int(os.getenv("DMCS_MAX_ACTIVE", "1"))
MAX_DAILY_SPEND_CENTS = int(round(float(os.getenv("DMCS_MAX_DAILY_SPEND_USD", "0")) * 100))
MAX_SLIPPAGE_PCT = float(os.getenv("DMCS_MAX_SLIPPAGE_PCT", "3"))
POLL_SEC = int(os.getenv("DMCS_POLL_SEC", "300"))
DIRECT_INVENTORY_BACKOFF_SEC = int(os.getenv("DMCS_STEAM_INVENTORY_BACKOFF_SEC", "900"))
MIN_MARKET_ORDER_USD = float(os.getenv("DMCS_MIN_MARKET_ORDER_USD", "2"))
MIN_SALES_14D = int(os.getenv("DMCS_MIN_SALES_14D", "300"))
HISTORY_CACHE_SEC = int(os.getenv("DMCS_HISTORY_CACHE_SEC", str(6 * 3600)))
_direct_inventory_cache: list[dict] = []
_direct_inventory_last_try = 0.0
_market_prerequisites_cache: tuple[float, dict] = (0.0, {})
_market_ping_cache: tuple[float, dict] = (0.0, {})
_sales_cache: dict[str, tuple[float, int]] = {}
_history_ids_cache: tuple[float, dict] = (0.0, {})
STEAM_MAFILE_PATH = Path(os.getenv("DMCS_STEAM_MAFILE", "")) if os.getenv("DMCS_STEAM_MAFILE") else None
STEAM_SESSION_CACHE_PATH = ROOT / "steam_web_session.json"
STEAM_LOGIN_BACKOFF_SEC = int(os.getenv("DMCS_STEAM_LOGIN_BACKOFF_SEC", "600"))
_steam_login_last_try = 0.0


def _mafile_session() -> dict:
    if not STEAM_MAFILE_PATH:
        print("[steam] maFile is not configured")
        return {}
    if not STEAM_MAFILE_PATH.is_file():
        print("[steam] maFile is missing")
        return {}
    try:
        data = json.loads(STEAM_MAFILE_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"[steam] maFile read failed: {type(exc).__name__}")
        return {}
    session_data = data.get("Session") or {}
    steam_id = str(session_data.get("SteamID") or "")
    if not steam_id:
        print("[steam] maFile SteamID is missing")
        return {}
    if steam_id != str(steam.STEAM_ID):
        print("[steam] maFile SteamID does not match configured account")
        return {}
    return session_data


def _jwt_exp(token: str) -> int:
    try:
        encoded = token.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        return int(claims.get("exp") or 0)
    except Exception:
        return 0


def _jwt_is_current(token: str, margin_sec: int = 60) -> bool:
    return token.count(".") == 2 and _jwt_exp(token) > time.time() + margin_sec


def _mafile_access_token() -> str:
    token = str(_mafile_session().get("AccessToken") or "")
    return token if _jwt_is_current(token) else ""


def _load_cached_steam_session() -> bool:
    if steam._cookies and time.time() - steam._login_ts < 3000:
        return True
    if not STEAM_SESSION_CACHE_PATH.is_file():
        return False
    try:
        data = json.loads(STEAM_SESSION_CACHE_PATH.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or {}
        token = extract_steam_access_token(cookies.get("steamLoginSecure", ""))
        if not cookies.get("sessionid") or not token or not _jwt_is_current(token):
            return False
        steam._cookies = {str(k): str(v) for k, v in cookies.items()}
        steam._login_ts = time.time()
        print("[steam] restored persisted Steam web session")
        return True
    except Exception as exc:
        print(f"[steam] persisted session read failed: {type(exc).__name__}")
        return False


def _save_cached_steam_session() -> None:
    if not steam._cookies.get("sessionid") or not steam._cookies.get("steamLoginSecure"):
        return
    temp_path = STEAM_SESSION_CACHE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps({"cookies": steam._cookies}), encoding="utf-8")
    os.chmod(temp_path, 0o600)
    temp_path.replace(STEAM_SESSION_CACHE_PATH)


def _ensure_steam_session() -> bool:
    global _steam_login_last_try
    if _load_cached_steam_session():
        return True
    now = time.time()
    if now - _steam_login_last_try < STEAM_LOGIN_BACKOFF_SEC:
        return False
    _steam_login_last_try = now
    if _bootstrap_steam_from_mafile():
        _save_cached_steam_session()
        return True
    if not steam.get_steam_session():
        return False
    _save_cached_steam_session()
    return True


def _bootstrap_steam_from_mafile() -> bool:
    """Restore Steam web cookies with the maFile refresh token.

    This avoids password/Guard logins, which are unreliable in pysteamauth,
    while keeping the maFile encrypted-at-rest permissions on the server.
    """
    if steam._cookies and time.time() - steam._login_ts < 3000:
        return True
    try:
        session_data = _mafile_session()
        refresh_token = str(session_data.get("RefreshToken") or "")
        steam_id = str(session_data.get("SteamID") or "")
        if not session_data:
            return False
        if not refresh_token:
            print("[steam] maFile refresh token is missing")
            return False
        if not _jwt_is_current(refresh_token):
            print("[steam] maFile refresh token is expired")
            return False
        session_id = uuid.uuid4().hex
        web = requests.Session()
        web.headers.update({"User-Agent": "Mozilla/5.0"})
        web.cookies.set("sessionid", session_id, domain="steamcommunity.com")
        response = web.post(
            "https://login.steampowered.com/jwt/finalizelogin",
            data={"nonce": refresh_token, "sessionid": session_id,
                  "redir": "https://steamcommunity.com/login/home/?goto="},
            headers={"Origin": "https://steamcommunity.com"}, timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        transfer_results = []
        for transfer in payload.get("transfer_info") or []:
            params = transfer.get("params") or {}
            transfer_response = web.post(
                transfer.get("url"),
                data={"nonce": params.get("nonce"), "auth": params.get("auth"),
                       "steamID": steam_id}, timeout=25,
            )
            transfer_results.append((
                transfer_response.status_code,
                str(transfer.get("url") or "").split("/", 3)[2],
            ))
            transfer_response.raise_for_status()
        cookies = {cookie.name: cookie.value for cookie in web.cookies
                   if "steamcommunity.com" in (cookie.domain or "")}
        if not cookies.get("steamLoginSecure"):
            cookie_meta = sorted({f"{cookie.name}@{cookie.domain}" for cookie in web.cookies})
            print("[steam] maFile session restore returned no Steam web cookie; "
                  f"transfers={transfer_results}; cookies={cookie_meta}; "
                  f"payload_keys={sorted(payload.keys())}")
            return False
        steam._cookies = cookies
        steam._login_ts = time.time()
        print("[steam] restored web session from maFile refresh token")
        return True
    except Exception as exc:
        print(f"[steam] maFile session restore failed: {type(exc).__name__}")
        return False


def _default_state() -> dict:
    return {"version": 1, "active": {}, "done": [], "daily_spend": {}}


def _save(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def _adopt_legacy(state: dict) -> None:
    return


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = _default_state()
    for key, value in _default_state().items():
        state.setdefault(key, value)
    return state


def controls() -> dict:
    defaults = {"running": True, "auto_buy": False, "allow_withdraw": True,
                "allow_market": True, "auto_deliver": True}
    try:
        defaults.update(json.loads(CONTROL_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return defaults


def today_key() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def spent_today(state: dict) -> int:
    return int(state.get("daily_spend", {}).get(today_key(), 0))


def route_active_count(state: dict) -> int:
    return len(state.get("active", {}))


def _market_rows() -> tuple[dict, dict]:
    dmarket, _table_orders, _rates = skins._fetch_raw(APP_ID, "DMARKET", "MARKET ORDER")
    now_ms = time.time() * 1000
    orders = {name: {"p": float(row.get("buy_order") or 0),
                     "c": int(row.get("buy_order_volume") or 0),
                     "t": now_ms}
              for name, row in market.get_prices("USD").items()}
    return dmarket, orders


def _rub_per_usd() -> float:
    return 1.0


def _history_ids() -> dict:
    global _history_ids_cache
    checked_at, cached = _history_ids_cache
    if cached and time.time() - checked_at < HISTORY_CACHE_SEC:
        return cached
    response = requests.get("https://market.csgo.com/api/v2/full-history/all.json", timeout=45)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("history") or {}
    _history_ids_cache = (time.time(), result)
    return result


def _sales_14d(name: str) -> int:
    cached = _sales_cache.get(name)
    if cached and time.time() - cached[0] < HISTORY_CACHE_SEC:
        return cached[1]
    item_id = _history_ids().get(name)
    if item_id is None:
        return 0
    response = requests.get(
        f"https://market.csgo.com/api/v2/full-history/{item_id}.json", timeout=30)
    response.raise_for_status()
    cutoff = int(time.time()) - 14 * 86400
    history = ((response.json().get("data") or {}).get("history") or [])
    count = sum(1 for row in history if row and int(row[0]) >= cutoff)
    _sales_cache[name] = (time.time(), count)
    time.sleep(0.26)  # keep public API traffic below the documented 5 req/s limit
    return count


def scan() -> list[dict]:
    dmarket, orders = _market_rows()
    rows = build_candidates(dmarket, orders, LIMITS)
    accepted = []
    for row in rows:
        name = row["name"]
        if (row["exit_price"] < MIN_MARKET_ORDER_USD
                or not is_regular_weapon_name(name)
                or not cs2_item_types.is_plain_weapon_skin(name)):
            continue
        sales = _sales_14d(name)
        if sales < MIN_SALES_14D:
            continue
        accepted.append({**row, "sales_14d": sales})
    return accepted


def _steam_inventory_ids(name: str) -> list[str]:
    try:
        items = steam.get_app_inventory(APP_ID, require_marketable=False)
    except Exception as exc:
        print(f"[steam] initial inventory unavailable: {type(exc).__name__}")
        return []
    return [str(x.get("assetid")) for x in items
            if x.get("market_hash_name") == name and x.get("assetid")]


def _direct_inventory_with_backoff() -> list[dict]:
    """Avoid keeping Steam's public inventory endpoint in a 429 loop."""
    global _direct_inventory_cache, _direct_inventory_last_try
    now = time.time()
    if now - _direct_inventory_last_try < DIRECT_INVENTORY_BACKOFF_SEC:
        return _direct_inventory_cache
    _direct_inventory_last_try = now
    try:
        items = steam.get_app_inventory(APP_ID, require_marketable=False)
    except Exception as exc:
        print(f"[steam] inventory fallback unavailable: {type(exc).__name__}")
        return _direct_inventory_cache
    if items:
        _direct_inventory_cache = items
    return _direct_inventory_cache


def _request_market_inventory_refresh() -> tuple[bool, str]:
    """Refresh rust.tm using the documented path and required language."""
    status, payload = market._req(
        "GET", "update-inventory/", params={"lang": "en"}, retries=2,
    )
    ok = status == 200 and isinstance(payload, dict) and payload.get("success", False)
    if ok:
        return True, "ok"
    detail = payload if isinstance(payload, dict) else str(payload)[:160]
    return False, f"HTTP {status} {detail}"


def _market_inventory_status() -> dict:
    status, payload = market._req("GET", "inventory-status/", retries=2)
    if status == 200 and isinstance(payload, dict):
        return payload
    return {"success": False, "http": status}


def _market_prerequisites(force: bool = False) -> dict:
    """Cache rust.tm's sale preflight so a broken account cannot buy more stock."""
    global _market_prerequisites_cache
    checked_at, cached = _market_prerequisites_cache
    now = time.time()
    if not force and cached and now - checked_at < 300:
        return cached
    status, payload = market._req("GET", "test", retries=2)
    checks = payload.get("status") if status == 200 and isinstance(payload, dict) else None
    result = checks if isinstance(checks, dict) else {}
    _market_prerequisites_cache = (now, result)
    return result


def _market_ready_for_sale() -> tuple[bool, str]:
    checks = _market_prerequisites()
    required = ("user_token", "trade_check", "site_online", "site_notmpban")
    missing = [name for name in required if checks.get(name) is not True]
    ping_at, ping = _market_ping_cache
    modern_p2p_ready = (
        time.time() - ping_at < 300
        and ping.get("success") is True
        and ping.get("online") is True
        and ping.get("p2p") is True
    )
    if not modern_p2p_ready:
        missing.append("p2p_ping")
    return not missing, ",".join(missing)


def live_validate(row: dict) -> dict | None:
    offer = choose_cs2_dmarket_offer(dm.get_offers(GAME, row["name"], limit=20),
                                     row["table_buy"], MAX_SLIPPAGE_PCT)
    if not offer:
        return None
    quote = market.get_prices("USD").get(row["name"]) or {}
    live_listing = float(quote.get("price") or 0)
    live_order = float(quote.get("buy_order") or 0)
    if not market_order_is_executable(live_listing, live_order):
        print(f"[skip] {row['name']}: broken MARKET ORDER "
              f"(listing=${live_listing:.3f}, order=${live_order:.3f})")
        return None
    exit_price = executable_price(live_order, LIMITS.order_undercut_usd)
    net = net_after_market_fee(exit_price, LIMITS.market_fee_pct)
    buy = int(offer["priceCents"]) / 100
    pct = profit_pct(buy, net)
    if (live_order < MIN_MARKET_ORDER_USD
            or pct + 1e-9 < LIMITS.min_profit_pct):
        print(f"[skip] {row['name']}: live profit {pct:.2f}% below {LIMITS.min_profit_pct:.2f}%")
        return None
    return {**row, "offer": offer, "live_buy": buy, "live_order": live_order,
            "exit_price": exit_price, "projected_net": net, "profit_pct": pct}


def start_purchase(row: dict, state: dict, ctl: dict) -> bool:
    if (not ctl["auto_buy"] or route_active_count(state) >= MAX_ACTIVE
            or not market.MARKET_CSGO_API_KEY):
        return False
    if exposure_key(row["name"]) in {exposure_key(x.get("name", ""))
                                      for x in state.get("active", {}).values()}:
        return False
    market_ready, missing = _market_ready_for_sale()
    if not market_ready:
        print(f"[buy-blocked] market.csgo.com prerequisites missing: {missing}")
        return False
    live = live_validate(row)
    if not live:
        return False
    price_cents = int(live["offer"]["priceCents"])
    if MAX_DAILY_SPEND_CENTS > 0 and spent_today(state) + price_cents > MAX_DAILY_SPEND_CENTS:
        return False
    result = dm.buy_offer(live["offer"]["offerId"], price_cents)
    if not result.get("success"):
        print(f"[buy] {live['name']}: {result.get('data')}")
        return False
    attrs = live["offer"]["attributes"]
    now = int(time.time())
    source_unlock = dmarket_unlock_at(attrs, now)
    txid = str(uuid.uuid4())
    state["active"][txid] = {
        "name": live["name"], "stage": "bought" if source_unlock <= now else "dmarket_locked",
        "buy_cents": price_cents,
        "market_order_before": live["live_order"], "planned_exit": live["exit_price"],
        "projected_net": live["projected_net"], "projected_profit_pct": live["profit_pct"],
        "dmarket_asset_id": attrs["id"], "class_id": attrs["classId"],
        "dmarket_unlock_at": source_unlock,
        "dmarket_unlock_date": attrs.get("unlockDate"),
        "known_steam_asset_ids": _steam_inventory_ids(live["name"]),
        "created_at": now, "updated_at": now,
    }
    state["daily_spend"][today_key()] = spent_today(state) + price_cents
    _save(state)
    lock_note = ("готов к выводу" if source_unlock <= now else
                 f"DMarket unlock: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(source_unlock))}")
    tg.send(f"🛒 <b>DMarket → MARKET ORDER (CS2)</b>\n📦 {live['name']}\n"
            f"💰 ${live['live_buy']:.2f} → ${live['exit_price']:.3f}; "
            f"после 10%: ${live['projected_net']:.3f} ({live['profit_pct']:+.2f}%)\n{lock_note}")
    return True


def _accept_dmarket_delivery(info: dict) -> None:
    offers = steam.get_trade_offers()
    matches = [o for o in offers if o.get("trade_offer_state") == 2 and not o.get("items_to_give")
               and (o.get("_item_names") or []) == [info["name"]]]
    if len(matches) != 1:
        return
    offer = matches[0]
    partner64 = str(int(offer.get("accountid_other", 0)) + 76561197960265728)
    if steam.accept_trade(str(offer["tradeofferid"]), partner64):
        accepted_at = int(time.time())
        info["delivery_offer_id"] = str(offer["tradeofferid"])
        info["steam_accepted_at"] = accepted_at
        info["steam_unlock_at"] = steam_unlock_at(accepted_at)
        info["stage"] = "steam_locked"


def _market_items() -> list[dict] | None:
    status, payload = market._req("GET", "items", retries=2)
    if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
        return None
    # rust.tm returns items=null when the account has no active market items.
    # That is a valid empty result, not an API failure.
    return payload.get("items") or []


def _market_ping() -> bool:
    global _market_ping_cache
    token = _mafile_access_token()
    token_source = "mafile"
    if not token:
        if not _ensure_steam_session():
            return False
        token = extract_steam_access_token(steam._cookies.get("steamLoginSecure", ""))
        token_source = "steam_session"
    if not token:
        print("[market] ping-new: Steam JWT is unavailable")
        return False
    status, payload = market._req("POST", "ping-new", json_body={"access_token": token}, retries=2)
    if (token_source == "mafile" and isinstance(payload, dict)
            and (payload.get("error") or payload.get("message")) == "invalid_access_token"):
        print("[market] maFile access token expired; refreshing Steam web session")
        if not _ensure_steam_session():
            return False
        token = extract_steam_access_token(steam._cookies.get("steamLoginSecure", ""))
        if not token:
            print("[market] refreshed Steam session has no JWT")
            return False
        status, payload = market._req(
            "POST", "ping-new", json_body={"access_token": token}, retries=2)
    safe_payload = payload if isinstance(payload, dict) else {}
    _market_ping_cache = (time.time(), safe_payload)
    ok = (status == 200 and safe_payload.get("success") is True
          and safe_payload.get("online") is True and safe_payload.get("p2p") is True)
    if not ok:
        print(f"[market] ping-new failed: HTTP {status}; "
              f"success={safe_payload.get('success')}; online={safe_payload.get('online')}; "
              f"p2p={safe_payload.get('p2p')}; error={safe_payload.get('error') or safe_payload.get('message')}")
    else:
        print("[market] ping-new ready: online=true, p2p=true")
    return bool(ok)


def progress(txid: str, info: dict, state: dict, ctl: dict) -> None:
    stage = info.get("stage")
    if stage == "dmarket_locked":
        unlock_at = int(info.get("dmarket_unlock_at") or 0)
        if time.time() < unlock_at + 60:
            info["last_wait_reason"] = f"dmarket_lock_until:{unlock_at}"
            _save(state)
            return
        info["stage"] = "bought"
        info["updated_at"] = int(time.time())
        info.pop("last_wait_reason", None)
        _save(state)
        stage = "bought"

    if stage == "bought":
        if not ctl["allow_withdraw"]:
            return
        wd = dm.withdraw_asset(info["dmarket_asset_id"], GAME, info["class_id"])
        if not wd.get("success"):
            info["last_wait_reason"] = f"withdraw_failed:{str(wd.get('data'))[:200]}"
            _save(state)
            return
        info.update(stage="withdrawing", transfer_id=wd["transfer_id"], updated_at=int(time.time()))
        _save(state)
        return

    if stage in {"withdrawing", "waiting_steam", "steam_locked"}:
        _accept_dmarket_delivery(info)
        if info.get("stage") == "steam_locked":
            unlock_at = int(info.get("steam_unlock_at") or 0)
            if time.time() < unlock_at:
                info["last_wait_reason"] = f"steam_lock_until:{unlock_at}"
                _save(state)
                return
        _request_market_inventory_refresh()
        inventory = market.get_my_inventory("en")
        asset_id = exact_inventory_asset_id(
            info["name"], info.get("known_steam_asset_ids", []),
            inventory,
            preferred_asset_id=info.get("steam_asset_id"),
        )
        if not asset_id:
            asset_id = exact_inventory_asset_id(
                info["name"], info.get("known_steam_asset_ids", []),
                inventory, _direct_inventory_with_backoff(),
                preferred_asset_id=info.get("steam_asset_id"),
            )
        if not asset_id:
            info["last_wait_reason"] = "waiting_exact_steam_asset"
            _save(state)
            return
        info.pop("last_wait_reason", None)
        info.update(stage="waiting_market", steam_asset_id=asset_id, updated_at=int(time.time()))
        _save(state)
        return

    if stage == "waiting_market":
        if not ctl["allow_market"]:
            return
        market_ready, missing = _market_ready_for_sale()
        if not market_ready:
            info["last_wait_reason"] = f"market_prerequisites_missing:{missing}"
            _save(state)
            return
        asset_id = str(info.get("steam_asset_id") or "")
        if not asset_id:
            info["last_wait_reason"] = "asset_not_sellable_on_market_yet"
            _save(state)
            return
        refreshed_at = int(info.get("market_refresh_requested_at") or 0)
        now = int(time.time())
        inventory = market.get_my_inventory("en")
        visible = exact_inventory_asset_id(
            info["name"], info.get("known_steam_asset_ids", []), inventory,
            preferred_asset_id=asset_id,
        )
        if visible != asset_id:
            status = _market_inventory_status()
            if status.get("is_updating"):
                info["last_wait_reason"] = (
                    "market_inventory_refreshing:"
                    f"items={status.get('items')},"
                    f"last_success={status.get('last_time_success_update')}"
                )
                _save(state)
                return
            if not refreshed_at or now - refreshed_at >= 300:
                ok, detail = _request_market_inventory_refresh()
                info["market_refresh_requested_at"] = now
                if ok:
                    info["last_wait_reason"] = (
                        "market_inventory_refreshing:"
                        f"items={status.get('items')},"
                        f"last_success={status.get('last_time_success_update')}"
                    )
                else:
                    info["last_wait_reason"] = f"market_inventory_refresh_failed:{detail[:180]}"
                _save(state)
            else:
                info["last_wait_reason"] = (
                    "market_item_not_visible:"
                    f"items={status.get('items')},"
                    f"last_success={status.get('last_time_success_update')}"
                )
                _save(state)
            return
        if refreshed_at and now - refreshed_at < 20:
            return
        quote = market.get_prices("USD").get(info["name"]) or {}
        live_listing = float(quote.get("price") or 0)
        live_order = float(quote.get("buy_order") or 0)
        if not market_order_is_executable(live_listing, live_order):
            info["last_wait_reason"] = (f"broken_market_order:listing={live_listing:.3f},"
                                        f"order={live_order:.3f}")
            _save(state)
            return
        exit_price = executable_price(live_order, LIMITS.order_undercut_usd)
        net = net_after_market_fee(exit_price, LIMITS.market_fee_pct)
        pct = profit_pct(info["buy_cents"] / 100, net)
        if live_order <= 0 or pct + 1e-9 < LIMITS.min_profit_pct:
            info["last_wait_reason"] = f"live_profit_{pct:.2f}_below_min"
            _save(state)
            return
        balance_before = market.get_balance()
        result = market.add_to_sale(asset_id, exit_price, "USD")
        if not result.get("success"):
            info["last_wait_reason"] = f"add_to_sale_failed:{str(result.get('data'))[:200]}"
            _save(state)
            return
        info.pop("last_wait_reason", None)
        info.update(stage="listed", market_item_id=str(result["item_id"]), sale_price=exit_price,
                    expected_net=net, actual_profit_pct=pct, market_balance_before=balance_before,
                    updated_at=int(time.time()))
        _save(state)
        return

    items = _market_items()
    if items is None:
        return
    market_item = next((x for x in items if str(x.get("item_id")) == str(info.get("market_item_id"))), None)
    if stage == "listed":
        status = str(market_item.get("status")) if market_item else ""
        if market_item and status == "1":
            return
        if not ctl["auto_deliver"]:
            info["last_wait_reason"] = f"market_status_{status}"
            _save(state)
            return
        if market_item and status != "2":
            info["last_wait_reason"] = f"market_status_{status}"
            _save(state)
            return
        give = market.trade_request_give_p2p_all()
        offer = exact_market_offer(give.get("offers") or [], info["steam_asset_id"])
        if not offer:
            if not market_item:
                inventory = market.get_my_inventory("en")
                visible = exact_inventory_asset_id(
                    info["name"], info.get("known_steam_asset_ids", []), inventory,
                    preferred_asset_id=info["steam_asset_id"],
                )
                if visible == info["steam_asset_id"]:
                    info.update(stage="waiting_market", updated_at=int(time.time()))
                    info["last_wait_reason"] = "cancelled_sale_returned_to_inventory"
                    _save(state)
                    return
            info["last_wait_reason"] = (
                "exact_market_p2p_offer_not_found" if market_item
                else "listed_item_missing_and_p2p_not_ready"
            )
            _save(state)
            return
        access_token = offer.get("token") or offer.get("token_seller") or ""
        result = steam.create_trade_offer(int(offer.get("partner") or 0), access_token,
                                          [info["steam_asset_id"]], appid=APP_ID,
                                          message=offer.get("tradeoffermessage", ""))
        if not result.get("success"):
            info["last_wait_reason"] = f"steam_send_failed:{str(result.get('error'))[:160]}"
            _save(state)
            return
        ready = market.trade_ready(result["tradeofferid"])
        if not ready.get("success"):
            info["last_wait_reason"] = f"trade_ready_failed:{str(ready)[:160]}"
            _save(state)
            return
        info.update(stage="delivering", outgoing_offer_id=str(result["tradeofferid"]), updated_at=int(time.time()))
        _save(state)
        return

    if stage == "delivering":
        if market_item and str(market_item.get("status")) in {"2", "7"}:
            return
        balance_after = market.get_balance()
        before = float(info.get("market_balance_before", -1))
        expected = float(info.get("expected_net", 0))
        if before < 0 or expected <= 0 or balance_after + 0.002 < before + expected:
            info.update(last_wait_reason="market_balance_credit_not_visible", market_balance_after=balance_after)
            _save(state)
            return
        credit = round(balance_after - before, 3)
        done = {**info, "txid": txid, "market_balance_after": balance_after,
                "market_credit": credit, "closed_at": int(time.time())}
        state["done"].append(done)
        state["active"].pop(txid, None)
        _save(state)
        tg.send(f"✅ <b>DMarket → MARKET ORDER (CS2) завершено</b>\n📦 {info['name']}\n"
                f"💰 ${info['buy_cents']/100:.2f} → ${expected:.3f} ({info['actual_profit_pct']:+.2f}%)")


def run_once(state: dict) -> None:
    ctl = controls()
    for txid, info in list(state["active"].items()):
        try:
            progress(txid, info, state, ctl)
        except Exception as exc:
            info["last_wait_reason"] = f"progress_error:{type(exc).__name__}:{str(exc)[:140]}"
            _save(state)
            traceback.print_exc()
    if not ctl["running"]:
        return
    rows = scan()
    print(f"[scan] candidates={len(rows)} route_active={route_active_count(state)} "
          f"recovery={len(state['active'])-route_active_count(state)} spent=${spent_today(state)/100:.2f} gates={ctl}")
    for row in rows[:5]:
        print(f"  {row['name']}: ${row['table_buy']:.2f} -> ${row['projected_net']:.3f} ({row['profit_pct']:+.2f}%)")
    for row in rows:
        if start_purchase(row, state, ctl):
            break


def main() -> None:
    print("[dmcs] DMarket -> MARKET ORDER, CS2")
    max_buy = "unlimited" if LIMITS.max_buy_usd <= 0 else f"${LIMITS.max_buy_usd:.2f}"
    print(f"[leg2] min_profit={LIMITS.min_profit_pct}% after fee={LIMITS.market_fee_pct}% "
          f"budget=${LIMITS.min_buy_usd:.2f}-{max_buy}")
    state = load_state()
    while True:
        try:
            ctl = controls()
            needs_market_ping = any(
                info.get("stage") in {"waiting_market", "listed", "delivering"}
                for info in state.get("active", {}).values()
            ) or ctl.get("auto_buy", False)
            if market.MARKET_CSGO_API_KEY and needs_market_ping and ctl.get("allow_market"):
                _market_ping()
            run_once(state)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
