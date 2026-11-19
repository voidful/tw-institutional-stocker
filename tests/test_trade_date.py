import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pandas as pd
import update_all


class TradeDateTests(unittest.TestCase):
    def test_publication_boundaries(self):
        cases = [
            ("2026-11-18T03:51:00+08:00", "2026-11-17"),
            ("2026-11-18T18:59:59+08:00", "2026-11-17"),
            ("2026-11-18T19:00:00+08:00", "2026-11-18"),
            ("2026-11-18T23:59:59+08:00", "2026-11-18"),
            ("2026-11-19T00:00:00+08:00", "2026-11-18"),
            ("2026-11-16T08:00:00+08:00", "2026-11-13"),
            ("2026-11-14T20:00:00+08:00", "2026-11-13"),
            ("2026-11-15T20:00:00+08:00", "2026-11-13"),
            ("2026-01-01T03:00:00+08:00", "2025-12-31"),
            ("2026-11-17T19:51:00+00:00", "2026-11-17"),
        ]
        for instant, expected in cases:
            with self.subTest(instant=instant):
                self.assertEqual(update_all.get_target_trade_date(
                    datetime.fromisoformat(instant)), date.fromisoformat(expected))

    def test_default_clock_uses_taipei(self):
        with patch.object(update_all, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 18, 3, 51,
                                             tzinfo=ZoneInfo("Asia/Taipei"))
            self.assertEqual(update_all.get_target_trade_date(), date(2026, 11, 17))
            clock.now.assert_called_once_with(ZoneInfo("Asia/Taipei"))

    def test_reject_naive_datetime(self):
        with self.assertRaises(ValueError):
            update_all.get_target_trade_date(datetime(2026, 11, 18))

    def test_stale_data_still_fails_validation(self):
        history = pd.DataFrame([{
            "date": date(2026, 11, 16), "code": "2330", "name": "TSMC",
            "foreign_net": 1, "trust_net": 0, "dealer_net": 0, "market": "TWSE",
        }])
        with self.assertRaisesRegex(RuntimeError, "latest date.*target date"):
            update_all.validate_flow_history(history, "TWSE", date(2026, 11, 17))


if __name__ == "__main__":
    unittest.main()
