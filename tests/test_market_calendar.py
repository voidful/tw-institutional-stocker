import unittest
from datetime import date, datetime

import pandas as pd

from market_calendar import CLOSURES, is_scheduled_session
from update_all import get_target_trade_date, validate_flow_history


class CalendarTests(unittest.TestCase):
    def test_all_reviewed_weekday_closures(self):
        self.assertEqual({y: len(ds) for y, ds in CLOSURES.items()}, {2026: 18, 2027: 19})
        for year, days in CLOSURES.items():
            for md in days:
                day = date.fromisoformat(f"{year}-{md}")
                with self.subTest(day=day):
                    self.assertLess(day.weekday(), 5)
                    self.assertFalse(is_scheduled_session(day))

    def test_target_holidays_and_publication_boundaries(self):
        cases = [
            ('2026-12-25T18:59:59+08:00', '2026-12-24'),
            ('2026-12-25T19:00:00+08:00', '2026-12-24'),
            ('2026-12-26T20:00:00+08:00', '2026-12-24'),
            ('2026-12-27T20:00:00+08:00', '2026-12-24'),
            ('2026-12-28T18:59:59+08:00', '2026-12-24'),
            ('2026-12-28T19:00:00+08:00', '2026-12-28'),
            ('2026-12-28T11:00:00+00:00', '2026-12-28'),
            ('2026-10-09T20:00:00+08:00', '2026-10-08'),
            ('2026-10-26T20:00:00+08:00', '2026-10-23'),
            ('2026-02-11T19:00:00+08:00', '2026-02-11'),
            ('2026-02-12T19:00:00+08:00', '2026-02-11'),
            ('2026-02-20T19:00:00+08:00', '2026-02-11'),
            ('2026-02-23T18:59:59+08:00', '2026-02-11'),
            ('2026-02-23T19:00:00+08:00', '2026-02-23'),
            ('2027-01-01T19:00:00+08:00', '2026-12-31'),
            ('2027-01-04T18:59:59+08:00', '2026-12-31'),
            ('2027-01-04T19:00:00+08:00', '2027-01-04'),
            ('2027-02-01T19:00:00+08:00', '2027-02-01'),
            ('2027-02-03T19:00:00+08:00', '2027-02-01'),
            ('2027-02-10T19:00:00+08:00', '2027-02-01'),
            ('2027-02-11T18:59:59+08:00', '2027-02-01'),
            ('2027-02-11T19:00:00+08:00', '2027-02-11'),
            ('2027-12-24T19:00:00+08:00', '2027-12-23'),
            ('2027-12-31T19:00:00+08:00', '2027-12-30'),
        ]
        for instant, expected in cases:
            with self.subTest(instant=instant):
                self.assertEqual(get_target_trade_date(datetime.fromisoformat(instant)),
                                 date.fromisoformat(expected))

    def test_unknown_year_fails_even_before_cutoff_or_on_weekend(self):
        for instant in ['2028-01-01T01:00:00+08:00', '2028-01-02T20:00:00+08:00',
                        '2025-12-30T20:00:00+08:00']:
            with self.subTest(instant=instant), self.assertRaisesRegex(RuntimeError, 'calendar does not cover'):
                get_target_trade_date(datetime.fromisoformat(instant))

    def test_holiday_target_accepts_prior_complete_session_but_not_reopening(self):
        for market, count in [('TWSE', 1000), ('TPEX', 700)]:
            history = pd.DataFrame([{
                'date': date(2026, 12, 24), 'code': str(1000 + i), 'name': 'test',
                'foreign_net': 1, 'trust_net': 0, 'dealer_net': 0, 'market': market,
            } for i in range(count)])
            with self.subTest(market=market):
                holiday_target = get_target_trade_date(datetime.fromisoformat('2026-12-25T20:00:00+08:00'))
                validate_flow_history(history, market, holiday_target)
                reopened_target = get_target_trade_date(datetime.fromisoformat('2026-12-28T20:00:00+08:00'))
                with self.assertRaisesRegex(RuntimeError, 'latest date.*target date'):
                    validate_flow_history(history, market, reopened_target)
                with self.assertRaisesRegex(RuntimeError, 'expected >='):
                    validate_flow_history(history.iloc[:1], market, holiday_target)


if __name__ == '__main__':
    unittest.main()
