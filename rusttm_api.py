# -*- coding: utf-8 -*-
"""
rust.tm API v2 — покупка на DMarket, продажа на rust.tm (P2P-листинг с
buy-ордерами, движок семейства market.csgo.com/market.dota2.net).
Доки: https://rust.tm/docs-v2

ВАЖНО (в отличие от DMarket): здесь нет инстант-селла по таргету — add-to-sale
это ЛИСТИНГ. Чтобы продать быстро, выставляем цену на/ниже текущего buy_order
(лучший чужой ордер на покупку) — сайты этого семейства матчат листинг против
buy-ордеров автоматически, если цена подходит. Гарантии мгновенной продажи нет.

Комиссия продавца НЕ задокументирована явно в API-доках — CUR_FEE_PCT ниже
взят как консервативная оценка (типично для этого семейства площадок,
5-7%), ТРЕБУЕТ ПРОВЕРКИ на реальной продаже перед тем как доверять марже.
"""
import os, time, requests
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

RUSTTM_API_KEY = os.getenv('RUSTTM_API_KEY', '')
RUSTTM_BASE    = "https://rust.tm/api/v2"
RUSTTM_FEE_PCT = float(os.getenv('RUSTTM_FEE_PCT', '5')) / 100  # подтверждено живой сделкой 2026-08-08: $0.435 -> receive $0.413 = ровно 5%

CUR_MULT = {"RUB": 100, "USD": 1000, "EUR": 1000}  # целые единицы для передачи цены в API


def _req(method: str, path: str, params: dict | None = None, json_body=None, retries: int = 0, timeout: int = 20):
    params = dict(params or {})
    params["key"] = RUSTTM_API_KEY
    attempt = 0
    while True:
        try:
            r = requests.request(method, f"{RUSTTM_BASE}/{path}", params=params, json=json_body, timeout=timeout)
            break
        except requests.exceptions.RequestException:
            if attempt >= retries:
                raise
            time.sleep(1 + attempt)
            attempt += 1
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:300]


# ─── Цены (публично, без ключа) ────────────────────────────────────────────────

def get_prices(currency: str = "USD") -> dict[str, dict]:
    """
    {market_hash_name: {"price": float, "buy_order": float, "classid_instance": str}}
    price — минимальный текущий листинг; buy_order — лучший чужой ордер на покупку
    (по нему можно продать мгновенно, если выставиться на/ниже него).

    Санити-фильтр (подтверждено вживую ранее, скриншотом с сайта): если
    buy_order > price (текущий лучший листинг), это висящая нерабочая заявка,
    а не реально исполнимый ордер — иначе сделка уже сматчилась бы сама
    (bid>=ask обязаны схлопнуться). Пример: Precious Antiques Facemask —
    листинг $1.63, "заявка" $2.413, годами не исполняется. Такие buy_order
    пропускаем (result[name]["buy_order"] будет 0 — как "нет ордера").
    """
    r = requests.get(f"https://rust.tm/api/v2/prices/class_instance/{currency}.json", timeout=20)
    if r.status_code != 200:
        return {}
    d = r.json()
    if not d.get("success"):
        return {}
    result = {}
    for key, it in d.get("items", {}).items():
        name = it.get("market_hash_name", "")
        if not name:
            continue
        price = float(it.get("price") or 0)
        buy_order = float(it.get("buy_order") or 0)
        result[name] = {
            "price":            price,
            "buy_order":        buy_order,
            "classid_instance": key,
        }
    # The class_instance feed can expose the lowest order when several price
    # levels exist. The dedicated orders feed is the authoritative aggregated
    # list and its `price` is the maximum order shown on the item page.
    orders = get_buy_orders(currency)
    for name, row in result.items():
        order = orders.get(name) or {}
        row["buy_order"] = float(order.get("price") or 0)
        row["buy_order_volume"] = int(order.get("volume") or 0)
        if row["buy_order"] and row["price"] and row["buy_order"] > row["price"]:
            row["buy_order"] = 0.0  # битый/висящий ордер, не исполнится
    return result


def get_buy_orders(currency: str = "USD") -> dict[str, dict]:
    """Return the maximum public buy order and aggregate volume by item name."""
    try:
        r = requests.get(f"https://rust.tm/api/v2/prices/orders/{currency}.json", timeout=20)
    except requests.exceptions.RequestException:
        return {}
    if r.status_code != 200:
        return {}
    try:
        data = r.json()
    except Exception:
        return {}
    if not isinstance(data, dict) or not data.get("success"):
        return {}
    result = {}
    for item in data.get("items", []):
        name = str(item.get("market_hash_name") or "")
        if not name:
            continue
        price = float(item.get("price") or 0)
        volume = int(item.get("volume") or 0)
        previous = result.get(name)
        if previous is None or price > previous["price"]:
            result[name] = {"price": price, "volume": volume}
    return result


# ─── Аккаунт ────────────────────────────────────────────────────────────────────

def get_balance() -> float:
    s, d = _req("GET", "get-money", retries=2)
    if s == 200 and isinstance(d, dict) and d.get("success"):
        return float(d.get("money", 0))
    return -1.0


