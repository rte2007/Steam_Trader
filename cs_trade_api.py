# -*- coding: utf-8 -*-
"""
cs.trade API — обмен ботами, для связки CS.trade <-> mannco на аккаунте
mannco_bot (Bubbletea). Отдельный модуль, mannco_api.py и dmarket_api.py не
трогает.

Авторизация: у cs.trade НЕТ отдельного API-ключа — вход через Steam OpenID
(тот же механизм, что "Войти через Steam" на сайте). Раньше в проекте это
делалось только браузером; здесь сделано полностью программно поверх уже
рабочей pysteamauth-сессии (steam_login.py):
  1) steam_login.get_steam_session() — логиним Steam, получаем cookies
     (sessionid, steamLoginSecure, ...).
  2) GET https://cs.trade/ru/login_steam с этими cookies — редиректит на
     steamcommunity.com/openid/login?...; страница уже показывает "залогинен
     как <ник>", отдаёт форму (action=steam_openid_login, openid.mode,
     openidparams, nonce).
  3) POST эту форму на https://steamcommunity.com/openid/login.
     КРИТИЧНО: форма enctype="multipart/form-data" — обычный
     x-www-form-urlencoded (requests data=...) даёт "Invalid Params". Нужно
     слать через files={k: (None, v)}, тогда requests сам поставит нужный
     Content-Type. Подтверждено вживую 2026-08-05.
  4) Steam делает 5 редиректов до https://cs.trade/ru/trade, попутно
     выставляя новый PHPSESSID у cs.trade — это и есть рабочая сессия.
PHPSESSID живёт как обычная PHP-сессия (часы, не дни) — перелогиниваемся
при первом же признаке разлогина (баланс/страница без ника).

Cloudflare: cs.trade прикрыт Cloudflare, голый requests получает "Attention
Required" (проверено). Все вызовы идут через cloudscraper.

Цены и лимиты:
  * GET /loadBotInventory/ru?order_by=price_desc&bot=all — ПУБЛИЧНЫЙ, без
    авторизации, отдаёт ВЕСЬ инвентарь бота разом (~36k лотов, все игры
    вперемешку — параметр game на сервере игнорируется, фильтровать по
    полю app_id самим). price = цена, которую МЫ платим, чтобы получить
    предмет от бота (для направления CS.trade -> mannco).
  * GET https://cdn.cs.trade/developer/api/prices_{TF2,RUST} — официальный
    прайс-лист по имени: {price, store_price, have, max, can_take,
    tradable, reservable, available_stock, ...}. `price` — их справочная
    оценка стоимости предмета (для направления mannco -> CS.trade, чтобы
    прикинуть, купит ли/сколько даст бот ЗА предмет, которого у нас пока
    нет). `max`/`can_take` > 0 — бот вообще готов принять депозит этого
    предмета сейчас. ВАЖНО: этот CDN-хост не резолвился с локальной
    машины при разработке, но с самой VPS работает нормально (проверено
    2026-08-05) — если код при локальном тесте падает с DNS-ошибкой на
    cdn.cs.trade, это окружение, не баг.
  * Комиссия по /ru/our-prices на 2026-08-05: 6-8% без подписки, 3-5% с
    Trader PRO (сейчас БЕСПЛАТНА для всех). Не зашито числом нигде —
    реальная комиссия видна по факту в ответе send_trade()/по факту
    полученного баланса, откалибровать после первой живой сделки (как
    было с DM_FEE_PCT).

Сама сделка (send_trade): контракт СОБРАН ПО СТАРЫМ ЗАМЕТКАМ (POST
/sendTrade, tt='s' и для дачи, и для получения предметов, поля
user_chosen_items/bot_chosen_items) и адаптирован под свежее подтверждённое
`urls['tradeUrl'] = "/sendTrade/ru"`. Реальный формат запроса (поля bot/tt/
user_chosen_items/bot_chosen_items/si/tm, массивы — ПОЛНЫЕ объекты предмета,
не голые id) вытащен 2026-08-05 из живого JS сайта через Playwright (dump
sendSingleTrade.toString() на аутентифицированной сессии) и **ПОДТВЕРЖДЁН
МНОГОКРАТНЫМИ живыми сделками** в обе стороны — суммы зачисления сверены
напрямую с реальным балансом CS.trade, совпадают до цента.
"""
import os, re, json, time, pathlib, threading
import cloudscraper
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

