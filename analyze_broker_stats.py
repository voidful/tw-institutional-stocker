# -*- coding: utf-8 -*-
"""Analyze broker branch trading statistics.

分析各券商分點的買賣超統計。

主要功能：
- 統計各分點的買超前10名股票
- 統計各分點的賣超前10名股票
- 生成分點績效報告（JSON格式供前端使用）
"""

import os
import json
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo
import pandas as pd

# 台灣時區（CI 在 UTC 執行，輸出時間戳記用台北時間）
TPE = ZoneInfo("Asia/Taipei")

# Constants
DATA_DIR = "data"
BROKER_DATA_DIR = os.path.join(DATA_DIR, "broker")
DOCS_DIR = os.path.join("docs", "data")


def ensure_dirs():
    """確保必要目錄存在"""
    for d in [DATA_DIR, BROKER_DATA_DIR, DOCS_DIR]:
        os.makedirs(d, exist_ok=True)


def load_broker_history(days: int = 60) -> pd.DataFrame:
    """
    載入券商歷史交易數據

    Args:
        days: 要載入的天數

    Returns:
        DataFrame with columns: full_date, date, stock_code, broker_name, broker_id,
                                net_vol, buy_vol, sell_vol, pct, rank, side
    """
    history_path = os.path.join(BROKER_DATA_DIR, "broker_history.csv")

    if not os.path.exists(history_path):
        print(f"Broker history not found at {history_path}")
        return pd.DataFrame()

    df = pd.read_csv(history_path)

    # 建立真實 ISO 交易日 trade_date：新檔已含此欄；舊檔則由無年份的 'date' + scrape 日
    # 'full_date' 反推（含 12 月→1 月跨年），確保視窗/聚合以真實交易日為準 (ABS-001/002)
    if "trade_date" not in df.columns and {"date", "full_date"}.issubset(df.columns):
        fd = pd.to_datetime(df["full_date"], errors="coerce")
        mm = df["date"].astype(str).str.extract(r"(\d{1,2})/(\d{1,2})")
        month = pd.to_numeric(mm[0], errors="coerce")
        day = pd.to_numeric(mm[1], errors="coerce")
        yr = fd.dt.year - (month > fd.dt.month).astype("Int64")
        df["trade_date"] = pd.to_datetime(
            dict(year=yr, month=month, day=day), errors="coerce"
        ).dt.strftime("%Y-%m-%d")

    # 防禦性去重：即使讀到已污染的舊檔，也讓聚合正確（同股票/分點/交易日/方向僅留一筆）(ABS-002)
    if {"stock_code", "broker_id", "trade_date", "side"}.issubset(df.columns):
        sort_col = "full_date" if "full_date" in df.columns else "trade_date"
        df = df.sort_values(sort_col).drop_duplicates(
            subset=["stock_code", "broker_id", "trade_date", "side"], keep="last"
        )

    # 視窗以「資料中的真實交易日」為錨點（非系統時鐘），取最近 N 個交易日，
    # 確保同一份資料不論哪天執行都得到相同排名 (ABS-001/004)
    if "trade_date" in df.columns:
        td = pd.to_datetime(df["trade_date"], errors="coerce")
        valid_days = sorted(td.dropna().unique())
        if valid_days:
            keep_days = set(valid_days[-days:])
            df = df[td.isin(keep_days)].copy()
        if "full_date" in df.columns:
            df["full_date"] = pd.to_datetime(df["full_date"])
        df = df.sort_values("trade_date").reset_index(drop=True)
    elif "full_date" in df.columns:
        # 後備：無 trade_date 時，以資料最新 full_date 為錨點（避免時鐘相依）
        df["full_date"] = pd.to_datetime(df["full_date"])
        cutoff = df["full_date"].max() - timedelta(days=days)
        df = df[df["full_date"] >= cutoff].copy()
        df = df.sort_values("full_date").reset_index(drop=True)

    return df


