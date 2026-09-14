# -*- coding: utf-8 -*-
"""
lis-skins.com market API — получение цен и покупка.
"""
import os, time, requests
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

LS_API_KEY = os.getenv('LISSKINS_API_KEY', '')
LS_GAME    = os.getenv('LS_GAME', 'rust')
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
LS_BASE = "https://api.lis-skins.com"

_headers = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Authorization": f"Bearer {LS_API_KEY}" if LS_API_KEY else "",
}


def _parse_price(raw) -> float:
    """LS API возвращает цены в USD."""
    try:
        return round(float(raw), 4)
    except Exception:
        return 0.0


def fetch_market_items(game: str = LS_GAME,
                       min_price: float = 0.0,
                       max_price: float = 0.0,
                       target_names: set | None = None) -> list[dict]:
    """
    Returns [{id, name, price}] sorted by price ascending.
    min_price / max_price: price range filter.
    target_names: if provided, stop fetching once all names are found
                  (since API is sorted by lowest_price, first hit = cheapest listing).
    """
    url    = f"{LS_BASE}/v1/market/search"
    result = []
    seen_names: set = set()
    cursor = None
    page   = 0

    while True:
        params = {"game": game, "sort_by": "lowest_price", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        if min_price > 0:
            params["price_from"] = min_price
        if max_price > 0:
            params["price_to"] = max_price
        data = None
        for attempt in range(4):
            try:
                r = requests.get(url, params=params, headers=_headers, timeout=45)
                if r.status_code == 429:
                    fallback = 3 * (attempt + 1)
                    try:
                        wait = float(r.headers.get('Retry-After', fallback))
                    except ValueError:
                        wait = fallback
                    wait = max(wait, fallback)
                    print(f"[ls] page {page}: 429, ждём {wait:.0f}с (попытка {attempt+1}/4)")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                print(f"[ls] page {page} error: {e}")
                time.sleep(2)
        if data is None:
            print(f"[ls] page {page}: не удалось получить после повторов — останавливаю скан")
            break

        items = data.get("data") or []
        if not items:
            break

        page_min_price = None
        for it in items:
            name     = it.get("market_hash_name") or it.get("name", "")
            price    = _parse_price(it.get("price", 0))
            iid      = it.get("id") or it.get("skin_id") or it.get("item_id")
            class_id = str(it.get("item_class_id") or "")
            if not name or price <= 0 or not iid:
                continue
            if min_price > 0 and price < min_price:
                continue
            # Only keep cheapest listing per name (API is sorted asc so first = cheapest)
            if name not in seen_names:
                seen_names.add(name)
                result.append({"id": int(iid), "name": name, "price": price,
                               "class_id": class_id})
                if page_min_price is None or price < page_min_price:
                    page_min_price = price

        # Stop when every target name has been found
        if target_names and target_names.issubset(seen_names):
            print(f"[ls] all {len(target_names)} target names found — stopping early at page {page+1}")
            break

        # Stop when all items on this page are above budget
        if max_price > 0 and page_min_price is not None and page_min_price > max_price:
            print(f"[ls] page {page}: min price ${page_min_price:.2f} > max ${max_price:.2f} — stopping")
            break

        cursor = (data.get("meta") or {}).get("next_cursor") or data.get("next_cursor")
        page += 1
        if not cursor:
            break

    print(f"[ls] fetched {len(result)} unique items ({page} pages)")
    return result


def get_balance() -> float:
    """GET /v1/user/balance -> баланс в USD (API отдаёт уже в долларах, не в центах —
    в отличие от DMarket; подтверждено вживую 2026-07-27: значение 99.4 совпало
    1-в-1 с балансом на сайте)."""
    try:
        r = requests.get(f"{LS_BASE}/v1/user/balance", headers=_headers, timeout=15)
        if r.status_code == 200:
            return float(r.json().get("data", {}).get("balance", 0))
    except Exception as e:
        print(f"[ls] get_balance error: {e}")
    return -1.0


def build_price_map(items: list[dict]) -> dict[str, float]:
    """Из списка items → {name: lowest_price}."""
    pm: dict[str, float] = {}
    for it in items:
        name, price = it["name"], it["price"]
        if name not in pm or price < pm[name]:
            pm[name] = price
    return pm


def find_cheapest(name: str, items: list[dict]) -> dict | None:
    """Найти самый дешёвый лот по имени."""
    candidates = [it for it in items if it["name"] == name]
    if not candidates:
        return None
    return min(candidates, key=lambda x: x["price"])


def buy_item_api(item_id: int, expected_price: float,
                 partner: str = "", token: str = "",
                 custom_id: str = "", skip_unavailable: bool = False) -> dict:
    """
    Купить предмет(ы) через официальный LIS-SKINS API:
      POST https://api.lis-skins.com/v1/market/buy
    (см. https://lis-skins.stoplight.io/docs/lis-skins/f50c0w3odutep-buy-a-skin-s-for-a-specific-user)

    partner/token — параметры из Trade URL получателя (STEAM_PARTNER32 /
    STEAM_TRADE_TOKEN в .env, если не переданы явно).

    expected_price передаётся как max_price — сервер САМ откажет купить
    дороже этой цены. Это официально рекомендованная LIS-SKINS защита от
    роста цены между сканированием и покупкой (их /v1/market/search может
    отставать на несколько минут — см. "Skin Purchase Recommendations").

    custom_id — по рекомендации LIS-SKINS: при обрыве соединения НЕ повторяй
    покупку вслепую, сначала проверь get_purchase_info(custom_ids=[custom_id]).
    """
    partner = partner or os.getenv('STEAM_PARTNER32', '')
    token   = token or os.getenv('STEAM_TRADE_TOKEN', '')
    if not partner or not token:
        return {"ok": False, "status": 0,
                "body": "STEAM_PARTNER32/STEAM_TRADE_TOKEN не заданы в .env"}

    body = {
        "ids":       [item_id],
        "partner":   partner,
        "token":     token,
        "max_price": round(expected_price, 2),
    }
    if custom_id:
        body["custom_id"] = custom_id
    if skip_unavailable:
        body["skip_unavailable"] = True

    try:
        r = requests.post(f"{LS_BASE}/v1/market/buy", json=body,
                          headers=_headers, timeout=20)
        data = r.json() if r.content else {}
    except Exception as e:
        # По рекомендации LIS-SKINS: при сетевой ошибке проверь custom_id,
        # прежде чем считать покупку неудавшейся и повторять её.
        return {"ok": False, "status": 0, "body": str(e), "network_error": True}

    if r.status_code in (200, 201):
        return {"ok": True, "status": r.status_code, "body": data}
    return {"ok": False, "status": r.status_code, "body": data}


def get_purchase_info(custom_ids: list[str] | None = None,
                      purchase_ids: list[int] | None = None) -> list[dict]:
    """
    GET https://api.lis-skins.com/v1/market/info
    Проверить статус покупок по custom_id/purchase_id — используется после
    сетевой ошибки при buy_item_api(), чтобы не купить один и тот же предмет
    дважды (официальная рекомендация LIS-SKINS).
    """
    params = {}
    if custom_ids:
        params["custom_ids[]"] = custom_ids
    if purchase_ids:
        params["purchase_ids[]"] = purchase_ids
    try:
        r = requests.get(f"{LS_BASE}/v1/market/info", params=params,
                         headers=_headers, timeout=15)
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"[ls] get_purchase_info error: {e}")
    return []
