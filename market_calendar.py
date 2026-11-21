"""Reviewed annual TWSE/TPEx equity closures; never infer holidays from fetches.

Coverage: 2026 and 2027 scheduled closures, reviewed 2026-11-19.
Sources and limitations: docs/market-calendar.md.
"""
from datetime import date

# Weekday closures only: weekends are handled separately. Settlement-only days
# are closed for equity trading. Annual reopening/last-trading rows are NOT holidays.
CLOSURES = {
    2026: frozenset("""
        01-01 02-12 02-13 02-16 02-17 02-18 02-19 02-20 02-27
        04-03 04-06 05-01 06-19 09-25 09-28 10-09 10-26 12-25
    """.split()),
    2027: frozenset("""
        01-01 02-02 02-03 02-04 02-05 02-08 02-09 02-10 03-01
        04-05 04-06 04-30 06-09 09-15 09-28 10-11 10-25 12-24 12-31
    """.split()),
}
# Supports the 2026 New Year rollback, not arbitrary 2025 backfills. Both
# repository histories contain this session (TWSE 1241 rows, TPEx 852 rows).
PREVIOUS_YEAR_SESSION = date(2025, 12, 31)


def is_scheduled_session(day: date) -> bool:
    """Return calendar status, failing closed outside reviewed coverage.

    This is NOT evidence that trading really occurred on a scheduled session.
    Typhoon/emergency closures require a separately verified calendar update;
    missing market data remains an error under the pipeline's freshness checks.
    """
    if day == PREVIOUS_YEAR_SESSION:
        return True
    if day.year not in CLOSURES:
        raise RuntimeError(
            f"Market calendar does not cover {day}; review official TWSE/TPEx "
            "equity calendars before extending coverage"
        )
    return day.weekday() < 5 and day.strftime("%m-%d") not in CLOSURES[day.year]
