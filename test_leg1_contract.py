import sys
import types
import unittest
from unittest.mock import patch

# Contract tests do not need network/Steam/Telegram dependencies.  Stubbing
# them keeps the tests runnable on a clean workstation and on CI.
dotenv = types.ModuleType('dotenv')
dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault('dotenv', dotenv)

cloudscraper = types.ModuleType('cloudscraper')
cloudscraper.CloudScraper = object
cloudscraper.create_scraper = lambda: None
sys.modules.setdefault('cloudscraper', cloudscraper)

steam_login = types.ModuleType('steam_login')
steam_login.STEAM_LOGIN = ''
steam_login._cookies = {}
steam_login.get_steam_session = lambda force=False: False
sys.modules.setdefault('steam_login', steam_login)

for module_name in ('telegram_bot', 'bot_control', 'trade_log', 'lisskins_api'):
    sys.modules.setdefault(module_name, types.ModuleType(module_name))

import dmarket_api as dm
import skinstable_api as st
from leg1_scanner import build_skins_table_snapshot, register_live_confirmation


class FirstLegContractTests(unittest.TestCase):
    def test_skins_table_uses_order_pair(self):
        with patch.object(st, '_fetch2', return_value=({}, {})) as fetch:
            st.get_lisskins_and_dmarket_order(252490)
        fetch.assert_called_once_with(252490, 'LIS-SKINS', 'DMARKET ORDER')

    def test_table_snapshot_rejects_listing_substitution_and_stale_rows(self):
        now = 2_000_000_000
        fresh = now * 1000
        names, prices = build_skins_table_snapshot(
            {'Good': {'p': 1.0, 'c': 1, 't': fresh},
             'Stale': {'p': 1.0, 'c': 1, 't': (now - 4000) * 1000}},
            {'Good': {'p': 2.0, 'c': 2, 't': fresh},
             'Stale': {'p': 2.0, 'c': 2, 't': (now - 4000) * 1000}},
            min_buy_usd=0,
            max_buy_usd=10,
            min_orders=1,
            min_profit_pct=20,
            dmarket_fee=.05,
            max_age_sec=1800,
            now=now,
        )
        self.assertEqual(names, {'Good'})
        self.assertEqual(prices['Good']['order'], 2.0)

    def test_ten_percent_threshold_is_net_after_fee(self):
        # Gross spread is 15%, but after DMarket's 5% fee the net result is
        # only 9.25%, so it must not pass a 10% threshold.
        names, _ = build_skins_table_snapshot(
            {'GrossOnly': {'p': 1.0, 'c': 1}},
            {'GrossOnly': {'p': 1.15, 'c': 1}},
            min_buy_usd=0,
            max_buy_usd=10,
            min_orders=1,
            min_profit_pct=10,
            dmarket_fee=.05,
            max_age_sec=1800,
        )
        self.assertEqual(names, set())

        # At cent precision DMarket rounds the 5.8-cent fee up to 6 cents:
        # 1.16 - .06 = 1.10, exactly 10%, so it does pass.
        names, _ = build_skins_table_snapshot(
            {'NetTen': {'p': 1.0, 'c': 1}},
            {'NetTen': {'p': 1.16, 'c': 1}},
            min_buy_usd=0,
            max_buy_usd=10,
            min_orders=1,
            min_profit_pct=10,
            dmarket_fee=.05,
            max_age_sec=1800,
        )
        self.assertEqual(names, {'NetTen'})

        # The live run exposed the boundary case: $0.51 * .95 looked like
        # $0.4845 (+10.11% on a $0.44 buy), but the actual three-cent fee
        # leaves $0.48 (+9.09%). It must now be rejected before purchase.
        names, _ = build_skins_table_snapshot(
            {'RoundedBelowTen': {'p': .44, 'c': 1}},
            {'RoundedBelowTen': {'p': .51, 'c': 1}},
            min_buy_usd=0,
            max_buy_usd=10,
            min_orders=1,
            min_profit_pct=10,
            dmarket_fee=.05,
            max_age_sec=1800,
        )
        self.assertEqual(names, set())

    def test_live_profit_must_be_confirmed_twice(self):
        checks = {}
        self.assertFalse(register_live_confirmation(
            checks, 'Skin', 10.5, minimum_profit_pct=10, required=2,
            window_sec=300, now=1000,
        ))
        self.assertTrue(register_live_confirmation(
            checks, 'Skin', 10.2, minimum_profit_pct=10, required=2,
            window_sec=300, now=1030,
        ))
        self.assertFalse(register_live_confirmation(
            checks, 'Skin', 9.99, minimum_profit_pct=10, required=2,
            window_sec=300, now=1060,
        ))
        self.assertNotIn('Skin', checks)

    def test_best_target_uses_highest_live_price(self):
        response = {'orders': [
            {'price': '90', 'amount': '5', 'attributes': {}},
            {'price': '110', 'amount': '1', 'attributes': {}},
        ]}
        with patch.object(dm, '_req', return_value=(200, response)):
            target = dm.get_best_target('Skin')
        self.assertEqual(target['price_cents'], 110)

    def test_order_match_rechecks_target_and_honors_floor(self):
        with patch.object(dm, 'get_best_target', return_value={
            'price_cents': 90, 'price': .9, 'amount': 1, 'attributes': {},
        }), patch.object(dm, 'create_offer') as create:
            result = dm.create_order_matching_offer('asset', 'Skin', minimum_price_cents=100)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'target_below_floor')
        create.assert_not_called()

        with patch.object(dm, 'get_best_target', return_value={
            'price_cents': 110, 'price': 1.1, 'amount': 1, 'attributes': {},
        }), patch.object(dm, 'create_offer', return_value={
            'success': True, 'offer_id': 'offer-1', 'data': {},
        }) as create:
            result = dm.create_order_matching_offer('asset', 'Skin', minimum_price_cents=100)
        self.assertTrue(result['success'])
        create.assert_called_once_with('asset', 110)

    def test_sale_requires_target_and_exact_asset(self):
        trades = {'Trades': [
            {'Title': 'Skin', 'Status': 'successful', 'AssetID': 'wrong',
             'TargetID': 't1', 'OfferClosedAt': '200',
             'Price': {'Amount': 2}, 'Fee': {'Amount': {'Amount': .1}}},
            {'Title': 'Skin', 'Status': 'successful', 'AssetID': 'asset-1',
             'TargetID': '', 'OfferClosedAt': '200',
             'Price': {'Amount': 2}, 'Fee': {'Amount': {'Amount': .1}}},
            {'Title': 'Skin', 'Status': 'successful', 'AssetID': 'asset-1',
             'TargetID': 'target-1', 'OfferClosedAt': '200',
             'Price': {'Amount': 2}, 'Fee': {'Amount': {'Amount': .1}}},
        ]}
        with patch.object(dm, '_req', return_value=(200, trades)):
            sale = dm.find_recent_sale('Skin', 100, asset_id='asset-1', require_target=True)
        self.assertEqual(sale['target_id'], 'target-1')


if __name__ == '__main__':
    unittest.main()
