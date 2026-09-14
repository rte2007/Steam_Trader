"""Interactive first-run configuration.  Secrets are written only to .env."""
from __future__ import annotations

import getpass
import json
import os
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
PRIVATE_DIR = ROOT / "private"


FIELDS = (
    ("LISSKINS_API_KEY", "LIS-SKINS API key", True),
    ("DM_PRIVATE_KEY", "DMarket private key", True),
    ("DM_PUBLIC_KEY", "DMarket public key", False),
    ("STEAM_API_KEY", "Steam Web API key", True),
    ("STEAM_ID", "SteamID64", False),
    ("STEAM_LOGIN", "Steam login", False),
    ("STEAM_PASSWORD", "Steam password", True),
    ("STEAM_SHARED_SECRET", "Steam Guard shared_secret", True),
    ("STEAM_IDENTITY_SECRET", "Steam Guard identity_secret", True),
    ("TELEGRAM_TOKEN", "Telegram bot token", True),
    ("TELEGRAM_CHAT_ID", "Telegram owner chat ID", False),
    ("RUSTTM_API_KEY", "rust.tm API key (optional)", True),
    ("MARKET_CSGO_API_KEY", "Market.CS2 API key (optional)", True),
)


DEFAULTS = {
    "DMM_STEAM_MAFILE": "private/steam_guard.maFile",
    "DMCS_STEAM_MAFILE": "private/steam_guard.maFile",
    "MIN_PROFIT_PCT": "8",
    "MIN_BUY_USD": "0.30",
    "MAX_BUY_USD": "0",
    "MAX_CONCURRENT": "1",
    "MAX_DAILY_SPEND_USD": "0",
    "MAX_REBUYS_PER_NAME": "1",
    "AUTO_BUY": "false",
    "AUTO_ACCEPT": "true",
    "AUTO_DEPOSIT": "true",
    "POLL_INTERVAL": "30",
    "TRADE_POLL_SEC": "15",
    "STEAM_LOGIN_RETRY_SEC": "300",
    "TG_CONTROL_ENABLED": "false",
    "LIVE_CONFIRMATIONS_REQUIRED": "2",
    "LIVE_CONFIRMATION_WINDOW_SEC": "300",
    "MAX_LOSS_PCT": "0",
    "ONE_SHOT": "false",
    "DMM_MIN_PROFIT_PCT": "1",
    "DMM_MARKET_FEE_PCT": "10",
    "DMM_MIN_BUY_USD": "0.30",
    "DMM_MAX_BUY_USD": "0",
    "DMM_MAX_ACTIVE": "1",
    "DMM_MAX_DAILY_SPEND_USD": "0",
    "DMM_MAX_SLIPPAGE_PCT": "3",
    "DMM_ORDER_UNDERCUT_USD": "0",
    "DMM_POLL_SEC": "60",
    "DMCS_MIN_PROFIT_PCT": "1",
    "DMCS_MARKET_FEE_PCT": "10",
    "DMCS_MIN_MARKET_ORDER_USD": "2",
    "DMCS_MIN_SALES_14D": "300",
    "DMCS_MIN_BUY_USD": "0.30",
    "DMCS_MAX_BUY_USD": "0",
    "DMCS_MAX_ACTIVE": "1",
    "DMCS_MAX_DAILY_SPEND_USD": "0",
    "DMCS_MAX_SLIPPAGE_PCT": "3",
    "DMCS_POLL_SEC": "300",
}


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for raw in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        values[key.strip()] = value
    return values


def ask(label: str, current: str, secret: bool) -> str:
    suffix = " [уже задано]" if current else ""
    prompt = f"{label}{suffix}: "
    value = getpass.getpass(prompt) if secret else input(prompt)
    return value.strip() or current


def parse_trade_url(value: str) -> tuple[str, str]:
    query = parse_qs(urlparse(value).query)
    return (query.get("partner") or [""])[0], (query.get("token") or [""])[0]


def write_env(values: dict[str, str]) -> None:
    lines = [
        "# Generated locally by configure.py. Never commit this file.",
        *[f"{key}={json.dumps(str(value), ensure_ascii=False)}" for key, value in values.items()],
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    if os.name != "nt":
        ENV_PATH.chmod(0o600)


def main() -> None:
    print("Настройка Steam Skin Trader. Введённые секреты не отображаются.")
    values = {**DEFAULTS, **read_env()}
    for key, label, secret in FIELDS:
        values[key] = ask(label, values.get(key, ""), secret)

    trade_url = ask("Steam trade URL", "", True)
    if trade_url:
        partner, token = parse_trade_url(trade_url)
        if not partner or not token:
            raise SystemExit("Не удалось прочитать partner/token из Steam trade URL.")
        values["STEAM_PARTNER32"] = partner
        values["STEAM_TRADE_TOKEN"] = token

    mafile = ask("Путь к Steam Guard maFile (optional)", "", False)
    if mafile:
        source = Path(mafile).expanduser().resolve()
        if not source.is_file():
            raise SystemExit(f"maFile не найден: {source}")
        PRIVATE_DIR.mkdir(exist_ok=True)
        target = PRIVATE_DIR / "steam_guard.maFile"
        shutil.copy2(source, target)
        if os.name != "nt":
            target.chmod(0o600)

    write_env(values)
    for name in ("dm_market_control.json", "dm_market_cs2_control.json"):
        path = ROOT / name
        if not path.exists():
            path.write_text(json.dumps({
                "running": True,
                "auto_buy": False,
                "allow_withdraw": True,
                "allow_market": True,
                "auto_deliver": True,
            }, indent=2), encoding="utf-8")
    print(f"Готово: {ENV_PATH}")
    print("Автопокупки оставлены выключенными. Сначала выполните тесты и dry-run.")


if __name__ == "__main__":
    main()
