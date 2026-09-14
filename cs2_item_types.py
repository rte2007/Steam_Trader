# -*- coding: utf-8 -*-
"""
Классификация предметов CS2 по типу — по требованию пользователя 2026-08-18:
"скины только на оружия ... и еще агенты и кейсы но не капсулы". Изначально
здесь был ещё фильтр по редкости (фиолетовый/розовый/тайное), УБРАН тем же
днём — пользователь прислал реальный топ skins-table.com как "правильный",
там M4A1-S | Fizzy POP на первом месте, а это Mil-Spec (синий), подтверждено
и живыми данными DMarket, и этим же датасетом — раз это "правильно", цвета
на сайте не фильтруются, оставлен только фильтр по типу предмета.

Источник — открытый статический датасет ByMykel/CSGO-API (GitHub,
raw.githubusercontent.com), НЕ DMarket API: у DMarket через
/marketplace-api/v2/offers тоже есть attributes.cs2.itemType на живом лоте,
но кейс и капсула там ОБА itemType='container' — не различить без датасета
с явным полем type ('Case' vs 'Sticker Capsule'/'Autograph Capsule'/'Patch
Capsule' и т.д., см. crates.json). Плюс bulk-запрос по имени на DMarket
отдельно на каждый из 1000+ кандидатов слишком дорог — этот датасет
статичный (обновляется редко, только с патчами CS2), кэшируем на диск с
длинным TTL.
"""
import os, json, time, pathlib
import requests

_BASE = 'https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en'
_CACHE_FILE = pathlib.Path(__file__).parent / '.cs2_item_types_cache.json'
_CACHE_TTL = int(os.getenv('CS2_ITEM_TYPES_TTL', str(24 * 3600)))

# Категории обычного оружия (см. skins.json category.name) — НАЙДЕНО ЖИВЬЁМ
# 2026-08-18: реальные значения в датасете — {'Pistols','Rifles','Knives',
# 'Gloves','Equipment','SMGs','Heavy'} (множественное число! отдельных
# Shotgun/Sniper Rifle нет — снайперки внутри Rifles, дробовики/пулемёты
# внутри Heavy — первая версия со старыми DMarket-подобными именами
# ('Rifle','Sniper Rifle' и т.п.) не матчила НИЧЕГО, weapon_rarity был
# пустым). НЕ включает Knives/Gloves: у них своя шкала редкости (всегда
# Covert/Extraordinary независимо от скина), фильтр "фиолетовый/розовый/
# тайное" на них не имеет смысла, и пользователь их отдельно не упоминал.
# Equipment (Zeus/kevlar/defuser) тоже не включён — не "скины на оружия".
WEAPON_CATEGORIES = {'Rifles', 'Pistols', 'SMGs', 'Heavy'}

_cache: dict | None = None   # {'ts':..., 'weapon_rarity': {name: rarity}, 'agents': set, 'cases': set}


def _fetch_json(path: str) -> list:
    r = requests.get(f'{_BASE}/{path}', timeout=30)
    r.raise_for_status()
    return r.json()


def _build() -> dict:
    print('[cs2_types] загружаю датасет ByMykel/CSGO-API (skins/agents/crates)...')
    skins = _fetch_json('skins.json')
    agents = _fetch_json('agents.json')
    crates = _fetch_json('crates.json')

    weapon_rarity = {}
    for it in skins:
        cat = (it.get('category') or {}).get('name')
        if cat not in WEAPON_CATEGORIES:
            continue
        name = (it.get('weapon') or {}).get('name')
        pattern = (it.get('pattern') or {}).get('name')
        if not name or not pattern:
            continue
        base_name = f'{name} | {pattern}'
        rarity = (it.get('rarity') or {}).get('name')
        weapon_rarity[base_name] = rarity

    agent_names = set()
    for it in agents:
        n = it.get('name')
        if n:
            agent_names.add(n)

    case_names = set()
    for it in crates:
        if it.get('type') == 'Case':
            n = it.get('name')
            if n:
                case_names.add(n)

    data = {'ts': time.time(), 'weapon_rarity': weapon_rarity,
            'agents': sorted(agent_names), 'cases': sorted(case_names)}
    print(f'[cs2_types] загружено: {len(weapon_rarity)} скинов оружия, '
          f'{len(agent_names)} агентов, {len(case_names)} кейсов')
    try:
        _CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception as e:
        print(f'[cs2_types] не смог сохранить кэш на диск: {e}')
    return data


