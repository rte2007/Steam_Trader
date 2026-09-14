# -*- coding: utf-8 -*-
"""DMarket API — цены, депозит, instant sell."""
import os, json, time, uuid, math, requests
from urllib.parse import quote, unquote
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

_full_key   = os.getenv('DM_PRIVATE_KEY', '').strip()
DM_PUB      = os.getenv('DM_PUBLIC_KEY', '').strip()
_priv       = None
if _full_key:
    try:
        _priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(_full_key[:64]))
        # Older deployments store private+public as one 128-char value.
        if not DM_PUB and len(_full_key) >= 128:
            DM_PUB = _full_key[64:128]
    except (TypeError, ValueError):
        _priv = None
DM_BASE     = "https://api.dmarket.com"
UA          = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DM_FEE      = float(os.getenv('DM_FEE_PCT', '5')) / 100


def net_after_fee(price_usd: float) -> float:
    """Conservative DMarket net, including fee rounding up to whole cents."""
    price_cents = int(round(float(price_usd) * 100))
    fee_cents = int(math.ceil(price_cents * DM_FEE - 1e-12))
    return (price_cents - fee_cents) / 100


def _sign(method: str, path: str, body: str = "") -> dict:
    if _priv is None or not DM_PUB:
        raise RuntimeError('DM_PRIVATE_KEY/DM_PUBLIC_KEY are not configured correctly')
    ts  = str(int(time.time()))
    sig = _priv.sign((method + path + body + ts).encode()).hex()
    return {
        "User-Agent":    UA,
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "X-Api-Key":     DM_PUB,
        "X-Sign-Date":   ts,
        "X-Request-Sign": f"dmar ed25519 {sig}",
    }


def _req(method: str, path: str, body: dict | None = None, timeout: int = 20, retries: int = 0):
    """
    retries — сколько ДОПОЛНИТЕЛЬНЫХ попыток сделать при сетевом сбое
    (таймаут/обрыв соединения), с коротким backoff. По умолчанию 0 — старое
    поведение, потому что слепой ретрай POST'а не всегда безопасен (buy_offer/
    create_offer/deposit_assets могли уже исполниться на сервере, просто
    ответ не успел дойти за timeout — повтор рискует задвоить действие).
    Передавай retries>0 ЯВНО только для GET и для заведомо идемпотентных POST
    (например user-inventory/sync — просто просит пересканировать инвентарь,
    повторный вызов ничего не портит). Добавлено 2026-08-06 — DMarket API
    минут на 15 подряд не укладывался в 20с таймаут, и КАЖДЫЙ вызов get_balance/
    fetch_prices/sync ронял цикл бота с одной и той же сетевой ошибкой.
    """
    body_str = json.dumps(body, separators=(',', ':')) if body else ''
    # DMarket подписывает ДЕКОДИРОВАННЫЙ path-сегмент (пробелы/спецсимволы как
    # есть — подтверждено на targets-by-title/{title}), НО query-строку — как
    # есть, percent-encoded (подтверждено на user/inventory?title=...%20...).
    # Раздельно: decode только часть до "?", после — не трогаем.
    path_part, sep, query_part = path.partition("?")
    sign_path = unquote(path_part) + sep + query_part
    fn = getattr(requests, method.lower())

    attempt = 0
    while True:
        headers = _sign(method, sign_path, body_str)  # ts в подписи — пересчитываем на каждой попытке
        try:
            r = fn(DM_BASE + path, headers=headers, data=body_str or None, timeout=timeout)
            break
        except requests.exceptions.RequestException:
            if attempt >= retries:
                raise
            time.sleep(1 + attempt)  # 1с, 2с, ...
            attempt += 1
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:300]


# ─── Баланс ───────────────────────────────────────────────────────────────────

def get_balance() -> dict:
    """{"usd": float, "usd_available": float} — поле 'usd' от DMarket в центах."""
    s, d = _req("GET", "/account/v1/balance", retries=2)
    if s == 200 and isinstance(d, dict):
        return {
            "usd":           int(d.get("usd", 0)) / 100,
            "usd_available": int(d.get("usdAvailableToWithdraw", 0)) / 100,
        }
    return {"usd": 0.0, "usd_available": 0.0}


