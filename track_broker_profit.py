# -*- coding: utf-8 -*-
"""Track broker branch trading performance and next-day profit.

追蹤特定券商分點的交易績效，計算其持倉隔日的獲利情況。

主要功能：
- calculate_next_day_profit: 計算分點的隔日獲利
- aggregate_broker_performance: 彙總分點績效
- track_target_brokers: 追蹤特定目標分點
"""

import os
import json
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

# Target broker branches to track
TARGET_BROKERS = [
    "凱基-台北",
    "凱基-虎尾",
    "美林",
    "摩根士丹利",  # Morgan Stanley
    "摩根大通",    # JP Morgan
    "高盛",        # Goldman Sachs
    "瑞銀",        # UBS
    "元大-台北",
    "富邦-台北",
]

# Data directories
DATA_DIR = "data"
BROKER_DATA_DIR = os.path.join(DATA_DIR, "broker")
DOCS_DIR = os.path.join("docs", "data")


def ensure_dirs():
    """Ensure required directories exist."""
    os.makedirs(BROKER_DATA_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)


def load_stock_prices(stock_code: str) -> pd.DataFrame:
    """
    Load stock price data for calculating next-day returns.
    
    This function tries to load from existing timeseries data.
    Falls back to fetching from API if not available.
    
    Args:
        stock_code: Stock code (e.g., "2330")
    
    Returns:
        DataFrame with columns: date, close, change_pct
    """
    timeseries_path = os.path.join(DOCS_DIR, "timeseries", f"{stock_code}.json")
    
    if os.path.exists(timeseries_path):
        with open(timeseries_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Convert to DataFrame
        records = []
        for item in data.get("data", []):
            records.append({
                "date": item.get("date"),
                "close": item.get("close", 0),
                "change_pct": item.get("change_pct", 0),
            })
        return pd.DataFrame(records)
    
    return pd.DataFrame()


def calculate_next_day_profit(
    broker_trades: pd.DataFrame,
    prices: Optional[pd.DataFrame] = None
) -> pd.DataFrame:
    """
    Calculate next-day profit/loss for broker trades.
    
    Logic:
    1. Get broker's net buy/sell on date D
    2. Get stock's price change on date D+1
    3. Calculate: direction_match = (net_vol > 0 and next_day_change > 0) or 
                                    (net_vol < 0 and next_day_change < 0)
    
    Args:
        broker_trades: DataFrame from fetch_broker_trading()
        prices: DataFrame with stock prices (optional, will load if not provided)
    
    Returns:
        DataFrame with columns:
            - date: 交易日
            - stock_code: 股票代碼
            - broker_name: 分點名稱
            - net_vol: 買賣超張數
            - next_day_change: 隔日漲跌幅(%)
            - direction_match: 方向是否正確
            - estimated_profit: 估計獲利 (net_vol * 1000 * close * next_day_change / 100)
    """
    if broker_trades.empty:
        return pd.DataFrame()
    
    results = []
    
    # Group by stock code
    for stock_code, stock_df in broker_trades.groupby("stock_code"):
        # Load prices if not provided
        if prices is None or prices.empty:
            stock_prices = load_stock_prices(stock_code)
        else:
            stock_prices = prices[prices.get("stock_code", "") == stock_code]
        
        if stock_prices.empty:
            continue
        
        # Create price lookup by date
        price_lookup = {}
        prev_date = None
        for _, row in stock_prices.iterrows():
            d = row["date"]
            price_lookup[d] = {
                "close": row.get("close", 0),
                "change_pct": row.get("change_pct", 0),
                "prev_date": prev_date
            }
            prev_date = d
        
        # Process each broker trade
        for _, trade in stock_df.iterrows():
            trade_date = trade["date"]
            broker_name = trade["broker_name"]
            net_vol = trade["net_vol"]
            
            # Find next trading day's change
            # Since our date format might be "12/12", we need to handle this carefully
            next_day_change = 0.0
            close_price = 0.0
            
            # Try to find the next day's data
            dates_list = sorted(price_lookup.keys())
            for i, d in enumerate(dates_list):
                if trade_date in d or d in trade_date:  # Fuzzy match
                    if i + 1 < len(dates_list):
                        next_d = dates_list[i + 1]
                        next_day_change = price_lookup[next_d].get("change_pct", 0)
                        close_price = price_lookup[d].get("close", 0)
                    break
            
            # Calculate direction match
            direction_match = False
            if net_vol > 0 and next_day_change > 0:
                direction_match = True
            elif net_vol < 0 and next_day_change < 0:
                direction_match = True
            
            # Estimate profit (張 * 1000股 * 價格 * 漲跌幅%)
            estimated_profit = net_vol * 1000 * close_price * next_day_change / 100.0
            
            results.append({
                "date": trade_date,
                "stock_code": stock_code,
                "broker_name": broker_name,
                "net_vol": net_vol,
                "close_price": close_price,
                "next_day_change": next_day_change,
                "direction_match": direction_match,
                "estimated_profit": estimated_profit
            })
    
    return pd.DataFrame(results)


def aggregate_broker_performance(profit_df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate broker performance statistics.
    
    Args:
        profit_df: DataFrame from calculate_next_day_profit()
    
    Returns:
        DataFrame with columns:
            - broker_name: 分點名稱
            - total_trades: 總交易次數
            - win_count: 方向正確次數
            - win_rate: 勝率 (%)
            - total_profit: 總估計獲利
            - avg_profit: 平均獲利
            - stocks_traded: 交易股票數量
    """
    if profit_df.empty:
        return pd.DataFrame()
    
    results = []
    
    for broker_name, broker_df in profit_df.groupby("broker_name"):
        total_trades = len(broker_df)
        win_count = broker_df["direction_match"].sum()
        win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0
        total_profit = broker_df["estimated_profit"].sum()
        avg_profit = broker_df["estimated_profit"].mean()
        stocks_traded = broker_df["stock_code"].nunique()
        
        results.append({
            "broker_name": broker_name,
            "total_trades": total_trades,
            "win_count": int(win_count),
            "win_rate": round(win_rate, 2),
            "total_profit": round(total_profit, 2),
            "avg_profit": round(avg_profit, 2),
            "stocks_traded": stocks_traded
        })
    
    return pd.DataFrame(results).sort_values("win_rate", ascending=False)


def filter_target_brokers(broker_df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter broker trades to only include target brokers.
    
    Args:
        broker_df: DataFrame from fetch_broker_trading()
    
    Returns:
        Filtered DataFrame containing only target broker trades
    """
    if broker_df.empty:
        return broker_df
    
    # Create a mask for target brokers (partial match)
    mask = broker_df["broker_name"].apply(
        lambda x: any(target in x or x in target for target in TARGET_BROKERS)
    )
    
    return broker_df[mask].copy()


def track_target_brokers(
    stock_codes: list[str],
    save_results: bool = True
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Track target broker performance across multiple stocks.
    
    Args:
        stock_codes: List of stock codes to track
        save_results: Whether to save results to files
    
    Returns:
        Tuple of (all_trades, performance_summary) DataFrames
    """
    from fetch_broker_data import fetch_multiple_stocks, close_browser
    
    ensure_dirs()
    
    # Fetch broker trading data
    print(f"Fetching broker data for {len(stock_codes)} stocks...")
    all_trades = fetch_multiple_stocks(stock_codes, delay=1.5)
    
    if all_trades.empty:
        print("No broker trading data fetched")
        return pd.DataFrame(), pd.DataFrame()
    
    print(f"Got {len(all_trades)} total trades")
    
    # Filter to target brokers
    target_trades = filter_target_brokers(all_trades)
    print(f"Found {len(target_trades)} trades from target brokers")
    
    # Calculate profit
    profit_df = calculate_next_day_profit(target_trades)
    
    # Aggregate performance
    performance = aggregate_broker_performance(profit_df)
    
    # Save results
    if save_results and not all_trades.empty:
        today = date.today().isoformat()
        
        # Save raw trades
        trades_path = os.path.join(BROKER_DATA_DIR, f"broker_trades_{today}.csv")
        all_trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
        print(f"Saved raw trades to {trades_path}")
        
        # Save target broker trades
        target_path = os.path.join(BROKER_DATA_DIR, f"target_broker_trades_{today}.csv")
        target_trades.to_csv(target_path, index=False, encoding="utf-8-sig")
        
        # Save performance summary
        perf_path = os.path.join(BROKER_DATA_DIR, "broker_performance.json")
        if not performance.empty:
            performance.to_json(perf_path, orient="records", force_ascii=False, indent=2)
            print(f"Saved performance to {perf_path}")
    
    # Cleanup browser
    close_browser()
    
    return target_trades, performance


def export_broker_ranking(all_trades: pd.DataFrame, output_path: Optional[str] = None):
    """
    Export broker ranking for frontend display.
    
    Args:
        all_trades: DataFrame from fetch_broker_trading()
        output_path: Path to save JSON (default: docs/data/broker_ranking.json)
    """
    if all_trades.empty:
        return
    
    if output_path is None:
        output_path = os.path.join(DOCS_DIR, "broker_ranking.json")
    
    # Group by broker and aggregate
    broker_stats = all_trades.groupby("broker_name").agg({
        "net_vol": "sum",
        "buy_vol": "sum",
        "sell_vol": "sum",
        "stock_code": "nunique",
    }).reset_index()
    
    broker_stats.columns = ["broker_name", "total_net_vol", "total_buy_vol", 
                            "total_sell_vol", "stocks_count"]
    
    # Sort by absolute net volume
    broker_stats["abs_net_vol"] = broker_stats["total_net_vol"].abs()
    broker_stats = broker_stats.sort_values("abs_net_vol", ascending=False)
    
    # Save to JSON
    result = {
        "updated": datetime.now().isoformat(),
        "data": broker_stats.drop(columns=["abs_net_vol"]).to_dict(orient="records")
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved broker ranking to {output_path}")


if __name__ == "__main__":
    # Test with some hot stocks
    HOT_STOCKS = ["2330", "2454", "2317"]
    
    print("="*50)
    print("Tracking target broker performance...")
    print("="*50)
    
    trades, performance = track_target_brokers(HOT_STOCKS)
    
    if not trades.empty:
        print("\n--- Target Broker Trades ---")
        print(trades.to_string())
    
    if not performance.empty:
        print("\n--- Performance Summary ---")
        print(performance.to_string())