import steam_login

BASE = 'https://cs.trade'
CDN  = 'https://cdn.cs.trade'
SESSION_CACHE = pathlib.Path(__file__).parent / '.cstrade_session'

GAME_APPID = {'tf2': 440, 'rust': 252490, 'csgo': 730}
# ПОДТВЕРЖДЕНО ВЖИВУЮ 2026-08-17: /developer/api/prices_CSGO отдаёт 200 (не
# 'CS2'/'CS' — оба 404) — cs.trade держит игру под старым ключом CSGO, хотя
# торгует актуальными CS2-предметами.
PRICES_GAME_NAME = {440: 'TF2', 252490: 'RUST', 730: 'CSGO'}

_scraper: cloudscraper.CloudScraper | None = None
_login_ts = 0.0
# arb_mannco.py сканирует все 4 направления ПАРАЛЛЕЛЬНО (2026-08-05) —
# csfwd и csrev оба могут одновременно решить, что сессия протухла, и
# полезть логиниться разом. Без лока это гонка (двойной логин, лишний
# запрос к Steam) — а именно частые логины уже один раз словили
# RateLimitExceeded в этом же сеансе.
_login_lock = threading.Lock()

_bot_inv_cache: dict = {'ts': 0.0, 'data': []}
BOT_INV_TTL = int(os.getenv('CS_MN_BOT_INV_TTL', '60'))

_prices_cache: dict = {}   # game_appid -> {'ts':..., 'data': {...}}
PRICES_TTL = int(os.getenv('CS_MN_PRICES_TTL', '1800'))


# ─── Авторизация ──────────────────────────────────────────────────────────────

def _new_scraper() -> cloudscraper.CloudScraper:
    s = cloudscraper.create_scraper()
    cached = SESSION_CACHE.read_text().strip() if SESSION_CACHE.exists() else ''
    if cached:
        s.cookies.set('PHPSESSID', cached, domain='cs.trade')
    return s


def login(force: bool = False) -> bool:
    """Тонкая обёртка над _login_impl() под локом — arb_mannco.py сканирует
    все 4 направления параллельно (2026-08-05), csfwd и csrev могут решить,
    что сессия протухла, одновременно. Без лока это гонка (двойной логин
    в Steam разом) — а именно частые логины уже цепляли RateLimitExceeded
    в этом же сеансе."""
    with _login_lock:
        return _login_impl(force)


def _login_impl(force: bool = False) -> bool:
    """Полный вход через Steam OpenID. См. docstring модуля. Кэширует
    PHPSESSID на диск и переиспользует между запусками процесса.

    ВАЖНО (поймано живьём 2026-08-05): steam_login.get_steam_session() —
    это ПОЛНЫЙ логин в Steam, а не просто чтение кэша. При частых отдельных
    запусках процесса (каждый тестовый скрипт — новый процесс, значит новый
    вход) это быстро цепляет Steam'овский RateLimitExceeded. Поэтому ПЕРЕД
    полным релогином сперва пробуем закэшированный на диске PHPSESSID —
    если под ним сайт всё ещё отдаёт страницу с ником (не редиректит на
    логин), логиниться в Steam заново вообще не нужно."""
    global _scraper, _login_ts
    if _scraper is None:
        _scraper = _new_scraper()
    if not force and _login_ts and time.time() - _login_ts < 3000:
        return True

    if not force and SESSION_CACHE.exists():
        try:
            r = _scraper.get(f'{BASE}/ru/trade', timeout=30)
            if not _relogin_if_needed(r.text):
                _login_ts = time.time()
                print('[cstrade] переиспользую закэшированный PHPSESSID (без Steam-логина)')
                return True
        except Exception as e:
            print(f'[cstrade] проверка кэшированной сессии не удалась: {e}')

    if not steam_login.get_steam_session(force=force):
        print('[cstrade] Steam login failed — не могу залогиниться на cs.trade')
        return False
    steam_cookies = steam_login._cookies

    scraper = cloudscraper.create_scraper()
    for k, v in steam_cookies.items():
        scraper.cookies.set(k, v, domain='steamcommunity.com')

    try:
        r1 = scraper.get(f'{BASE}/ru/login_steam', allow_redirects=True, timeout=30)
        m = re.search(r'<form[^>]*action="([^"]*)"[^>]*>(.*?)</form>', r1.text, re.S)
        if not m:
            print('[cstrade] login: форма Steam OpenID не найдена (уже залогинены иначе?)')
            _scraper = scraper
            _login_ts = time.time()
            return True
        action = m.group(1)
        inputs = re.findall(r'<input[^>]*name="([^"]*)"[^>]*value="([^"]*)"', m.group(2))
        data = {name: val for name, val in inputs}
        files = {k: (None, v) for k, v in data.items()}
        r2 = scraper.post(action, files=files, allow_redirects=True, timeout=30,
                          headers={'Referer': r1.url, 'Origin': 'https://steamcommunity.com'})
        if 'cs.trade' not in r2.url:
            print(f'[cstrade] login: неожиданная посадочная страница {r2.url}')
            return False
    except Exception as e:
        print(f'[cstrade] login error: {e}')
        return False

    phpsessid = scraper.cookies.get('PHPSESSID', domain='cs.trade')
    if not phpsessid:
        print('[cstrade] login: PHPSESSID не получен')
        return False
    SESSION_CACHE.write_text(phpsessid)
    _scraper = scraper
    _login_ts = time.time()
    print(f'[cstrade] Вошли как {steam_login.STEAM_LOGIN}, PHPSESSID закэширован')
    return True