# ─── Цены ─────────────────────────────────────────────────────────────────────

def fetch_prices(game: str = "rust") -> dict[str, dict]:
    """Возвращает {title: {order, offer, order_cnt, offer_cnt}}."""
    path      = "/marketplace-api/v1/aggregated-prices"
    all_items = []
    cursor    = None

    while True:
        body = {"filter": {"game": game}, "limit": 1000, "currency": "USD"}
        if cursor:
            body["cursor"] = cursor
        s, d = _req("POST", path, body, retries=2)  # чтение-поиск, без побочных эффектов — ретрай безопасен
        if s != 200:
            print(f"[dmarket] aggregated-prices error {s}: {json.dumps(d)[:80]}")
            break
        items  = d.get("aggregatedPrices", [])
        all_items.extend(items)
        cursor = d.get("nextCursor", "")
        if not cursor or len(items) < 1000:
            break
        time.sleep(0.2)

    result = {}
    for item in all_items:
        title = item.get("title", "")
        if not title:
            continue
        op = item.get("orderBestPrice") or {}
        fp = item.get("offerBestPrice") or {}
        result[title] = {
            "order":     int(op.get("Amount") or 0) / 100.0,
            "offer":     int(fp.get("Amount") or 0) / 100.0,
            "order_cnt": int(item.get("orderCount") or 0),
            "offer_cnt": int(item.get("offerCount") or 0),
        }
    print(f"[dmarket] fetched {len(result)} {game} items")
    return result


# ─── Реальная ⚡ instant-sell цена ────────────────────────────────────────────
# orderBestPrice из aggregated-prices — это агрегированный/кэшированный buy-order,
# который может уже не соответствовать реально исполнимому ордеру (пример:
# Twisted Furnace показывал orderBestPrice=$0.62, но реальный ⚡=$0.38, потому
# что лучший таргет был в статусе "OrderDisabled" и реально не исполнялся бы).
# Официальный эндпоинт для живого списка buy-order'ов (targets) по title:
#   GET /marketplace-api/v1/targets-by-title/{game_id}/{title}
# Возвращает {"orders": [{"amount", "price" (в центах!), "title", "attributes"}]}.
def get_best_target(title: str, game: str = "rust") -> dict | None:
    """Return the best currently executable aggregated buy-order row.

    DMarket's public endpoint intentionally does not expose somebody else's
    target id.  It returns price/amount/attributes.  Matching is performed by
    creating an offer at that exact live price and is considered complete only
    after closed-offer history contains a successful trade with ``TargetID``.
    """
    path = f"/marketplace-api/v1/targets-by-title/{game}/{quote(title)}"
    s, d = _req("GET", path, retries=2)
    if s != 200 or not isinstance(d, dict):
        return None
    candidates = []
    for order in d.get("orders", []):
        try:
            price_cents = int(order.get("price", 0))
            amount = int(order.get("amount", 0))
        except (TypeError, ValueError):
            continue
        if price_cents > 0 and amount > 0:
            candidates.append({
                "price_cents": price_cents,
                "price": price_cents / 100.0,
                "amount": amount,
                "attributes": order.get("attributes") or {},
            })
    return max(candidates, key=lambda row: row["price_cents"], default=None)


def get_target_price(title: str, game: str = "rust") -> float | None:
    """
    Лучшая ЖИВАЯ цена buy-order (Target) по title — то, что реально
    сработает при instant sell. В отличие от orderBestPrice (aggregated-prices),
    который бывает устаревшим/недостижимым.
    Возвращает цену в USD или None (нет активных ордеров / ошибка).
    """
    target = get_best_target(title, game)
    return target["price"] if target else None


# ─── Пользовательские предметы в DMarket ─────────────────────────────────────

def get_user_offers(game: str = "rust") -> list[dict]:
    """
    Предметы пользователя, выставленные в exchange.
    /exchange/v1/user/offers отдаёт 410 Gone (retired) — заменён на
    /marketplace-api/v2/user/offers (другая форма ответа: {"items":[...]}
    вместо {"objects":[...]}, и без itemId/classId — только offerId/priceCents/
    attributes.title).
    """
    s, d = _req("GET", f"/marketplace-api/v2/user/offers?gameId={game}&limit=100", retries=2)
    if s == 200 and isinstance(d, dict):
        return d.get("items", [])
    return []


