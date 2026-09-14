# -*- coding: utf-8 -*-
"""
Steam login через pysteamauth (правильный, современный Steam auth flow —
IAuthenticationService, не устаревший /login/dologin/, который стабильно
ловит 429 и был сломан 24+ часа подряд 2026-07-23/24). Портировано с
рабочей реализации на VPS (arb_bot.py, cstrade-проект).

Логин и получение cookies — единственная async-часть (через asyncio.run());
все остальные вызовы (accept trade, mobileconf) — обычный синхронный
requests с закэшированными cookies, чтобы не тащить asyncio по всему коду.
"""
import os, time, json, asyncio, requests
from dotenv import load_dotenv
from pysteamauth.auth import Steam
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

STEAM_LOGIN    = os.getenv('STEAM_LOGIN', '')
STEAM_PASS     = os.getenv('STEAM_PASSWORD', '')
STEAM_SHARED   = os.getenv('STEAM_SHARED_SECRET', '')
STEAM_IDENTITY = os.getenv('STEAM_IDENTITY_SECRET', '')
STEAM_ID       = os.getenv('STEAM_ID', '')
STEAM_API_KEY  = os.getenv('STEAM_API_KEY', '')

_API = "https://api.steampowered.com"
_COM = "https://steamcommunity.com"

_steam_obj    = None
_cookies: dict = {}
_login_ts     = 0.0
_mobileconf_cooldown: dict = {}   # offer_id -> last attempt ts (5 мин кулдаун)


def get_steam_session(force: bool = False) -> bool:
    """
    Логин через pysteamauth. Сессия живёт ~50 мин (Steam обычно инвалидирует
    раньше редко), затем сама обновляется при следующем вызове.
    """
    global _steam_obj, _cookies, _login_ts
    if not force and _cookies and time.time() - _login_ts < 3000:
        return True
    # ВАЖНО: пересоздаём Steam-объект заново на КАЖДЫЙ реальный (пере)логин,
    # а не только один раз. pysteamauth держит внутри aiohttp-сессию, которая
    # привязывается к event loop-у ПЕРВОГО asyncio.run() — при повторном
    # логине (когда кэш протух через ~50 мин) новый asyncio.run() создаёт
    # новый loop, а старая сессия внутри закэшированного _steam_obj пытается
    # писать в уже закрытый loop -> "RuntimeError: Event loop is closed".
    # Подтверждено живьём: 31 такой ошибки за ночь, ни один повторный логин
    # не прошёл, из-за чего accept_trade() падал на каждом новом трейде.
    _steam_obj = Steam(login=STEAM_LOGIN, password=STEAM_PASS,
                       shared_secret=STEAM_SHARED, identity_secret=STEAM_IDENTITY)

    async def _do():
        await _steam_obj.login_to_steam()
        raw = await _steam_obj.cookies()
        return {k: str(v) for k, v in raw.items() if not callable(v)}

    try:
        # Network stalls inside pysteamauth used to freeze the whole trading
        # loop indefinitely after a service restart. Bound the complete auth
        # flow so the caller can fall back to signal-only mode and retry.
        _cookies = asyncio.run(asyncio.wait_for(_do(), timeout=60))
        _login_ts = time.time()
        print(f"[steam] Logged in as {STEAM_LOGIN} via pysteamauth. Cookies: {list(_cookies.keys())}")
        return True
    except Exception as e:
        print(f"[steam] pysteamauth login failed: {e}")
        return False


def login() -> requests.Session:
    """
    Обратная совместимость со старым интерфейсом (steam_client.get_client()) —
    возвращает requests.Session с cookies от pysteamauth-логина.
    """
    if not get_steam_session():
        raise RuntimeError("Steam login failed (pysteamauth)")
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    for k, v in _cookies.items():
        s.cookies.set(k, v, domain="steamcommunity.com")
    return s


def _steam_headers() -> dict:
    return {
        "Cookie": "; ".join(f"{k}={v}" for k, v in _cookies.items()),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }


def get_trade_offers() -> list:
    """Get active incoming trade offers via Steam Web API (не требует логина)."""
    r = requests.get(
        f"{_API}/IEconService/GetTradeOffers/v1/",
        params={
            "key":              STEAM_API_KEY,
            "get_received_offers": 1,
            "active_only":         1,
            "get_descriptions":    1,
        },
        timeout=15,
    )
    data = r.json().get("response", {})
    offers = data.get("trade_offers_received", [])
    desc_map = {(d["classid"], d.get("instanceid","0")): d
                for d in data.get("descriptions", [])}
    for o in offers:
        names = []
        for item in o.get("items_to_receive", []):
            key = (item.get("classid",""), item.get("instanceid","0"))
            d = desc_map.get(key, {})
            names.append(d.get("market_hash_name") or d.get("name", "?"))
        o["_item_names"] = names
    return [o for o in offers if o.get("trade_offer_state") == 2]


