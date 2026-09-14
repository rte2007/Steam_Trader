# Steam Skin Trader

Автоматизация торговли игровыми предметами с тремя независимыми маршрутами:

- Rust: LIS-SKINS → DMarket Order (`arb_bot_ls.py`), минимум 8% по умолчанию;
- Rust recovery: DMarket → rust.tm (`dm_market_leg2.py`), новые покупки по умолчанию выключены;
- CS2: DMarket → Market.CS2 Order (`dm_market_cs2.py`), фильтры ликвидности, типа предмета и блокировок Steam.

Это очищенная версия проекта для передачи другому владельцу. В репозитории нет аккаунтов, API-ключей, Steam Guard, cookies, сессий, истории сделок или серверных паролей.

## Быстрый запуск на Ubuntu 22.04+

```bash
git clone <repository-url> /opt/steam-skin-trader
cd /opt/steam-skin-trader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python configure.py
.venv/bin/python -m unittest discover -v
.venv/bin/python scan_secrets.py
```

Мастер `configure.py` попросит API-ключи, данные Steam, Telegram и при необходимости путь к `maFile`. Он создаёт локальный `.env` и копирует `maFile` в игнорируемую папку `private/`. Эти файлы нельзя отправлять в GitHub.

## Безопасное включение

После настройки все новые покупки выключены. Сначала запустите нужный процесс вручную и проверьте его вывод:

```bash
.venv/bin/python -u arb_bot_ls.py
.venv/bin/python -u dm_market_leg2.py
.venv/bin/python -u dm_market_cs2.py
```

Для первого маршрута покупка включается параметром `AUTO_BUY=true` в `.env`. Rust recovery и CS2 используют соответственно `dm_market_control.json` и `dm_market_cs2_control.json`; мастер создаёт их с `auto_buy: false`. Не включайте покупку до проверки API, Steam-трейдов и расчёта комиссий на своём аккаунте.

## systemd

Готовые службы рассчитаны на каталог `/opt/steam-skin-trader` и читают секреты только из `/opt/steam-skin-trader/.env`:

```bash
sudo cp project-steam-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now project-steam-leg1.service
```

Остальные маршруты включайте отдельно только после ручной проверки. Логи доступны через `journalctl -u <service-name>`.

## Telegram

Укажите `TELEGRAM_TOKEN` и `TELEGRAM_CHAT_ID`. Интерактивное управление первого маршрута запускается при `TG_CONTROL_ENABLED=true`. Бот принимает действия только из указанного чата владельца.

## Перед публикацией

```bash
python scan_secrets.py
git status --short
```

`.gitignore` блокирует основные приватные файлы, но перед каждым push всё равно проверяйте список добавленных файлов. Если ключ когда-либо попадал в коммит или публичный чат, его нужно отозвать и выпустить заново — удаления строки из нового коммита недостаточно.

## Важно

Торговля не гарантирует прибыль. Цены, комиссии, ликвидность, API и правила площадок меняются. Владелец аккаунтов самостоятельно отвечает за лимиты, условия сервисов, налоги и возможные убытки. Начинайте с минимальных сумм.