# ─── Instant sell ─────────────────────────────────────────────────────────────
# /exchange/v1/instant-sale (старый) НЕ подтверждён живым тестом — эндпоинт
# отвечает (не 404/410), но реальную продажу так и не вызвал в проверке
# 2026-07-23/24. Реально подтверждённый рабочий v2-эндпоинт для выставления
# оффера — POST /marketplace-api/v2/offers:batchCreate ({"assetId","priceCents"}).
# ВАЖНО: даже он не гарантированно исполняется МГНОВЕННО как настоящий
# instant-sell (в тесте оффер повис как обычный, не исполнился сам за ~90 сек;
# реальная продажа того предмета в итоге случилась одновременно с остальным
# инвентарём — похоже, через отдельное действие на сайте, не через этот вызов).
# Поэтому create_offer() создаёт РЕАЛЬНЫЙ resting-оффер по live-цене (который
# должен рано или поздно исполниться, если цена не хуже таргета), а
# find_recent_sale() — единственный надёжный способ подтвердить факт продажи.

def create_offer(item_id: str, price_cents: int) -> dict:
    """
    Выставить предмет на продажу по цене price_cents (в центах).
    item_id — DMarket UUID предмета (DmarketAssetID из deposit-status).
    """
    s, d = _req("POST", "/marketplace-api/v2/offers:batchCreate",
               {"requests": [{"assetId": item_id, "priceCents": price_cents}]})
    if s == 200 and isinstance(d, dict) and d.get("offers"):
        return {"success": True, "offer_id": d["offers"][0].get("id"), "data": d}
    return {"success": False, "offer_id": None, "data": d}


def create_order_matching_offer(item_id: str, title: str,
                                minimum_price_cents: int = 0,
                                game: str = "rust") -> dict:
    """Match a *live* DMarket buy order, never a storefront listing.

    The live order is fetched again immediately before the write.  A successful
    batchCreate response only means the offer was accepted; it does *not* mean
    the order filled.  The caller must confirm the closed sale with
    :func:`find_recent_sale(require_target=True)`.
    """
    target = get_best_target(title, game)
    if not target:
        return {"success": False, "offer_id": None, "error": "no_live_target"}
    if target["price_cents"] < int(minimum_price_cents or 0):
        return {
            "success": False,
            "offer_id": None,
            "error": "target_below_floor",
            "target": target,
        }
    result = create_offer(item_id, target["price_cents"])
    result["target"] = target
    return result


def get_offers(game: str, title: str, limit: int = 20) -> list[dict]:
    """
    Реальные лоты (asks) на покупку конкретного title. Каждый лот содержит
    offerId (для buy_offer()) и attributes.id — НАСТОЯЩИЙ UUID предмета
    (в отличие от /marketplace-api/v2/user/inventory, где attributes.id —
    составная строка, непригодная для create_offer/withdraw-assets).
    """
    s, d = _req("GET", f"/marketplace-api/v2/offers?gameId={game}&title={quote(title)}&limit={limit}", retries=2)
    if s != 200 or not isinstance(d, dict):
        return []
    return d.get("items", [])


def buy_offer(offer_id: str, price_cents: int) -> dict:
    """
    Купить конкретный лот (offerId из get_offers()) по цене price_cents —
    сервер сам откажет, если цена лота уже изменилась (защита от гонки).
    """
    s, d = _req("PATCH", "/exchange/v1/offers-buy", {
        "offers": [{"offerId": offer_id, "price": {"amount": str(price_cents), "currency": "USD"}, "type": "dmarket"}]
    })
    if s == 200:
        return {"success": True, "data": d}
    return {"success": False, "data": d}


