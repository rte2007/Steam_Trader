# -*- coding: utf-8 -*-
"""
skinstable_api.py — обёртка над внутренним (не публичным платным API, а тем
же, что использует их веб-страница /table/ после логина) эндпоинтом
skins-table.com. Единственный источник цен по обеим сторонам (mannco.store И
CS.TRADE) для обоих направлений арбитража — по решению пользователя
2026-08-06 отказались от собственного медленного обхода каталога mannco
(30k+ позиций, ~2 минуты на цикл) в пользу одного быстрого запроса сюда,
который отдаёт ОБЕ площадки разом.

ВАЖНО: skins-table.com НЕ включает CS.TRADE в свой документированный платный
API (/api_docs, /api_v2/...) — там его просто нет в списке площадок. Это
эндпоинт, который использует их собственная таблица (/table/ajax_beta.php)
под залогиненной через Steam сессией.

Авторизация: Steam OpenID, ТОЧНО ТОТ ЖЕ паттерн, что в cs_trade_api.py
(POST multipart на steamcommunity.com/openid/login), только точка входа —
https://skins-table.com/steam/steamauth.php?login.

Формат запроса (снят из main_beta.js, функция get_price_platform):
  POST /table/ajax_beta.php
  {game_id: 'TF2'|'RUST'|'CS:GO'|'DOTA2',   # ИМЕНЕМ, не appid — иначе всегда null
   parser_1, parser_2: <имя площадки без суффикса>,
   form_1: '<имя площадки>_<game_id>', form_2: то же для второй,
   last_ts: '' при первом запросе}
Ответ: [site1_prices|null, site2_prices|null, {currency rates}, [ts1, ts2]]
  price-запись: {"p": цена, "c": сколько есть, "m": сколько ещё примут (квота),
                 "b": trade lock (дней?), "f": float, "o": overstock, "t": timestamp}

СВЕЖЕСТЬ: подтверждено вживую 2026-08-06 — их снимок MANNCO.STORE отстаёт от
реального времени примерно на CACHE_TTL (по умолчанию 10 мин). Для сверхдешёвых
высокооборотных предметов (мусорные War Paint и т.п.) конкретная цена лота за
эти 10 минут может уже не существовать — итоговое решение о покупке ВСЕГДА
проверяется живым запросом к mannco/cs.trade перед тратой денег
(mn.item_listings()/best_bid() и cs.get_prices(force=True)/cheapest_bot_offer()),
skins-table используется только чтобы быстро НАЙТИ кандидатов, не как
источник цены для самой сделки.
"""
import os, re, time, pathlib
import cloudscraper
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

import steam_login

BASE = 'https://skins-table.com'
SESSION_CACHE = pathlib.Path(__file__).parent / '.skinstable_session'

GAME_NAME = {440: 'TF2', 252490: 'RUST', 730: 'CS:GO', 570: 'DOTA2'}

_scraper: cloudscraper.CloudScraper | None = None
_login_ts = 0.0

_cache: dict = {}   # (game_id, marketplace1, marketplace2) -> {'ts':..., 'site1':{}, 'site2':{}}
CACHE_TTL = int(os.getenv('SKINSTABLE_CACHE_TTL', '600'))


def _new_scraper() -> cloudscraper.CloudScraper:
    s = cloudscraper.create_scraper()
    cached = SESSION_CACHE.read_text().strip() if SESSION_CACHE.exists() else ''
    if cached:
        s.cookies.set('PHPSESSID', cached, domain='skins-table.com')
    return s


def _relogin_if_needed(html: str) -> bool:
    return 'Log in' in html and 'My profile' not in html and 'nav-account__item--logout' not in html


def login(force: bool = False) -> bool:
    """Steam OpenID вход — идентичный паттерну cs_trade_api.login()."""
    global _scraper, _login_ts
    if _scraper is None:
        _scraper = _new_scraper()
    if not force and _login_ts and time.time() - _login_ts < 3000:
        return True

    if not force and SESSION_CACHE.exists():
        try:
            r = _scraper.get(f'{BASE}/panel', timeout=30)
            if not _relogin_if_needed(r.text):
                _login_ts = time.time()
                print('[skinstable] переиспользую закэшированный PHPSESSID (без Steam-логина)')
                return True
        except Exception as e:
            print(f'[skinstable] проверка кэшированной сессии не удалась: {e}')

    if not steam_login.get_steam_session(force=force):
        print('[skinstable] Steam login failed')
        return False
    steam_cookies = steam_login._cookies

    scraper = cloudscraper.create_scraper()
    for k, v in steam_cookies.items():
        scraper.cookies.set(k, v, domain='steamcommunity.com')

    try:
        r1 = scraper.get(f'{BASE}/steam/steamauth.php?login', allow_redirects=True, timeout=30)
        m = re.search(r'<form[^>]*action="([^"]*)"[^>]*>(.*?)</form>', r1.text, re.S)
        if not m:
            print('[skinstable] login: форма Steam OpenID не найдена')
            return False
        action = m.group(1)
        inputs = re.findall(r'<input[^>]*name="([^"]*)"[^>]*value="([^"]*)"', m.group(2))
        data = {name: val for name, val in inputs}
        files = {k: (None, v) for k, v in data.items()}
        r2 = scraper.post(action, files=files, allow_redirects=True, timeout=30,
                          headers={'Referer': r1.url, 'Origin': 'https://steamcommunity.com'})
        if 'skins-table.com' not in r2.url:
            print(f'[skinstable] login: неожиданная посадочная страница {r2.url}')
            return False
    except Exception as e:
        print(f'[skinstable] login error: {e}')
        return False

    phpsessid = scraper.cookies.get('PHPSESSID', domain='skins-table.com')
    if not phpsessid:
        print('[skinstable] login: PHPSESSID не получен')
        return False
    SESSION_CACHE.write_text(phpsessid)
    _scraper = scraper
    _login_ts = time.time()
    print(f'[skinstable] Вошли как {steam_login.STEAM_LOGIN}, PHPSESSID закэширован')
    return True