def get_active_brokers(broker_history: pd.DataFrame, min_trades: int = 20) -> pd.DataFrame:
    """
    獲取活躍的券商分點列表

    Args:
        broker_history: 券商歷史交易數據
        min_trades: 最少交易次數

    Returns:
        DataFrame with columns: broker_id, broker_name, total_trades, stocks_traded, total_net_vol
    """
    if broker_history.empty:
        return pd.DataFrame()

    # total_trades 以真實交易日去重後的筆數計（避免重抓 scrape 日造成的重複計算）(ABS-002)
    count_col = "trade_date" if "trade_date" in broker_history.columns else "full_date"
    broker_stats = broker_history.groupby(["broker_id", "broker_name"]).agg({
        "stock_code": "nunique",
        count_col: "count",
        "net_vol": "sum"
    }).reset_index()

    broker_stats.columns = ["broker_id", "broker_name", "stocks_traded",
                            "total_trades", "total_net_vol"]

    # Filter by minimum trades
    broker_stats = broker_stats[broker_stats["total_trades"] >= min_trades]
    # 加上 broker_id 次要排序鍵，使 head(N) 在 total_trades 相同時仍為確定性結果 (ABS-004)
    broker_stats = broker_stats.sort_values(
        ["total_trades", "broker_id"], ascending=[False, True]
    )

    return broker_stats


def get_broker_top_stocks(
    broker_history: pd.DataFrame,
    broker_id: str,
    top_n: int = 10,
    min_days: int = 3
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
        Each DataFrame contains: stock_code, total_net_vol, total_buy_vol,
                                total_sell_vol, trading_days, avg_net_vol
    """
    if broker_history.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Filter by broker
    broker_df = broker_history[broker_history["broker_id"] == broker_id].copy()

    if broker_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Aggregate by stock；trading_days 以真實交易日 trade_date 計（非 scrape 日）(ABS-002)
    days_col = "trade_date" if "trade_date" in broker_df.columns else "full_date"
    stock_stats = broker_df.groupby("stock_code").agg({
        "net_vol": ["sum", "mean"],
        "buy_vol": "sum",
        "sell_vol": "sum",
        days_col: "nunique"
    }).reset_index()

    stock_stats.columns = ["stock_code", "total_net_vol", "avg_net_vol",
                           "total_buy_vol", "total_sell_vol", "trading_days"]
    # 正規化 stock_code 為乾淨字串（去掉 int64 -> 2303 而非 2303.0），讓名稱查找與 JSON 輸出正確 (ABS-003)
    stock_stats["stock_code"] = stock_stats["stock_code"].apply(
        lambda x: str(int(float(x))) if str(x).replace(".", "").isdigit() else str(x).strip()
    )

    # Filter by minimum trading days
    stock_stats = stock_stats[stock_stats["trading_days"] >= min_days]

    # Separate buy and sell
    buy_stocks = stock_stats[stock_stats["total_net_vol"] > 0].copy()
    sell_stocks = stock_stats[stock_stats["total_net_vol"] < 0].copy()

    # Sort and take top N
    buy_stocks = buy_stocks.sort_values("total_net_vol", ascending=False).head(top_n)
    sell_stocks = sell_stocks.sort_values("total_net_vol", ascending=True).head(top_n)

    return buy_stocks, sell_stocks


def get_stock_name(stock_code) -> str:
    """
    從現有的 flows CSV 中取得股票名稱

    Args:
        stock_code: 股票代碼（可能為 int64/float/str）

    Returns:
        股票名稱，找不到則返回空字串
    """
    # 正規化：2303.0 / int64 / '2303' 一律轉成 '2303'；保留含字母/前導零的代碼（如 '00679B'）(ABS-003)
    try:
        code = str(int(float(stock_code)))
    except (TypeError, ValueError):
        code = str(stock_code).strip()

    for csv_file in ["twse_flows.csv", "tpex_flows.csv"]:
        csv_path = os.path.join(DATA_DIR, csv_file)
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path, dtype={"code": str})  # 保留前導零/字母
                if "code" in df.columns and "name" in df.columns:
                    match = df[df["code"].str.strip() == code]
                    if not match.empty:
                        return match.iloc[0]["name"]
            except Exception as e:
                print(f"[WARN] get_stock_name failed for {csv_file}: {e}")

    return ""


def analyze_broker_stats(
    broker_id: str,
    broker_name: str,
    broker_history: pd.DataFrame,
    top_n: int = 10
) -> Dict:
    """
    分析單個分點的統計數據

    Args:
        broker_id: 分點代碼
        broker_name: 分點名稱
        broker_history: 券商歷史交易數據
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
        "top_buy_stocks": [],
        "top_sell_stocks": []
    }

    # Process top buy stocks
    print(f"  買超前{top_n}名股票:")
    for idx, row in top_buy.iterrows():
        stock_code = row["stock_code"]
        stock_name = get_stock_name(stock_code)
        net_vol = int(row["total_net_vol"])
        buy_vol = int(row["total_buy_vol"])
        sell_vol = int(row["total_sell_vol"])
        trading_days = int(row["trading_days"])
        avg_net = round(float(row["avg_net_vol"]), 2)

        stock_info = {
            "rank": len(result["top_buy_stocks"]) + 1,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "total_net_vol": net_vol,
            "total_buy_vol": buy_vol,
            "total_sell_vol": sell_vol,
            "trading_days": trading_days,
            "avg_net_vol": avg_net
        }

        result["top_buy_stocks"].append(stock_info)
        print(f"    {stock_code} {stock_name}: {net_vol:+,} 張 "
              f"(買:{buy_vol:,} 賣:{sell_vol:,}, {trading_days}天, 日均:{avg_net:+.1f})")

    # Process top sell stocks
    print(f"  賣超前{top_n}名股票:")
    for idx, row in top_sell.iterrows():
        stock_code = row["stock_code"]
        stock_name = get_stock_name(stock_code)
        net_vol = int(row["total_net_vol"])
        buy_vol = int(row["total_buy_vol"])
        sell_vol = int(row["total_sell_vol"])
        trading_days = int(row["trading_days"])
        avg_net = round(float(row["avg_net_vol"]), 2)

        stock_info = {
            "rank": len(result["top_sell_stocks"]) + 1,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "total_net_vol": net_vol,
            "total_buy_vol": buy_vol,
            "total_sell_vol": sell_vol,
            "trading_days": trading_days,
            "avg_net_vol": avg_net
        }

        result["top_sell_stocks"].append(stock_info)
        print(f"    {stock_code} {stock_name}: {net_vol:+,} 張 "
              f"(買:{buy_vol:,} 賣:{sell_vol:,}, {trading_days}天, 日均:{avg_net:+.1f})")

    return result