def _s() -> cloudscraper.CloudScraper:
    if _scraper is None or not _login_ts:
        login()
    return _scraper


def _relogin_if_needed(html: str) -> bool:
    """True, если страница похожа на разлогиненную (нет своей аватарки/ника)."""
    return 'login_steam' in html and 'OpenID_loggedInName' not in html


# ─── Баланс ────────────────────────────────────────────────────────────────

def get_balance() -> float:
    """Баланс аккаунта на cs.trade в USD. Парсится из HTML (отдельного
    JSON-эндпоинта для баланса на сайте нет — значение рендерится сервером
    прямо в страницу, см. class="balance-value")."""
    r = _s().get(f'{BASE}/ru/trade', timeout=30)
    if _relogin_if_needed(r.text):
        if not login(force=True):
            return -1.0
        r = _s().get(f'{BASE}/ru/trade', timeout=30)
    m = re.search(r'class="balance-value">([\d.,]+)<', r.text)
    if not m:
        return -1.0
    return float(m.group(1).replace(',', ''))


# ─── Инвентарь бота (то, что МОЖНО купить) ───────────────────────────────────

def _load_bot_inventory_raw() -> list[dict]:
    if time.time() - _bot_inv_cache['ts'] < BOT_INV_TTL and _bot_inv_cache['data']:
        return _bot_inv_cache['data']
    r = _s().get(f'{BASE}/loadBotInventory/ru',
                 params={'order_by': 'price_desc', 'bot': 'all', '_': int(time.time() * 1000)},
                 timeout=45)
    try:
        d = r.json()
    except Exception:
        print(f'[cstrade] loadBotInventory: не JSON (первые 200 симв.): {r.text[:200]}')
        return _bot_inv_cache['data']
    if d.get('status') != 'ok':
        print(f'[cstrade] loadBotInventory: status={d.get("status")}')
        return _bot_inv_cache['data']
    inv = d.get('inventory') or []
    _bot_inv_cache['data'] = inv
    _bot_inv_cache['ts'] = time.time()
    return inv


def get_bot_inventory(game: str) -> list[dict]:
    """Лоты бота для игры ('tf2'/'rust'): [{name, price, id, bot, bot_id,
    tradable, wear, classid, ...}]. Один name может встречаться много раз
    (разные лоты/цены), как в loadBotInventory у cs.trade.

    ВАЖНО: поле tradable_bool в этом эндпоинте оказалось False у ВСЕХ без
    исключения лотов (проверено 2026-08-05 на полном TF2-дампе) — то есть
    оно не отражает реальную доступность предмета к обмену прямо сейчас,
    иначе сайт был бы вообще неработоспособен. Не фильтруем по нему на
    этапе поиска кандидатов — как и остальные направления в проекте,
    реальная проверка доступности идёт в момент самой покупки (send_trade),
    а не на этапе скана."""
    appid = str(GAME_APPID[game])
    inv = _load_bot_inventory_raw()
    out = []
    for it in inv:
        if str(it.get('app_id')) != appid:
            continue
        out.append({
            'name':     it.get('market_hash_name'),
            'price':    float(it.get('price') or 0),
            'id':       it.get('id'),
            'bot':      it.get('bot'),
            'bot_id':   it.get('bot_id'),
            'classid':  it.get('classid'),
            'tradable': bool(it.get('tradable_bool')),
            'raw':      it,   # нужен целиком для send_trade() — см. ниже
        })
    return out


