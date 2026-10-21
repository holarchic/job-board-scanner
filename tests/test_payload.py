"""Shared adapter helpers tested on their own: pay ranges written out."""
import unittest

from jobscan.payload import MAX_PAY, pay_range


class PayRangeTest(unittest.TestCase):
    def test_amounts_are_written_with_separators_and_decimals_only_when_needed(self):
        for args, written in [(("USD", 165000, 190000), "USD 165,000 to 190,000"),
                              (("USD", 16500000, 19000000, 100), "USD 165,000 to 190,000"),
                              (("USD", 2696, 2696, 100), "USD 26.96"),
                              (("USD", 52.5, 64), "USD 52.50 to 64.00"),
                              (("GBP", 0, 1), "GBP 0 to 1"),
                              ((" GBP\n", 85000.0, 85000), "GBP 85,000")]:
            self.assertEqual(pay_range(*args), written, args)

    def test_malformed_pay_is_none(self):
        for args in [("USD", True, 2), ("USD", 1, False), ("USD", "1", 2), ("USD", None, 2), ("USD", 1, None),
                     ("USD", float("nan"), 2), ("USD", 1, float("inf")), ("USD", -1, 2), ("USD", 3, 2),
                     ("USD", 1, MAX_PAY + 1), ("USD", 1, 10**400), ("", 1, 2), ("  ", 1, 2), (None, 1, 2), (3, 1, 2),
                     ("U\ud800", 1, 2)]:
            self.assertIsNone(pay_range(*args), args)


if __name__ == "__main__":
    unittest.main()