def main():
    """主程式"""
    print("=" * 60)
    print("Broker Trading Statistics Analysis")
    print(f"Time: {datetime.now(TPE).isoformat()}")
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
    _range_col = "trade_date" if "trade_date" in broker_history.columns else "full_date"
    print(f"交易日範圍: {broker_history[_range_col].min()} 到 {broker_history[_range_col].max()}")

    # Get active brokers
    print("\n獲取活躍券商分點...")
    active_brokers = get_active_brokers(broker_history, min_trades=30)

    if active_brokers.empty:
        print("[ERROR] No active brokers found!")
        return

    print(f"找到 {len(active_brokers)} 個活躍分點")
    print("\n前20名活躍分點:")
    for idx, row in active_brokers.head(20).iterrows():
        print(f"  {row['broker_name']:<20} ({row['broker_id']}): "
              f"{row['total_trades']:>4} 筆交易, {row['stocks_traded']:>3} 支股票, "
              f"淨買賣超: {row['total_net_vol']:+,} 張")

    # Analyze top brokers
    top_brokers_to_analyze = min(30, len(active_brokers))
    print(f"\n開始分析前 {top_brokers_to_analyze} 個分點...")

    all_results = []

    for i, row in active_brokers.head(top_brokers_to_analyze).iterrows():
        broker_id = row["broker_id"]
        broker_name = row["broker_name"]

        print(f"\n[{len(all_results)+1}/{top_brokers_to_analyze}] ", end="")

        try:
            result = analyze_broker_stats(
                broker_id=broker_id,
                broker_name=broker_name,
                broker_history=broker_history,
                top_n=10
            )
            all_results.append(result)
        except Exception as e:
            print(f"  [ERROR] 分析失敗: {e}")
            import traceback
            traceback.print_exc()
            continue

    # Export results；日期範圍以真實交易日 trade_date 為準（資料錨定，與執行日無關）
    output_path = os.path.join(DOCS_DIR, "broker_stats.json")
    range_col = "trade_date" if "trade_date" in broker_history.columns else "full_date"
    range_series = pd.to_datetime(broker_history[range_col])
    output_data = {
        "updated": datetime.now(TPE).isoformat(),
        "analysis_days": analysis_days,
        "date_range": {
            "start": range_series.min().isoformat(),
            "end": range_series.max().isoformat()
        },
        "brokers_analyzed": len(all_results),
        "total_active_brokers": len(active_brokers),
        "results": all_results
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"分析券商數: {len(all_results)}")
    print(f"分析天數: {analysis_days}")
    print(f"結果已儲存至: {output_path}")
    print("\n分析完成！")


if __name__ == "__main__":
    main()