def _get_steam_access_token() -> str | None:
    """
    Токен для ping-new — берётся из steamLoginSecure через официальный метод
    Steam (ajaxgetasyncconfig), не из нашего API-ключа. Живёт ~24ч.
    """
    import steam_login as sl
    sl.get_steam_session()
    cookie = sl._cookies.get("steamLoginSecure")
    if not cookie:
        return None
    r = requests.get("https://steamcommunity.com/pointssummary/ajaxgetasyncconfig",
                     headers={"Cookie": f"steamLoginSecure={cookie}"}, timeout=15)
    if r.status_code != 200:
        return None
    d = r.json()
    if not d.get("success"):
        return None
    return d.get("data", {}).get("webapi_token")


def ping() -> bool:
    """
    Держит продажи активными — слать раз в ~3 мин, иначе листинги перестают
    быть видны покупателям. Старый GET-метод "ping" ОТКЛЮЧЁН на стороне
    rust.tm (подтверждено живьём 2026-08-08: {"success": false, "message":
    "This method is deprecated..."}) — используем ping-new с Steam
    access_token вместо API-ключа.
    """
    token = _get_steam_access_token()
    if not token:
        print("[rust.tm] ping: не удалось получить Steam access_token")
        return False
    s, d = _req("POST", "ping-new", json_body={"access_token": token}, retries=2)
    ok = s == 200 and isinstance(d, dict) and d.get("success", False) and d.get("online", False)
    if not ok:
        print(f"[rust.tm] ping-new failed: {s} {d}")
    return ok


def go_offline() -> bool:
    s, d = _req("GET", "go-offline")
    return s == 200 and isinstance(d, dict) and d.get("success", False)


# ─── Инвентарь и листинг ────────────────────────────────────────────────────────

def get_my_inventory(lang: str = "ru") -> list[dict]:
    """Предметы в Steam-инвентаре, ещё НЕ выставленные на продажу. id — steam assetid для add-to-sale."""
    s, d = _req("GET", "my-inventory/", params={"lang": lang}, retries=2)
    if s != 200 or not isinstance(d, dict) or not d.get("success"):
        return []
    return d.get("items", [])


def update_inventory() -> bool:
    s, d = _req("GET", "update-inventory")
    return s == 200 and isinstance(d, dict) and d.get("success", False)


def add_to_sale(steam_asset_id: str, price: float, currency: str = "USD") -> dict:
    """price — в валюте (доллары), сама переведёт в целые единицы API."""
    price_units = int(round(price * CUR_MULT[currency]))
    s, d = _req("GET", "add-to-sale", params={"id": steam_asset_id, "price": price_units, "cur": currency})
    if s == 200 and isinstance(d, dict) and d.get("success"):
        return {"success": True, "item_id": d.get("item_id")}
    return {"success": False, "data": d}


def set_price(item_id: str, price: float, currency: str = "USD") -> dict:
    """price=0 снимает с продажи."""
    price_units = int(round(price * CUR_MULT[currency])) if price else 0
    s, d = _req("GET", "set-price", params={"item_id": item_id, "price": price_units, "cur": currency})
    return {"success": s == 200 and isinstance(d, dict) and d.get("success", False), "data": d}


def get_items() -> list[dict]:
    """
    Статусы: 1=на продаже, 2=продан-надо передать боту, 3=ждём передачи от продавца
    (нам как покупателю), 4=готово к получению, 7=ждём принятия покупателем.
    """
    s, d = _req("GET", "items", retries=2)
    if s != 200 or not isinstance(d, dict) or not d.get("success"):
        return []
    return d.get("items", [])


# ─── Передача проданных предметов боту rust.tm ─────────────────────────────────

def trade_request_give() -> dict:
    """
    Данные для создания трейда: кому (partner steamid) отдать проданные
    предметы. НЕ РАБОТАЕТ на rust.tm (подтверждено живьём 2026-08-08:
    {"success": false, "message": "for CS:GO use: trade-request-give-p2p"})
    — площадка отдаёт предметы напрямую покупателю, не через своего бота.
    Используй trade_request_give_p2p().
    """
    s, d = _req("GET", "trade-request-give")
    if s == 200 and isinstance(d, dict) and d.get("success"):
        return d
    return {"success": False, "data": d}


def trade_request_give_p2p() -> dict:
    """
    Данные для передачи ОДНОГО проданного предмета напрямую покупателю —
    partner (accountid32) + token + список items для create_trade_offer().
    """
    s, d = _req("GET", "trade-request-give-p2p")
    if s == 200 and isinstance(d, dict) and d.get("success"):
        return d
    return {"success": False, "data": d}


def trade_request_give_p2p_all() -> dict:
    """То же самое, но сразу для ВСЕХ проданных предметов — {"success", "offers": [...]}."""
    s, d = _req("GET", "trade-request-give-p2p-all")
    if s == 200 and isinstance(d, dict) and d.get("success"):
        return d
    return {"success": False, "data": d}


def trade_ready(tradeoffer_id: str) -> dict:
    """Регистрирует у rust.tm созданный нами в Steam трейд-оффер."""
    s, d = _req("GET", "trade-ready", params={"tradeoffer": tradeoffer_id})
    return d if isinstance(d, dict) else {"success": False, "data": d}


# ─── Покупка (не используется в схеме DMarket->rust.tm, но пригодится) ─────────

def buy(market_hash_name: str, price_units: int, custom_id: str = "") -> dict:
    params = {"hash_name": market_hash_name, "price": price_units}
    if custom_id:
        params["custom_id"] = custom_id
    s, d = _req("GET", "buy", params=params)
    return d if isinstance(d, dict) else {"success": False, "data": d}
