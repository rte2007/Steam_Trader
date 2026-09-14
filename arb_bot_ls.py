# -*- coding: utf-8 -*-
"""
arb_bot_ls.py — lis-skins -> DMarket instant sell bot.
AUTO_BUY=false: only sends Telegram signals (no Steam/buy needed).
"""
import sys, os, json, time, uuid, pathlib, traceback, socket
sys.stdout.reconfigure(encoding='utf-8')

# The VPS resolver intermittently routes Steam to an unreachable edge.  Keep
# the workaround process-local so it cannot affect the rest of the server.
_STEAM_HOST_IPS = {
    "steamcommunity.com": os.getenv("LEG1_STEAM_COMMUNITY_IP", "").strip(),
    "api.steampowered.com": os.getenv("LEG1_STEAM_API_IP", "").strip(),
    "login.steampowered.com": os.getenv("LEG1_STEAM_LOGIN_IP", "").strip(),
    "store.steampowered.com": os.getenv("LEG1_STEAM_STORE_IP", "").strip(),
    "help.steampowered.com": os.getenv("LEG1_STEAM_HELP_IP", "").strip(),
}
_STEAM_HOST_IPS = {host: ip for host, ip in _STEAM_HOST_IPS.items() if ip}
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
if _STEAM_HOST_IPS:
    def _route_steam(host, port, *args, **kwargs):
        return _ORIGINAL_GETADDRINFO(
            _STEAM_HOST_IPS.get(str(host).lower(), host), port, *args, **kwargs)
    socket.getaddrinfo = _route_steam

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))
from bootstrap_config import load_shared_config
load_shared_config(override=True)

import threading
import lisskins_api  as ls
import dmarket_api   as dm
import skinstable_api as st
from leg1_scanner import (build_skins_table_snapshot as _build_table_snapshot,
                          register_live_confirmation)
import telegram_bot  as tg
import bot_control
import trade_log

# ─── Settings ──────────────────────────────────────────────────────────────────

MIN_PROFIT_PCT  = float(os.getenv('MIN_PROFIT_PCT', '20'))
MIN_ORDERS      = int(os.getenv('MIN_ORDERS',       '1'))
POLL_INTERVAL   = int(os.getenv('POLL_INTERVAL',    '120'))
TRADE_POLL      = int(os.getenv('TRADE_POLL_SEC',   '30'))
STEAM_LOGIN_RETRY_SEC = int(os.getenv('STEAM_LOGIN_RETRY_SEC', '300'))
MIN_BUY_USD     = float(os.getenv('MIN_BUY_USD',    '0.0'))
MAX_BUY_USD     = float(os.getenv('MAX_BUY_USD',    '10.0'))
MAX_CONCURRENT  = int(os.getenv('MAX_CONCURRENT',   '3'))
MAX_DAILY_SPEND_USD = float(os.getenv('MAX_DAILY_SPEND_USD', '10.0'))
BUY_COOLDOWN    = int(os.getenv('BUY_COOLDOWN',     '300'))
AUTO_BUY        = os.getenv('AUTO_BUY',     'false').lower() == 'true'
AUTO_ACCEPT     = os.getenv('AUTO_ACCEPT',  'true').lower() == 'true'
AUTO_DEPOSIT    = os.getenv('AUTO_DEPOSIT', 'true').lower() == 'true'
LS_BALANCE_ALERT_USD  = float(os.getenv('LS_BALANCE_ALERT_USD', '2.0'))
LS_BALANCE_CHECK_SEC  = int(os.getenv('LS_BALANCE_CHECK_SEC', '600'))
MAX_REBUYS_PER_NAME   = int(os.getenv('MAX_REBUYS_PER_NAME', '1'))
REBUY_WINDOW_SEC      = int(os.getenv('REBUY_WINDOW_SEC', '3600'))
PENDING_INV_STALE_SEC = int(os.getenv('PENDING_INV_STALE_SEC', '3600'))
POST_CLOSE_COOLDOWN_SEC = int(os.getenv('POST_CLOSE_COOLDOWN_SEC', '1800'))
# Если LIS-SKINS САМА отменяет заказ (status=return) — обычного 30-минутного
# POST_CLOSE_COOLDOWN_SEC мало: тайтл с проблемой на стороне продавца может
# отменяться раз за разом (подтверждено вживую 2026-08-08: Pirate Poncho —
# 4 отмены за 40 минут). Отдельный, гораздо более долгий бан именно на такие
# случаи, чтобы не долбить один и тот же проблемный тайтл весь день.
LS_RETURN_BAN_SEC = int(os.getenv('LS_RETURN_BAN_SEC', str(24 * 3600)))
ERROR_ALERT_COOLDOWN_SEC = int(os.getenv('ERROR_ALERT_COOLDOWN_SEC', '300'))
STALE_BUY_TIMEOUT_SEC = int(os.getenv('STALE_BUY_TIMEOUT_SEC', '5400'))  # было 3600 — LS иногда финализирует
                                                                          # возврат почти ровно на часовой отметке
LOSS_FOLLOWUP_DELAY_SEC = int(os.getenv('LOSS_FOLLOWUP_DELAY_SEC', '1800'))  # было 1200 — 30 мин на возврат денег LS
MAX_LOSS_FOLLOWUP_ATTEMPTS = int(os.getenv('MAX_LOSS_FOLLOWUP_ATTEMPTS', '3'))  # см. check_loss_followups()
# Пол при переоценке оффера: насколько ниже цены закупки бот готов опустить
# цену, если стакан просел. Раньше пола не было вообще -> сделки уходили в
# -36% и -67%. 0 = не снижать ниже безубытка.
# Never intentionally lock in a negative result.  The old 10% default let an
# order-book drop turn a profitable purchase into a realised loss while the
# item was travelling through Steam/DMarket.
MAX_LOSS_PCT          = float(os.getenv('MAX_LOSS_PCT', '0'))
SKINSTABLE_MAX_AGE_SEC = int(os.getenv('SKINSTABLE_MAX_AGE_SEC', '1800'))
LIVE_CONFIRMATIONS_REQUIRED = int(os.getenv('LIVE_CONFIRMATIONS_REQUIRED', '2'))
LIVE_CONFIRMATION_WINDOW_SEC = int(os.getenv('LIVE_CONFIRMATION_WINDOW_SEC', '300'))
ONE_SHOT = os.getenv('ONE_SHOT', 'false').lower() == 'true'
TG_CONTROL_ENABLED = os.getenv('TG_CONTROL_ENABLED', 'false').lower() == 'true'
RUST_APP_ID = 252490

STATE_FILE = pathlib.Path(__file__).parent / "ls_bot_state.json"

# Only import steam_client when we actually need it
_steam = None

def _get_steam():
    global _steam
    if _steam is None:
        import steam_client as sc
        _steam = sc
    return _steam


def load_state() -> dict:
    try:
        s = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        s = {}
    s.setdefault("pending_buys",     {})
    s.setdefault("notified_trades",  {})
    s.setdefault("pending_inv",      [])
    s.setdefault("pending_inv_ts",   {})  # name -> ts, когда попал в pending_inv (для таймаута)
    s.setdefault("pending_inv_alerted", {})  # name -> True, чтобы не спамить одним и тем же предупреждением
    s.setdefault("recently_closed", {})  # name -> ts закрытия сделки, для POST_CLOSE_COOLDOWN_SEC
    s.setdefault("ls_return_banned", {})  # name -> ts отмены самой LIS-SKINS, для LS_RETURN_BAN_SEC
    s.setdefault("buy_custom_ids", {})  # name -> custom_id последней покупки LS, живёт до _mark_closed
    s.setdefault("loss_followups", [])  # отложенная перепроверка возврата денег LS после списания в убыток
    # pending_deposits: КЛЮЧ — свой uuid, pending_deposit: КЛЮЧ — DMarket item_id.
    # Раньше оба ключевались по НАЗВАНИЮ предмета, и это был корень целой серии
    # багов (2026-07-31): два одинаковых предмета затирали друг друга, второй
    # исчезал из конвейера навсегда и оставался лежать в Steam. Название теперь
    # хранится ВНУТРИ записи в поле "name".
    s.setdefault("pending_deposits", {})
    if not isinstance(s.get("pending_deposit"), dict):
        s["pending_deposit"] = {}  # миграция со старого формата (list[str])
    # Миграция со старого формата "ключ = название": заводим уникальный ключ и
    # переносим название внутрь. Без неё старые записи потерялись бы на первом
    # же обращении к info["name"].
    for _k, _v in list(s["pending_deposits"].items()):
        if isinstance(_v, dict) and "name" not in _v:
            _v["name"] = _k
            s["pending_deposits"][uuid.uuid4().hex[:12]] = _v
            del s["pending_deposits"][_k]
    for _k, _v in list(s["pending_deposit"].items()):
        if isinstance(_v, dict) and "name" not in _v:
            _v["name"] = _k
            new_key = _v.get("item_id") or uuid.uuid4().hex[:12]
            s["pending_deposit"][new_key] = _v
            if new_key != _k:
                del s["pending_deposit"][_k]
    s.setdefault("buy_prices",       {})
    for _name, _val in list(s["buy_prices"].items()):
        if not isinstance(_val, list):  # миграция со старого формата {name: price}
            s["buy_prices"][_name] = [_val] if _val else []
    s.setdefault("last_heartbeat_hour", -1)
    s.setdefault("ls_balance_alert_sent", False)
    s.setdefault("last_balance_check_ts", 0)
    s.setdefault("recent_buys", {})  # name -> [timestamps] (защита от частых повторных покупок одного тайтла)
    s.setdefault("opportunity_checks", {})  # name -> repeated live profitability checks
    return s


def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')


def _peek_buy_price(state: dict, name: str) -> float:
    """Цена закупки БЕЗ изъятия из очереди — нужна для проверки пола при
    переоценке оффера. Забирать её здесь нельзя: очередь расходуется только
    в момент фактической продажи (_pop_buy_price), иначе сделка закроется
    с 'Куплено: $0.00'."""
    q = state.get("buy_prices", {}).get(name)
    return q[0] if q else 0.0


def _pop_buy_price(state: dict, name: str) -> float:
    """buy_prices[name] - очередь цен (FIFO), не одно значение: если в
    работе одновременно несколько покупок одного и того же тайтла, плоский
    словарь {name: price} терял/путал цену при перезаписи (обнаружено
    вживую 2026-07-28: Ornate Wooden Door закрылся с "Куплено: $0.00").
    Забираем самую старую (первую купленную) цену из очереди на это имя."""
    q = state.get("buy_prices", {}).get(name)
    if not q:
        return 0.0
    return q.pop(0)


