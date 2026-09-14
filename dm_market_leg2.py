"""Automated Rust DMarket -> rust.tm MARKET ORDER worker."""
from __future__ import annotations

import json
import os
import socket
import time
import traceback
import uuid
import base64
from pathlib import Path

import requests


# Some VPS resolvers return an unreachable Akamai edge for steamcommunity.com.
# Pin only this process/hostname to a verified edge; leave server DNS untouched.
_STEAM_HOST_IPS = {
    "steamcommunity.com": os.getenv("DMM_STEAM_COMMUNITY_IP", "").strip(),
    "api.steampowered.com": os.getenv("DMM_STEAM_API_IP", "").strip(),
    "login.steampowered.com": os.getenv("DMM_STEAM_LOGIN_IP", "").strip(),
    "store.steampowered.com": os.getenv("DMM_STEAM_STORE_IP", "").strip(),
    "help.steampowered.com": os.getenv("DMM_STEAM_HELP_IP", "").strip(),
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
import rusttm_api as market
import skinstable_api as skins
import steam_login as steam
import telegram_bot as tg
from dm_market_core import (Limits, build_candidates, choose_dmarket_offer,
                            exact_inventory_asset_id, exact_market_offer, executable_price,
                            extract_steam_access_token, market_order_is_executable,
                            net_after_market_fee,
                            profit_pct)


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "dm_market_state.json"
CONTROL_PATH = ROOT / "dm_market_control.json"
LEGACY_STATE_PATH = ROOT / "dm_cstrade_state.json"
APP_ID = 252490
GAME = "rust"
LIMITS = Limits(
    min_profit_pct=float(os.getenv("DMM_MIN_PROFIT_PCT", "1")),
    market_fee_pct=float(os.getenv("DMM_MARKET_FEE_PCT", "10")),
    min_buy_usd=float(os.getenv("DMM_MIN_BUY_USD", "0.30")),
    max_buy_usd=float(os.getenv("DMM_MAX_BUY_USD", "10")),
    max_snapshot_age_sec=int(os.getenv("DMM_SNAPSHOT_MAX_AGE_SEC", str(24 * 3600))),
    order_undercut_usd=float(os.getenv("DMM_ORDER_UNDERCUT_USD", "0")),
)
MAX_ACTIVE = int(os.getenv("DMM_MAX_ACTIVE", "1"))
MAX_DAILY_SPEND_CENTS = int(round(float(os.getenv("DMM_MAX_DAILY_SPEND_USD", "10")) * 100))
MAX_SLIPPAGE_PCT = float(os.getenv("DMM_MAX_SLIPPAGE_PCT", "3"))
POLL_SEC = int(os.getenv("DMM_POLL_SEC", "60"))
DIRECT_INVENTORY_BACKOFF_SEC = int(os.getenv("DMM_STEAM_INVENTORY_BACKOFF_SEC", "900"))
WITHDRAW_RETRY_SEC = int(os.getenv("DMM_WITHDRAW_RETRY_SEC", "1800"))
MARKET_CACHE_RECOVERY_SEC = int(os.getenv("DMM_MARKET_CACHE_RECOVERY_SEC", "900"))
_direct_inventory_cache: list[dict] = []
_direct_inventory_last_try = 0.0
_market_prerequisites_cache: tuple[float, dict] = (0.0, {})
_market_ping_cache: tuple[float, dict] = (0.0, {})
STEAM_MAFILE_PATH = Path(os.getenv("DMM_STEAM_MAFILE", "")) if os.getenv("DMM_STEAM_MAFILE") else None
STEAM_SESSION_CACHE_PATH = ROOT / "steam_web_session.json"
STEAM_LOGIN_BACKOFF_SEC = int(os.getenv("DMM_STEAM_LOGIN_BACKOFF_SEC", "600"))
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
    return {"version": 1, "active": {}, "done": [], "daily_spend": {}, "legacy_adopted": False}


def _save(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def _adopt_legacy(state: dict) -> None:
    if state.get("legacy_adopted") or state["active"] or not LEGACY_STATE_PATH.exists():
        return
    try:
        legacy = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for old_id, info in (legacy.get("active") or {}).items():
        stage = info.get("stage")
        if stage not in {"bought", "withdrawing", "waiting_tradable"}:
            continue
        txid = f"legacy-{old_id}"
        state["active"][txid] = {
            "name": info.get("name"), "stage": "waiting_steam" if stage == "waiting_tradable" else stage,
            "buy_cents": int(info.get("buy_cents") or 0),
            "dmarket_asset_id": info.get("dmarket_asset_id"), "class_id": info.get("class_id"),
            "transfer_id": info.get("transfer_id"),
            "known_steam_asset_ids": info.get("known_steam_asset_ids") or [],
            "steam_asset_id": info.get("steam_asset_id"), "created_at": info.get("created_at") or int(time.time()),
            "updated_at": int(time.time()), "adopted_from_cstrade_route": True,
        }
    state["legacy_adopted"] = True
    _save(state)


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = _default_state()
    for key, value in _default_state().items():
        state.setdefault(key, value)
    _adopt_legacy(state)
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
    """Recovery items do not consume the single new-purchase slot."""
    return sum(1 for info in state.get("active", {}).values()
               if not info.get("adopted_from_cstrade_route")
               and not info.get("recovery_only"))


def _market_rows() -> tuple[dict, dict]:
    dmarket, _table_orders, rates = skins._fetch_raw(APP_ID, "DMARKET", "MARKET ORDER")
    rub_per_usd = float(rates.get("RUB") or 0)
    if rub_per_usd <= 0:
        raise RuntimeError("skins-table returned no RUB/USD rate")
    # Use skins-table only for the DMarket ask and FX rate.  Its MARKET ORDER
    # column has repeatedly diverged from rust.tm's executable book.  Build
    # the candidate side directly from the official aggregated orders feed.
    now_ms = time.time() * 1000
    prices = market.get_prices("RUB")
    orders = {
        name: {
            "p": float(row.get("buy_order") or 0) / rub_per_usd,
            "c": max(1, int(row.get("buy_order_volume") or 0)),
            "t": now_ms,
        }
        for name, row in prices.items()
        if market_order_is_executable(row.get("price"), row.get("buy_order"))
    }
    return dmarket, orders


def _rub_per_usd() -> float:
    """Use the same FX snapshot as skins-table; rust.tm listings accept RUB."""
    _left, _right, rates = skins._fetch_raw(APP_ID, "DMARKET", "MARKET ORDER")
    rate = float(rates.get("RUB") or 0)
    if rate <= 0:
        raise RuntimeError("skins-table returned no RUB/USD rate")
    return rate


def scan() -> list[dict]:
    dmarket, orders = _market_rows()
    return build_candidates(dmarket, orders, LIMITS)


def _steam_inventory_ids(name: str) -> list[str]:
    try:
        items = steam.get_inventory()
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
        items = steam.get_inventory()
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


def _recover_market_inventory_cache() -> tuple[bool, str]:
    """Kick the legacy cache endpoint when v2 update-inventory stays stale."""
    try:
        response = requests.get(
            "https://rust.tm/api/UpdateInventory/",
            params={"key": market.RUSTTM_API_KEY}, timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        return False, type(exc).__name__
    try:
        payload = response.json()
    except Exception:
        payload = response.text[:160]
    ok = response.status_code == 200 and (
        not isinstance(payload, dict) or payload.get("success", True)
    )
    return bool(ok), f"HTTP {response.status_code} {str(payload)[:160]}"


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
    offer = choose_dmarket_offer(dm.get_offers(GAME, row["name"], limit=20),
                                 row["table_buy"], MAX_SLIPPAGE_PCT)
    if not offer:
        return None
    quote = market.get_prices("RUB").get(row["name"]) or {}
    live_listing_rub = float(quote.get("price") or 0)
    live_order_rub = float(quote.get("buy_order") or 0)
    if not market_order_is_executable(live_listing_rub, live_order_rub):
        print(f"[skip] {row['name']}: broken MARKET ORDER "
              f"(listing={live_listing_rub:.2f} RUB, order={live_order_rub:.2f} RUB)")
        return None
    rub_per_usd = _rub_per_usd()
    live_order = live_order_rub / rub_per_usd
    exit_price = executable_price(live_order, LIMITS.order_undercut_usd)
    net = net_after_market_fee(exit_price, LIMITS.market_fee_pct)
    buy = int(offer["priceCents"]) / 100
    pct = profit_pct(buy, net)
    if live_order <= 0 or pct + 1e-9 < LIMITS.min_profit_pct:
        return None
    return {**row, "offer": offer, "live_buy": buy, "live_order": live_order,
            "live_order_rub": live_order_rub, "rub_per_usd": rub_per_usd,
            "exit_price": exit_price, "projected_net": net, "profit_pct": pct}


def start_purchase(row: dict, state: dict, ctl: dict) -> bool:
    if not ctl["auto_buy"] or route_active_count(state) >= MAX_ACTIVE or not market.RUSTTM_API_KEY:
        return False
    market_ready, missing = _market_ready_for_sale()
    if not market_ready:
        print(f"[buy-blocked] rust.tm prerequisites missing: {missing}")
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
    txid = str(uuid.uuid4())
    state["active"][txid] = {
        "name": live["name"], "stage": "bought", "buy_cents": price_cents,
        "market_order_before": live["live_order"], "planned_exit": live["exit_price"],
        "projected_net": live["projected_net"], "projected_profit_pct": live["profit_pct"],
        "dmarket_asset_id": attrs["id"], "class_id": attrs["classId"],
        "known_steam_asset_ids": _steam_inventory_ids(live["name"]),
        "created_at": int(time.time()), "updated_at": int(time.time()),
    }
    state["daily_spend"][today_key()] = spent_today(state) + price_cents
    _save(state)
    tg.send(f"🛒 <b>DMarket → MARKET ORDER</b>\n📦 {live['name']}\n"
            f"💰 ${live['live_buy']:.2f} → ${live['exit_price']:.3f}; "
            f"после 10%: ${live['projected_net']:.3f} ({live['profit_pct']:+.2f}%)")
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
        info["delivery_offer_id"] = str(offer["tradeofferid"])


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

    if stage in {"withdrawing", "waiting_steam"}:
        _accept_dmarket_delivery(info)
        _request_market_inventory_refresh()
        inventory = market.get_my_inventory("en")
        # Read-only fallback: CS.TRADE exposes the exact Steam asset id even
        # while rust.tm is refreshing and Steam's public inventory is on 429.
        auxiliary_inventory = cs.get_user_inventory(GAME)
        asset_id = exact_inventory_asset_id(
            info["name"], info.get("known_steam_asset_ids", []),
            inventory, auxiliary_inventory,
            preferred_asset_id=info.get("steam_asset_id"),
        )
        if not asset_id:
            asset_id = exact_inventory_asset_id(
                info["name"], info.get("known_steam_asset_ids", []),
                inventory, _direct_inventory_with_backoff(),
                preferred_asset_id=info.get("steam_asset_id"),
            )
        if not asset_id:
            now = int(time.time())
            last_retry = int(info.get("withdraw_retry_at") or info.get("updated_at") or 0)
            if stage == "withdrawing" and now - last_retry >= WITHDRAW_RETRY_SEC:
                transfer = dm.get_withdraw_status(str(info.get("transfer_id") or ""))
                if transfer.get("status") in {
                    "TransferStatusPending", "TransferStatusCreated", "TransferStatusOnHold",
                    "TransferStatusError", "TransferStatusFailedToCreate", "Error",
                }:
                    retry = dm.withdraw_asset(
                        info["dmarket_asset_id"], GAME, info["class_id"])
                    info["withdraw_retry_at"] = now
                    info["withdraw_retry_count"] = int(info.get("withdraw_retry_count") or 0) + 1
                    if retry.get("success"):
                        info["transfer_id"] = retry["transfer_id"]
                        info["last_wait_reason"] = "withdraw_restarted"
                    else:
                        info["last_wait_reason"] = (
                            "withdraw_retry_failed:" + str(retry.get("data"))[:180]
                        )
                    _save(state)
                    return
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
                last_success = int(status.get("last_time_success_update") or 0)
                last_recovery = int(info.get("market_cache_recovery_at") or 0)
                if (last_success and now - last_success >= MARKET_CACHE_RECOVERY_SEC
                        and now - last_recovery >= MARKET_CACHE_RECOVERY_SEC):
                    recovered, recovery_detail = _recover_market_inventory_cache()
                    info["market_cache_recovery_at"] = now
                    info["market_cache_recovery_result"] = (
                        "ok" if recovered else recovery_detail[:180]
                    )
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
        quote = market.get_prices("RUB").get(info["name"]) or {}
        live_listing_rub = float(quote.get("price") or 0)
        live_order_rub = float(quote.get("buy_order") or 0)
        if not market_order_is_executable(live_listing_rub, live_order_rub):
            info["last_wait_reason"] = (f"broken_market_order:listing={live_listing_rub:.2f},"
                                        f"order={live_order_rub:.2f}")
            _save(state)
            return
        rub_per_usd = _rub_per_usd()
        exit_price_rub = round(live_order_rub, 2)
        exit_price = executable_price(exit_price_rub / rub_per_usd, LIMITS.order_undercut_usd)
        net = net_after_market_fee(exit_price, LIMITS.market_fee_pct)
        pct = profit_pct(info["buy_cents"] / 100, net)
        if live_order_rub <= 0 or pct + 1e-9 < LIMITS.min_profit_pct:
            info["last_wait_reason"] = f"live_profit_{pct:.2f}_below_min"
            _save(state)
            return
        balance_before = market.get_balance()
        result = market.add_to_sale(asset_id, exit_price_rub, "RUB")
        if not result.get("success"):
            info["last_wait_reason"] = f"add_to_sale_failed:{str(result.get('data'))[:200]}"
            _save(state)
            return
        info.pop("last_wait_reason", None)
        info.update(stage="listed", market_item_id=str(result["item_id"]), sale_price=exit_price,
                    sale_price_rub=exit_price_rub, rub_per_usd=rub_per_usd,
                    expected_net_rub=round(exit_price_rub * (1 - LIMITS.market_fee_pct / 100), 2),
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
        expected_rub = float(info.get("expected_net_rub") or
                             (float(info.get("sale_price_rub") or 0) *
                              (1 - LIMITS.market_fee_pct / 100)))
        if before < 0 or expected_rub <= 0 or balance_after + 0.011 < before + expected_rub:
            info.update(last_wait_reason="market_balance_credit_not_visible", market_balance_after=balance_after)
            _save(state)
            return
        credit_rub = round(balance_after - before, 2)
        done = {**info, "txid": txid, "market_balance_after": balance_after,
                "market_credit_rub": credit_rub, "closed_at": int(time.time())}
        state["done"].append(done)
        state["active"].pop(txid, None)
        _save(state)
        tg.send(f"✅ <b>DMarket → MARKET ORDER завершено</b>\n📦 {info['name']}\n"
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
    print("[leg2] DMarket -> MARKET ORDER, Rust")
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
            )
            if market.RUSTTM_API_KEY and needs_market_ping and ctl.get("allow_market"):
                _market_ping()
            run_once(state)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
