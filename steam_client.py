# -*- coding: utf-8 -*-
"""Proxy to steam_login — kept for backward compatibility."""
from steam_login import (login, get_trade_offers, accept_trade, get_inventory, get_recent_offers,
                         create_trade_offer, sell_on_market, get_app_inventory)

def get_client():
    return login()
