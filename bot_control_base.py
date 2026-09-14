# -*- coding: utf-8 -*-
"""
Управление ботом через Telegram: /status, /start, /stop, вкл/выкл AUTO_BUY
кнопкой. Слушает команды в отдельном потоке (long polling), не блокируя
основной цикл arb_bot_ls.py. Реагирует только на TG_CHAT_ID из .env —
команды из других чатов игнорируются.
"""
import os, sys, time, json, threading, requests
import telegram_bot as tg
import dmarket_api as dm
import lisskins_api as ls
import trade_log
import steam_login as sl
import steam_client as sc
from steampy.guard import generate_one_time_code

class BotControl:
    def __init__(self, auto_buy_default: bool):
        self.running  = True
        self.auto_buy = auto_buy_default


def _help_text() -> str:
    return (
        "<b>Команды</b>\n"
        "/status — статус бота, баланс DMarket, сколько сделок в процессе\n"
        "/start — возобновить сканирование и покупки (снять с паузы)\n"
        "/stop — поставить на паузу (новые покупки не начинаются; уже "
        "купленные предметы всё равно доводятся до продажи)\n"
        "/restart — полный перезапуск процесса бота (подхватывает новый код/.env)\n"
        "/autobuy — вкл/выкл авто-покупку кнопкой\n"
        "/trades — прислать файл со всеми сделками (trade_log.xlsx)\n"
        "/stats — расходы, доходы и прибыль за сегодня / 7 дней / всё время\n"
        "/code — логин/пароль/код Steam Guard (удобно для копирования)\n"
        "/confirm — принять ожидаемые входящие трейды и подтвердить свои "
        "депозитные заявки (вместо SDA); чужое не трогает, а показывает ссылкой\n"
        "/confirm &lt;id&gt; — принять КОНКРЕТНЫЙ трейд по ID вручную (когда /confirm "
        "его пропустил, но ты сам проверил трейд в Steam и решил принять)\n"
        "/help — это сообщение"
    )


def _code_text() -> str:
    guard = generate_one_time_code(sl.STEAM_SHARED) if sl.STEAM_SHARED else "—"
    return (
        f"<b>Steam аккаунт</b>\n"
        f"Логин: <code>{sl.STEAM_LOGIN}</code>\n"
        f"Пароль: <code>{sl.STEAM_PASS}</code>\n"
        f"Guard код: <code>{guard}</code> (живёт ~30 сек)"
    )


