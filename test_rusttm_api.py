import unittest
from unittest.mock import Mock, patch

import rusttm_api


def response(payload, status=200):
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    return result


class RustTmPriceTests(unittest.TestCase):
    @patch("rusttm_api.requests.get")
    def test_dedicated_orders_feed_overrides_wrong_class_instance_order(self, get):
        get.side_effect = [
            response({"success": True, "items": {"1_0": {
                "market_hash_name": "Tarp Rug", "price": 436.32, "buy_order": 3
            }}}),
            response({"success": True, "items": [
                {"market_hash_name": "Tarp Rug", "price": 46.41, "volume": 4}
            ]}),
        ]
        row = rusttm_api.get_prices("RUB")["Tarp Rug"]
        self.assertEqual(46.41, row["buy_order"])
        self.assertEqual(4, row["buy_order_volume"])

    @patch("rusttm_api.requests.get")
    def test_order_above_lowest_listing_is_rejected(self, get):
        get.side_effect = [
            response({"success": True, "items": {"1_0": {
                "market_hash_name": "Broken", "price": 10, "buy_order": 1
            }}}),
            response({"success": True, "items": [
                {"market_hash_name": "Broken", "price": 11, "volume": 1}
            ]}),
        ]
        self.assertEqual(0, rusttm_api.get_prices("RUB")["Broken"]["buy_order"])


if __name__ == "__main__":
    unittest.main()