def withdraw_asset(asset_id: str, game: str, class_id: str) -> dict:
    """
    Вывести предмет с DMarket в Steam (обратная операция депозиту).
    asset_id — НАСТОЯЩИЙ UUID предмета (attributes.id из get_offers()/только
    что купленного через buy_offer() — НЕ из /marketplace-api/v2/user/inventory,
    там для старых предметов составная строка вместо UUID, withdraw её не примет).
    Подтверждено живым тестом 2026-07-26: работает, приходит реальный трейд в Steam.
    """
    req_id = str(uuid.uuid4())
    s, d = _req("POST", "/exchange/v1/withdraw-assets", {
        "assets": [{"id": asset_id, "gameId": game, "classId": class_id}],
        "requestId": req_id,
    })
    if s == 200 and isinstance(d, dict) and d.get("transferId"):
        return {"success": True, "transfer_id": d["transferId"], "data": d}
    return {"success": False, "transfer_id": None, "data": d}


def get_withdraw_status(transfer_id: str) -> dict:
    """
    Статус вывода. Не найден задокументированный способ получить Steam
    trade offer ID из этого статуса напрямую (в отличие от deposit-status) —
    трейд нужно ловить через steam_login.get_trade_offers() отдельно.
    """
    s, d = _req("GET", f"/marketplace-api/v1/withdraw-status/{transfer_id}")
    if s != 200 or not isinstance(d, dict):
        return {"status": "Error", "error": f"HTTP {s}: {d}"}
    return {"status": d.get("Status", "Unknown"), "error": d.get("Error", "")}


def update_offer(offer_id: str, price_cents: int) -> dict:
    """
    Изменить цену уже выставленного оффера. ВАЖНО: ~15 мин после создания
    (или предыдущего обновления/удаления) оффер под AssetTimeLocked — вызов
    вернёт success=False с этим кодом, если ещё рано.
    """
    s, d = _req("POST", "/marketplace-api/v2/offers:batchUpdate",
               {"requests": [{"offerId": offer_id, "priceCents": price_cents}]})
    if s == 200 and isinstance(d, dict) and d.get("offers"):
        # DMarket не правит оффер на месте — batchUpdate гасит старый и создаёт
        # новый с новым offerId (тот же assetId). Возвращаем его вызывающему,
        # иначе следующий update_offer/find по старому id промахнётся.
        new_id = d["offers"][0].get("id")
        return {"success": True, "data": d, "offer_id": new_id}
    return {"success": False, "data": d}


def instant_sell(item_id: str, class_id: str, price_cents: int,
                 game_id: str = "rust") -> dict:
    """
    УСТАРЕВШИЙ/неподтверждённый способ — оставлен для обратной совместимости,
    но не используется в arb_bot_ls.py (см. create_offer() выше).
    """
    body = {
        "asset": {
            "itemId":   item_id,
            "classId":  class_id,
            "gameId":   game_id,
            "price":    {"amount": str(price_cents), "currency": "USD"},
            "currency": "USD",
        },
        "donate": False,
    }
    s, d = _req("POST", "/exchange/v1/instant-sale", body)
    return {"status": s, "data": d, "success": s == 200}


def lower_offer_to_target(offer_id: str, item_id: str, target_price_cents: int) -> dict:
    """Снизить цену оффера до Target цены (чтобы сработал instant sell)."""
    body = {
        "Offer": {
            "offerId":  offer_id,
            "itemId":   item_id,
            "price":    {"amount": str(target_price_cents), "currency": "USD"},
        }
    }
    s, d = _req("PATCH", "/exchange/v1/offers", body)
    return {"status": s, "data": d, "success": s == 200}


# ─── Депозит ──────────────────────────────────────────────────────────────────
# Официальный документированный эндпоинт (проверен живым запросом — отдаёт
# осмысленные семантические ошибки, а не 401/404/410):
#   POST /marketplace-api/v1/deposit-assets  {"AssetID": ["<steam asset id>", ...]}
#   → {"DepositID": "..."}
#   GET  /marketplace-api/v1/deposit-status/{DepositID}
#   → {"Status": "TransferStatus...", "Assets": [{"InGameAssetID","DmarketAssetID"}],
#      "SteamDepositInfo": {"TradeOfferID","Message"}, "Error": "..."}
# Старые /exchange/v1/deposit и /marketplace-api/v1/deposit (использовались
# раньше) — не подтверждены официальной документацией, заменены этим.