# trade_offer_state: 2=Active, 6=Canceled, 7=Declined (см. Steam ETradeOfferState)
DECLINED_STATES = (6, 7)


def get_recent_offers(hours: int = 24) -> list:
    """
    Все входящие офферы (любой статус, не только активные) за последние N
    часов — нужно, чтобы заметить, если покупку отменили/отклонили (такой
    оффер пропадает из get_trade_offers(), но не исчезает из истории).
    """
    cutoff = int(time.time()) - hours * 3600
    r = requests.get(
        f"{_API}/IEconService/GetTradeOffers/v1/",
        params={
            "key":                    STEAM_API_KEY,
            "get_received_offers":    1,
            "get_descriptions":       1,
            "time_historical_cutoff": cutoff,
        },
        timeout=15,
    )
    data = r.json().get("response", {})
    offers = data.get("trade_offers_received", [])
    desc_map = {(d["classid"], d.get("instanceid","0")): d
                for d in data.get("descriptions", [])}
    for o in offers:
        names = []
        for item in o.get("items_to_receive", []):
            key = (item.get("classid",""), item.get("instanceid","0"))
            d = desc_map.get(key, {})
            names.append(d.get("market_hash_name") or d.get("name", "?"))
        o["_item_names"] = names
    return offers


def _mobile_confirm_offer(offer_id: str) -> bool:
    """
    Подтверждение трейда через steampy.confirmation.ConfirmationExecutor —
    готовая, проверенная реализация (правильный generate_device_id() для "p",
    сопоставление confirmation<->offer через HTML страницы деталей, а не
    просто creator_id). Первая версия (ручной HMAC + TOTP вместо device_id)
    падала с "Oh nooooooes!" — steampy делает это корректно.
    """
    if not _cookies:
        if not get_steam_session():
            return False
    from steampy.confirmation import ConfirmationExecutor, ConfirmationExpected

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    for k, v in _cookies.items():
        s.cookies.set(k, v, domain="steamcommunity.com")

    executor = ConfirmationExecutor(STEAM_IDENTITY, STEAM_ID, s)
    try:
        resp = executor.send_trade_allow_request(str(offer_id))
    except ConfirmationExpected:
        print(f"[steam] No mobileconf entry found for offer {offer_id}")
        return False
    except Exception as e:
        print(f"[steam] mobileconf error: {e}")
        return False

    ok = bool(resp.get("success"))
    print(f"[steam] Mobile confirm offer {offer_id}: {'OK' if ok else 'FAIL'} {resp}")
    return ok


def _confirm_sell_listing(asset_id: str) -> bool:
    """Подтверждение выставленного на продажу лота — тот же ConfirmationExecutor,
    что и для трейдов, но другой тип записи (steampy сам их различает)."""
    if not _cookies:
        if not get_steam_session():
            return False
    from steampy.confirmation import ConfirmationExecutor, ConfirmationExpected

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    for k, v in _cookies.items():
        s.cookies.set(k, v, domain="steamcommunity.com")

    executor = ConfirmationExecutor(STEAM_IDENTITY, STEAM_ID, s)
    try:
        resp = executor.confirm_sell_listing(str(asset_id))
    except ConfirmationExpected:
        print(f"[steam] No mobileconf entry found for sell listing {asset_id}")
        return False
    except Exception as e:
        print(f"[steam] sell mobileconf error: {e}")
        return False

    ok = bool(resp.get("success"))
    print(f"[steam] Mobile confirm sell {asset_id}: {'OK' if ok else 'FAIL'} {resp}")
    return ok


