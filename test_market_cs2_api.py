import unittest
from unittest.mock import Mock, patch

import market_cs2_api


def response(payload, status=200):
    result = Mock()
    result.status_code = status
    result.content = b"x"
    result.json.return_value = payload
    return result


class MarketCs2ApiTests(unittest.TestCase):
    @patch("market_cs2_api.requests.get")
    def test_dedicated_orders_are_used(self, get):
        get.side_effect = [
            response({"success": True, "items": {"1_0": {
                "market_hash_name": "AK-47 | Test (Field-Tested)",
                "price": 4.0, "buy_order": 1.0}}}),
            response({"success": True, "items": [{
                "market_hash_name": "AK-47 | Test (Field-Tested)",
                "price": 3.5, "volume": 12}]}),
        ]
        row = market_cs2_api.get_prices("USD")["AK-47 | Test (Field-Tested)"]
        self.assertEqual(3.5, row["buy_order"])
        self.assertEqual(12, row["buy_order_volume"])


if __name__ == "__main__":
    unittest.main()