def cheapest_bot_offer(game: str, name: str) -> dict | None:
    """Самый дешёвый живой лот бота с этим именем, или None."""
    cands = [x for x in get_bot_inventory(game) if x['name'] == name]
    if not cands:
        return None
    return min(cands, key=lambda x: x['price'])


# ─── Официальный прайс-лист (справочная оценка + лимиты депозита) ───────────

def get_prices(game: str, force: bool = False) -> dict[str, dict]:
    """{name: {price, store_price, have, max, can_take, tradable,
    reservable, available_stock}} — официальный прайс cs.trade. `price` —
    их справочная стоимость предмета (годится для оценки, ЧТО дадут за
    депозит, направление mannco -> CS.trade). `can_take` — ЖИВАЯ квота на
    депозит прямо сейчас (в отличие от `max` — исторического потолка, см.
    docstring фильтра в arb_mannco_cs.py).

    force=True обходит PRICES_TTL (30 мин по умолчанию) — нужен ПРЯМО ПЕРЕД
    send_trade(): между покупкой на mannco и реальным депозитом проходит
    несколько минут, а can_take за это время меняется (квоту тратят и
    другие пользователи cs.trade, не только мы) — кэш к моменту депозита
    может быть безнадёжно устаревшим. Подтверждено живьём 2026-08-05:
    несколько предметов (Unlocked Cosmetic Crate Multi-Class, Backpack
    Expander) прошли предфильтр по кэшу, но депозит отбился
    bot_current_limit=0 — квота уже успела кончиться к моменту отправки."""
    appid = GAME_APPID[game]
    cached = _prices_cache.get(appid)
    if not force and cached and time.time() - cached['ts'] < PRICES_TTL:
        return cached['data']
    name = PRICES_GAME_NAME[appid]
    try:
        r = _s().get(f'{CDN}/developer/api/prices_{name}', timeout=45)
        d = r.json()
    except Exception as e:
        print(f'[cstrade] prices_{name}: ошибка загрузки ({e})')
        return (cached or {}).get('data', {})
    if not isinstance(d, dict):
        print(f'[cstrade] prices_{name}: неожиданный формат ответа')
        return (cached or {}).get('data', {})
    _prices_cache[appid] = {'ts': time.time(), 'data': d}
    return d


# ─── Trade link (нужно один раз выставить, иначе бот не знает, куда слать) ──

def update_trade_link(trade_url: str) -> bool:
    s, d = _post('/updateTradeLink/ru', {'trade_link': trade_url})
    ok = s == 200 and isinstance(d, dict) and not d.get('error')
    print(f'[cstrade] updateTradeLink: {"OK" if ok else "FAIL"} {json.dumps(d)[:200]}')
    return ok


# ─── Низкоуровневый POST с автологином на разлогин ───────────────────────────