def _confirm_trades(state: dict | None = None) -> str:
    """
    Ручной триггер /confirm: заменяет SDA, когда бот гоняет тот же TOTP-секрет
    и мобильное приложение не успевает увидеть заявку до её обработки.

    ВАЖНО, что подтверждается, а что нет (ужесточено 2026-07-29):
    * входящие — только те, чьи предметы мы РЕАЛЬНО ждём (есть в pending_buys).
      Раньше принималось всё подряд, из-за чего в инвентарь заезжал посторонний
      хлам, а бот перехватывал выводы соседних схем.
    * исходящие — только НАШИ депозитные заявки, чьи offer_id получены из
      deposit-status по активным депозитам. Раньше подтверждалась ЛЮБАЯ
      исходящая заявка без проверки получателя — то есть чужая заявка на
      отдачу предметов была бы подтверждена автоматически. Это направление,
      в котором вещи уходят со счёта, поэтому проверка обязательна.
    Всё, что не прошло проверку, показывается со ссылкой — решает пользователь.
    """
    state = state or {}
    lines, skipped = [], []

    expected = set(state.get("pending_buys", {}))
    try:
        received = sc.get_trade_offers()
    except Exception as e:
        received = []
        lines.append(f"⚠️ Ошибка получения входящих: {e}")
    for o in received:
        oid = o["tradeofferid"]
        names = o.get("_item_names", [])
        name_str = ", ".join(names) or "?"
        if not (expected & set(names)):
            skipped.append(f"⏭ Входящий {oid} ({name_str}) — не ждём такого\n"
                           f"   https://steamcommunity.com/tradeoffer/{oid}/")
            continue
        partner = str(int(o.get("accountid_other", 0)) + 76561197960265728)
        ok = sc.accept_trade(oid, partner)
        lines.append(f"{'✅' if ok else '❌'} Входящий {oid} ({name_str})")

    # Свои исходящие: offer_id берём из статуса активных депозитов.
    ours = set()
    for info in (state.get("pending_deposits") or {}).values():
        did = info.get("deposit_id")
        if not did:
            continue
        try:
            st = dm.get_deposit_status(did)
            if st.get("steam_trade_offer_id"):
                ours.add(str(st["steam_trade_offer_id"]))
        except Exception:
            pass

    try:
        r = requests.get(f"{sl._API}/IEconService/GetTradeOffers/v1/", params={
            "key": sl.STEAM_API_KEY, "get_sent_offers": 1, "active_only": 1,
        }, timeout=15)
        sent = r.json().get("response", {}).get("trade_offers_sent", [])
    except Exception as e:
        sent = []
        lines.append(f"⚠️ Ошибка получения исходящих: {e}")
    for o in sent:
        if o.get("trade_offer_state") != 9:  # 9 = CreatedNeedsConfirmation
            continue
        oid = str(o["tradeofferid"])
        if oid not in ours:
            skipped.append(f"⏭ Исходящий {oid} — НЕ наша депозитная заявка, "
                           f"не подтверждаю\n   https://steamcommunity.com/tradeoffer/{oid}/")
            continue
        ok = sl._mobile_confirm_offer(oid)
        lines.append(f"{'✅' if ok else '❌'} Исходящий {oid} (подтверждение Guard)")

    if not lines and not skipped:
        return "Нет трейдов, ожидающих действия."
    out = lines or ["Нечего подтверждать автоматически."]
    if skipped:
        out += ["", "<b>Пропущено (реши сам):</b>"] + skipped
    return "\n".join(out)


def _confirm_specific(oid: str) -> str:
    """
    Ручное подтверждение ОДНОГО конкретного входящего трейда по ID —
    для случаев, когда /confirm его намеренно пропустил как неожиданный
    (см. _confirm_trades) и пользователь, ЛИЧНО проверив трейд в Steam,
    решил принять его сам. В отличие от /confirm, здесь никакой автоматики:
    подтверждается только тот id, что явно указан командой.
    """
    try:
        offers = sc.get_trade_offers()
    except Exception as e:
        return f"⚠️ Ошибка получения входящих: {e}"
    o = next((x for x in offers if str(x.get("tradeofferid")) == oid), None)
    if not o:
        return f"Входящий {oid} не найден среди активных (уже обработан или не твой?)."
    names = o.get("_item_names", []) or ["?"]
    partner = str(int(o.get("accountid_other", 0)) + 76561197960265728)
    ok = sc.accept_trade(oid, partner)
    return (f"{'✅' if ok else '❌'} Входящий {oid} ({', '.join(names)})\n"
            f"Отправитель: {partner}")


def _autobuy_keyboard(control: BotControl) -> dict:
    label = ("🟢 AUTO_BUY: ON (нажми чтобы выключить)" if control.auto_buy
             else "🔴 AUTO_BUY: OFF (нажми чтобы включить)")
    return {"inline_keyboard": [[{"text": label, "callback_data": "toggle_autobuy"}]]}


