# Equity-market calendar coverage

Reviewed 2026-11-19. This is an offline, explicit calendar for TWSE listed and TPEx OTC **stock trading**, not the bond/dealer system or government office calendar.

## Sources

- TWSE annual holiday table (2026 indexed table): https://accessibility.twse.com.tw/zh/trading/holiday.html
- TWSE annual table (currently 2027): https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=html
- TPEx 2026 official annex: https://www.tpex.org.tw/storage/eb_data/11411/114007545811-1.pdf
- TPEx 2027 official announcement: https://dsp.tpex.org.tw/web/announcement/announcement_detail.php?content_file=MTE1MDA4MDM4NzEuaHRtbA%3D%3D&content_number=MTE1MDA4MDM4NzE%3D

The annual pages change their default year. Verify the year heading and the stock-system section before updating. The 2026 weekday closures in TWSE and TPEx agree; 2027 dates are transcribed from TWSE's published 116-year table; TPEx's announcement is available but its annex returned 403, so 2027 has not been independently cross-checked against the TPEx annex. Do not classify first/last trading days as closures. In 2026 February 11 is the last session and February 12–13 are settlement-only; in 2027 February 1 is the last session and February 2–3 are settlement-only.

## Scope and safety

- Target-date selection covers all scheduled equity sessions in calendar years 2026 and 2027, plus the single 2025-12-31 session needed for New Year rollback. The existing source CSVs at base c569391 contain 1241 TWSE and 852 TPEx rows on that anchor date.
- The conservative publication cutoff remains 19:00 Asia/Taipei. Before then, roll back at least one calendar day before applying exchange closures/weekends.
- Unknown years fail explicitly before fetching/exporting. Extend the reviewed calendar before 2028; no silent weekday-only fallback.
- This is the announced annual schedule, not a complete record of emergency/typhoon closures. Update only from verified exchange announcements. Missing/empty responses are not proof of closure.
- Strict latest-date and minimum-row validation is unchanged: a failed fetch on a scheduled session still fails. No freshness gate is relaxed, no history is synthesized, and no live workflow is run by the tests.
- Historical backfill iteration retains its previous weekday behavior, so arbitrary historical requests are not silently dropped by this bounded calendar. It may attempt known closed dates; target-date freshness is the part fixed here.

## Validation

`python -m unittest discover -s tests -v` is offline. Tests include publication boundaries, Dec 25–28 2026, both Lunar New Year reopenings, substitute holidays, 2027 year end, unknown-year rejection, and retained stale-data failures for both markets.