def sell_on_market(asset_id: str, appid: int, price_cents: int, contextid: int = 2) -> dict:
    """
    Выставить предмет на продажу на Steam Community Market (POST /market/sellitem/).
    price_cents — цена, которую заплатит покупатель (то же число, что показывает
    lowest_price/sell_price в публичных данных CM), Steam сам вычитает комиссию.
    Может потребовать mobile-подтверждение — обрабатывается так же, как у трейдов.
    """
    if not get_steam_session():
        return {"success": False, "error": "steam login failed"}
    try:
        r = requests.post(
            f"{_COM}/market/sellitem/",
            data={
                "sessionid": _cookies.get("sessionid", ""),
                "appid":     appid,
                "contextid": contextid,
                "assetid":   asset_id,
                "amount":    1,
                "price":     price_cents,
            },
            headers={
                **_steam_headers(),
                "Referer":          f"{_COM}/profiles/{STEAM_ID}/inventory",
                "Origin":           _COM,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15,
        )
        print(f"[steam] Sell item {asset_id} @ {price_cents}c: HTTP {r.status_code} | {r.text[:200]}")
        if r.status_code in (401, 403):
            get_steam_session(force=True)
            return {"success": False, "error": f"HTTP {r.status_code}"}
        if r.status_code != 200:
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

        resp = r.json() if r.content else {}
        if resp.get("needs_mobile_confirmation"):
            key = f"sell_{asset_id}"
            last_try = _mobileconf_cooldown.get(key, 0)
            if time.time() - last_try < 300:
                return {"success": False, "error": "mobileconf cooldown"}
            _mobileconf_cooldown[key] = time.time()
            ok = _confirm_sell_listing(asset_id)
            if ok:
                _mobileconf_cooldown.pop(key, None)
            return {"success": ok}

        return {"success": bool(resp.get("success"))}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_app_inventory(appid: int, contextid: int = 2, require_marketable: bool = True) -> list:
    """
    Инвентарь произвольной игры через авторизованную сессию. Нужен здесь,
    потому что Steam переназначает assetid при переходе предмета между
    инвентарями (см. get_inventory() выше) — после приёма трейда от mannco
    нужно заново найти актуальный assetid по имени перед выставлением на продажу.

    require_marketable: по умолчанию True (нужно для листинга на Steam
    Community Market). ОБНАРУЖЕНО ВЖИВУЮ 2026-08-13: свежепринятые трейдом
    TF2-предметы часто временно "Недоступен" на Steam Market (marketable=0,
    обычный маркет-холд), но при этом ПОЛНОСТЬЮ tradable — депозит на
    DMarket идёт обычным трейдом, а не через Market, и от marketable не
    зависит. Со старым жёстким фильтром такие предметы были невидимы этой
    функции, хотя реально лежали в инвентаре и были готовы к депозиту.
    Вызывающий код, который не листит на Steam Market (например депозит на
    DMarket), должен передавать require_marketable=False.

    ВАЖНО: новый /inventory/{steamid}/{appid}/{contextid} с авторизованными
    куками отдаёт 400 (проверено вживую 2026-08-09) — рабочий для залогиненной
    сессии оказался только старый /profiles/{steamid}/inventory/json/.../.
    """
    if not get_steam_session():
        return []
    try:
        r = requests.get(
            f"{_COM}/profiles/{STEAM_ID}/inventory/json/{appid}/{contextid}/",
            params={"trading": 1},
            headers=_steam_headers(),
            timeout=20,
        )
        if r.status_code == 429:
            print(f"[steam] 429 on inventory({appid}) — back off, don't retry immediately")
            return []
        data = r.json()
        if not isinstance(data, dict) or not data.get("success"):
            print(f"[steam] unexpected inventory({appid}) response (status={r.status_code}): {data}")
            return []
        # Steam отдаёт rgInventory/rgDescriptions как [] (список), а не {},
        # когда инвентарь пуст — .get(..., {}) не спасает, т.к. ключ
        # присутствует со значением []; используем `or {}`, чтобы привести
        # такой пустой список к пустому dict вместо падения на .items().
        assets = data.get("rgInventory") or {}
        descs  = data.get("rgDescriptions") or {}
        result = []
        for asset_id, a in assets.items():
            key = f'{a.get("classid","")}_{a.get("instanceid","0")}'
            d = descs.get(key, {})
            ok = str(d.get("tradable")) == "1" and (not require_marketable or str(d.get("marketable")) == "1")
            if ok:
                result.append({
                    "assetid":          asset_id,
                    "classid":          a.get("classid",""),
                    "market_hash_name": d.get("market_hash_name",""),
                })
        return result
    except Exception as e:
        print(f"[steam] get_app_inventory({appid}) error: {e}")
        return []


def accept_trade(offer_id: str, partner_steamid: str = "") -> bool:
    """Accept a trade offer через pysteamauth-сессию + mobileconf при необходимости."""
    if not get_steam_session():
        return False
    try:
        r = requests.post(
            f"{_COM}/tradeoffer/{offer_id}/accept",
            data={
                "sessionid":    _cookies.get("sessionid", ""),
                "serverid":     "1",
                "tradeofferid": offer_id,
                "partner":      partner_steamid,
                "captcha":      "",
            },
            headers={
                **_steam_headers(),
                "Referer":          f"{_COM}/tradeoffer/{offer_id}/",
                "Origin":           _COM,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15,
        )
        print(f"[steam] Accept offer {offer_id}: HTTP {r.status_code} | {r.text[:200]}")

        if r.status_code in (401, 403):
            print("[steam] Session expired, refreshing...")
            get_steam_session(force=True)
            return False
        if r.status_code != 200:
            return False

        resp = r.json() if r.content else {}
        if resp.get("needs_mobile_confirmation"):
            last_try = _mobileconf_cooldown.get(str(offer_id), 0)
            if time.time() - last_try < 300:
                print(f"[steam] Offer {offer_id}: mobileconf cooldown, skip for now")
                return False
            _mobileconf_cooldown[str(offer_id)] = time.time()
            ok = _mobile_confirm_offer(offer_id)
            if ok:
                _mobileconf_cooldown.pop(str(offer_id), None)
            return ok

        print(f"[steam] Trade {offer_id} accepted")
        return True
    except Exception as e:
        print(f"[steam] accept error: {e}")
        return False


def create_trade_offer(partner_accountid: int, token: str, asset_ids: list[str],
                       appid: int = 252490, contextid: int = 2, message: str = "") -> dict:
    """
    Создать и отправить ИСХОДЯЩИЙ трейд-оффер — в отличие от accept_trade()
    (только принимает входящие), нужен для площадок вроде rust.tm, где ПОСЛЕ
    продажи именно МЫ инициируем передачу предмета их боту (trade-request-give
    отдаёт partner accountid + token, а сам трейд создаём мы через этот метод).
    partner_accountid — 32-битный accountid (не steamid64!) — тот же формат,
    что Steam отдаёт в URL "tradeoffer/new/?partner=X&token=Y" и что rust.tm
    возвращает в поле "partner" своего trade-request-give-p2p.
    Возвращает {"success", "tradeofferid", "needs_mobile_confirmation", "error"}.
    """
    if not get_steam_session():
        return {"success": False, "error": "steam login failed"}
    partner_steamid64 = str(partner_accountid + 76561197960265728)
    payload = {
        "sessionid": _cookies.get("sessionid", ""),
        "serverid": "1",
        "partner": partner_steamid64,
        "tradeoffermessage": message,
        "json_tradeoffer": json.dumps({
            "newversion": True,
            "version": 4,
            "me":   {"assets": [{"appid": appid, "contextid": str(contextid), "amount": 1, "assetid": str(a)}
                                for a in asset_ids], "currency": [], "ready": False},
            "them": {"assets": [], "currency": [], "ready": False},
        }),
        "captcha": "",
        "trade_offer_create_params": json.dumps({"trade_offer_access_token": token}),
    }
    try:
        r = requests.post(
            f"{_COM}/tradeoffer/new/send",
            data=payload,
            headers={
                **_steam_headers(),
                "Referer": f"{_COM}/tradeoffer/new/?partner={partner_accountid}&token={token}",
                "Origin": _COM,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=20,
        )
        print(f"[steam] Create offer to {partner_steamid64}: HTTP {r.status_code} | {r.text[:200]}")
        if r.status_code in (401, 403):
            get_steam_session(force=True)
            return {"success": False, "error": f"HTTP {r.status_code}"}
        if r.status_code != 200:
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        resp = r.json() if r.content else {}
        oid = resp.get("tradeofferid")
        if not oid:
            return {"success": False, "error": resp}
        if resp.get("needs_mobile_confirmation"):
            _mobile_confirm_offer(oid)
        return {"success": True, "tradeofferid": oid,
                "needs_mobile_confirmation": resp.get("needs_mobile_confirmation", False)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_inventory() -> list:
    """Get Rust inventory via Steam public API (не требует логина)."""
    r = requests.get(
        f"{_COM}/inventory/{STEAM_ID}/252490/2",
        params={"l": "english", "count": 5000},
        timeout=20,
    )
    if r.status_code == 429:
        print("[steam] 429 Too Many Requests on inventory endpoint — back off, don't retry immediately")
        return []
    data = r.json()
    if not isinstance(data, dict):
        print(f"[steam] unexpected inventory response (status={r.status_code}): {data}")
        return []
    assets = data.get("assets", [])
    desc_map = {(d["classid"], d.get("instanceid","0")): d
                for d in data.get("descriptions", [])}
    result = []
    for a in assets:
        key = (a.get("classid",""), a.get("instanceid","0"))
        d = desc_map.get(key, {})
        if d.get("tradable"):
            result.append({
                "assetid":          a["assetid"],
                "classid":          a.get("classid",""),
                "market_hash_name": d.get("market_hash_name",""),
                "tradable":         1,
            })
    return result