def _status_text(control: BotControl, state: dict) -> str:
    bal = dm.get_balance()
    try:
        ls_bal = ls.get_balance()
        ls_bal_str = f"${ls_bal:.2f}"
    except Exception as e:
        ls_bal_str = f"н/д ({e})"
    running = "▶️ работает" if control.running else "⏸ на паузе"
    return (
        f"<b>Статус бота</b>\n"
        f"{running} | AUTO_BUY: {'ON' if control.auto_buy else 'OFF'}\n"
        f"Баланс DMarket: ${bal['usd']:.2f} (доступно к выводу: ${bal['usd_available']:.2f})\n"
        f"Баланс lis-skins: {ls_bal_str}\n\n"
        f"Ожидают покупки: {len(state.get('pending_buys', {}))}\n"
        f"В Steam-инвентаре: {len(state.get('pending_inv', []))}\n"
        f"Депозит в процессе: {len(state.get('pending_deposits', {}))}\n"
        f"Готовы к продаже: {len(state.get('pending_deposit', {}))}"
    )


def _stats_text() -> str:
    """
    Расходы/доходы/прибыль за 24 часа, 7 дней и всё время по trade_log.xlsx.

    Считаем по ДАТЕ ПРОДАЖИ и только по закрытым сделкам: тогда расход и
    доход внутри периода относятся к одним и тем же сделкам и прибыль
    сходится. Если брать расход по дате покупки, а доход по дате продажи,
    сделки на стыке периодов попадали бы только одной половиной и цифры
    не били бы друг с другом.
    Незакрытые ('В процессе') показываем отдельной строкой — это деньги,
    которые сейчас в обороте и ещё не вернулись.
    """
    import openpyxl
    from datetime import datetime, timedelta   # datetime нужен для strptime дат из лога
    try:
        wb = openpyxl.load_workbook(trade_log.LOG_FILE, data_only=True)
        ws = wb['Сделки']
    except Exception as e:
        return f"Не смог прочитать лог сделок: {e}"

    now = trade_log._now().replace(tzinfo=None)

    def _num(v):
        return v if isinstance(v, (int, float)) else None

    def _dt(v):
        if not v:
            return None
        if hasattr(v, 'year'):
            return v.replace(tzinfo=None)
        try:
            return datetime.strptime(str(v).strip(), '%Y-%m-%d %H:%M')
        except Exception:
            return None

    closed, open_spent, open_cnt = [], 0.0, 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        # 'Не доставлено' — покупка, которую продавец так и не отправил;
        # lis-skins возвращает за неё деньги. Это НЕ сделка: если считать её
        # как закрытую с нулём, расход раздувается, а средняя маржа падает
        # (2026-07-31: 7 таких записей на $6.66 занижали её с 10.9% до 10.6%).
        if str(row[4] or '').strip() == 'Не доставлено':
            continue
        spent = _num(row[2]) or 0.0
        profit = _num(row[9])
        status = str(row[4] or '').strip()
        if profit is None:
            # 'Продано' с пустой прибылью — не "ещё не продано", а бонусный/
            # незапрошенный экземпляр без известной цены закупки (лог_sale
            # не находит открытую строку и добавляет отдельную с buy_price
            # пустым — см. trade_log.py). Раньше такая строка НАВСЕГДА висела
            # в "В обороте", хотя сделка давно закрыта и деньги за неё
            # получены (2026-08-06: Firecracker Hide Poncho, $0.12).
            if status == 'Продано':
                continue
            if row[1]:
                open_cnt += 1
                open_spent += spent
            continue
        closed.append({'sold': _dt(row[5]), 'spent': spent,
                       'net': _num(row[8]) or 0.0, 'profit': profit})

    def block(title, since):
        rows = [c for c in closed if since is None or (c['sold'] and c['sold'] >= since)]
        if not rows:
            return f"<b>{title}</b>\nсделок нет\n"
        spent = sum(r['spent'] for r in rows)
        got   = sum(r['net'] for r in rows)
        prof  = sum(r['profit'] for r in rows)
        pct   = prof / spent * 100 if spent else 0
        win   = sum(1 for r in rows if r['profit'] > 0)
        return (f"<b>{title}</b>\n"
                f"сделок: {len(rows)} (в плюс {win})\n"
                f"расход: ${spent:.2f}\n"
                f"доход: ${got:.2f}\n"
                f"прибыль: <b>${prof:+.2f} ({pct:+.1f}%)</b>\n")

    # Календарные сутки, а не скользящие 24 часа: «за сегодня» — это от
    # 00:00 текущего дня по МСК (лог тоже пишется в МСК, см. trade_log._now).
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    parts = [
        block('За сегодня', midnight),
        block('За 7 дней',  midnight - timedelta(days=6)),   # сегодня + 6 прошлых суток
        block('За всё время', None),
    ]
    if open_cnt:
        parts.append(f"<i>В обороте: {open_cnt} шт. на ${open_spent:.2f} "
                     f"(куплено, ещё не продано)</i>")
    return "📊 <b>Статистика LS → DMarket</b>\n\n" + "\n".join(parts)


