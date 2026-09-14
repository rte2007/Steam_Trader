# -*- coding: utf-8 -*-
"""
Лог сделок LS→DMarket в Excel (trade_log.xlsx). Одна строка на предмет:
создаётся при покупке (log_buy), дополняется при продаже (log_sale).
"""
import os
from datetime import datetime, timezone, timedelta
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:
    MSK = timezone(timedelta(hours=3))  # fallback: МСК без БД таймзон


def _now() -> datetime:
    return datetime.now(MSK)


LOG_FILE = os.path.join(os.path.dirname(__file__), 'trade_log.xlsx')

HEADERS = [
    'Дата покупки', 'Предмет', 'Цена LS ($)', 'Живой профит ожид. (%)',
    'Статус', 'Дата продажи', 'Цена продажи ($)', 'Комиссия DMarket ($)',
    'Чистыми ($)', 'Прибыль ($)', 'Прибыль (%)',
]
COL_WIDTHS = [16, 28, 11, 18, 12, 16, 14, 18, 14, 14, 13]

_HDR_FILL = PatternFill('solid', fgColor='1F4E79')
_HDR_FONT = Font(bold=True, color='FFFFFF', size=10)
_ALT_FILL = PatternFill('solid', fgColor='D6E4F0')
_PROFIT_FILL = PatternFill('solid', fgColor='E2EFDA')
_LOSS_FILL   = PatternFill('solid', fgColor='FCE4E4')
_BD = Side(style='thin')
_BORDER = Border(left=_BD, right=_BD, top=_BD, bottom=_BD)


