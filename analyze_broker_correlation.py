# -*- coding: utf-8 -*-
"""Analyze broker branch correlation with stock prices.

分析各券商分點與股票價格的相關性。

主要功能：
- 統計各分點的買超/賣超前10名股票
- 計算分點交易量與股票收盤價漲跌幅的相關性（15/30/45/60天）
- 生成分點績效報告
"""

import os
import json
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np

from fetch_stock_prices import (
    fetch_stock_price_range,
    calculate_price_changes,
    load_stock_prices,
    save_stock_prices,
    get_stock_market
)

# Constants
DATA_DIR = "data"
BROKER_DATA_DIR = os.path.join(DATA_DIR, "broker")
PRICE_DATA_DIR = os.path.join(DATA_DIR, "prices")
DOCS_DIR = os.path.join("docs", "data")
CORRELATION_WINDOWS = [15, 30, 45, 60]

# 最少交易天數要求（避免樣本數太少導致相關性不準確）
MIN_TRADING_DAYS = 10


def ensure_dirs():
    """確保必要目錄存在"""
    for d in [DATA_DIR, BROKER_DATA_DIR, PRICE_DATA_DIR, DOCS_DIR]:
        os.makedirs(d, exist_ok=True)


def load_broker_history(days: int = 60) -> pd.DataFrame:
    """
    載入券商歷史交易數據

    Args:
        days: 要載入的天數

    Returns:
        DataFrame with columns: full_date, stock_code, broker_name, broker_id, net_vol, buy_vol, sell_vol
    """
    history_path = os.path.join(BROKER_DATA_DIR, "broker_history.csv")

    if not os.path.exists(history_path):
        print(f"Broker history not found at {history_path}")
        return pd.DataFrame()

    df = pd.read_csv(history_path)

    # Filter to recent days
    if "full_date" in df.columns:
        df["full_date"] = pd.to_datetime(df["full_date"])
        cutoff = datetime.now() - timedelta(days=days)
        df = df[df["full_date"] >= cutoff].copy()
        df = df.sort_values("full_date").reset_index(drop=True)

    return df