# ─── Find opportunities ────────────────────────────────────────────────────────

def find_opportunities(price_map: dict, dm_prices: dict, source: str) -> list:
    """
    Быстрый bulk-фильтр по aggregated-prices — только для предварительного
    отбора. aggregated orderBestPrice может быть устаревшей/недостижимой
    (см. project_arb_bot memory), поэтому реальные деньги тратятся ТОЛЬКО
    после живой проверки через dm.get_target_price() в try_buy() и
    check_dmarket_offers().

    net/pct считаются С УЧЁТОМ комиссии DMarket (dm.DM_FEE), как и в живой
    проверке try_buy() — иначе (баг, найденный вживую 2026-07-27) порог
    MIN_PROFIT_PCT здесь по факту требует на ~7.3пп больше, чем реально
    нужно, и почти ни одна "находка" не проходит живую проверку (0 покупок
    за 12ч при 24+ "агрегированных" находках).
    """
    results = []
    for name, buy_p in price_map.items():
        if buy_p < MIN_BUY_USD:
            continue
        d = dm_prices.get(name)
        if not d:
            continue
        target    = d["order"]
        order_cnt = d["order_cnt"]
        if target == 0 or order_cnt < MIN_ORDERS:
            continue
        net = dm.net_after_fee(target)
        pct = (net - buy_p) / buy_p * 100
        if pct >= MIN_PROFIT_PCT:
            results.append({
                "name":   name,
                "ls":     buy_p,
                "source": source,
                "target": target,
                "net":    round(net, 3),
                "pct":    round(pct, 1),
                "orders": order_cnt,
            })
    results.sort(key=lambda x: -x["pct"])
    return results


def build_skins_table_snapshot(ls_rows: dict, order_rows: dict,
                               now: float | None = None) -> tuple[set, dict]:
    return _build_table_snapshot(
        ls_rows, order_rows,
        min_buy_usd=MIN_BUY_USD,
        max_buy_usd=(float("inf") if MAX_BUY_USD <= 0 else MAX_BUY_USD),
        min_orders=MIN_ORDERS,
        min_profit_pct=MIN_PROFIT_PCT,
        dmarket_fee=dm.DM_FEE,
        max_age_sec=SKINSTABLE_MAX_AGE_SEC,
        now=now,
    )


def confirm_opportunity_live(opp: dict, ls_items: list, state: dict) -> bool:
    """First live confirmation; try_buy performs the final check again."""
    name = opp["name"]
    lots = [item for item in ls_items if item["name"] == name]
    if not lots:
        state.setdefault("opportunity_checks", {}).pop(name, None)
        return False
    lot = min(lots, key=lambda item: item["price"])
    target = dm.get_best_target(name)
    if not target:
        state.setdefault("opportunity_checks", {}).pop(name, None)
        return False
    live_net = dm.net_after_fee(target["price"])
    live_pct = (live_net - lot["price"]) / lot["price"] * 100
    confirmed = register_live_confirmation(
        state.setdefault("opportunity_checks", {}), name, live_pct,
        minimum_profit_pct=MIN_PROFIT_PCT,
        required=LIVE_CONFIRMATIONS_REQUIRED,
        window_sec=LIVE_CONFIRMATION_WINDOW_SEC,
    )
    count = state["opportunity_checks"].get(name, {}).get("count", 0)
    print(f"[confirm] {name}: live net {live_pct:+.2f}% "
          f"({count}/{LIVE_CONFIRMATIONS_REQUIRED})")
    return confirmed


# ─── Buy on lis-skins ─────────────────────────────────────────────────────────

def _name_in_flight(state: dict, name: str) -> bool:
    """
    Есть ли ЛЮБОЙ незакрытый экземпляр этого тайтла на любой стадии конвейера
    (куплен на LS / в Steam-инвентаре / депозит идёт / выставлен на DMarket).

    Раньше единственной защитой было pending_buys + окно REBUY_WINDOW_SEC=600с —
    как только покупка уходила из pending_buys (доставилась), название снова
    становилось доступно для покупки уже через 10 минут, даже если сама сделка
    (депозит/продажа) ещё не завершилась. Обнаружено 2026-08-03/04: Toy Crossbow,
    Frog Cosplay Pants, Banger AR и др. покупались повторно, пока первый экземпляр
    ещё был в пути — инвентарный детектор "трейд принят вне бота" матчит по
    ИМЕНИ, не по конкретному предмету, и спутал вторую покупку с тем же
    физическим экземпляром первой: деньги на LS списались, а второй копии
    предмета не было. Теперь блокируем повтор, пока весь цикл не закрыт.
    """
    if name in state["pending_buys"]:
        return True
    if name in state["pending_inv"]:
        return True
    if any(info.get("name") == name for info in state["pending_deposits"].values()):
        return True
    if any(info.get("name") == name for info in state["pending_deposit"].values()):
        return True
    return False


def _mark_closed(state: dict, name: str):
    """
    Отмечает момент, когда цикл по тайтлу name реально завершился (продан,
    признан утерянным, или сдан после расширенной проверки) — используется
    POST_CLOSE_COOLDOWN_SEC ниже, чтобы не покупать тот же тайтл сразу
    следом. _name_in_flight() защищает, ПОКА предмет едет; этого было
    недостаточно — как только сделка закрывалась (например, менее чем за
    минуту при бойком тайтле вроде Rumble AK47/Copper Hoodie), бот тут же
    покупал следующий экземпляр, и инвентарный детектор снова путал его с
    только что уехавшим (обнаружено 2026-08-06: 6 таких потерь подряд).
    Теперь после закрытия — пауза.
    """
    state.setdefault("recently_closed", {})[name] = time.time()
    state.get("buy_custom_ids", {}).pop(name, None)
    state.get("opportunity_checks", {}).pop(name, None)


def _purchase_precheck_blocked(state: dict, name: str) -> bool:
    """Cheap guard used before live API confirmations to avoid pointless polling."""
    now = time.time()
    if _name_in_flight(state, name):
        return True
    banned_at = state.get("ls_return_banned", {}).get(name)
    if banned_at and now - banned_at < LS_RETURN_BAN_SEC:
        return True
    closed_at = state.get("recently_closed", {}).get(name)
    if closed_at and now - closed_at < POST_CLOSE_COOLDOWN_SEC:
        return True
    if now - state.get("last_buy_ts", 0) < BUY_COOLDOWN:
        return True
    if len(state.get("pending_buys", {})) >= MAX_CONCURRENT:
        return True
    return False