def _handle_update(upd: dict, control: BotControl, state_getter, next_offset: int):
    msg = upd.get("message")
    cq  = upd.get("callback_query")

    if msg:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(tg.TG_CHAT_ID):
            return  # игнорируем чужие чаты
        text = (msg.get("text") or "").strip().lower()

        if text in ("/help", "/menu", "/start@help"):
            tg.send(_help_text())
        elif text == "/status":
            tg.send(_status_text(control, state_getter()))
        elif text == "/start":
            control.running = True
            tg.send("▶️ Бот возобновил работу (сканирование и покупки снова активны)")
        elif text == "/stop":
            control.running = False
            tg.send("⏸ Бот на паузе (сканирование/покупки остановлены; процесс не убит, "
                    "депозит/продажа уже купленных предметов продолжается; "
                    "можно возобновить командой /start)")
        elif text == "/restart":
            # Подтверждаем апдейт у Telegram ПЕРЕД execv — иначе offset живёт только
            # в локальной переменной цикла и после замены процесса теряется, а эта же
            # команда /restart придёт снова при следующем getUpdates -> бесконечный
            # цикл рестартов. timeout=0 — не ждём новых апдейтов, просто подтверждаем.
            tg.get_updates(next_offset, timeout=0)
            tg.send("🔄 Перезапускаю бота (полный рестарт процесса)...")
            os.execv(sys.executable, [sys.executable, "-u", sys.argv[0]])
        elif text == "/autobuy":
            tg.send("Управление AUTO_BUY:", reply_markup=_autobuy_keyboard(control))
        elif text == "/trades":
            tg.send_document(trade_log.LOG_FILE, caption="📊 Все сделки (актуально на сейчас)")
        elif text == "/stats":
            tg.send(_stats_text())
        elif text == "/code":
            tg.send(_code_text())
        elif text == "/confirm":
            tg.send(_confirm_trades(state_getter()))
        elif text.startswith("/confirm "):
            oid = text.split(maxsplit=1)[1].strip()
            if oid.isdigit():
                tg.send(_confirm_specific(oid))
            else:
                tg.send("Формат: /confirm <id трейда>, например /confirm 9272876257")
        elif text.startswith("/"):
            tg.send(f"Не знаю команду {text}. Список команд — /help")

    elif cq:
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != str(tg.TG_CHAT_ID):
            return
        if cq.get("data") == "toggle_autobuy":
            control.auto_buy = not control.auto_buy
            tg.answer_callback(cq["id"], f"AUTO_BUY {'включён' if control.auto_buy else 'выключен'}")
            tg.edit_message(chat_id, cq["message"]["id"], "Управление AUTO_BUY:",
                            reply_markup=_autobuy_keyboard(control))


def poll_commands_loop(control: BotControl, state_getter):
    """state_getter — функция без аргументов, возвращающая текущий state dict бота."""
    offset = 0
    while True:
        try:
            updates = tg.get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                _handle_update(upd, control, state_getter, offset)
        except Exception as e:
            print(f"[control] poll error: {e}")
            time.sleep(5)


def start(control: BotControl, state_getter) -> threading.Thread:
    """Запустить слушатель команд в фоновом daemon-потоке."""
    t = threading.Thread(target=poll_commands_loop, args=(control, state_getter), daemon=True)
    t.start()
    return t