STEAM_APP_ID = "252490"  # Rust


def build_deposit_asset_id(class_id: str, steam_asset_id: str, app_id: str = STEAM_APP_ID) -> str:
    """
    deposit-assets ждёt составной ID "0:{classId}:{steamAssetId}:{appId}"
    (тот же формат, что attributes.inGameAssetId в /marketplace-api/v2/user/inventory) —
    НЕ голый steamAssetId. Пример в докe DMarket с UUID-подобными строками вводит в
    заблуждение — это просто обфусцированный плейсхолдер, не реальный формат.
    Голый steamAssetId даёт "InventoryItemsNotFound" на этапе валидации запроса.
    """
    return f"0:{class_id}:{steam_asset_id}:{app_id}"


def deposit_assets(assets: list[dict], app_id: str = STEAM_APP_ID) -> dict:
    """
    Отправить предметы из Steam-инвентаря в DMarket.
    assets = [{"assetId": "<steamAssetId>", "classId": "..."}]
    app_id — Steam appid игры; по умолчанию Rust (252490), чтобы не менять
    поведение существующих вызовов. Для TF2 передавать "440".
    Возвращает {"success", "deposit_id", "data"}.
    """
    asset_ids = [build_deposit_asset_id(a["classId"], a["assetId"], app_id) for a in assets]
    s, d = _req("POST", "/marketplace-api/v1/deposit-assets", {"AssetID": asset_ids})
    if s == 200 and isinstance(d, dict) and d.get("DepositID"):
        return {"success": True, "deposit_id": d["DepositID"], "data": d}
    return {"success": False, "deposit_id": None, "data": d}


def get_uninvested_steam_items(game: str = "rust", limit: int = 100) -> dict[str, dict]:
    """
    Предметы в Steam-инвентаре, ещё НЕ задепонированные на DMarket
    (inMarket=false), полученные через DMarket API — НЕ через
    steamcommunity.com (который часто ловит 429 и не связан с нашим Steam
    login). Надёжнее и быстрее, чем steam_login.get_inventory().
    Возвращает {title: {steam_asset_id, class_id, tradable}}.
    """
    result = {}
    for it in get_uninvested_steam_assets(game, limit):
        result.setdefault(it["title"], it)   # первый попавшийся экземпляр
    return result


def get_uninvested_steam_assets(game: str = "rust", limit: int = 100) -> list[dict]:
    """
    ВСЕ незадепонированные предметы списком: [{title, steam_asset_id, class_id}].

    Нужна отдельно от get_uninvested_steam_items(), потому что тот кладёт
    результат в словарь ПО НАЗВАНИЮ и два одинаковых предмета схлопываются
    в одну запись. Пока бот покупал разные тайтлы, это не мешало, но с
    окном перекупки в 10 минут дубликаты стали нормой, и вылезло сразу два
    эффекта (подтверждено живьём 2026-07-31):
      * второй экземпляр становился НЕВИДИМЫМ и висел в Steam вечно
        (2x Bombshell Armored Door, Kraken Shell Facemask, Suitor Burlap Shirt);
      * при депозите брался assetId уже отправленного экземпляра, и DMarket
        отвечал InventoryItemsNotFound.
    """
    s, d = _req("GET", f"/marketplace-api/v2/user/inventory?gameId={game}&limit={limit}", retries=2)
    if s != 200 or not isinstance(d, dict):
        return []
    out = []
    for it in d.get("items", []):
        if it.get("inMarket"):
            continue
        a = it.get("attributes", {})
        title = a.get("title", "")
        if not title or not a.get("tradable"):
            continue
        out.append({
            "title":          title,
            "steam_asset_id": a.get("steamAssetId", ""),
            "class_id":       a.get("classId", ""),
        })
    return out