def _daily_spend(state: dict) -> dict:
    """Return today's cumulative buy spend, resetting only on a new UTC date."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    row = state.setdefault("daily_spend", {"date": today, "usd": 0.0})
    if row.get("date") != today:
        row.clear()
        row.update({"date": today, "usd": 0.0})
    row["usd"] = max(0.0, float(row.get("usd", 0.0)))
    return row


def try_buy(opp: dict, ls_items: list, state: dict) -> bool:
    name = opp["name"]
    source = opp.get("source", "lis-skins")

    banned_at = state.get("ls_return_banned", {}).get(name)
    if banned_at and time.time() - banned_at < LS_RETURN_BAN_SEC:
        remaining = int(LS_RETURN_BAN_SEC - (time.time() - banned_at))
        print(f"[buy] {name}: LIS-SKINS сама отменяла этот тайтл — бан ещё {remaining}с — пропуск")
        return False

    closed_at = state.get("recently_closed", {}).get(name)
    if closed_at and time.time() - closed_at < POST_CLOSE_COOLDOWN_SEC:
        remaining = int(POST_CLOSE_COOLDOWN_SEC - (time.time() - closed_at))
        print(f"[buy] {name}: сделка закрылась недавно, жду ещё {remaining}с "
              f"(пауза после закрытия) — пропуск")
        return False

    if _name_in_flight(state, name):
        print(f"[buy] {name}: предыдущая сделка ещё не завершена — пропуск")
        return False

    # Защита от частых повторных покупок ОДНОГО тайтла (обнаружено вживую
    # 2026-07-27: бот честно купил один и тот же скин 3 раза подряд за
    # ~5 минут — не баг, но пользователь захотел ограничить). Считаем
    # только покупки за последние REBUY_WINDOW_SEC секунд. Работает ПОВЕРХ
    # _name_in_flight — даже после полного закрытия сделки не даёт долбить
    # один и тот же тайтл слишком часто.
    recent = [t for t in state["recent_buys"].get(name, []) if time.time() - t < REBUY_WINDOW_SEC]
    state["recent_buys"][name] = recent
    if len(recent) >= MAX_REBUYS_PER_NAME:
        print(f"[buy] {name}: уже куплен {len(recent)}x за последние {REBUY_WINDOW_SEC}s "
              f"(лимит {MAX_REBUYS_PER_NAME}) — пропуск")
        return False

    last_buy = state.get("last_buy_ts", 0)
    if time.time() - last_buy < BUY_COOLDOWN:
        remaining = int(BUY_COOLDOWN - (time.time() - last_buy))
        print(f"[buy] Cooldown: next buy in {remaining}s")
        return False

    if len(state["pending_buys"]) >= MAX_CONCURRENT:
        print(f"[buy] Max {MAX_CONCURRENT} concurrent buys reached")
        return False

    daily = _daily_spend(state)
    daily_remaining = (float("inf") if MAX_DAILY_SPEND_USD <= 0
                       else max(0.0, MAX_DAILY_SPEND_USD - daily["usd"]))
    if MAX_DAILY_SPEND_USD > 0 and daily_remaining < MIN_BUY_USD:
        print(f"[buy] Daily spend limit reached: ${daily['usd']:.2f}/"
              f"${MAX_DAILY_SPEND_USD:.2f}")
        return False

    if opp["ls"] < MIN_BUY_USD or (MAX_BUY_USD > 0 and opp["ls"] > MAX_BUY_USD):
        print(f"[buy] {name}: ${opp['ls']:.2f} > MAX_BUY_USD=${MAX_BUY_USD}")
        return False

    candidates = sorted((it for it in ls_items if it["name"] == name), key=lambda x: x["price"])
    if not candidates:
        print(f"[buy] {name}: not found in {source} listings")
        return False

    # Живая проверка реальной ⚡ instant-цены ПЕРЕД тратой денег — aggregated
    # orderBestPrice (opp['target']) может быть устаревшей/недостижимой. Не
    # зависит от того, какой именно лот LS выберем — считаем один раз.
    real_instant = dm.get_target_price(name)
    if real_instant is None:
        print(f"[buy] {name}: не удалось подтвердить живую ⚡ цену — пропуск")
        return False
    real_net = dm.net_after_fee(real_instant)

    # РАНЬШЕ брали только САМЫЙ дешёвый лот — если именно он оказывался
    # недоступен (status=return/skins_unavailable на стороне продавца),
    # бот сдавался по всей возможности целиком, хотя следующий по цене лот
    # мог быть ещё вполне прибыльным. Теперь при отказе конкретного лота
    # (400/422 "цена/наличие изменились") пробуем следующий по цене, пока
    # маржа не упадёт ниже порога или не кончатся попытки.
    MAX_ATTEMPTS = 3
    for attempt, item in enumerate(candidates[:MAX_ATTEMPTS], 1):
        if item["price"] < MIN_BUY_USD or (MAX_BUY_USD > 0 and item["price"] > MAX_BUY_USD):
            continue
        if item["price"] > daily_remaining + 1e-9:
            print(f"[buy] {name}: ${item['price']:.2f} exceeds remaining daily "
                  f"budget ${daily_remaining:.2f} — skip")
            break
        real_pct = (real_net - item["price"]) / item["price"] * 100
        if real_pct < MIN_PROFIT_PCT:
            print(f"[buy] {name}: лот #{attempt} ${item['price']:.2f} — живой профит "
                  f"{real_pct:.1f}% < {MIN_PROFIT_PCT}% — пропуск (aggregated показывал "
                  f"{opp['pct']:.1f}%)")
            break  # дальше по списку лоты только дороже — смысла продолжать нет

        print(f"[buy] Buying {name} on {source} for ${item['price']:.2f} "
              f"(live confirmed: {real_pct:+.1f}%, лот #{attempt})")

        # custom_id — по рекомендации LIS-SKINS, чтобы не купить дважды при обрыве связи.
        custom_id = f"arb_{item['id']}_{int(time.time())}"
        # max_price=item['price'] — сервер LIS-SKINS сам не даст купить дороже
        # ожидаемой цены (защита от изменения цены между сканом и покупкой).
        result = ls.buy_item_api(item["id"], item["price"], custom_id=custom_id)
        if not result.get("ok") and result.get("network_error"):
            time.sleep(3)
            info = ls.get_purchase_info(custom_ids=[custom_id])
            if info:
                print(f"[buy] {name}: покупка всё же прошла (custom_id найден) — не дублируем")
                result = {"ok": True, "status": 200, "body": {"data": info[0]}}

        if result.get("ok"):
            # ОБНАРУЖЕНО ВЖИВУЮ 2026-08-12: item['price'] — это цена из СНИМКА
            # сканирования (candidates), а не то, что LIS-SKINS реально
            # списала — max_price лишь потолок, реальный лот мог за это время
            # подешеветь, и LIS-SKINS спишет МЕНЬШЕ. Расхождение снимок vs
            # факт (например лог писал "$0.69", а по факту купили за $0.67)
            # сломало сопоставление записей при разборе потерянной покупки —
            # больше часа разбирались, какая строка какой реальной покупке
            # соответствует. Берём настоящую цену из ответа API, если она там
            # есть; иначе остаёмся на снимке как раньше.
            real_price = item["price"]
            try:
                real_price = float(result["body"]["data"]["skins"][0]["price"])
            except (KeyError, IndexError, TypeError, ValueError):
                pass

            state["pending_buys"][name] = {
                "id":        item.get("id", ""),
                "price":     real_price,
                "class_id":  item.get("class_id", ""),
                "source":    source,
                "ts":        int(time.time()),
                "custom_id": custom_id,  # для ls.get_purchase_info() при разборе потерь позже
            }
            # ОТДЕЛЬНО от pending_buys — тот чистится/переносится по пути (pending_inv
            # список без данных, pending_deposits/pending_deposit по своим ключам), а
            # custom_id должен дожить до самого конца цикла, до _mark_closed(), чтобы
            # при потере можно было спросить lis-skins напрямую "что стало с заказом".
            state.setdefault("buy_custom_ids", {})[name] = custom_id
            state.setdefault("buy_prices", {}).setdefault(name, []).append(real_price)
            state["last_buy_ts"] = int(time.time())
            daily["usd"] = round(daily["usd"] + real_price, 2)
            state["recent_buys"].setdefault(name, []).append(time.time())
            state.setdefault("opportunity_checks", {}).pop(name, None)
            save_state(state)
            trade_log.log_buy(name, real_price, real_pct)
            tg.send(
                f"🛒 <b>Bought on {source}</b>\n"
                f"📦 {name}\n"
                f"💰 ${real_price:.2f} → DMarket ⚡ ${real_instant:.2f} "
                f"(net ${real_net:.2f}, {real_pct:+.1f}%)"
            )
            print(f"[buy] OK: {name} for ${real_price:.2f}"
                  + (f" (снимок показывал ${item['price']:.2f})" if abs(real_price - item['price']) > 0.001 else ""))
            return True

        if result.get("status") in (400, 422):
            print(f"[buy] {name}: лот #{attempt} ${item['price']:.2f} — цена/наличие "
                  f"изменились ({result.get('body')}) — пробую следующий лот")
            continue

        print(f"[buy] Error: {result.get('body', '?')}")
        return False

    return False


def check_ls_balance(state: dict):
    """Раз в LS_BALANCE_CHECK_SEC проверяет баланс lis-skins и шлёт в Telegram
    предупреждение, если он опустился ниже LS_BALANCE_ALERT_USD (иначе бот
    молча перестаёт покупать, когда денег не хватает). Шлёт один раз за
    "просадку" — сбрасывается, когда баланс снова поднимется выше порога."""
    if time.time() - state.get("last_balance_check_ts", 0) < LS_BALANCE_CHECK_SEC:
        return
    state["last_balance_check_ts"] = int(time.time())

    balance = ls.get_balance()
    if balance < 0:
        return  # API error — не считаем это просадкой баланса

    if balance < LS_BALANCE_ALERT_USD:
        if not state.get("ls_balance_alert_sent"):
            tg.send(f"💸 <b>Баланс LIS-SKINS низкий: ${balance:.2f}</b>\n"
                    f"Пополни — иначе покупки скоро остановятся.")
            state["ls_balance_alert_sent"] = True
            save_state(state)
    elif state.get("ls_balance_alert_sent"):
        state["ls_balance_alert_sent"] = False
        save_state(state)


def _ls_status(info: list[dict]) -> str:
    """Статус конкретного скина в ответе get_purchase_info() лежит ВНУТРИ
    purchase["skins"][0]["status"], а не на верхнем уровне объекта purchase.

    ОБНАРУЖЕНО ВЖИВУЮ 2026-08-15 (Scientific Components Storage, $1.31):
    три места в этом файле (проверка "зависшей" покупки, check_loss_followups,
    сообщение о списании) читали info[0].get('status') — несуществующий на
    этом уровне ключ, всегда молча дававший дефолт '?'. Из-за этого детекция
    возврата денег НИКОГДА не срабатывала в этих трёх местах, хотя LIS-SKINS
    честно отдавала status='return' (return_reason='trade_timeout') для
    заказа, который потом списали как 100%-убыток. Верный доступ уже был в
    этом же файле (см. ниже) — вынесен сюда, чтобы больше не разъезжался."""
    if not info:
        return "?"
    skins = info[0].get("skins") or [{}]
    return skins[0].get("status", "?")


def check_ls_purchase_status(state: dict):
    """
    Спрашивает саму LIS-SKINS о статусе покупок, которые ещё ждут Steam-трейд.
    Обнаружено 2026-08-08 живьём (Pirate Poncho, $2.89): LIS-SKINS иногда
    отменяет покупку СРАЗУ — status="return", return_reason="trade_create_error"
    (например error="skins_unavailable") — и мгновенно возвращает деньги на
    баланс, причём steam_trade_offer_id вообще null: Steam-трейд не создавался
    и никогда не появится. Раньше бот об этом не знал и час ждал трейда,
    которому неоткуда взяться, а потом ложно записывал это как "необъяснимую
    потерю" — хотя деньги давно вернулись на LS. Теперь проверяем статус
    заказа НАПРЯМУЮ, пока покупка висит в pending_buys, и если LS сама
    сообщает об отмене — сразу закрываем без лишнего ожидания и без пометки
    "убыток" (это не убыток: деньги на LS не потрачены).
    """
    if not state["pending_buys"]:
        return
    changed = False
    for name, info in list(state["pending_buys"].items()):
        custom_id = info.get("custom_id")
        if not custom_id:
            continue
        try:
            data = ls.get_purchase_info(custom_ids=[custom_id])
        except Exception as e:
            print(f"[ls-status] {name}: ошибка проверки статуса: {e}")
            continue
        if not data:
            continue
        skin = (data[0].get("skins") or [{}])[0]
        status = _ls_status(data)
        if status == "return":
            reason = skin.get("return_reason") or skin.get("error") or "?"
            print(f"[ls-status] {name}: LIS-SKINS отменила заказ сама "
                  f"(status=return, reason={reason}) — деньги уже на балансе LS, "
                  f"убираю из ожидания")
            state["pending_buys"].pop(name, None)
            _pop_buy_price(state, name)  # эта цена никогда не станет реальной закупкой — не должна влиять на пол будущей продажи
            trade_log.remove_pending(name)
            # РАНЬШЕ после такой отмены не было паузы длиннее обычного 10-минутного
            # окна recent_buys — если у конкретного тайтла проблема на стороне
            # продавца держится долго, бот бился в неё каждые ~10 минут по кругу
            # (подтверждено живьём 2026-08-08: Pirate Poncho отменялась 4 раза за
            # 40 минут подряд). Деньгам это не вредит (LS всегда возвращает), но
            # это бессмысленный цикл и спам в Telegram. Используем тот же
            # 30-минутный кулдаун, что и после обычного закрытия сделки.
            _mark_closed(state, name)
            state["ls_return_banned"][name] = time.time()
            tg.send(f"↩️ <b>{name}</b>: LIS-SKINS сама отменила покупку "
                    f"(${info.get('price', 0):.2f}, причина: {reason}) и вернула "
                    f"деньги на баланс — это не убыток, просто товар оказался "
                    f"недоступен у продавца. Бан тайтла на {LS_RETURN_BAN_SEC // 3600} ч "
                    f"перед повторной попыткой.")
            changed = True
    if changed:
        save_state(state)


# ─── Steam trades ─────────────────────────────────────────────────────────────

def check_steam_trades(state: dict):
    try:
        steam = _get_steam()
        offers = steam.get_trade_offers()
    except Exception as e:
        print(f"[trades] error: {e}")
        return

    active = [o for o in offers if o.get("trade_offer_state") == 2]
    print(f"[trades] Active trades: {len(active)}")

    for offer in active:
        oid   = offer["tradeofferid"]
        names = offer.get("_item_names", [])
        name_str = ", ".join(names) if names else "?"
        partner  = str(int(offer.get("accountid_other", 0)) + 76561197960265728)

        entry = state["notified_trades"].get(oid)
        if isinstance(entry, str):
            # Старый формат (просто имя строкой, до фикса) — раньше писался
            # независимо от того, прошёл accept_trade() или нет (тот самый
            # баг). Раз этот oid ВСЁ ЕЩЁ в active (Steam сам говорит "не
            # принято" — иначе оффер бы сюда не попал), значит запись НЕ
            # отражает реальное принятие. Мигрируем как непринятую, чтобы
            # бот продолжил пытаться, а не бросал трейд навсегда.
            entry = {"name": entry, "accepted": False, "attempts": 0}
            state["notified_trades"][oid] = entry

        if entry is None:
            items_to_give = offer.get("items_to_give", [])
            is_give_only  = len(items_to_give) == 0 and len(offer.get("items_to_receive", [])) > 0
            is_expected   = any(n in state["pending_buys"] for n in names)
            # ВАЖНО: принимаем ТОЛЬКО то, что реально ждём (есть в pending_buys).
            # Раньше принималась любая "подарочная" заявка (is_give_only) — из-за
            # этого в инвентарь заезжал посторонний хлам (Dota-меч, TF2-ключ,
            # 2026-07-29), а заодно бот молча перехватывал выводы соседних схем.
            # Незапрошенные подарки больше не принимаем: шлём уведомление один
            # раз и оставляем решение пользователю.
            if not is_expected:
                if is_give_only and oid not in state.setdefault("notified_gifts", []):
                    state["notified_gifts"].append(oid)
                    state["notified_gifts"] = state["notified_gifts"][-50:]
                    save_state(state)
                    tg.send(f"🎁 <b>Незапрошенный подарок</b> (не принимаю сам)\n"
                            f"📦 {name_str}\n"
                            f"https://steamcommunity.com/tradeoffer/{oid}/")
                continue
            print(f"[trades] Incoming trade {oid}: {name_str}")
            entry = {"name": name_str, "accepted": False, "attempts": 0}
            state["notified_trades"][oid] = entry
            if not AUTO_ACCEPT:
                tg.send(f"📨 <b>Trade received</b> (accept manually)\n"
                        f"📦 {name_str}\nhttps://steamcommunity.com/tradeoffer/{oid}/")
                save_state(state)
                continue

        if not AUTO_ACCEPT or entry.get("accepted"):
            continue

        # ВАЖНО: раньше это место безусловно переносило предмет в pending_inv
        # и запоминало oid в notified_trades ДАЖЕ если accept_trade() вернул
        # False — трейд оставался непринятым в Steam навсегда (notified_trades
        # блокирует повторную попытку выше), а бот считал вещь "уже едет на
        # депозит" и вечно не находил её через DMarket. Подтверждено живьём
        # 2026-07-25: баг в pysteamauth-релогине (см. steam_login.py) валил
        # КАЖДУЮ попытку accept_trade() после первого часа работы, все ночные
        # покупки повисли непринятыми. Теперь: не принято -> пробуем снова
        # каждый цикл, состояние не трогаем, пока реально не примется.
        accepted = steam.accept_trade(oid, partner)
        entry["attempts"] = entry.get("attempts", 0) + 1
        if accepted:
            entry["accepted"] = True
            tg.send(f"✅ <b>Trade accepted</b>\n📦 {name_str}")
            for n in names:
                state["pending_buys"].pop(n, None)
                if n not in state["pending_inv"]:
                    state["pending_inv"].append(n)
                state["pending_inv_ts"].setdefault(n, int(time.time()))
        elif entry["attempts"] == 3 and not entry.get("manual_hint_sent"):
            entry["manual_hint_sent"] = True
            tg.send(f"⚠️ <b>{name_str}</b>: не получается принять трейд автоматически "
                    f"({entry['attempts']} попытки). Бот продолжит пробовать сам, но "
                    f"можно принять и вручную:\nhttps://steamcommunity.com/tradeoffer/{oid}/")
        else:
            print(f"[trades] {oid} ({name_str}): accept не прошёл, попробую снова "
                  f"в следующем цикле (попытка {entry['attempts']})")
        save_state(state)

    # Clean stale buys (>STALE_BUY_TIMEOUT_SEC без трейда). Раньше было 30 мин
    # (1800с) — короче реальной доставки lis-skins (замерена 25-42 мин), т.е.
    # бот мог сдаться и списать покупку в убыток буквально за несколько минут
    # до того, как трейд пришёл бы сам. Обнаружено 2026-08-06: Summer Rug и
    # Lovestruck Coffee Can, обе удалены на 30-й минуте без единого замеченного
    # входящего трейда — правдоподобно, что просто не успели доехать.
    stale_cutoff = time.time() - STALE_BUY_TIMEOUT_SEC
    stale = [n for n, v in list(state["pending_buys"].items())
             if v.get("ts", 0) < stale_cutoff]
    if not stale:
        return

    # ВАЖНО: предмет мог УЖЕ прийти в инвентарь — например, трейд принят
    # вручную (или через /confirm), и тогда бот никогда не видел заявку
    # активной, а значит не перевёл покупку в pending_inv сам. Выбрасывать
    # её в этом случае нельзя: предмет оплачен и лежит в Steam, но выпадает
    # из конвейера навсегда и остаётся мёртвым грузом.
    # Подтверждено живьём 2026-07-29: The Tiger Hoodie ($0.47) и Twig Box
    # ($0.66) были удалены отсюда по таймауту, хотя оба лежали в инвентаре;
    # после ручного переноса в pending_inv бот сразу продал их с +12.8% и
    # +10.6%. Поэтому перед удалением — всегда сверяемся с инвентарём.
    status, _ = dm._req("GET", "/marketplace-api/v2/user/inventory?gameId=rust&limit=1", retries=2)
    if status != 200:
        # get_uninvested_steam_items() отдаёт {} и при ошибке API, и при
        # реально пустом инвентаре — различить нельзя. Поэтому при недоступном
        # API ничего не удаляем: лучше почистить на следующем цикле, чем
        # выбросить оплаченный предмет из-за сетевого сбоя.
        print(f"[trades] stale-check: инвентарь недоступен (HTTP {status}) — "
              f"покупки не трогаю, проверю в следующем цикле")
        return

    inv_map = dm.get_uninvested_steam_items()
    for n in stale:
        buy_info = state["pending_buys"].get(n, {})
        state["pending_buys"].pop(n, None)
        if n in inv_map:
            print(f"[trades] Stale buy {n}: предмет УЖЕ в инвентаре — "
                  f"перевожу в pending_inv вместо удаления")
            if n not in state["pending_inv"]:
                state["pending_inv"].append(n)
            state["pending_inv_ts"].setdefault(n, int(time.time()))
        else:
            # РАНЬШЕ здесь просто удаляли строку из trade_log.xlsx через
            # remove_pending() без единой проверки и без алерта — деньги
            # списывались на LS, доставка так и не была замечена ботом ни
            # разу за 30 минут (ни один "Incoming trade" в логе), и итог
            # пропадал БЕСследно — даже строки не оставалось, чтобы потом
            # спросить "что случилось". Обнаружено 2026-08-06: Summer Rug
            # ($1.61) и Lovestruck Coffee Can ($0.94) ушли именно так.
            # Теперь сверяемся с фактом так же, как при потере депозита:
            # широкий поиск продажи + проверка незадепозиченного, и если
            # ничего не подтверждает — явно алертим и фиксируем убыток,
            # а не стираем историю покупки.
            wide_sale = dm.find_recent_sale(n, since_ts=buy_info.get("ts", 0), limit=200)
            if wide_sale:
                buy_price = _pop_buy_price(state, n)
                net = wide_sale["price"] - wide_sale["fee"]
                trade_log.log_sale(n, wide_sale["price"], wide_sale["fee"], buy_price)
                tg.send_item_on_dmarket(n, wide_sale["price"], net, buy_price)
                print(f"[trades] Stale buy {n}: нашёлся при расширенной проверке — "
                      f"продан за ${wide_sale['price']:.2f} (net ${net:.2f})")
                _mark_closed(state, n)
                continue
            if dm.get_unlisted_items("rust", n):
                print(f"[trades] Stale buy {n}: нашёлся в незадепозиченном на "
                      f"DMarket — жду обычный цикл продажи")
                if n not in state["pending_inv"]:
                    state["pending_inv"].append(n)
                state["pending_inv_ts"].setdefault(n, int(time.time()))
                continue
            print(f"[trades] Stale buy removed: {n}: не подтверждено ни фактом "
                  f"продажи, ни наличием на любой стороне")
            lost_price = _peek_buy_price(state, n)
            ls_custom_id = buy_info.get("custom_id") or state.get("buy_custom_ids", {}).get(n)
            ls_status = None
            if ls_custom_id:
                ls_info = ls.get_purchase_info(custom_ids=[ls_custom_id])
                if ls_info:
                    ls_status = _ls_status(ls_info)

            # ОБНАРУЖЕНО ВЖИВУЮ 2026-08-13 (Santa's Helper AR, $0.53): и
            # STALE_BUY_TIMEOUT_SEC (списание) и финализация возврата на
            # LIS-SKINS происходят примерно в одну и ту же ~часовую отметку —
            # отложенная перепроверка (check_loss_followups) иногда стартует
            # ДО того как LIS-SKINS успевает обновить статус, и единственная
            # попытка потом уже ничего не находит. Раз статус УЖЕ запрошен
            # прямо сейчас — используем его сразу: если это явный возврат, не
            # пишем убыток в журнал вообще, вместо того чтобы гадать позже.
            refund_words = ("refund", "return", "cancel", "declin", "возврат", "отмен")
            looks_refunded_now = ls_status and any(w in str(ls_status).lower() for w in refund_words)

            _pop_buy_price(state, n)
            if looks_refunded_now:
                trade_log.remove_pending(n)
                tg.send(f"💰 <b>{n}</b>: покупка не пришла (LIS-SKINS статус: {ls_status}) — "
                        f"деньги вернулись, это не убыток. Строка в журнале не заводится.")
            else:
                tg.send(f"❓ <b>{n}</b>: покупка потеряна — доставка ни разу не "
                        f"замечена за 30 мин, предмета нет ни в Steam, ни на "
                        f"DMarket, продажа не найдена. Деньги (${lost_price:.2f}) "
                        f"списываю как убыток в журнале."
                        + (f"\nСтатус на LIS-SKINS: {ls_status}" if ls_status else ""))
                trade_log.log_sale(n, 0, 0, lost_price)
                # LIS-SKINS может вернуть деньги на баланс уже ПОСЛЕ того, как мы
                # сдались (подтверждено пользователем 2026-08-07: "спустя время
                # деньги возвращаются на лисскинс") — статус в момент потери мог
                # ещё не обновиться. Планируем повторную проверку через
                # LOSS_FOLLOWUP_DELAY_SEC — см. check_loss_followups().
                if ls_custom_id:
                    state.setdefault("loss_followups", []).append({
                        "name": n, "custom_id": ls_custom_id, "buy_price": lost_price,
                        "marked_ts": time.time(),
                    })
            _mark_closed(state, n)
    save_state(state)


def check_loss_followups(state: dict):
    """
    Перепроверяет заказы, которые мы недавно списали как потерю (see
    _mark_closed callers) — LIS-SKINS может вернуть деньги на баланс уже
    ПОСЛЕ того как бот сдался (подтверждено пользователем 2026-08-07: "спустя
    время деньги возвращаются на лисскинс"), а статус заказа в момент самой
    потери мог ещё не обновиться. Через LOSS_FOLLOWUP_DELAY_SEC спрашиваем
    ещё раз; если статус похож на отмену/возврат — правим запись в trade_log
    сами (см. trade_log.mark_refunded) вместо того, чтобы просить пользователя
    лезть в Excel руками.

    ОБНАРУЖЕНО ВЖИВУЮ 2026-08-12 (Wings Of Death SKS, $0.67): раньше это была
    РОВНО ОДНА перепроверка — а реальный возврат у LIS-SKINS оформился только
    через ~65 минут после покупки (создание оффера + 30-мин окно истечения +
    ~35 мин до финального статуса), тогда как списание в убыток само по себе
    уже происходит через ~30 мин ожидания, плюс LOSS_FOLLOWUP_DELAY_SEC — итого
    единственная проверка иногда попадала РАНЬШЕ, чем LIS-SKINS успевала
    зафиксировать возврат, и автофикс молча не срабатывал. Теперь до
    MAX_LOSS_FOLLOWUP_ATTEMPTS попыток с тем же интервалом.
    """
    followups = state.get("loss_followups", [])
    if not followups:
        return
    now = time.time()
    remaining = []
    for f in followups:
        attempt = f.get("attempt", 0)
        if now - f.get("last_check_ts", f["marked_ts"]) < LOSS_FOLLOWUP_DELAY_SEC:
            remaining.append(f)
            continue
        info = ls.get_purchase_info(custom_ids=[f["custom_id"]])
        status = _ls_status(info) if info else "не найден"
        refund_words = ("refund", "return", "cancel", "declin", "возврат", "отмен")
        looks_refunded = any(w in str(status).lower() for w in refund_words)
        if looks_refunded:
            fixed = trade_log.mark_refunded(f["name"], f["marked_ts"])
            icon = "💰"
            fix_line = ("\n(запись в trade_log поправлена автоматически — это не убыток, деньги вернулись)"
                        if fixed else
                        "\n(похоже на возврат, но НЕ нашёл строку в trade_log, чтобы поправить "
                        "автоматически — проверь вручную)")
            tg.send(f"{icon} <b>{f['name']}</b>: повторная проверка (попытка {attempt + 1}) "
                    f"после списания в убыток (${f['buy_price']:.2f}). "
                    f"Статус на LIS-SKINS: {status}{fix_line}")
            continue  # нашли возврат — дальше не перепроверяем, из remaining не пополняем
        attempt += 1
        if attempt >= MAX_LOSS_FOLLOWUP_ATTEMPTS:
            tg.send(f"ℹ️ <b>{f['name']}</b>: финальная проверка после списания в убыток "
                    f"(${f['buy_price']:.2f}). Статус на LIS-SKINS: {status} — "
                    f"на возврат не похоже, оставляю как убыток.")
            continue  # исчерпали попытки — из remaining не пополняем
        f["attempt"] = attempt
        f["last_check_ts"] = now
        remaining.append(f)
    state["loss_followups"] = remaining
    save_state(state)


def check_declined_trades(state: dict):
    """
    Если входящий трейд с покупкой отменён/отклонён (получателем — то есть
    нами — или отправителем), он пропадает из get_trade_offers() (только
    Active), но не исчезает из истории. Проверяем последние 24ч на предмет
    Canceled/Declined среди предметов, которые всё ещё числятся в
    pending_buys — 2026-07-25 обнаружено вживую: за ночь накопилось 31
    отменённая (пользователем, вручную в Steam) покупка, которые повисли
    в pending_buys/trade_log.xlsx навсегда, поскольку ничего их оттуда не
    убирало. Теперь: отменили -> убираем из ожидания и из таблицы сразу.
    """
    if not state["pending_buys"]:
        return
    try:
        offers = _get_steam().get_recent_offers(hours=24)
    except Exception as e:
        print(f"[trades] check_declined error: {e}")
        return

    changed = False
    for offer in offers:
        if offer.get("trade_offer_state") not in (6, 7):  # Canceled, Declined
            continue
        for name in offer.get("_item_names", []):
            if name in state["pending_buys"]:
                print(f"[trades] {name}: трейд отменён/отклонён — убираю из "
                      f"ожидания и из trade_log.xlsx")
                state["pending_buys"].pop(name, None)
                trade_log.remove_pending(name)
                tg.send(f"🗑 <b>{name}</b>: трейд отменён/отклонён — покупка "
                        f"не завершилась, убрал из таблицы сделок")
                changed = True
    if changed:
        save_state(state)


# ─── Steam inventory ──────────────────────────────────────────────────────────

def check_steam_inventory(state: dict):
    """
    Проверяем приход предмета в Steam-инвентарь через DMarket API
    (get_uninvested_steam_items) — НЕ через steamcommunity.com напрямую,
    тот часто ловит 429 и блокируется на часы (проверено на практике).
    Перед проверкой триггерим sync — иногда DMarket не подхватывает свежий
    Steam-трейд сам по себе даже за 30+ минут, а после ручного sync находит
    сразу (проверено на практике 2026-07-24).

    Сверяем НЕ ТОЛЬКО pending_inv, но и pending_buys. Если трейд приняли
    вручную (или бот не успел увидеть заявку активной — get_trade_offers()
    показывает только state==2), покупка навсегда остаётся в pending_buys,
    хотя предмет уже лежит в Steam. Раньше это всплывало только на
    stale-пороге в 30 минут, то есть КАЖДЫЙ принятый вручную трейд
    простаивал полчаса (подтверждено живьём 2026-07-29 трижды подряд:
    The Tiger Hoodie, Twig Box, No Mercy Electric Furnace). Теперь предмет
    подхватывается на ближайшем цикле — задержка ~POLL_INTERVAL, а не 30 мин.
    """
    watch = list(state["pending_inv"])
    watch += [n for n in state["pending_buys"] if n not in watch]
    if not watch:
        return
    dm._req("POST", "/marketplace-api/v1/user-inventory/sync", {"Type": "Inventory", "GameID": "Rust"}, retries=2)
    assets = dm.get_uninvested_steam_assets()

    # Берём СПИСОК экземпляров, а не словарь по названию: одинаковых предметов
    # в инвентаре может лежать несколько (окно перекупки 10 мин делает это
    # обычным делом). Раньше здесь был словарь, второй экземпляр становился
    # невидимым, а на депозит уходил assetId уже отправленного — DMarket
    # отвечал InventoryItemsNotFound (подтверждено живьём 2026-07-31).
    # Исключаем то, что уже уехало в депозит: pending_deposits хранит asset_id.
    busy = {v.get("asset_id") for v in state["pending_deposits"].values()}
    free = {}
    for a in assets:
        if a["steam_asset_id"] in busy:
            continue
        free.setdefault(a["title"], []).append(a)

    arrived = [n for n in watch if free.get(n)]

    for name in arrived:
        print(f"[inv] {name} in inventory!")
        if name in state["pending_inv"]:
            state["pending_inv"].remove(name)
        state["pending_inv_ts"].pop(name, None)
        state["pending_inv_alerted"].pop(name, None)
        if name in state["pending_buys"]:
            # Доставка прошла мимо бота (принята вручную//confirm) — снимаем
            # ожидание трейда, иначе покупка провисит до stale-порога и
            # только там будет подобрана.
            #
            # ВАЖНО: снимок assets был снят ОДИН РАЗ в начале функции (строка
            # выше) и мог устареть — обнаружено 2026-08-04/05 (Defender Pants,
            # Dragon Totem AR): предыдущая покупка того же тайтла уже полностью
            # завершилась (продана), а этот детектор всё равно матчил по
            # устаревшему снимку и закрывал pending_buys для НОВОЙ покупки,
            # чья реальная доставка ещё не пришла. Итог — деньги списаны,
            # депозит потом бил в пустоту ("предмета уже нет в инвентаре").
            # Перед тем как закрыть pending_buys, перепроверяем СВЕЖИМ
            # ресинком именно в этот момент — если предмета там больше нет,
            # оставляем покупку висеть дальше вместо ложного закрытия.
            dm._req("POST", "/marketplace-api/v1/user-inventory/sync",
                    {"Type": "Inventory", "GameID": "Rust"}, retries=2)
            fresh_assets = [a for a in dm.get_uninvested_steam_assets()
                            if a["title"] == name and a["steam_asset_id"] not in busy]
            if not fresh_assets:
                print(f"[inv] {name}: при повторной проверке предмета нет — "
                      f"не закрываю pending_buys, жду реальную доставку")
                continue
            print(f"[inv] {name}: трейд принят вне бота — закрываю pending_buys")
            state["pending_buys"].pop(name, None)
            free[name] = fresh_assets
        item = free[name].pop(0)

        if AUTO_DEPOSIT:
            tg.send(f"📦 <b>{name}</b> in inventory — depositing to DMarket...")
            res = dm.deposit_assets([{"assetId": item["steam_asset_id"], "classId": item["class_id"]}])
            print(f"[inv] deposit result: {res}")
            # Ключ — СВОЙ уникальный, не название. По названию два одинаковых
            # предмета затирали друг друга, и второй терялся навсегда.
            state["pending_deposits"][uuid.uuid4().hex[:12]] = {
                "name":           name,
                "deposit_id":     res.get("deposit_id"),
                "asset_id":       item["steam_asset_id"],
                "class_id":       item["class_id"],
                "ts":             int(time.time()),
                "retries":        0,
                "last_retry_ts":  int(time.time()),
            }
            if res["success"]:
                print(f"[inv] Deposit started: {name} (id={res['deposit_id']})")
            else:
                print(f"[inv] Deposit request failed, will auto-retry: {res['data']}")
        else:
            tg.send(f"📦 <b>{name}</b> in inventory — deposit to DMarket manually")

    if arrived:
        save_state(state)

    # РАНЬШЕ pending_inv не имел таймаута вообще (в отличие от pending_buys) —
    # предмет, который так и не появился в инвентаре (Summer Rug, 2026-08-05,
    # исчез сам ещё до того, как детектор успел его подобрать), мог висеть
    # там бесконечно, а бот молча продолжал бы ждать факта, который никогда
    # не наступит. Раз в PENDING_INV_STALE_SEC предупреждаем один раз и не
    # снимаем автоматически — решение оставляем пользователю, как и для
    # других "не сходится с фактом" случаев.
    now_ts = time.time()
    changed_ts = False
    for name in list(state["pending_inv"]):
        first_seen = state["pending_inv_ts"].get(name)
        if first_seen is None:
            state["pending_inv_ts"][name] = int(now_ts)
            changed_ts = True
            continue
        if now_ts - first_seen > PENDING_INV_STALE_SEC and not state.get("pending_inv_alerted", {}).get(name):
            tg.send(f"❓ <b>{name}</b>: в очереди на доставку уже больше "
                    f"{PENDING_INV_STALE_SEC // 60} мин, но в Steam-инвентаре так и "
                    f"не появился. Проверь вручную — либо ещё едет, либо потерян.")
            state.setdefault("pending_inv_alerted", {})[name] = True
            changed_ts = True
    if changed_ts:
        save_state(state)


# ─── DMarket depozit — ждём завершения ────────────────────────────────────────

# Свежепринятые трейдом предметы иногда не депозятся сразу — deposit_assets()
# отдаёт TransferStatusError/InventoryRevoked. ВАЖНО (проверено 2026-07-23):
# повторные вызовы ИМЕННО ЭТОГО API-эндпоинта не подтверждены как решение —
# тот конкретный предмет так и не задепозился через 2 API-попытки (оба
# deposit_id остались в TransferStatusError даже post-factum, когда предмет
# уже успешно продался). Помогло только ручное "Продать сейчас" на сайте —
# видимо, отдельный недокументированный flow. Поэтому ретрай ниже — best-effort
# (вдруг всё же сработает на длинной дистанции), а не гарантированное решение;
# после нескольких попыток бот сам подсказывает попробовать сайт вручную, и
# независимо от способа продажи find_recent_sale() подхватит результат.
DEPOSIT_RETRY_BASE = 15 * 60     # 15 минут
DEPOSIT_RETRY_MAX  = 4 * 3600    # cap 4 часа между попытками
DEPOSIT_MAX_RETRIES = 40         # ~ до нескольких дней суммарно, потом сдаёмся
MANUAL_HINT_AFTER_RETRIES = 2    # ~45 мин неудач — подсказать попробовать сайт вручную
NOT_FOUND_GIVEUP_STREAK = 3      # столько ПОДРЯД неудачных проверок "нигде не найден"
                                  # нужно, прежде чем считать предмет реально потерянным
                                  # (не с первой же — см. Space Station Electric Furnace,
                                  # обнаружено вживую 2026-08-14: DMarket попросту не успел
                                  # досинхронизировать инвентарь к моменту первой проверки,
                                  # предмет всё это время спокойно лежал в Steam)


def _deposit_retry_wait(retries: int) -> int:
    return min(DEPOSIT_RETRY_BASE * (2 ** retries), DEPOSIT_RETRY_MAX)


def check_deposit_status(state: dict):
    """Опрашивает депозиты в процессе; при успехе переносит в pending_deposit
    с реальным DMarket item_id (полученным из deposit-status, а не подбором
    по названию — избегаем гонки при нескольких одинаковых предметах)."""
    if not state["pending_deposits"]:
        return
    for dkey, info in list(state["pending_deposits"].items()):
        name = info["name"]
        deposit_id = info.get("deposit_id")
        st = dm.get_deposit_status(deposit_id) if deposit_id else {"status": "TransferStatusError", "error": "no deposit_id"}
        status = st["status"]

        if status == "TransferStatusSuccess":
            item_id = next((a["dmarket_asset_id"] for a in st["assets"] if a["dmarket_asset_id"]), None)
            if not item_id:
                print(f"[deposit] {name}: успех, но нет dmarket_asset_id — {st}")
                continue
            # Ключ — DMarket item_id: он уникален, поэтому несколько одинаковых
            # предметов спокойно ждут продажи одновременно.
            state["pending_deposit"][item_id] = {
                "name":     name,
                "item_id":  item_id,
                "class_id": info["class_id"],
                "ts":       int(time.time()),
            }
            del state["pending_deposits"][dkey]
            tg.send(f"✅ <b>{name}</b> задепозичен на DMarket — продаю...")
            save_state(state)

        elif status in ("TransferStatusPending", "TransferStatusCreated", "TransferStatusOnHold"):
            # DMarket создаёт свой Steam-трейд (мы отдаём предмет боту DMarket) —
            # его тоже надо принять, как и обычную покупку. Пытаемся один раз
            # на каждый offer_id (не спамим accept на одном и том же оффере).
            offer_id = st.get("steam_trade_offer_id")
            if offer_id and info.get("deposit_offer_accepted") != offer_id:
                try:
                    ok = _get_steam().accept_trade(offer_id)
                except Exception as e:
                    print(f"[deposit] {name}: accept_trade error: {e}")
                    ok = False
                if ok:
                    info["deposit_offer_accepted"] = offer_id
                    save_state(state)
                    print(f"[deposit] {name}: депозит-трейд {offer_id} принят")
                else:
                    print(f"[deposit] {name}: не удалось принять депозит-трейд {offer_id} "
                          f"(попробую снова в следующем цикле)")

        else:  # TransferStatusError / TransferStatusFailedToCreate / нет deposit_id
            # Уже бывало: депозит через API падает в InventoryRevoked, а предмет
            # тем временем реально продаётся другим путём (сайт DMarket). Проверяем
            # закрытые офферы перед каждым повтором, чтобы не долбить впустую.
            sale = dm.find_recent_sale(name, since_ts=info["ts"])
            if sale:
                buy_price = _pop_buy_price(state, name)
                net = sale["price"] - sale["fee"]
                trade_log.log_sale(name, sale["price"], sale["fee"], buy_price)
                tg.send_item_on_dmarket(name, sale["price"], net, buy_price)
                print(f"[deposit] {name}: уже продан другим путём (${sale['price']:.2f}, "
                      f"net ${net:.2f}) — закрываю сделку", flush=True)
                del state["pending_deposits"][dkey]
                _mark_closed(state, name)
                save_state(state)
                continue

            retries = info.get("retries", 0)
            wait = _deposit_retry_wait(retries)
            elapsed = time.time() - info.get("last_retry_ts", info["ts"])
            if elapsed < wait:
                continue  # ещё рано для повтора

            if retries >= DEPOSIT_MAX_RETRIES:
                tg.send(f"❌ <b>{name}</b>: депозит не удался после {retries} попыток "
                        f"({st.get('error')}). Попробуй вручную на dmarket.com "
                        f"(«Продать сейчас» — иногда срабатывает раньше, чем прямой API-депозит).")
                del state["pending_deposits"][dkey]
                save_state(state)
                continue

            # NB: повторные вызовы deposit_assets() — best-effort, но НЕ подтверждённое
            # решение. На практике (2026-07-23) конкретный предмет так и не задепозился
            # через повторные API-вызовы (оба deposit_id остались в TransferStatusError
            # даже после того как предмет успешно продался) — реально помогло только
            # ручное "Продать сейчас" на сайте (видимо, отдельный недокументированный
            # flow депозит+продажа). Поэтому после нескольких неудачных попыток один раз
            # подсказываем в Telegram попробовать сайт вручную, не дожидаясь max retries —
            # но продолжаем ретраить и проверять find_recent_sale в фоне на случай, если
            # пользователь (или само API) всё же решит вопрос.
            if retries == MANUAL_HINT_AFTER_RETRIES and not info.get("manual_hint_sent"):
                tg.send(f"💡 <b>{name}</b>: депозит через API пока не проходит ({st.get('error')}). "
                        f"Попробуй вручную на dmarket.com → «Продать сейчас» на этом предмете — "
                        f"так уже срабатывало быстрее, чем повторные попытки API. "
                        f"Бот продолжит проверять сам и подхватит результат, если продашь так.")
                info["manual_hint_sent"] = True

            # Steam ПЕРЕНАЗНАЧАЕТ assetId при перемещении предмета, поэтому
            # сохранённый id может быть уже мёртвым — тогда повторы обречены:
            # DMarket вечно отвечает InventoryItemsNotFound. Проверено живьём
            # 2026-07-31: в состоянии лежал asset 501739234710818161, а предмет
            # в инвентаре имел 501739234710849688, и бот 4 раза бил в пустоту.
            # Поэтому перед повтором заново находим экземпляр по названию.
            dm._req("POST", "/marketplace-api/v1/user-inventory/sync",
                    {"Type": "Inventory", "GameID": "Rust"}, retries=2)
            fresh = next((a for a in dm.get_uninvested_steam_assets()
                          if a["title"] == name), None)
            if fresh:
                info["not_found_streak"] = 0
                if fresh["steam_asset_id"] != info["asset_id"]:
                    print(f"[deposit] {name}: assetId сменился "
                          f"{info['asset_id']} -> {fresh['steam_asset_id']}, беру новый")
                    info["asset_id"] = fresh["steam_asset_id"]
                    info["class_id"] = fresh["class_id"]
            else:
                # РАНЬШЕ здесь просто снимали запись с комментарием "продан
                # или выведен" — предположение, не факт. Обнаружено 2026-08-04/05
                # (Defender Pants, Dragon Totem AR, Pirate Electric Furnace):
                # предмет иногда реально приходит ПОЗЖЕ, чем этот код сдаётся,
                # и остаётся неучтённым навсегда, либо деньги теряются молча —
                # никто не узнаёт, пока не спросит вручную. Теперь перед тем как
                # снять запись, сверяемся с фактом по двум независимым
                # источникам (широкое окно закрытых офферов + список
                # незадепозиченного на DMarket), и если ничего не подтверждает
                # ни продажу, ни наличие — явно сообщаем в Telegram вместо
                # тихого удаления.
                wide_sale = dm.find_recent_sale(name, since_ts=info["ts"], limit=200)
                if wide_sale:
                    buy_price = _pop_buy_price(state, name)
                    net = wide_sale["price"] - wide_sale["fee"]
                    trade_log.log_sale(name, wide_sale["price"], wide_sale["fee"], buy_price)
                    tg.send_item_on_dmarket(name, wide_sale["price"], net, buy_price)
                    print(f"[deposit] {name}: нашёлся при расширенной проверке — "
                          f"продан за ${wide_sale['price']:.2f} (net ${net:.2f})")
                    del state["pending_deposits"][dkey]
                    _mark_closed(state, name)
                    save_state(state)
                    continue
                if dm.get_unlisted_items("rust", name):
                    print(f"[deposit] {name}: нашёлся в незадепозиченном на DMarket "
                          f"при повторной проверке — жду обычный цикл продажи")
                    continue

                # ОБНАРУЖЕНО ВЖИВУЮ 2026-08-15 (Scientific Components Storage, -$1.31):
                # все проверки выше опираются на DMarket-эндпоинты (get_uninvested_
                # steam_assets/get_unlisted_items) — а именно ИХ рассинхрон с реальным
                # Steam-инвентарём и стал причиной потери (get_uninvested_steam_assets()
                # сказал "есть" за секунду до deposit_assets(), который сказал "нет";
                # на всех 3 ретраях DMarket-кэш item тоже не видел). Прежде чем сдаваться,
                # сверяемся с Steam НАПРЯМУЮ (get_inventory() — публичный эндпоинт, не
                # зависит от кэша DMarket) — если предмет физически в Steam, это не
                # потеря, а застрявший на стороне DMarket депозит: не списываем, а
                # продолжаем ретраить и явно просим сделать "Продать сейчас" вручную на
                # сайте (единственный подтверждённый воркэраунд, см. коммент выше).
                try:
                    steam_inv = _get_steam().get_inventory()
                except Exception as e:
                    steam_inv = []
                    print(f"[deposit] {name}: не смог свериться со Steam напрямую ({e})")
                if any(a.get("market_hash_name") == name for a in steam_inv):
                    print(f"[deposit] {name}: DMarket не видит предмет, но он РЕАЛЬНО "
                          f"есть в Steam-инвентаре (проверено напрямую) — не считаю "
                          f"потерянным, продолжаю ретраить")
                    info["not_found_streak"] = 0
                    info["retries"] = retries + 1
                    info["last_retry_ts"] = time.time()
                    if not info.get("manual_hint_sent"):
                        tg.send(f"⚠️ <b>{name}</b>: депозит на DMarket не проходит через "
                                f"API, но предмет точно у тебя в Steam-инвентаре (прямая "
                                f"проверка). Попробуй вручную на dmarket.com → «Продать "
                                f"сейчас» — так обычно срабатывает быстрее API. Бот "
                                f"продолжит ретраить и проверять сам.")
                        info["manual_hint_sent"] = True
                    save_state(state)
                    continue

                # DMarket мог просто не успеть досинхронизировать инвентарь —
                # это НЕ повод сразу считать предмет потерянным. Даём
                # NOT_FOUND_GIVEUP_STREAK попыток подряд (каждая — со своим
                # backoff-ожиданием) и только тогда сдаёмся.
                streak = info.get("not_found_streak", 0) + 1
                info["not_found_streak"] = streak
                if streak < NOT_FOUND_GIVEUP_STREAK:
                    print(f"[deposit] {name}: не найден нигде (попытка {streak}/"
                          f"{NOT_FOUND_GIVEUP_STREAK} подряд) — возможно, DMarket ещё "
                          f"не досинхронизировал инвентарь, жду следующего повтора")
                    info["retries"] = retries + 1
                    info["last_retry_ts"] = time.time()
                    save_state(state)
                    continue

                print(f"[deposit] {name}: не подтверждено ни фактом продажи, ни "
                      f"наличием на любой стороне {NOT_FOUND_GIVEUP_STREAK} проверки "
                      f"подряд — снимаю запись, требуется проверка вручную")
                lost_price = _peek_buy_price(state, name)
                # Спрашиваем саму LIS-SKINS напрямую: может, заказ у них отменился/
                # вернулся сам (тогда баланс уже восстановлен их стороной) — раньше
                # custom_id нигде не хранился и такую проверку нельзя было сделать
                # даже постфактум (см. разбор потерь 2026-08-06).
                ls_custom_id = state.get("buy_custom_ids", {}).get(name)
                ls_status_line = ""
                if ls_custom_id:
                    ls_info = ls.get_purchase_info(custom_ids=[ls_custom_id])
                    if ls_info:
                        ls_status_line = f"\nСтатус на LIS-SKINS: {_ls_status(ls_info)}"
                tg.send(f"❓ <b>{name}</b>: депозит потерян — предмета нет ни в Steam, "
                        f"ни на DMarket, продажа не найдена даже при расширенной "
                        f"проверке. Деньги (${lost_price:.2f} по цене покупки) под "
                        f"вопросом — списываю как убыток в журнале.{ls_status_line}")
                _pop_buy_price(state, name)
                trade_log.log_sale(name, 0, 0, lost_price)
                del state["pending_deposits"][dkey]
                _mark_closed(state, name)
                if ls_custom_id:
                    state.setdefault("loss_followups", []).append({
                        "name": name, "custom_id": ls_custom_id, "buy_price": lost_price,
                        "marked_ts": time.time(),
                    })
                save_state(state)
                continue

            print(f"[deposit] {name}: попытка #{retries+1} после {st.get('error')} "
                  f"(следующая через {_deposit_retry_wait(retries+1)}с если снова не выйдет)")
            res = dm.deposit_assets([{"assetId": info["asset_id"], "classId": info["class_id"]}])
            if res["success"]:
                info["deposit_id"] = res["deposit_id"]
            info["retries"] = retries + 1
            info["last_retry_ts"] = time.time()
            save_state(state)


# ─── DMarket instant sell ─────────────────────────────────────────────────────
# ВАЖНО: не подтверждено, что create_offer() исполняется МГНОВЕННО (проверено
# 2026-07-23/24 — оффер повис как обычный resting-ордер, не исполнился сам
# за ~90 сек в тесте; итоговая продажа того предмета произошла позже вместе
# со всем остальным инвентарём, похоже через отдельное действие на сайте).
# Поэтому: создаём оффер один раз по live-цене, дальше просто ждём и
# проверяем find_recent_sale() — единственный надёжный признак реальной
# продажи. Цену оффера не дёргаем повторно без нужды — после создания на
# ~15 мин действует AssetTimeLocked (нельзя менять цену/удалять).
OFFER_RECHECK_INTERVAL = 20 * 60  # раз в ~20 мин смотрим, не пора ли перевыставить

def check_dmarket_offers(state: dict):
    if not state["pending_deposit"]:
        return

    # Ключ здесь — DMarket item_id (уникален), название лежит внутри записи.
    # Раньше ключом было название, и два одинаковых предмета не могли ждать
    # продажи одновременно: второй затирал первого.
    for pkey, info in list(state["pending_deposit"].items()):
        name     = info.get("name") or pkey
        item_id  = info["item_id"]
        class_id = info["class_id"]

        sale = dm.find_recent_sale(name, since_ts=info["ts"], asset_id=item_id,
                                   require_target=True)
        if sale:
            net       = sale["price"] - sale["fee"]
            buy_price = _pop_buy_price(state, name)
            trade_log.log_sale(name, sale["price"], sale["fee"], buy_price)
            tg.send_item_on_dmarket(name, sale["price"], net, buy_price)
            print(f"[dm] {name}: продан за ${sale['price']:.2f} (net ${net:.2f})")
            del state["pending_deposit"][pkey]
            _mark_closed(state, name)
            save_state(state)
            continue

        if info.get("offer_id"):
            # Оффер уже стоит — раз в OFFER_RECHECK_INTERVAL проверяем, не пора
            # ли обновить цену под текущий live-таргет (AssetTimeLocked не даст
            # обновить раньше ~15 мин после создания — просто получим ошибку,
            # подождём следующего цикла).
            if time.time() - info.get("offer_ts", info["ts"]) < OFFER_RECHECK_INTERVAL:
                continue
            real_instant = dm.get_target_price(name)
            if real_instant:
                new_price_c = int(round(real_instant * 100))
                # ПОЛ ПО ЦЕНЕ ПОКУПКИ. Раньше цена переставлялась под live-таргет
                # без всякой сверки с тем, за сколько предмет куплен: если стакан
                # обваливался, бот покорно шёл за ним вниз и продавал в глубокий
                # минус. Так возникли все 12 убыточных сделок (Tire Kilt
                # $1.94 -> $1.31 = -36%, Redemption DBS -67%): переоценка
                # срабатывала 771 раз, и в 12 случаях попадала на пустой стакан.
                # Теперь ниже -MAX_LOSS_PCT от закупки не опускаемся: держим
                # оффер и ждём восстановления стакана.
                buy_price = _peek_buy_price(state, name)
                floor_c = None
                if buy_price:
                    floor_net = buy_price * (1 - MAX_LOSS_PCT / 100)
                    floor_c = int(round(floor_net / (1 - dm.DM_FEE) * 100))
                if floor_c and new_price_c < floor_c:
                    print(f"[dm] {name}: live-таргет ${new_price_c/100:.2f} ниже пола "
                          f"${floor_c/100:.2f} (закупка ${buy_price:.2f}, лимит "
                          f"-{MAX_LOSS_PCT:.0f}%) — цену НЕ снижаю, жду стакан")
                    continue
                res = dm.update_offer(info["offer_id"], new_price_c)
                print(f"[dm] {name}: обновляю цену оффера до ${new_price_c/100:.2f} -> {res}")
                if res["success"]:
                    if res.get("offer_id"):
                        info["offer_id"] = res["offer_id"]
                    info["offer_ts"] = int(time.time())
                    save_state(state)
            continue

        real_instant = dm.get_target_price(name)
        if not real_instant:
            print(f"[dm] {name}: нет живой ⚡ цены — жду следующего цикла")
            continue
        target_c = int(round(real_instant * 100))

        # ТОТ ЖЕ ПОЛ, ЧТО И ПРИ ПЕРЕОЦЕНКЕ. Раньше он стоял только в ветке
        # update_offer, а первичное выставление шло по live-таргету без всякой
        # сверки с закупкой. Из-за этого предмет, пролежавший в инвентаре пока
        # рынок ушёл, сразу выставлялся в минус и продавался: 2026-07-31 так
        # ушли Kraken Shell Facemask -21.4%, Suitor Burlap Shirt -12.2%,
        # Bombshell Armored Door -10.3% — все три сразу после депозита.
        buy_price = _peek_buy_price(state, name)
        if buy_price:
            floor_c = int(round(buy_price * (1 - MAX_LOSS_PCT / 100) / (1 - dm.DM_FEE) * 100))
            if target_c < floor_c:
                print(f"[dm] {name}: live-таргет ${target_c/100:.2f} ниже пола "
                      f"${floor_c/100:.2f} (закупка ${buy_price:.2f}, лимит "
                      f"-{MAX_LOSS_PCT:.0f}%) — оффер НЕ выставляю, жду стакан")
                continue

        print(f"[dm] {name}: сопоставляю с живым buy order @ ${target_c/100:.2f}...")
        res = dm.create_order_matching_offer(item_id, name,
                                             minimum_price_cents=floor_c if buy_price else 0)
        print(f"[dm] create_order_matching_offer: {res}")

        if res["success"]:
            info["offer_id"] = res["offer_id"]
            info["offer_ts"] = int(time.time())
            save_state(state)
        else:
            tg.send(f"⚠️ <b>{name}</b> on DMarket, не удалось выставить оффер.\n"
                    f"{json.dumps(res.get('data',''))[:150]}")


# ─── Main loop ─────────────────────────────────────────────────────────────────

def main():
    src_label = "lis-skins"
    print(f"[bot] {src_label} -> DMarket instant sell bot")
    daily_cap = "unlimited" if MAX_DAILY_SPEND_USD <= 0 else f"${MAX_DAILY_SPEND_USD}"
    max_buy = "unlimited" if MAX_BUY_USD <= 0 else f"${MAX_BUY_USD}"
    print(f"[bot] min_profit={MIN_PROFIT_PCT}% max_buy={max_buy} "
          f"daily_cap={daily_cap} poll={POLL_INTERVAL}s AUTO_BUY={AUTO_BUY}")

    state = load_state()
    one_shot_name = None
    one_shot_started_ts = time.time()
    control = bot_control.BotControl(auto_buy_default=AUTO_BUY)
    if TG_CONTROL_ENABLED:
        bot_control.start(control, lambda: state)
        print("[bot] Telegram control listener started")
    else:
        print("[bot] Telegram commands/buttons disabled; notifications only")

    # Steam login only needed when actually buying/accepting
    steam_ok = False
    steam_login_last_attempt = 0.0
    if control.auto_buy or AUTO_ACCEPT or AUTO_DEPOSIT:
        steam_login_last_attempt = time.time()
        try:
            _get_steam().get_client()
            steam_ok = True
            print("[bot] Steam login OK")
        except Exception as e:
            print(f"[bot] Steam login error: {e}")
            print("[bot] Continuing in signal-only mode")

    mode = "AUTO-BUY" if control.auto_buy else "SIGNAL-ONLY"
    tg.send(
        f"🤖 <b>{src_label} → DMarket bot started</b>\n"
        f"📊 Min profit: +{MIN_PROFIT_PCT}% | Max buy: "
        f"{'unlimited' if MAX_BUY_USD <= 0 else '$' + str(MAX_BUY_USD)}\n"
        f"⚡ Mode: {mode}"
    )

    dm_prices: dict = {}
    ls_items: list  = []
    ls_prices: dict = {}
    _fetch_lock    = threading.Lock()
    _fetching      = threading.Event()
    last_price_ts  = 0

    def _do_price_fetch():
        nonlocal dm_prices, ls_items, ls_prices, last_price_ts
        try:
            table_ls, table_orders = st.get_lisskins_and_dmarket_order(RUST_APP_ID)
            target_names, new_dm = build_skins_table_snapshot(table_ls, table_orders)
            if not target_names:
                raise RuntimeError('skins-table returned no fresh profitable LIS-SKINS → DMARKET ORDER candidates')
            new_ls = ls.fetch_market_items(
                min_price=MIN_BUY_USD,
                max_price=MAX_BUY_USD,
                target_names=target_names,
            )
            with _fetch_lock:
                ls_items  = new_ls
                ls_prices = ls.build_price_map(new_ls)
                dm_prices = new_dm
            print(f"[bot] Price update done: {len(ls_prices)} LS / {len(dm_prices)} DMarket items")
        except Exception as e:
            print(f"[bot] Price fetch error: {e}")
        finally:
            _fetching.clear()

    last_error_alert = {"msg": None, "ts": 0.0}
    while True:
        now = time.time()

        # Всё тело итерации — в try/except: одна сетевая ошибка (таймаут API
        # и т.п.) не должна убивать весь процесс и бросать уже купленные
        # предметы без дальнейшей обработки (так уже случалось — см. память
        # проекта, трейды тогда протухли по trade_timeout на стороне LisSkins).
        try:
            # Ежечасный heartbeat в Telegram (в начале часа), чтобы знать что
            # процесс жив, независимо от running/paused.
            cur_hour = int(now // 3600)
            if cur_hour != state.get("last_heartbeat_hour", -1):
                state["last_heartbeat_hour"] = cur_hour
                save_state(state)
                dm_bal = dm.get_balance()["usd"]
                ls_bal = ls.get_balance()
                tg.send(f"✅ Бот активен ({time.strftime('%H:%M', time.localtime(now))})\n"
                        f"Баланс DMarket: ${dm_bal:.2f} | lis-skins: ${ls_bal:.2f}")

            check_ls_balance(state)
            check_loss_followups(state)

            if not control.running:
                # На паузе: не сканируем и не покупаем новое, но депозит/продажа
                # уже купленных предметов продолжается (не бросаем на полпути).
                if state["pending_deposits"]:
                    check_deposit_status(state)
                if state["pending_deposit"]:
                    check_dmarket_offers(state)
            else:
                # Если AUTO_BUY включили кнопкой уже после старта (а не был
                # нужен Steam login при запуске) — пробуем залогиниться сейчас.
                if (control.auto_buy and not steam_ok
                        and now - steam_login_last_attempt >= STEAM_LOGIN_RETRY_SEC):
                    steam_login_last_attempt = now
                    try:
                        _get_steam().get_client()
                        steam_ok = True
                        print("[bot] Steam login OK (после включения AUTO_BUY)")
                    except Exception as e:
                        print(f"[bot] Steam login error: {e}")

                # ── Trigger background price fetch every POLL_INTERVAL ──────
                if now - last_price_ts >= POLL_INTERVAL and not _fetching.is_set():
                    elapsed = int((now - last_price_ts) // 60)
                    print(f"\n[bot] === Starting price fetch ({elapsed} min since last) ===")
                    _fetching.set()
                    last_price_ts = now
                    t = threading.Thread(target=_do_price_fetch, daemon=True)
                    t.start()

                # ── Process opportunities with latest prices ─────────────────
                with _fetch_lock:
                    cur_ls = dict(ls_prices)
                    cur_dm = dict(dm_prices)
                    cur_ls_items = list(ls_items)

                if cur_dm and cur_ls:
                    opps = find_opportunities(cur_ls, cur_dm, "lis-skins")
                    opps.sort(key=lambda x: -x["pct"])
                    if opps:
                        print(f"[bot] Opportunities: {len(opps)}")
                        for o in opps[:10]:
                            print(f"  [{o['source']}] {o['name']}: buy=${o['ls']:.2f} "
                                  f"target=${o['target']:.2f} net=${o['net']:.2f} "
                                  f"{o['pct']:+.1f}% [{o['orders']} orders]")

                    # Сигналы (найденные по агрегированным данным) в Telegram
                    # намеренно НЕ отправляются — по просьбе пользователя,
                    # т.к. агрегированный % не учитывает комиссию DMarket и
                    # часто вводил в заблуждение (см. живой % при покупке).

                    if control.auto_buy and steam_ok and not (ONE_SHOT and one_shot_name):
                        for o in opps:
                            if _purchase_precheck_blocked(state, o["name"]):
                                continue
                            if (confirm_opportunity_live(o, cur_ls_items, state)
                                    and try_buy(o, cur_ls_items, state)):
                                if ONE_SHOT:
                                    one_shot_name = o["name"]
                                    print(f"[one-shot] purchase started: {one_shot_name}; "
                                          "new purchases are now disabled")
                                break  # stop after first successful buy this cycle

                    save_state(state)

                # ── Trades -> accept ──────────────────────────────────────────
                if (AUTO_ACCEPT or control.auto_buy) and steam_ok and state["pending_buys"]:
                    check_ls_purchase_status(state)
                    check_steam_trades(state)
                    check_declined_trades(state)

                # ── Inventory -> deposit DMarket ──────────────────────────────
                # pending_buys тоже проверяем: предмет мог уже прийти в
                # инвентарь, если трейд приняли вручную (см. check_steam_inventory).
                if AUTO_DEPOSIT and steam_ok and (state["pending_inv"] or state["pending_buys"]):
                    check_steam_inventory(state)

                # ── Depozit в процессе -> ждём завершения ─────────────────────
                if state["pending_deposits"]:
                    check_deposit_status(state)

                # ── DMarket -> instant sell ───────────────────────────────────
                if state["pending_deposit"]:
                    check_dmarket_offers(state)

                print(f"[bot] pending: buy={len(state['pending_buys'])} "
                      f"inv={len(state['pending_inv'])} "
                      f"depositing={len(state['pending_deposits'])} "
                      f"ready_to_sell={len(state['pending_deposit'])}")

                if ONE_SHOT and one_shot_name:
                    closed_at = state.get("recently_closed", {}).get(one_shot_name, 0)
                    if (closed_at >= one_shot_started_ts
                            and not _name_in_flight(state, one_shot_name)):
                        print(f"[one-shot] full cycle finished: {one_shot_name}")
                        tg.send(f"✅ <b>Боевой тест завершён</b>\n{one_shot_name}\n"
                                "Новых покупок не будет: процесс one-shot остановлен.")
                        return

        except Exception as e:
            print(f"[bot] ОШИБКА в основном цикле (не фатально, продолжаю): {e}")
            traceback.print_exc()
            # РАНЬШЕ слали алерт в Telegram на КАЖДОЕ срабатывание — при
            # затяжном сбое DMarket API (2026-08-06, ~15 мин подряд
            # ReadTimeout) это давало по алерту чуть ли не на каждый цикл
            # (TRADE_POLL=30с), зaспамив чат десятками одинаковых сообщений.
            # Теперь одна и та же по тексту ошибка шлётся не чаще раза в
            # ERROR_ALERT_COOLDOWN_SEC — если ошибка сменилась (другой текст),
            # шлём сразу, не ждём остывания старой.
            err_text = str(e)[:200]
            same_error = err_text == last_error_alert["msg"]
            if not same_error or now - last_error_alert["ts"] > ERROR_ALERT_COOLDOWN_SEC:
                try:
                    tg.send(f"⚠️ Ошибка в цикле бота (не фатально, продолжаю): {err_text}")
                except Exception:
                    pass
                last_error_alert["msg"] = err_text
                last_error_alert["ts"] = now

        time.sleep(TRADE_POLL)


if __name__ == "__main__":
    main()
