# -*- coding: utf-8 -*-
"""Single Telegram command listener for leg1 and leg2.

The original leg1 controller is deployed as ``bot_control_base.py``.  This
wrapper owns getUpdates and delegates all existing leg1 commands to it, while
changing the leg2 control file atomically.  A second Telegram poller must not
be started for the same bot token.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import bot_control_base as base
import telegram_bot as tg


BotControl = base.BotControl
# A public checkout keeps all workers in one directory.  LEG2_ROOT can still
# point at a separate production directory when the old layout is used.
LEG2_ROOT = Path(os.getenv("LEG2_ROOT", str(Path(__file__).resolve().parent)))
LEG2_CONTROL = LEG2_ROOT / "dm_market_control.json"
LEG2_STATE = LEG2_ROOT / "dm_market_state.json"


def _json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def _leg2_control() -> dict:
    return _json(LEG2_CONTROL, {
        "running": True,
        "auto_buy": False,
        "allow_withdraw": True,
        "allow_market": True,
        "auto_deliver": True,
    })


def _save_leg2_control(value: dict) -> None:
    LEG2_ROOT.mkdir(parents=True, exist_ok=True)
    temp = LEG2_CONTROL.with_suffix(".tmp")
    temp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, LEG2_CONTROL)


def _set_leg2(**changes: bool) -> dict:
    control = _leg2_control()
    control.update(changes)
    _save_leg2_control(control)
    return control


def _leg2_status_text() -> str:
    ctl = _leg2_control()
    state = _json(LEG2_STATE, {"active": {}, "done": [], "daily_spend": {}})
    active = state.get("active") if isinstance(state.get("active"), dict) else {}
    lines = [
        "<b>Этап 2 · DMarket → MARKET ORDER (Rust)</b>",
        f"Процесс: {'▶️ ON' if ctl.get('running') else '⏸ PAUSE'}",
        f"Новые покупки: {'ON' if ctl.get('auto_buy') else 'OFF'}",
        f"Вывод: {'ON' if ctl.get('allow_withdraw') else 'OFF'} | "
        f"Market: {'ON' if ctl.get('allow_market') else 'OFF'} | "
        f"автопередача: {'ON' if ctl.get('auto_deliver') else 'OFF'}",
        f"Активных сделок: {len(active)}/1 | завершено: {len(state.get('done') or [])}",
    ]
    for info in active.values():
        lines.append(
            f"📦 {info.get('name', '?')} · {info.get('stage', '?')} · "
            f"${int(info.get('buy_cents') or 0)/100:.2f} → "
            f"${int(info.get('projected_net_cents') or 0)/100:.2f}"
        )
    return "\n".join(lines)


def _combined_status(control: BotControl, state: dict) -> str:
    return base._status_text(control, state) + "\n\n" + _leg2_status_text()


def _help_text() -> str:
    return base._help_text() + (
        "\n\n<b>Этап 2 · DMarket → MARKET ORDER (Rust)</b>\n"
        "/leg2_status — состояние и активная сделка\n"
        "/leg2_start — включить полный автоматический цикл\n"
        "/leg2_pause — запретить новые покупки; активную сделку продолжить\n"
        "/leg2_finish — довести активную сделку без новых покупок\n"
        "/leg2_buy_on — разрешить новые покупки\n"
        "/leg2_buy_off — запретить новые покупки"
    )


def _menu_text(control: BotControl) -> str:
    ctl2 = _leg2_control()
    state2 = _json(LEG2_STATE, {"active": {}})
    return (
        "<b>Управление торговлей</b>\n"
        f"1️⃣ LIS → DMarket: покупки {'ON' if control.auto_buy else 'OFF'}\n"
        f"2️⃣ DMarket → MARKET ORDER: покупки {'ON' if ctl2.get('auto_buy') else 'OFF'}\n"
        f"Активных сделок этапа 2: {len(state2.get('active') or {})}/1"
    )


def _menu_keyboard(control: BotControl) -> dict:
    ctl2 = _leg2_control()
    leg1 = "🟢 Этап 1: ON" if control.auto_buy else "🔴 Этап 1: OFF"
    leg2 = "🟢 Этап 2: ON" if ctl2.get("auto_buy") else "🔴 Этап 2: OFF"
    return {"inline_keyboard": [
        [{"text": "📊 Общий статус", "callback_data": "menu_status"}],
        [{"text": leg1, "callback_data": "menu_leg1_toggle"},
         {"text": leg2, "callback_data": "menu_leg2_toggle"}],
        [{"text": "🛡 Довести текущую без новых", "callback_data": "menu_leg2_finish"}],
        [{"text": "🔄 Обновить", "callback_data": "menu_refresh"}],
    ]}


def _show_menu(control: BotControl) -> None:
    tg.send(_menu_text(control), reply_markup=_menu_keyboard(control))


def _handle_leg2(text: str) -> str | None:
    if text in ("/leg2", "/leg2_status"):
        return _leg2_status_text()
    if text == "/leg2_start":
        _set_leg2(running=True, auto_buy=True, allow_withdraw=True,
                  allow_market=True, auto_deliver=True)
        return "▶️ Этап 2 включён: покупка, вывод, MARKET ORDER и точная P2P-передача."
    if text in ("/leg2_pause", "/leg2_finish"):
        _set_leg2(running=True, auto_buy=False, allow_withdraw=True,
                  allow_market=True, auto_deliver=True)
        return "⏸ Новые покупки этапа 2 выключены; активная сделка будет безопасно завершена."
    if text == "/leg2_buy_on":
        _set_leg2(running=True, auto_buy=True)
        return "🟢 Новые покупки этапа 2 разрешены; остальные защитные ворота не изменены."
    if text == "/leg2_buy_off":
        _set_leg2(auto_buy=False)
        return "🔴 Новые покупки этапа 2 запрещены; активная сделка продолжает обрабатываться."
    return None


def _handle_update(upd: dict, control: BotControl, state_getter, next_offset: int):
    msg = upd.get("message")
    if msg:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(tg.TG_CHAT_ID):
            return
        text = (msg.get("text") or "").strip().lower()
        command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
        if command == "/menu":
            _show_menu(control)
            return
        if command == "/help":
            tg.send(_help_text())
            return
        if command == "/status":
            tg.send(_combined_status(control, state_getter()))
            return
        response = _handle_leg2(command)
        if response is not None:
            tg.send(response)
            return
    cq = upd.get("callback_query")
    if cq:
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != str(tg.TG_CHAT_ID):
            return
        action = cq.get("data")
        if action in {"menu_status", "menu_leg1_toggle", "menu_leg2_toggle",
                      "menu_leg2_finish", "menu_refresh"}:
            if action == "menu_status":
                tg.send(_combined_status(control, state_getter()))
                answer = "Статус отправлен"
            elif action == "menu_leg1_toggle":
                control.auto_buy = not control.auto_buy
                answer = f"Этап 1: {'ON' if control.auto_buy else 'OFF'}"
            elif action == "menu_leg2_toggle":
                ctl2 = _leg2_control()
                enabled = not bool(ctl2.get("auto_buy"))
                _set_leg2(running=True, auto_buy=enabled,
                          allow_withdraw=True, allow_market=True,
                          auto_deliver=True)
                answer = f"Этап 2: {'ON' if enabled else 'OFF'}"
            elif action == "menu_leg2_finish":
                _set_leg2(running=True, auto_buy=False, allow_withdraw=True,
                          allow_market=True, auto_deliver=True)
                answer = "Новые покупки этапа 2 выключены"
            else:
                answer = "Обновлено"
            tg.answer_callback(cq["id"], answer)
            message = cq.get("message") or {}
            message_id = message.get("message_id", message.get("id"))
            if message_id:
                tg.edit_message(chat_id, message_id, _menu_text(control),
                                reply_markup=_menu_keyboard(control))
            return
    base._handle_update(upd, control, state_getter, next_offset)


def poll_commands_loop(control: BotControl, state_getter):
    offset = 0
    while True:
        try:
            updates = tg.get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                _handle_update(upd, control, state_getter, offset)
        except Exception as exc:
            print(f"[control] poll error: {exc}")
            time.sleep(5)


def start(control: BotControl, state_getter) -> threading.Thread:
    thread = threading.Thread(
        target=poll_commands_loop, args=(control, state_getter), daemon=True
    )
    thread.start()
    return thread