def _style_ws(ws):
    for ci, w in enumerate(COL_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[1].height = 30
    for cell in ws[1]:
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = Alignment(horizontal='center', wrap_text=True)
        cell.border = _BORDER
    for ri, row in enumerate(ws.iter_rows(min_row=2), 2):
        profit_cell = row[9]  # 'Прибыль ($)'
        for ci, cell in enumerate(row, 1):
            if ci in (9, 10) and isinstance(profit_cell.value, (int, float)):
                cell.fill = _LOSS_FILL if profit_cell.value < 0 else _PROFIT_FILL
            else:
                cell.fill = _ALT_FILL if ri % 2 == 0 else PatternFill()
            cell.border = _BORDER
            cell.alignment = Alignment(horizontal='center')
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions


def _load_wb():
    if os.path.exists(LOG_FILE):
        try:
            return openpyxl.load_workbook(LOG_FILE)
        except Exception:
            pass
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Сделки'
    ws.append(HEADERS)
    _style_ws(ws)
    wb.save(LOG_FILE)
    return wb


def _save(wb):
    try:
        wb.save(LOG_FILE)
    except Exception as e:
        print(f'[trade_log] save error: {e}')


def log_buy(name: str, ls_price: float, live_pct: float):
    """Записать покупку — новая строка со статусом 'В процессе'."""
    try:
        wb = _load_wb()
        ws = wb['Сделки']
        ws.append([
            _now().strftime('%Y-%m-%d %H:%M'), name,
            round(ls_price, 4), round(live_pct, 1),
            'В процессе', '', '', '', '', '', '',
        ])
        _style_ws(ws)
        _save(wb)
    except Exception as e:
        print(f'[trade_log] log_buy error: {e}')


def remove_pending(name: str):
    """
    Удалить последнюю незавершённую строку ('В процессе') по имени —
    когда трейд с покупкой отменён/отклонён и сделка никогда не завершится
    (см. arb_bot_ls.check_declined_trades()). Без этого такие строки
    висели бы в таблице вечно как "в процессе", хотя предмет уже не придёт.
    """
    try:
        wb = _load_wb()
        ws = wb['Сделки']
        for row in reversed(list(ws.iter_rows(min_row=2))):
            if row[1].value == name and row[4].value == 'В процессе':
                ws.delete_rows(row[0].row)
                break
        _style_ws(ws)
        _save(wb)
    except Exception as e:
        print(f'[trade_log] remove_pending error: {e}')


def log_sale(name: str, sale_price: float, fee: float, buy_price: float):
    """Дополнить последнюю незавершённую строку по имени данными о продаже."""
    try:
        wb = _load_wb()
        ws = wb['Сделки']
        net    = round(sale_price - fee, 4)
        profit = round(net - buy_price, 4) if buy_price else None
        pct    = round(profit / buy_price * 100, 1) if buy_price else None

        # FIFO: закрываем САМУЮ СТАРУЮ незакрытую покупку этого тайтла.
        # Раньше здесь был reversed() — закрывалась самая свежая, а старые
        # строки оставались "В процессе" НАВСЕГДА. При покупке одного тайтла
        # несколько раз подряд (окно перекупки 10 мин делает это обычным)
        # журнал накапливал мусор: 7 висящих строк на $6.66 при полностью
        # пустой очереди бота (2026-07-31). Порядок теперь совпадает с
        # _pop_buy_price() в arb_bot_ls.py, который тоже берёт старейшую цену.
        for row in ws.iter_rows(min_row=2):
            if row[1].value == name and not row[5].value:  # 'Дата продажи' пусто
                row[5].value = _now().strftime('%Y-%m-%d %H:%M')
                row[6].value = round(sale_price, 4)
                row[7].value = round(fee, 4)
                row[8].value = net
                row[9].value = profit
                row[10].value = pct
                row[4].value = 'Продано'
                break
        else:
            # Не нашли открытую покупку (например, продано без учтённой покупки) —
            # добавляем отдельную строку, чтобы сделка не потерялась из лога.
            ws.append([
                _now().strftime('%Y-%m-%d %H:%M'), name, buy_price or '', '',
                'Продано', _now().strftime('%Y-%m-%d %H:%M'),
                round(sale_price, 4), round(fee, 4), net, profit, pct,
            ])
        _style_ws(ws)
        _save(wb)
    except Exception as e:
        print(f'[trade_log] log_sale error: {e}')


def mark_refunded(name: str, marked_ts: float) -> bool:
    """
    Переправить запись, ранее списанную как убыток (log_sale(name, 0, 0, ...)
    в момент "потери" покупки), на статус 'Не доставлено' — когда более
    поздняя проверка (check_loss_followups) выясняет, что LIS-SKINS всё же
    вернула деньги. Раньше в такой ситуации бот только слал алерт в Telegram
    с просьбой поправить журнал руками — на практике это означало долгий
    ручной разбор задним числом (см. инцидент 2026-08-12, Wings Of Death
    SKS: 20+ сообщений туда-сюда, чтобы найти и поправить одну строку).
    Теперь правим сами, сразу.

    Ищем строку 'Продано' с profit == -buy_price (100% списание, наш
    сигнатурный след потери) и датой продажи БЛИЖАЙШЕЙ к marked_ts — на
    случай если этот тайтл терялся больше одного раза, чтобы не задеть
    чужую строку. Возвращает True, если строку нашли и поправили.
    """
    try:
        wb = _load_wb()
        ws = wb['Сделки']
        target_dt = datetime.fromtimestamp(marked_ts, MSK).strftime('%Y-%m-%d %H:%M')
        best = None
        for row in ws.iter_rows(min_row=2):
            if row[1].value != name or row[4].value != 'Продано':
                continue
            buy_price, profit = row[2].value, row[9].value
            if not (isinstance(buy_price, (int, float)) and isinstance(profit, (int, float))):
                continue
            if abs(profit - (-buy_price)) > 0.001:
                continue  # не 100%-списание — это обычная убыточная продажа, не трогаем
            sold = str(row[5].value or '')
            dist = abs((datetime.strptime(sold, '%Y-%m-%d %H:%M') - datetime.strptime(target_dt, '%Y-%m-%d %H:%M')).total_seconds()) if sold else float('inf')
            if best is None or dist < best[0]:
                best = (dist, row)
        if best is None:
            return False
        row = best[1]
        row[4].value = 'Не доставлено'
        row[8].value = None
        row[9].value = None
        row[10].value = None
        _style_ws(ws)
        _save(wb)
        return True
    except Exception as e:
        print(f'[trade_log] mark_refunded error: {e}')
        return False