def get_broker_top_stocks(
    broker_history: pd.DataFrame,
    broker_id: str,
    top_n: int = 10,
    min_days: int = 5
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    獲取指定分點的買超/賣超前N名股票

    Args:
        broker_history: 券商歷史交易數據
        broker_id: 分點代碼
        top_n: 取前幾名
        min_days: 最少交易天數

    Returns:
        Tuple of (top_buy_df, top_sell_df)
        Each DataFrame contains: stock_code, total_net_vol, trading_days
    """
    if broker_history.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Filter by broker
    broker_df = broker_history[broker_history["broker_id"] == broker_id].copy()

    if broker_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Aggregate by stock
    stock_stats = broker_df.groupby("stock_code").agg({
        "net_vol": "sum",
        "buy_vol": "sum",
        "sell_vol": "sum",
        "full_date": "nunique"
    }).reset_index()

    stock_stats.columns = ["stock_code", "total_net_vol", "total_buy_vol",
                           "total_sell_vol", "trading_days"]

    # Filter by minimum trading days
    stock_stats = stock_stats[stock_stats["trading_days"] >= min_days]

    # Separate buy and sell
    buy_stocks = stock_stats[stock_stats["total_net_vol"] > 0].copy()
    sell_stocks = stock_stats[stock_stats["total_net_vol"] < 0].copy()

    # Sort and take top N
    buy_stocks = buy_stocks.sort_values("total_net_vol", ascending=False).head(top_n)
    sell_stocks = sell_stocks.sort_values("total_net_vol", ascending=True).head(top_n)

    # Add absolute net_vol for sell stocks
    sell_stocks["abs_net_vol"] = sell_stocks["total_net_vol"].abs()

    return buy_stocks, sell_stocks


def calculate_broker_stock_correlation(
    broker_history: pd.DataFrame,
    broker_id: str,
    stock_code: str,
    stock_prices: pd.DataFrame,
    window: int = 30
) -> Optional[float]:
    """
    計算分點對特定股票的交易量與股價漲跌幅的相關性

    Args:
        broker_history: 券商歷史交易數據
        broker_id: 分點代碼
        stock_code: 股票代碼
        stock_prices: 股票價格數據（需包含 date, close, change_pct_X 欄位）
        window: 計算相關性的時間窗口（天數）

    Returns:
        相關係數，範圍 [-1, 1]，None 表示無法計算
    """
    # Filter broker trades for this stock
    broker_stock = broker_history[
        (broker_history["broker_id"] == broker_id) &
        (broker_history["stock_code"] == stock_code)
    ].copy()

    if broker_stock.empty or stock_prices.empty:
        return None

    # Merge broker trades with stock prices
    broker_stock["date"] = pd.to_datetime(broker_stock["full_date"]).dt.strftime("%Y-%m-%d")
    stock_prices = stock_prices.copy()

    if "date" not in stock_prices.columns:
        return None

    # Ensure date format consistency
    stock_prices["date"] = pd.to_datetime(stock_prices["date"]).dt.strftime("%Y-%m-%d")

    # Merge
    merged = broker_stock.merge(stock_prices, on="date", how="inner")

    if len(merged) < MIN_TRADING_DAYS:
        return None

    # Get price change column for the window
    price_change_col = f"change_pct_{window}"
    if price_change_col not in merged.columns:
        # Use daily change as fallback
        if "daily_change_pct" in merged.columns:
            price_change_col = "daily_change_pct"
        else:
            return None

    # Calculate correlation between net_vol and price change
    # 注意：這裡計算的是當天交易量與未來N天價格變化的相關性
    # 因此需要將 net_vol 與之後的價格變化對齊
    merged = merged.sort_values("date").reset_index(drop=True)

    # For correlation, we want to see if today's broker activity
    # correlates with future price movement
    net_vols = merged["net_vol"].values[:-window] if len(merged) > window else merged["net_vol"].values
    price_changes = merged[price_change_col].values[window:] if len(merged) > window else merged[price_change_col].values

    if len(net_vols) < MIN_TRADING_DAYS or len(price_changes) < MIN_TRADING_DAYS:
        return None

    # Ensure equal length
    min_len = min(len(net_vols), len(price_changes))
    net_vols = net_vols[:min_len]
    price_changes = price_changes[:min_len]

    # Remove NaN values
    valid_mask = ~(np.isnan(net_vols) | np.isnan(price_changes))
    net_vols = net_vols[valid_mask]
    price_changes = price_changes[valid_mask]

    if len(net_vols) < MIN_TRADING_DAYS:
        return None

    # Calculate Pearson correlation
    try:
        correlation = np.corrcoef(net_vols, price_changes)[0, 1]
        if np.isnan(correlation):
            return None
        return float(correlation)
    except:
        return None


def analyze_broker_correlations(
    broker_id: str,
    broker_name: str,
    broker_history: pd.DataFrame,
    days: int = 60,
    top_n: int = 10
) -> Dict:
    """
    分析單個分點的完整相關性報告

    Args:
        broker_id: 分點代碼
        broker_name: 分點名稱
        broker_history: 券商歷史交易數據
        days: 分析天數
        top_n: 買超/賣超前N名

    Returns:
        分析結果字典
    """
    print(f"\n分析分點: {broker_name} ({broker_id})")

    # Get top buy/sell stocks
    top_buy, top_sell = get_broker_top_stocks(broker_history, broker_id, top_n=top_n)

    result = {
        "broker_id": broker_id,
        "broker_name": broker_name,
        "analysis_days": days,
        "top_buy_stocks": [],
        "top_sell_stocks": [],
        "correlations": []
    }

    # Process top buy stocks
    print(f"  買超前{top_n}名股票:")
    for _, row in top_buy.iterrows():
        stock_code = row["stock_code"]
        net_vol = int(row["total_net_vol"])
        trading_days = int(row["trading_days"])

        stock_info = {
            "stock_code": stock_code,
            "total_net_vol": net_vol,
            "trading_days": trading_days,
            "side": "buy"
        }

        result["top_buy_stocks"].append(stock_info)
        print(f"    {stock_code}: {net_vol:+,} 張 ({trading_days} 天)")

    # Process top sell stocks
    print(f"  賣超前{top_n}名股票:")
    for _, row in top_sell.iterrows():
        stock_code = row["stock_code"]
        net_vol = int(row["total_net_vol"])
        abs_vol = int(row["abs_net_vol"])
        trading_days = int(row["trading_days"])

        stock_info = {
            "stock_code": stock_code,
            "total_net_vol": net_vol,
            "abs_net_vol": abs_vol,
            "trading_days": trading_days,
            "side": "sell"
        }

        result["top_sell_stocks"].append(stock_info)
        print(f"    {stock_code}: {net_vol:+,} 張 ({trading_days} 天)")

    # Calculate correlations for all traded stocks
    all_stocks = set(top_buy["stock_code"].tolist() + top_sell["stock_code"].tolist())

    print(f"  計算相關性係數...")
    for stock_code in all_stocks:
        # Load or fetch stock prices
        stock_prices = load_stock_prices(stock_code)

        if stock_prices.empty:
            # Fetch fresh data
            end_date = date.today()
            start_date = end_date - timedelta(days=days + 70)  # Extra days for calculation
            stock_prices = fetch_stock_price_range(stock_code, start_date, end_date)

            if not stock_prices.empty:
                stock_prices = calculate_price_changes(stock_prices, windows=CORRELATION_WINDOWS)
                save_stock_prices(stock_code, stock_prices)

        if stock_prices.empty:
            continue

        # Calculate correlation for each window
        correlations = {}
        for window in CORRELATION_WINDOWS:
            corr = calculate_broker_stock_correlation(
                broker_history, broker_id, stock_code, stock_prices, window=window
            )
            if corr is not None:
                correlations[f"corr_{window}d"] = round(corr, 4)

        if correlations:
            corr_info = {
                "stock_code": stock_code,
                **correlations
            }
            result["correlations"].append(corr_info)

            # Print correlation
            corr_str = ", ".join([f"{k}: {v:+.3f}" for k, v in correlations.items()])
            print(f"    {stock_code}: {corr_str}")

    # Sort correlations by absolute value of longest window correlation
    if result["correlations"]:
        longest_window = max(CORRELATION_WINDOWS)
        corr_key = f"corr_{longest_window}d"
        result["correlations"].sort(
            key=lambda x: abs(x.get(corr_key, 0)), reverse=True
        )

    return result


def get_active_brokers(broker_history: pd.DataFrame, min_trades: int = 20) -> pd.DataFrame:
    """
    獲取活躍的券商分點列表

    Args:
        broker_history: 券商歷史交易數據
        min_trades: 最少交易次數

    Returns:
        DataFrame with columns: broker_id, broker_name, total_trades, stocks_traded
    """
    if broker_history.empty:
        return pd.DataFrame()

    broker_stats = broker_history.groupby(["broker_id", "broker_name"]).agg({
        "stock_code": "nunique",
        "full_date": "count"
    }).reset_index()

    broker_stats.columns = ["broker_id", "broker_name", "stocks_traded", "total_trades"]

    # Filter by minimum trades
    broker_stats = broker_stats[broker_stats["total_trades"] >= min_trades]
    broker_stats = broker_stats.sort_values("total_trades", ascending=False)

    return broker_stats


def main():
    """主程式"""
    print("=" * 60)
    print("Broker-Stock Correlation Analysis")
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 60)

    ensure_dirs()

    # Load broker history
    analysis_days = 60
    print(f"\n載入券商歷史交易數據（最近 {analysis_days} 天）...")
    broker_history = load_broker_history(days=analysis_days)

    if broker_history.empty:
        print("[ERROR] No broker history data found!")
        print("Please run update_broker.py first to collect broker data.")
        return

    print(f"載入 {len(broker_history)} 筆交易記錄")

    # Get active brokers
    print("\n獲取活躍券商分點...")
    active_brokers = get_active_brokers(broker_history, min_trades=30)

    if active_brokers.empty:
        print("[ERROR] No active brokers found!")
        return

    print(f"找到 {len(active_brokers)} 個活躍分點")
    print("\n前10名活躍分點:")
    for _, row in active_brokers.head(10).iterrows():
        print(f"  {row['broker_name']} ({row['broker_id']}): "
              f"{row['total_trades']} 筆交易, {row['stocks_traded']} 支股票")

    # Analyze top brokers
    top_brokers_to_analyze = min(20, len(active_brokers))
    print(f"\n開始分析前 {top_brokers_to_analyze} 個分點...")

    all_results = []

    for i, (_, broker_row) in enumerate(active_brokers.head(top_brokers_to_analyze).iterrows(), 1):
        broker_id = broker_row["broker_id"]
        broker_name = broker_row["broker_name"]

        print(f"\n[{i}/{top_brokers_to_analyze}] ", end="")

        try:
            result = analyze_broker_correlations(
                broker_id=broker_id,
                broker_name=broker_name,
                broker_history=broker_history,
                days=analysis_days,
                top_n=10
            )
            all_results.append(result)
        except Exception as e:
            print(f"  [ERROR] 分析失敗: {e}")
            continue

    # Export results
    output_path = os.path.join(DOCS_DIR, "broker_correlations.json")
    output_data = {
        "updated": datetime.now().isoformat(),
        "analysis_days": analysis_days,
        "correlation_windows": CORRELATION_WINDOWS,
        "brokers_analyzed": len(all_results),
        "results": all_results
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"分析券商數: {len(all_results)}")
    print(f"相關性時間窗口: {CORRELATION_WINDOWS}")
    print(f"結果已儲存至: {output_path}")
    print("\n分析完成！")


if __name__ == "__main__":
    main()
