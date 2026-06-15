# -*- coding: utf-8 -*-
"""Re-fetch & repair 三大法人日買賣超歷史 (twse_flows.csv / tpex_flows.csv)。

何時需要跑：
- 修正了 dealer_net 欄位解析錯誤（舊版 find_col_any 會把 自營商買賣超股數 誤抓成
  外資自營商買賣超股數，導致 dealer_net 幾乎全 0）。舊 CSV 內的 dealer_net 是錯的，
  必須重抓覆蓋。
- TPEX 舊端點 (3itrade_hedge_result.php) 於 2025/12 失效，上櫃買賣超中斷；新版改用
  /www/zh-tw/insti/dailyTrade，需回補缺漏日。

作法：
- 對指定日期區間的每個交易日，用「修正後」的 fetch_twse_t86 / fetch_tpex_flows 重抓；
- 與既有 CSV 合併後，以 [date, code, market] 去重並「保留新值」(keep='last')，
  讓正確資料覆蓋舊的錯誤資料；重抓失敗（假日/無資料）的日子則沿用舊資料、不致遺失。

用法：
    python backfill_flows.py                 # 預設回補既有資料最早日 ~ 最新交易日
    python backfill_flows.py 2025-09-11 2026-04-22
"""
import os
import sys
import time
from datetime import date, datetime

import pandas as pd

from update_all import (
    DATA_DIR,
    FLOW_COLUMNS,
    empty_flows_df,
    fetch_tpex_flows,
    fetch_twse_t86,
    get_target_trade_date,
    iter_trading_days,
)


def replace_range(df_new: pd.DataFrame, path: str, start: date, end: date):
    """以重抓到的資料『取代』[start, end] 區間：先刪掉舊檔該區間所有列，再寫入新值。

    這樣可避免「某次重抓漏掉/捨棄某一列時，舊的錯誤列因 keep='last' 殘留」的問題，
    讓 backfill 對指定區間具權威性。區間外的歷史資料保留不動。
    """
    if df_new is None or df_new.empty:
        return
    df_new = df_new.copy()
    df_new["date"] = pd.to_datetime(df_new["date"], errors="coerce").dt.date
    df_new = df_new.dropna(subset=["date"])

    if os.path.exists(path):
        old = pd.read_csv(path)
        old["date"] = pd.to_datetime(old["date"], errors="coerce").dt.date
        old = old.dropna(subset=["date"])
        keep = old[(old["date"] < start) | (old["date"] > end)]
        combined = pd.concat([keep, df_new], ignore_index=True)
    else:
        combined = df_new

    combined = (
        combined.drop_duplicates(subset=["date", "code", "market"], keep="last")
        .sort_values(["date", "code"])
    )
    combined.to_csv(path, index=False, date_format="%Y-%m-%d")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _existing_min_date(*paths: str):
    mins = []
    for p in paths:
        if os.path.exists(p):
            try:
                d = pd.to_datetime(pd.read_csv(p, usecols=["date"])["date"], errors="coerce")
                if d.notna().any():
                    mins.append(d.min().date())
            except Exception:  # noqa: BLE001
                pass
    return min(mins) if mins else None


def main():
    twse_path = os.path.join(DATA_DIR, "twse_flows.csv")
    tpex_path = os.path.join(DATA_DIR, "tpex_flows.csv")

    if len(sys.argv) >= 3:
        start = _parse_date(sys.argv[1])
        end = _parse_date(sys.argv[2])
    else:
        start = _existing_min_date(twse_path, tpex_path) or date(2025, 9, 1)
        end = get_target_trade_date()

    days = list(iter_trading_days(start, end))
    print(f"[BACKFILL] repairing flows {start} -> {end}  ({len(days)} trading days)")

    twse_list, tpex_list = [], []
    for i, d in enumerate(days, 1):
        try:
            tw = fetch_twse_t86(d)
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] TWSE {d}: {e}")
            tw = empty_flows_df()
        try:
            tp = fetch_tpex_flows(d)
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] TPEX {d}: {e}")
            tp = empty_flows_df()
        if not tw.empty:
            twse_list.append(tw)
        if not tp.empty:
            tpex_list.append(tp)
        print(f"  [{i}/{len(days)}] {d}  TWSE={len(tw):4d}  TPEX={len(tp):4d}")
        time.sleep(0.4)  # 禮貌性延遲，避免被官方擋

    if twse_list:
        twse_new = pd.concat(twse_list, ignore_index=True)
        replace_range(twse_new, twse_path, start, end)
        print(f"[BACKFILL] TWSE flows range-replaced: +{len(twse_new)} rows fetched")
    else:
        print("[BACKFILL] no TWSE rows fetched")

    if tpex_list:
        tpex_new = pd.concat(tpex_list, ignore_index=True)
        replace_range(tpex_new, tpex_path, start, end)
        print(f"[BACKFILL] TPEX flows range-replaced: +{len(tpex_new)} rows fetched")
    else:
        print("[BACKFILL] no TPEX rows fetched")

    print("[BACKFILL] done.")


if __name__ == "__main__":
    main()
