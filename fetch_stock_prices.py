# -*- coding: utf-8 -*-
"""Fetch historical stock prices from TWSE and TPEX.

獲取台灣股票歷史收盤價數據。

主要功能：
- fetch_twse_stock_price: 抓取上市股票歷史價格
- fetch_tpex_stock_price: 抓取上櫃股票歷史價格
- fetch_stock_price: 自動判斷市場並抓取價格
- calculate_price_changes: 計算多個時間窗口的漲跌幅
"""

import os
import time
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List
import requests
import pandas as pd
import numpy as np
from io import StringIO


class MonthFetchError(Exception):
    """單月價格抓取因暫時性錯誤（網路/5xx/429）而失敗，需與「該月無交易」區分 (FSP-05)。"""


def _parse_price_or_nan(s) -> float:
    """解析價格；'--'/空白/'---' 等無交易標記回傳 NaN（不是 0.0），

    避免 pct_change 產生假的 -100%/inf 污染相關性 (FSP-03)。
    """
    s = str(s).strip().replace(",", "")
    if s in ("--", "", "---", "----"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")

# Constants
TWSE_STOCK_PRICE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_STOCK_PRICE_URL = "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"

DATA_DIR = "data"
PRICE_DATA_DIR = os.path.join(DATA_DIR, "prices")


def ensure_dirs():
    """確保必要目錄存在"""
    os.makedirs(PRICE_DATA_DIR, exist_ok=True)


def fetch_twse_stock_price(stock_code: str, year: int, month: int) -> pd.DataFrame:
    """
    抓取上市股票單月歷史價格

    Args:
        stock_code: 股票代碼
        year: 年份 (西元)
        month: 月份 (1-12)

    Returns:
        DataFrame with columns: date, code, open, high, low, close, volume
    """
    date_str = f"{year}{month:02d}01"

    params = {
        "response": "json",
        "date": date_str,
        "stockNo": stock_code,
    }

    # 對暫時性錯誤重試（最多 3 次，指數退避），最終仍失敗則 raise MonthFetchError，
    # 與「該月無交易」(stat!=OK) 區分，避免靜默漏掉整個月 (FSP-05)
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(TWSE_STOCK_PRICE_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
            print(f"Error fetching TWSE price for {stock_code} {year}-{month:02d} (attempt {attempt+1}/3): {e}")
            time.sleep(1.0 * (2 ** attempt))
    else:
        raise MonthFetchError(f"TWSE {stock_code} {year}-{month:02d}: {last_err}")

    # 該月無交易（非錯誤）：合法回傳空表
    if data.get("stat") != "OK":
        return pd.DataFrame()

    # Parse data
    records = []
    for row in data.get("data", []):
        if len(row) < 7:
            continue

        # row format: [日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數]
        date_str = row[0].strip().replace("/", "-")
        # Convert ROC date to Western date
        parts = date_str.split("-")
        if len(parts) == 3:
            year_roc = int(parts[0]) + 1911
            date_str = f"{year_roc}-{parts[1]}-{parts[2]}"

        # 價格用 NaN 標記無交易（FSP-03）；成交量另外解析，合法的 0 量不應變 NaN
        volume_raw = _parse_price_or_nan(row[1])
        volume = 0.0 if np.isnan(volume_raw) else volume_raw
        open_price = _parse_price_or_nan(row[3])
        high = _parse_price_or_nan(row[4])
        low = _parse_price_or_nan(row[5])
        close = _parse_price_or_nan(row[6])

        records.append({
            "date": date_str,
            "code": stock_code,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume / 1000  # Convert to 張 (1張 = 1000股)
        })

    return pd.DataFrame(records)


def fetch_tpex_stock_price(stock_code: str, year: int, month: int) -> pd.DataFrame:
    """
    抓取上櫃股票單月歷史價格

    Args:
        stock_code: 股票代碼
        year: 年份 (西元)
        month: 月份 (1-12)

    Returns:
        DataFrame with columns: date, code, open, high, low, close, volume
    """
    # TPEX uses ROC year
    year_roc = year - 1911

    params = {
        "l": "zh-tw",
        "d": f"{year_roc}/{month:02d}",
        "stkno": stock_code,
    }

    # 暫時性錯誤重試，最終失敗 raise MonthFetchError，與「該月無交易」區分 (FSP-05)
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(TPEX_STOCK_PRICE_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
            print(f"Error fetching TPEX price for {stock_code} {year}-{month:02d} (attempt {attempt+1}/3): {e}")
            time.sleep(1.0 * (2 ** attempt))
    else:
        raise MonthFetchError(f"TPEX {stock_code} {year}-{month:02d}: {last_err}")

    # 該月無交易（非錯誤）：合法回傳空表
    if data.get("aaData") is None or len(data.get("aaData", [])) == 0:
        return pd.DataFrame()

    # Parse data
    records = []
    for row in data["aaData"]:
        if len(row) < 7:
            continue

        # row format: [日期, 成交千股, 成交千元, 開盤, 最高, 最低, 收盤, ...]
        date_str = row[0].strip().replace("/", "-")
        # Convert ROC date to Western date
        parts = date_str.split("-")
        if len(parts) == 3:
            year_western = int(parts[0]) + 1911
            date_str = f"{year_western}-{parts[1]}-{parts[2]}"

        # 價格用 NaN 標記無交易（FSP-03）；成交量合法 0 不變 NaN
        volume_raw = _parse_price_or_nan(row[1])
        volume = 0.0 if np.isnan(volume_raw) else volume_raw
        open_price = _parse_price_or_nan(row[3])
        high = _parse_price_or_nan(row[4])
        low = _parse_price_or_nan(row[5])
        close = _parse_price_or_nan(row[6])

        records.append({
            "date": date_str,
            "code": stock_code,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume  # Already in 張
        })

    return pd.DataFrame(records)


def get_stock_market(stock_code: str) -> Optional[str]:
    """
    判斷股票所屬市場

    Args:
        stock_code: 股票代碼

    Returns:
        "TWSE" or "TPEX" or None
    """
    # 從現有的 flows CSV 判斷
    for csv_file, market in [("twse_flows.csv", "TWSE"), ("tpex_flows.csv", "TPEX")]:
        csv_path = os.path.join(DATA_DIR, csv_file)
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path)
                if "code" in df.columns:
                    codes = df["code"].astype(str).unique()
                    if stock_code in codes:
                        return market
            except:
                pass

    return None


def fetch_stock_price_range(
    stock_code: str,
    start_date: date,
    end_date: date,
    market: Optional[str] = None
) -> pd.DataFrame:
    """
    抓取股票指定日期範圍的歷史價格

    Args:
        stock_code: 股票代碼
        start_date: 起始日期
        end_date: 結束日期
        market: 市場 ("TWSE" or "TPEX"), None 表示自動判斷

    Returns:
        DataFrame with columns: date, code, open, high, low, close, volume
    """
    if market is None:
        market = get_stock_market(stock_code)

    if market is None:
        print(f"Cannot determine market for {stock_code}")
        return pd.DataFrame()

    all_data = []

    # Iterate through months
    current = start_date.replace(day=1)
    end = end_date.replace(day=1)

    while current <= end:
        year = current.year
        month = current.month

        # 月份抓取失敗（暫時性錯誤）視為致命：直接中止並回傳空表，避免儲存有缺口的序列 (FSP-05)
        try:
            if market == "TWSE":
                df = fetch_twse_stock_price(stock_code, year, month)
            else:
                df = fetch_tpex_stock_price(stock_code, year, month)
        except MonthFetchError as e:
            print(f"[ERROR] Aborting price range for {stock_code}: {e}")
            return pd.DataFrame()

        if not df.empty:
            all_data.append(df)

        # Move to next month
        if month == 12:
            current = current.replace(year=year + 1, month=1)
        else:
            current = current.replace(month=month + 1)

        time.sleep(0.3)  # Rate limiting

    if not all_data:
        return pd.DataFrame()

    result = pd.concat(all_data, ignore_index=True)
    result["date"] = pd.to_datetime(result["date"])
    result = result[(result["date"] >= pd.Timestamp(start_date)) &
                    (result["date"] <= pd.Timestamp(end_date))]
    result = result.sort_values("date").reset_index(drop=True)
    result["date"] = result["date"].dt.strftime("%Y-%m-%d")

    return result


def calculate_price_changes(prices_df: pd.DataFrame, windows: List[int] = [15, 30, 45, 60]) -> pd.DataFrame:
    """
    計算收盤價的漲跌幅

    注意：pct_change(periods=window) 的 window 是「交易日列數」位移，而非曆日；
    序列若有缺口（停牌/上市日不足），change_pct_{window} 代表的是 window 個「交易列」前的
    變化，並非剛好 window 個曆日 (FSP-02)。下游相關性已改為直接在價格序列上計算未來報酬，
    此處的 change_pct_{window} 僅作參考輸出。

    Args:
        prices_df: DataFrame with columns: date, close
        windows: 要計算的時間窗口列表（交易日列數）

    Returns:
        DataFrame with additional columns: change_pct_{window} for each window
    """
    if prices_df.empty:
        return prices_df

    df = prices_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    # 先排序、去重，確保位移正確且不重複日期
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)

    # 收盤價守衛：非正值（含 0、無交易遺留）轉 NaN，避免 pct_change 產生 -100%/inf (FSP-03)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df.loc[df["close"] <= 0, "close"] = np.nan

    # Calculate price change percentage for each window（window 為交易日列數位移）
    for window in windows:
        col = f"change_pct_{window}"
        df[col] = df["close"].pct_change(periods=window) * 100
        df[col] = df[col].replace([np.inf, -np.inf], np.nan)

    # Also calculate daily change
    df["daily_change_pct"] = (df["close"].pct_change(periods=1) * 100).replace(
        [np.inf, -np.inf], np.nan
    )

    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    return df


def save_stock_prices(stock_code: str, prices_df: pd.DataFrame):
    """
    儲存股票價格數據到 CSV

    Args:
        stock_code: 股票代碼
        prices_df: 價格數據
    """
    ensure_dirs()

    csv_path = os.path.join(PRICE_DATA_DIR, f"{stock_code}.csv")
    prices_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"Saved prices for {stock_code} to {csv_path}")