def _s() -> cloudscraper.CloudScraper:
    if _scraper is None or not _login_ts:
        login()
    return _scraper


def _fetch_raw(game: int, marketplace1: str, marketplace2: str) -> tuple[dict, dict, dict]:
    """
    Один запрос — сразу ДВА датасета площадок:
    {market_hash_name: {p: цена, c: сколько есть, m: квота/остаток,
    t: unix ms последнего обновления}} для каждой из двух площадок, ПЛЮС
    сырые курсы валют сайта {код: курс за 1 USD} третьим элементом — нужны
    паре CS.TRADE/MARKET (см. get_cstrade_and_market: MARKET у skins-table
    в РУБЛЯХ, не USD, в отличие от остальных площадок).
    Кэш общий на пару (game, marketplace1, marketplace2), TTL=CACHE_TTL.

    Пусто/старое при сетевой ошибке — вызывающий код должен воспринимать
    это как "нет свежих данных", а не как "цены нет вообще".
    """
    game_name = GAME_NAME[game]
    key = (game_name, marketplace1, marketplace2)
    cached = _cache.get(key)
    if cached and time.time() - cached['ts'] < CACHE_TTL:
        return cached['site1'], cached['site2'], cached.get('rates', {})

    s = _s()
    try:
        # Прогреваем сессию посещением страницы с нужной игрой — некоторые
        # игровые ключи сервер не резолвит без предварительного GET (see docstring).
        s.get(f'{BASE}/table/?g={game}', timeout=30)
        r = s.post(f'{BASE}/table/ajax_beta.php', timeout=45,
                   data={'game_id': game_name, 'parser_1': marketplace1, 'parser_2': marketplace2,
                         'form_1': f'{marketplace1}_{game_name}',
                         'form_2': f'{marketplace2}_{game_name}', 'last_ts': ''},
                   headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': f'{BASE}/table/?g={game}'})
        d = r.json()
    except Exception as e:
        print(f'[skinstable] _fetch2({game_name}, {marketplace1}, {marketplace2}): ошибка ({e})')
        return (cached or {}).get('site1', {}), (cached or {}).get('site2', {}), (cached or {}).get('rates', {})

    if not isinstance(d, list) or len(d) < 2 or not isinstance(d[0], dict) or not isinstance(d[1], dict):
        print(f'[skinstable] _fetch2({game_name}, {marketplace1}, {marketplace2}): неожиданный ответ '
              f'(возможно, сессия протухла) — {str(d)[:200]}')
        return (cached or {}).get('site1', {}), (cached or {}).get('site2', {}), (cached or {}).get('rates', {})

    rates = d[2] if len(d) > 2 and isinstance(d[2], dict) else {}
    _cache[key] = {'ts': time.time(), 'site1': d[0], 'site2': d[1], 'rates': rates}
    print(f'[skinstable] {game_name}: {marketplace1}={len(d[0])} {marketplace2}={len(d[1])}')
    return d[0], d[1], rates


def _fetch2(game: int, marketplace1: str, marketplace2: str) -> tuple[dict, dict]:
    """Как _fetch_raw(), без курсов валют — большинство пар это две площадки
    в USD и конвертация не нужна."""
    site1, site2, _ = _fetch_raw(game, marketplace1, marketplace2)
    return site1, site2


def get_mannco_and_deposit(game: int) -> tuple[dict, dict]:
    """(mannco_prices, cs_trade_deposit_prices) одним быстрым запросом —
    основной источник кандидатов для направления mannco -> CS.trade."""
    return _fetch2(game, 'MANNCO.STORE', 'CS.TRADE DEPOSIT')


def get_lisskins_and_dmarket_order(game: int = 252490) -> tuple[dict, dict]:
    """Return the exact first-leg pair from the skins-table web table.

    ``DMARKET ORDER`` is intentionally used here.  ``DMARKET`` is the
    storefront/listing price and must never be substituted for a buy order.
    The returned snapshot is only a candidate source; callers must re-check
    the LIS-SKINS lot and the live DMarket target before spending money.
    """
    return _fetch2(game, 'LIS-SKINS', 'DMARKET ORDER')


def get_cstrade_and_mannco(game: int) -> tuple[dict, dict]:
    """(cs_trade_sell_prices, mannco_prices) одним быстрым запросом —
    основной источник кандидатов для направления CS.trade -> mannco."""
    return _fetch2(game, 'CS.TRADE', 'MANNCO.STORE')


def get_mannco_and_steam(game: int) -> tuple[dict, dict]:
    """(mannco_prices, steam_order_prices) одним быстрым запросом — основной
    источник кандидатов для направления mannco -> Steam Community Market.

    'STEAM ORDER' (не просто 'STEAM'!) — подтверждено вживую 2026-08-06:
    отдельная площадка в их API с гораздо большим покрытием (130k+ позиций
    TF2 против 36k у 'STEAM') и именно ценой buy-ордера (сырой, ДО вычета
    комиссии — их собственный % на сайте считается так же: цена*0.87 против
    mannco, как и net_after_fee() здесь в проекте)."""
    return _fetch2(game, 'MANNCO.STORE', 'STEAM ORDER')


def get_deposit_prices(game: int) -> dict[str, dict]:
    """Цена ПРИЁМА депозита CS.TRADE (что нам заплатят при вкладе предмета) —
    используется как финальная живая-от-skins-table перепроверка перед
    покупкой в try_buy() направления mannco -> CS.trade."""
    return get_mannco_and_deposit(game)[1]


def get_market_prices(game: int) -> dict[str, dict]:
    """Обычная цена ПРОДАЖИ/вывода бота CS.TRADE (что мы платим при покупке
    предмета у бота) — независимая от cs_trade_api.py перепроверка перед
    покупкой в направлении CS.trade -> mannco."""
    return get_cstrade_and_mannco(game)[0]


def get_cstrade_and_market(game: int) -> tuple[dict, dict]:
    """(cs_trade_prices, market_prices_USD) одним быстрым запросом — источник
    кандидатов для направления CS.trade -> Steam Community Market (CS2).

    ВАЖНО: 'MARKET' у skins-table отдаёт цену в РУБЛЯХ, а не в USD, как
    остальные площадки в проекте — конвертируем по live-курсу из того же
    ответа (rates['RUB'], элемент [2] сырого ответа ajax_beta.php).
    ПОДТВЕРЖДЕНО ВЖИВУЮ 2026-08-17: сверено со скриншотом пользователя
    (Glock-18 | Trace Lock (Battle-Scarred) — CS.TRADE $1.36 совпало
    точь-в-точь; MARKET 120.97₽ / курс 85.1 = $1.42, тоже совпало).

    Это ТОЛЬКО для поиска кандидатов (см. арбитраж CS.trade -> Steam Market,
    arb_cstrade_market.py) — как и везде в проекте, финальная цена продажи
    перед реальным листингом берётся live с Steam (см.
    steam_market_api.get_lowest_listing_price), не из этого кэша."""
    cst, mkt_rub, rates = _fetch_raw(game, 'CS.TRADE', 'MARKET')
    rub = rates.get('RUB') or 0
    if not rub:
        print('[skinstable] get_cstrade_and_market: нет курса RUB в ответе — MARKET пуст')
        return cst, {}
    mkt_usd = {}
    for name, info in mkt_rub.items():
        conv = dict(info)
        conv['p'] = round((info.get('p') or 0) / rub, 4)
        mkt_usd[name] = conv
    return cst, mkt_usd


def get_dmarket_and_market(game: int) -> tuple[dict, dict]:
    """(dmarket_prices, market_prices_USD) одним быстрым запросом — источник
    кандидатов для направления DMarket -> market.csgo.com (CS2). Та же RUB-
    конвертация MARKET, что в get_cstrade_and_market() — см. её докстринг
    для подтверждения вживую. ПОДТВЕРЖДЕНО ВЖИВУЮ 2026-08-18 (Glock-18 |
    Trace Lock (Battle-Scarred)): DMARKET=$2.13, MARKET=100₽/85.2=$1.17."""
    dmp, mkt_rub, rates = _fetch_raw(game, 'DMARKET', 'MARKET')
    rub = rates.get('RUB') or 0
    if not rub:
        print('[skinstable] get_dmarket_and_market: нет курса RUB в ответе — MARKET пуст')
        return dmp, {}
    mkt_usd = {}
    for name, info in mkt_rub.items():
        conv = dict(info)
        conv['p'] = round((info.get('p') or 0) / rub, 4)
        mkt_usd[name] = conv
    return dmp, mkt_usd