def _load() -> dict:
    global _cache
    if _cache and time.time() - _cache['ts'] < _CACHE_TTL:
        return _cache
    if _CACHE_FILE.exists():
        try:
            d = json.loads(_CACHE_FILE.read_text(encoding='utf-8'))
            if time.time() - d.get('ts', 0) < _CACHE_TTL:
                d['agents'] = set(d['agents'])
                d['cases'] = set(d['cases'])
                _cache = d
                return _cache
        except Exception:
            pass
    try:
        d = _build()
    except Exception as e:
        print(f'[cs2_types] не смог загрузить датасет ({e}) — классификация недоступна')
        return {'ts': time.time(), 'weapon_rarity': {}, 'agents': set(), 'cases': set()}
    d['agents'] = set(d['agents'])
    d['cases'] = set(d['cases'])
    _cache = d
    return _cache


def _strip_wear_stattrak(market_hash_name: str) -> str:
    n = market_hash_name
    for prefix in ('StatTrak™ ', 'Souvenir '):
        if n.startswith(prefix):
            n = n[len(prefix):]
    if n.endswith(')') and '(' in n:
        n = n[:n.rindex('(')].rstrip()
    return n


def is_wanted(market_hash_name: str) -> bool:
    """True — предмет из разрешённого набора: оружейный скин (любой
    редкости — см. ниже), ИЛИ агент, ИЛИ кейс (не капсула). Souvenir —
    ЗАПРЕЩЕНЫ (по требованию пользователя 2026-08-18: у Souvenir-версий
    свой отдельный диапазон float/паттернов, не тот же товар, что обычная
    версия — та же категория риска, что и Head Of Defense/D-Eye-Monds с
    TF2 раньше в этой сессии, агрегированная цена по имени может не
    соответствовать реально покупаемому лоту).

    ИСПРАВЛЕНО 2026-08-18: ограничение по редкости (только Restricted/
    Classified/Covert) УБРАНО — пользователь прислал реальный топ
    skins-table.com ("вот правильные") с M4A1-S | Fizzy POP на первом
    месте; живая проверка (DMarket attributes.cs2.quality И статический
    датасет — оба независимо) подтвердила, что Fizzy POP это Mil-Spec
    Grade (синий), НЕ входит ни в один из трёх заявленных цветов. Раз
    пользователь называет этот список "правильным", фильтра по цвету на
    сайте фактически нет — оставлен только фильтр по ТИПУ предмета."""
    if market_hash_name.startswith('Souvenir '):
        return False
    d = _load()
    if market_hash_name in d['agents']:
        return True
    if market_hash_name in d['cases']:
        return True
    base = _strip_wear_stattrak(market_hash_name)
    return base in d['weapon_rarity']


def is_plain_weapon_skin(market_hash_name: str) -> bool:
    """True — ЧИСТО оружейный скин (Rifles/Pistols/SMGs/Heavy, см.
    WEAPON_CATEGORIES): НЕ StatTrak, НЕ Souvenir, НЕ наклейка/агент/кейс/
    капсула. По требованию пользователя 2026-08-27 для 24-часового анализа
    DMarket -> market.csgo.com MARKET ORDER — более строгий набор, чем
    is_wanted() (тот разрешает агентов/кейсы и не трогает StatTrak)."""
    if market_hash_name.startswith('Souvenir ') or market_hash_name.startswith('StatTrak™ '):
        return False
    if '(' not in market_hash_name:
        return False   # оружейные скины всегда имеют (Wear) — отсекает наклейки/прочее одним махом
    d = _load()
    return market_hash_name[:market_hash_name.rindex('(')].rstrip() in d['weapon_rarity']