def get_unlisted_items(game: str, title: str, limit: int = 100) -> list[str]:
    """
    ID предметов УЖЕ на DMarket (купленных напрямую, не через депозит из
    Steam) с точным совпадением title, ещё НЕ выставленных на продажу
    (inMarket=false). attributes.id — готовый составной ID для create_offer()
    (тот же формат, что и DmarketAssetID из deposit-status, но для предметов,
    которые уже были в DMarket-инвентаре, а не только что задепонированы).
    """
    s, d = _req("GET", f"/marketplace-api/v2/user/inventory?gameId={game}&limit={limit}", retries=2)
    if s != 200 or not isinstance(d, dict):
        return []
    return [
        it["attributes"]["id"]
        for it in d.get("items", [])
        if not it.get("inMarket") and it.get("attributes", {}).get("title") == title
    ]


def get_deposit_status(deposit_id: str) -> dict:
    """
    Проверить статус депозита. dmarket_asset_id (когда Status=TransferStatusSuccess)
    — это itemId для instant_sell()/lower_offer_to_target().
    """
    s, d = _req("GET", f"/marketplace-api/v1/deposit-status/{deposit_id}", retries=2)
    if s != 200 or not isinstance(d, dict):
        return {"status": "Error", "assets": [], "steam_trade_offer_id": None,
                "error": f"HTTP {s}: {d}"}
    assets = [
        {"in_game_asset_id": a.get("InGameAssetID"), "dmarket_asset_id": a.get("DmarketAssetID")}
        for a in (d.get("Assets") or [])
    ]
    steam_info = d.get("SteamDepositInfo") or {}
    return {
        "status":               d.get("Status", "Unknown"),
        "assets":               assets,
        "steam_trade_offer_id": steam_info.get("TradeOfferID"),
        "error":                d.get("Error", ""),
    }


def find_recent_sale(title: str, since_ts: float, limit: int = 20,
                     asset_id: str | None = None,
                     require_target: bool = False) -> dict | None:
    """
    Проверить последние закрытые офферы (/marketplace-api/v1/user-offers/closed) —
    вдруг предмет уже продался другим путём (например через сайт DMarket, минуя
    наш deposit_assets/instant_sell — так уже бывало: депозит через API падал в
    InventoryRevoked, а предмет тем временем продавался через кнопку "Продать
    сейчас" на сайте). Возвращает {"price", "fee", "closed_at"} или None.
    """
    s, d = _req("GET", f"/marketplace-api/v1/user-offers/closed?Limit={limit}&OrderDir=desc", retries=2)
    if s != 200 or not isinstance(d, dict):
        return None
    for t in d.get("Trades", []):
        if t.get("Title") != title or t.get("Status") != "successful":
            continue
        if asset_id and str(t.get("AssetID") or "") != str(asset_id):
            continue
        target_id = t.get("TargetID") or t.get("targetId")
        if require_target and not target_id:
            continue
        closed_at = float(t.get("OfferClosedAt", 0))
        if closed_at < since_ts:
            continue
        price = (t.get("Price") or {}).get("Amount", 0)
        fee = ((t.get("Fee") or {}).get("Amount") or {}).get("Amount", 0)
        return {"price": float(price), "fee": float(fee), "closed_at": closed_at,
                "asset_id": t.get("AssetID"), "target_id": target_id}
    return None


def wait_for_deposit(deposit_id: str, poll_sec: int = 15, max_wait: int = 900) -> dict | None:
    """
    Ждём завершения депозита (TransferStatusSuccess). Возвращает результат
    get_deposit_status() с заполненным assets[].dmarket_asset_id, либо None
    при ошибке/таймауте.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        st = get_deposit_status(deposit_id)
        status = st["status"]
        if status == "TransferStatusSuccess":
            return st
        if status in ("TransferStatusFailedToCreate", "TransferStatusError"):
            print(f"[dmarket] депозит {deposit_id} не удался: {st['error']}")
            return None
        time.sleep(poll_sec)
    print(f"[dmarket] депозит {deposit_id}: таймаут ожидания ({max_wait}с)")
    return None


def watch_user_inventory_for_item(item_name: str, poll_sec: int = 30,
                                   max_wait: int = 600) -> dict | None:
    """
    Ждём появления предмета в пользовательских офферах DMarket.
    Возвращает offer dict или None.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        offers = get_user_offers()
        for o in offers:
            if o.get('title', '') == item_name:
                return o
        time.sleep(poll_sec)
    return None