def _post(path: str, body: dict) -> tuple[int, object]:
    r = _s().post(f'{BASE}{path}', data=body, timeout=30,
                  headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': f'{BASE}/ru/trade'})
    if _relogin_if_needed(r.text):
        if not login(force=True):
            return r.status_code, r.text
        r = _s().post(f'{BASE}{path}', data=body, timeout=30,
                      headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': f'{BASE}/ru/trade'})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


# ─── Сама сделка ──────────────────────────────────────────────────────────

def send_trade(bot_items: list[dict] | None = None,
               user_items: list[dict] | None = None,
               bot_id: str | None = None) -> dict:
    """
    Отправить обмен. Формат снят 2026-08-05 РЕАЛЬНЫМ ИСХОДНИКОМ сайта через
    Playwright (dump sendSingleTrade.toString() на живой сессии) — раньше
    предполагавшийся по старым заметкам формат (bot_items/user_items как
    голые id) был неверен, сервер отвечал "Список выбора пуст" на ЛЮБОЙ
    комбинации параметров. Реальный POST (jQuery $.ajax, urlencoded):

        {bot, tt, user_chosen_items: JSON, bot_chosen_items: JSON, si, tm}

    КРИТИЧНО: bot_chosen_items/user_chosen_items — это JSON-массивы ПОЛНЫХ
    объектов предмета (как их отдают get_bot_inventory()[i]['raw'] или
    get_user_inventory()), НЕ голые id-строки. Подтверждено вживую: выбор
    предмета бота в реальном браузере кладёт в window.trades_queue именно
    такую запись:
        {"bot":"9","bot_sum":...,"bot_items":[{...полный объект...}],
         "trade_type":"s", ...}
    и `bot` в POST — это id БОТА (поле `bot` у самого предмета в
    loadBotInventory), не наш аккаунт.

    ПОДТВЕРЖДЕНО ЖИВЬЁМ 2026-08-05 (направление CS.trade -> mannco,
    bot_items): реальный вызов с ценой $0.03 при балансе $0 дал ЧЕСТНУЮ
    бизнес-ошибку "Сумма выбранных предметов пользователя меньше суммы
    выбранных предметов бота" — т.е. сервер принял и разобрал запрос
    правильно, отказ ровно по деньгам (баланс $0), не по формату. Формат
    рабочий, для покупки не хватает только реального баланса CS.trade.

    ⚠️ Направление депозита (user_items, отдаём без получения) НЕ
    проверено живой сделкой — неясно, что подставлять в `bot_id`, когда
    покупки от бота нет вовсе (все предметы в нашем инвентаре на момент
    проверки были либо без цены, либо упёрлись в квоту `vol`). Первый
    реальный вызов на этом направлении нужно разбирать по логу ответа.
    """
    bot_items = bot_items or []
    user_items = user_items or []
    if bot_id is None:
        bot_id = next((str(it.get('bot')) for it in bot_items if it.get('bot')), '')
    body = {
        'bot': bot_id,
        'tt': 's',
        'user_chosen_items': json.dumps(user_items),
        'bot_chosen_items': json.dumps(bot_items),
        'si': '',
        'tm': '',
    }
    s, d = _post('/sendTrade/ru', body)
    ok = s == 200 and isinstance(d, dict) and d.get('status') == 'ok'
    return {'success': ok, 'status': s, 'data': d}


def get_user_inventory(game: str) -> list[dict]:
    """GET /loadUserInventory/ru?game_id=... — наш Steam-инвентарь глазами
    cs.trade (нужен для получения id предметов перед депозитом и для
    реальной оценки price).

    КРИТИЧНО (поймано живьём 2026-08-05): без game_id всегда возвращает
    ПУСТОЙ список, даже когда в Steam-инвентаре реально есть предметы —
    это не "инвентарь ещё не синхронизировался", а просто отсутствующий
    обязательный параметр. game_id — это appid (440 для TF2, 252490 для
    Rust), а НЕ строка вроде 'tf2'.

    ВАЖНО про поля ответа: `price` есть НЕ у каждого предмета — None
    означает, что cs.trade сейчас не покупает этот предмет вообще (нет
    спроса). Даже когда price есть, `status` может быть "unavailable" —
    похоже, это отдельный лимит КВОТЫ на конкретное имя (поле `vol`,
    формат "used/max", напр. "5000/5000" у затоваренных Mann Co. Supply
    Crate Key — они и в bot-инвентаре, и в депозите оказались на пределе).
    Это тот же класс ограничения, что STOCK_LIMIT у mannco в
    arb_dm_mannco.py — не баг, а нормальная защита от затоваривания.
    `price`, где он есть, оказался БЛИЗОК к price_из_loadBotInventory * 0.95
    (напр. ключ: депозит $2.92 против продажи $3.07) — похоже на реальную
    комиссию ~5%, но подтверждено только на одном типе предмета (ключ),
    депозит которого сам send_trade ещё не подтверждён живьём (упирается в
    тот же лимит квоты) — сама механика send_trade() всё ещё не
    откалибрована до конца, см. docstring модуля."""
    appid = GAME_APPID[game]
    r = _s().get(f'{BASE}/loadUserInventory/ru',
                 params={'order_by': 'price_desc', 'game_id': appid}, timeout=30)
    try:
        d = r.json()
    except Exception:
        return []
    if d.get('status') != 'ok':
        return []
    return d.get('inventory') or []
