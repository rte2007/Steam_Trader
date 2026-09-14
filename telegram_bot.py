# -*- coding: utf-8 -*-
"""Telegram уведомления и кнопки."""
import os, requests, time
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

TG_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
UA         = "Mozilla/5.0"


def send(text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> dict:
    if not TG_TOKEN or not TG_CHAT_ID:
        print(f"[TG] {text[:200]}")
        return {}
    body = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=body, timeout=10
        )
        return r.json()
    except Exception as e:
        print(f"[TG] error: {e}")
        return {}


def send_document(file_path: str, caption: str = "") -> dict:
    """Отправить файл (например, trade_log.xlsx) документом в чат."""
    if not TG_TOKEN or not TG_CHAT_ID:
        print(f"[TG] send_document skipped (no token): {file_path}")
        return {}
    try:
        with open(file_path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT_ID, "caption": caption},
                files={"document": (os.path.basename(file_path), f)},
                timeout=30,
            )
        return r.json()
    except Exception as e:
        print(f"[TG] send_document error: {e}")
        return {}


def send_signal(item_name: str, ls_price: float, target: float, net: float,
                pct: float, order_cnt: int):
    """Сигнал: найдена возможность для instant sell."""
    text = (
        f"⚡ <b>{item_name}</b>\n"
        f"🛒 lis-skins: <b>${ls_price:.2f}</b>\n"
        f"🎯 DMarket Target: <b>${target:.2f}</b> → чистыми <b>${net:.2f}</b>\n"
        f"📊 {pct:+.1f}% | {order_cnt} ордеров"
    )
    send(text)


def send_buy_success(item_name: str, price: float, offer_id: str):
    """Уведомление об успешной покупке на cs.trade."""
    text = (
        f"✅ <b>Куплено на cs.trade</b>\n"
        f"📦 {item_name}\n"
        f"💰 ${price:.2f}\n"
        f"🔗 Ожидай трейд от бота cs.trade"
    )
    send(text)


def send_trade_received(item_name: str, offer_id: str, from_partner: str):
    """Уведомление о входящем трейде — нужно принять."""
    trade_url = f"https://steamcommunity.com/tradeoffer/{offer_id}/"
    text = (
        f"📨 <b>Входящий трейд Steam!</b>\n"
        f"📦 {item_name}\n"
        f"👤 От: {from_partner}\n\n"
        f"👇 Нажми чтобы принять:"
    )
    markup = {"inline_keyboard": [[
        {"text": "✅ Принять трейд", "url": trade_url}
    ]]}
    send(text, reply_markup=markup)


def send_item_in_inventory(item_name: str):
    """Предмет появился в Steam инвентаре."""
    deposit_url = "https://dmarket.com/account/inventory"
    text = (
        f"📦 <b>{item_name}</b> в Steam инвентаре!\n\n"
        f"👇 Задепозить на DMarket:"
    )
    markup = {"inline_keyboard": [[
        {"text": "💎 Депозит на DMarket", "url": deposit_url}
    ]]}
    send(text, reply_markup=markup)


def send_item_on_dmarket(item_name: str, target_price: float, net: float,
                         buy_price: float = 0.0):
    """Предмет на DMarket — instant sell выполнен."""
    profit = net - buy_price if buy_price else 0
    pct    = (profit / buy_price * 100) if buy_price else 0
    lines  = [
        f"✅ <b>СДЕЛКА ЗАКРЫТА</b>",
        f"📦 {item_name}",
        f"🛒 Куплено на lis-skins: <b>${buy_price:.2f}</b>",
        f"💰 Получено после продажи: <b>${net:.2f}</b>",
        f"📈 Прибыль: <b>${profit:+.2f} ({pct:+.1f}%)</b>",
    ]
    send("\n".join(lines))


def send_error(msg: str):
    send(f"❌ <b>Ошибка:</b> {msg}")


# ─── Приём команд/кнопок (long polling) ───────────────────────────────────────

def get_updates(offset: int = 0, timeout: int = 25) -> list:
    """Long-poll новые апдейты (команды, нажатия кнопок)."""
    if not TG_TOKEN:
        return []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + 10,
        )
        return r.json().get("result", [])
    except Exception as e:
        print(f"[TG] get_updates error: {e}")
        return []


def answer_callback(callback_query_id: str, text: str = ""):
    """Убрать 'часики' с нажатой inline-кнопки, опционально показать всплывашку."""
    if not TG_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"[TG] answer_callback error: {e}")


def edit_message(chat_id, message_id, text: str, reply_markup: dict = None,
                 parse_mode: str = "HTML"):
    """Обновить текст/кнопки уже отправленного сообщения (для toggle-кнопок)."""
    if not TG_TOKEN:
        return
    body = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
            json=body, timeout=10,
        )
    except Exception as e:
        print(f"[TG] edit_message error: {e}")
