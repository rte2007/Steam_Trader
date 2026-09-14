import unittest

from leg3_core import (choose_cs2_dmarket_offer, dmarket_unlock_at, exposure_key,
                       is_regular_weapon_name, listing_price, order_exit_price,
                       steam_unlock_at, wear_of)


class Leg3RulesTest(unittest.TestCase):
    def test_regular_weapon_variants(self):
        self.assertTrue(is_regular_weapon_name("AK-47 | Redline (Field-Tested)"))
        self.assertEqual(wear_of("AK-47 | Redline (Field-Tested)"), "FT")
        self.assertFalse(is_regular_weapon_name("StatTrak™ AK-47 | Redline (Field-Tested)"))
        self.assertFalse(is_regular_weapon_name("Souvenir AWP | Desert Hydra (Factory New)"))

    def test_exposure_is_exact_skin_and_wear(self):
        self.assertEqual(exposure_key("AK-47 | Redline (FT)"), exposure_key("ak-47 | redline (ft)"))
        self.assertNotEqual(exposure_key("AK-47 | Redline (FT)"), exposure_key("AK-47 | Redline (FN)"))

    def test_listing_price_never_below_reference_or_break_even(self):
        self.assertEqual(listing_price(3.00, 2.50, 2.00), 3.00)
        self.assertEqual(listing_price(3.00, 3.50, 2.00), 3.49)
        self.assertEqual(listing_price(1.00, 1.00, 2.00), 2.00)
        self.assertEqual(listing_price(1.00, 1.00, 2.00, market_fee_pct=5), 2.11)

    def test_order_exit_uses_highest_without_selling_below_reference(self):
        self.assertEqual(order_exit_price(3.20, 3.50, 3.00), 3.50)
        self.assertEqual(order_exit_price(3.20, 2.80, 3.00), 3.20)

    def test_dmarket_exact_unlock_date_wins(self):
        attrs = {"withdrawable": False, "tradeLockDays": 5,
                 "unlockDate": "2026-09-17T16:00:00Z"}
        self.assertEqual(1789660800, dmarket_unlock_at(attrs, 1789200000))

    def test_steam_fallback_waits_eight_days(self):
        self.assertEqual(1_691_200, steam_unlock_at(1_000_000))

    def test_locked_dmarket_offer_can_be_selected(self):
        offer = {"offerId": "o", "priceCents": 155,
                 "attributes": {"id": "a", "classId": "c",
                                "withdrawable": False, "tradeLockDays": 5}}
        self.assertIs(offer, choose_cs2_dmarket_offer([offer], 1.55, 3))


if __name__ == "__main__":
    unittest.main()
