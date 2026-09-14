import unittest

from dm_market_core import (Limits, build_candidates, choose_dmarket_offer,
                            exact_inventory_asset_id, exact_market_offer, extract_steam_access_token,
                            market_order_is_executable, net_after_market_fee)


class DmMarketCoreTests(unittest.TestCase):
    def test_one_percent_is_after_ten_percent_fee(self):
        now = 2_000_000
        lim = Limits(min_profit_pct=1, market_fee_pct=10, order_undercut_usd=0)
        rows = build_candidates(
            {"A": {"p": 1.00, "c": 1, "t": now}},
            {"A": {"p": 1.14, "c": 2, "t": now}}, lim, now_ms=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_price"], 1.14)
        self.assertEqual(rows[0]["projected_net"], 1.026)
        self.assertAlmostEqual(rows[0]["profit_pct"], 2.6)

    def test_fee_floor_never_overstates_revenue(self):
        self.assertEqual(net_after_market_fee(2.47, 10), 2.223)

    def test_zero_max_buy_means_unlimited(self):
        now = 2_000_000
        lim = Limits(min_profit_pct=1, market_fee_pct=10, max_buy_usd=0)
        rows = build_candidates(
            {"A": {"p": 100.0, "c": 1, "t": now}},
            {"A": {"p": 120.0, "c": 1, "t": now}}, lim, now_ms=now)
        self.assertEqual(1, len(rows))

    def test_broken_market_order_above_listing_is_rejected(self):
        self.assertTrue(market_order_is_executable(0.60, 0.58))
        self.assertTrue(market_order_is_executable(0.58, 0.58))
        self.assertFalse(market_order_is_executable(0.57, 0.58))
        self.assertFalse(market_order_is_executable(0, 0.58))

    def test_locked_or_nonwithdrawable_offer_is_rejected(self):
        base = {"offerId": "1", "priceCents": 100,
                "attributes": {"id": "a", "classId": "c", "withdrawable": True, "tradeLockDays": 0}}
        self.assertEqual(choose_dmarket_offer([base], 1.00)["offerId"], "1")
        self.assertIsNone(choose_dmarket_offer([{**base, "attributes": {**base["attributes"], "tradeLockDays": 1}}], 1.00))

    def test_market_delivery_must_contain_exact_asset_only(self):
        good = {"items": [{"assetid": "42"}], "partner": 1}
        bad = {"items": [{"assetid": "42"}, {"assetid": "99"}], "partner": 2}
        self.assertEqual(exact_market_offer([bad, good], "42"), good)

    def test_exact_inventory_asset_can_come_from_direct_steam(self):
        market_inventory = []
        steam_inventory = [{"market_hash_name": "Tarp Rug", "assetid": "42"}]
        self.assertEqual(
            exact_inventory_asset_id("Tarp Rug", [], market_inventory, steam_inventory),
            "42",
        )

    def test_exact_inventory_asset_rejects_duplicates(self):
        steam_inventory = [
            {"market_hash_name": "Tarp Rug", "assetid": "42"},
            {"market_hash_name": "Tarp Rug", "assetid": "43"},
        ]
        self.assertIsNone(exact_inventory_asset_id("Tarp Rug", [], steam_inventory))
        self.assertEqual(exact_inventory_asset_id("Tarp Rug", ["42"], steam_inventory), "43")

    def test_exact_inventory_asset_normalizes_cstrade_id(self):
        auxiliary = [{"market_hash_name": "Tarp Rug", "id": "42_252490"}]
        self.assertEqual(exact_inventory_asset_id("Tarp Rug", [], auxiliary), "42")

    def test_extracts_jwt_from_secure_cookie(self):
        self.assertEqual(extract_steam_access_token("765%7C%7Caaa.bbb.ccc"), "aaa.bbb.ccc")
        self.assertIsNone(extract_steam_access_token("invalid"))


if __name__ == "__main__":
    unittest.main()
