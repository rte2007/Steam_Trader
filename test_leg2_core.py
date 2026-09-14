import unittest

from leg2_core import (
    Limits, build_candidates, choose_live_offer, eligible_user_item,
    find_new_item, match_incoming_asset_offer, projected_net_cents,
)


class Leg2CoreTests(unittest.TestCase):
    def test_default_math_has_no_safety_reserve(self):
        lim = Limits(min_profit_pct=60)
        self.assertEqual(projected_net_cents(2.47, lim), 247)
        now = 2_000_000
        rows = build_candidates(
            {"Neon Auto Turret": {"p": 1.52, "c": 1, "t": now}},
            {"Neon Auto Turret": {"p": 2.47, "c": 1, "o": 0, "t": now}},
            lim,
            now_ms=now,
        )
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["profit_pct"], 62.5)

    def test_exact_pair_math_is_conservative(self):
        lim = Limits(min_profit_pct=10, deposit_safety_pct=2, fixed_cost_cents=1)
        self.assertEqual(projected_net_cents(1.00, lim), 97)
        now = 2_000_000
        rows = build_candidates(
            {"A": {"p": 0.89, "c": 1, "t": now}},
            {"A": {"p": 1.00, "c": 1, "o": 0, "t": now}},
            lim,
            now_ms=now,
        )
        self.assertEqual(rows, [])  # raw spread clears 10%, conservative cents do not

    def test_filters_overstock_zero_stock_and_stale(self):
        now = 100_000_000
        dm = {x: {"p": 1, "c": 1, "t": now} for x in "ABC"}
        dep = {
            "A": {"p": 2, "c": 1, "o": 1, "t": now},
            "B": {"p": 2, "c": 0, "o": 0, "t": now},
            "C": {"p": 2, "c": 1, "o": 0, "t": now - 25 * 3600 * 1000},
        }
        self.assertEqual(build_candidates(dm, dep, Limits(), now_ms=now), [])

    def test_live_offer_requires_withdrawable_unlocked_and_ids(self):
        offers = [
            {"offerId": "locked", "priceCents": 90, "attributes": {"withdrawable": True, "tradeLockDays": 1, "id": "x", "classId": "c"}},
            {"offerId": "no", "priceCents": 80, "attributes": {"withdrawable": False, "tradeLockDays": 0, "id": "y", "classId": "c"}},
            {"offerId": "ok", "priceCents": 101, "attributes": {"withdrawable": True, "tradeLockDays": 0, "id": "z", "classId": "c"}},
        ]
        self.assertEqual(choose_live_offer(offers, 100, max_slippage_pct=3)["offerId"], "ok")

    def test_new_item_must_be_unambiguous(self):
        items = [{"id": "11_252490", "market_hash_name": "A"}, {"id": "12_252490", "market_hash_name": "A"}]
        self.assertEqual(find_new_item(items, "A", {"11"})["id"], "12_252490")
        self.assertIsNone(find_new_item(items, "A", set()))

    def test_trade_protected_item_is_not_sent(self):
        self.assertEqual(eligible_user_item({"price": 1, "tradable": False})[1], "steam_trade_protected")
        self.assertEqual(eligible_user_item({"price": 1, "tradable_bool": 0})[1], "steam_trade_protected")
        self.assertEqual(eligible_user_item({"price": 1, "tradable": True}), (True, "ok"))

    def test_outgoing_offer_matches_exact_single_asset(self):
        good = {"tradeofferid": "7", "trade_offer_state": 2, "items_to_receive": [], "items_to_give": [{"assetid": "42"}]}
        bad = {"tradeofferid": "8", "trade_offer_state": 2, "items_to_receive": [], "items_to_give": [{"assetid": "42"}, {"assetid": "99"}]}
        self.assertEqual(match_incoming_asset_offer([bad, good], "42")["tradeofferid"], "7")


if __name__ == "__main__":
    unittest.main()