def load_stock_prices(
    stock_code: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> pd.DataFrame:
    """
    載入已儲存的股票價格數據

    若指定了 start_date/end_date，會檢查快取是否「涵蓋」整個請求區間；
    未涵蓋（過舊/過短）時回傳空表，避免靜默回傳過時快取而把分析視窗永遠凍結 (FSP-01)。

    Args:
        stock_code: 股票代碼
        start_date: 請求區間起日（None 表示不檢查）
        end_date: 請求區間迄日（None 表示不檢查）

    Returns:
        DataFrame or empty DataFrame if not found / 快取未涵蓋請求區間
    """
    csv_path = os.path.join(PRICE_DATA_DIR, f"{stock_code}.csv")

    if not os.path.exists(csv_path):
        return pd.DataFrame()

    df = pd.read_csv(csv_path)

    # 涵蓋性檢查：快取需 min(date) <= start_date 且 max(date) >= end_date 才算足夠
    if (start_date is not None or end_date is not None) and "date" in df.columns and not df.empty:
        dates = pd.to_datetime(df["date"], errors="coerce")
        cmin, cmax = dates.min(), dates.max()
        if pd.isna(cmin) or pd.isna(cmax):
            return pd.DataFrame()
        if start_date is not None and cmin.date() > start_date:
            return pd.DataFrame()
        if end_date is not None and cmax.date() < end_date:
            return pd.DataFrame()

    return df


if __name__ == "__main__":
    # Test fetching prices for TSMC (2330)
    print("Testing fetch_stock_price_range for 2330...")

    # 可用環境變數 PRICE_END_DATE=YYYY-MM-DD 固定結束日，使 smoke test 具確定性 (FSP-04)
    _end_env = os.environ.get("PRICE_END_DATE")
    end_date = datetime.strptime(_end_env, "%Y-%m-%d").date() if _end_env else date.today()
    start_date = end_date - timedelta(days=90)

    prices = fetch_stock_price_range("2330", start_date, end_date)

    if not prices.empty:
        print(f"Got {len(prices)} records")
        print(prices.head(10))

        # Calculate changes
        prices_with_changes = calculate_price_changes(prices)
        print("\nWith price changes:")
        print(prices_with_changes.tail(10))

        # Save
        save_stock_prices("2330", prices_with_changes)
    else:
        print("No data fetched")
