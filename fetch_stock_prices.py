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
from io import StringIO

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

    try:
        resp = requests.get(TWSE_STOCK_PRICE_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

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

            # Parse prices (handle '--' for no trading)
            def parse_price(s):
                s = str(s).strip().replace(",", "")
                if s in ("--", "", "---"):
                    return 0.0
                try:
                    return float(s)
                except:
                    return 0.0

            volume = parse_price(row[1])
            open_price = parse_price(row[3])
            high = parse_price(row[4])
            low = parse_price(row[5])
            close = parse_price(row[6])

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

    except Exception as e:
        print(f"Error fetching TWSE price for {stock_code}: {e}")
        return pd.DataFrame()


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

    try:
        resp = requests.get(TPEX_STOCK_PRICE_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

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

            # Parse prices
            def parse_price(s):
                s = str(s).strip().replace(",", "")
                if s in ("--", "", "---", "----"):
                    return 0.0
                try:
                    return float(s)
                except:
                    return 0.0

            volume = parse_price(row[1])
            open_price = parse_price(row[3])
            high = parse_price(row[4])
            low = parse_price(row[5])
            close = parse_price(row[6])

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

    except Exception as e:
        print(f"Error fetching TPEX price for {stock_code}: {e}")
        return pd.DataFrame()


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

        if market == "TWSE":
            df = fetch_twse_stock_price(stock_code, year, month)
        else:
            df = fetch_tpex_stock_price(stock_code, year, month)

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

    Args:
        prices_df: DataFrame with columns: date, close
        windows: 要計算的時間窗口列表（天數）

    Returns:
        DataFrame with additional columns: change_pct_{window} for each window
    """
    if prices_df.empty:
        return prices_df

    df = prices_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # Calculate price change percentage for each window
    for window in windows:
        df[f"change_pct_{window}"] = df["close"].pct_change(periods=window) * 100

    # Also calculate daily change
    df["daily_change_pct"] = df["close"].pct_change(periods=1) * 100

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


def load_stock_prices(stock_code: str) -> pd.DataFrame:
    """
    載入已儲存的股票價格數據

    Args:
        stock_code: 股票代碼

    Returns:
        DataFrame or empty DataFrame if not found
    """
    csv_path = os.path.join(PRICE_DATA_DIR, f"{stock_code}.csv")

    if os.path.exists(csv_path):
        return pd.read_csv(csv_path)

    return pd.DataFrame()


if __name__ == "__main__":
    # Test fetching prices for TSMC (2330)
    print("Testing fetch_stock_price_range for 2330...")

    end_date = date.today()
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
