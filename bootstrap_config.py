"""Load credentials from a local .env or an optional legacy JSON config."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv


KEY_MAP = {
    "lisskins_api_key": "LISSKINS_API_KEY",
    "dmarket_private_key": "DM_PRIVATE_KEY",
    "dmarket_public_key": "DM_PUBLIC_KEY",
    "steam_api_key": "STEAM_API_KEY",
    "steam_id": "STEAM_ID",
    "steam_login": "STEAM_LOGIN",
    "steam_password": "STEAM_PASSWORD",
    "shared_secret": "STEAM_SHARED_SECRET",
    "identity_secret": "STEAM_IDENTITY_SECRET",
    "telegram_bot_token": "TELEGRAM_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "rusttm_api_key": "RUSTTM_API_KEY",
    "market_api_key": "MARKET_CSGO_API_KEY",
}


def _candidate_paths() -> list[Path]:
    explicit = os.getenv("PROJECT_STEAM_CONFIG")
    here = Path(__file__).resolve().parent
    candidates = [
        Path(explicit) if explicit else None,
        here / "config.json",
        here.parent / "config.json",
        Path("/etc/steam-skin-trader/config.json"),
    ]
    return [path for path in candidates if path is not None]


def load_shared_config(*, override: bool = True) -> Path | None:
    # The public distribution keeps all private values in an ignored .env.
    # Existing JSON installations remain supported for backwards compatibility.
    here = Path(__file__).resolve().parent
    load_dotenv(here / ".env", override=False)
    config_path = next((path for path in _candidate_paths() if path.is_file()), None)
    if config_path is None:
        return None
    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    for json_key, env_key in KEY_MAP.items():
        value = data.get(json_key)
        if value not in (None, "") and (override or not os.getenv(env_key)):
            os.environ[env_key] = str(value)

    trade_url = data.get("trade_offer_url") or ""
    if trade_url:
        query = parse_qs(urlparse(trade_url).query)
        partner = (query.get("partner") or [""])[0]
        token = (query.get("token") or [""])[0]
        if partner and (override or not os.getenv("STEAM_PARTNER32")):
            os.environ["STEAM_PARTNER32"] = partner
        if token and (override or not os.getenv("STEAM_TRADE_TOKEN")):
            os.environ["STEAM_TRADE_TOKEN"] = token
    return config_path
