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
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

# 台灣時區（CI 在 UTC 執行，輸出時間戳記用台北時間）
TPE = ZoneInfo("Asia/Taipei")

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


def _norm_code(stock_code) -> str:
    """正規化股票代碼為乾淨字串（2303.0/int64 -> '2303'；保留含字母/前導零的代碼），

    確保寫入 JSON 時可序列化（避免 numpy int64 not JSON serializable）。
    """
    try:
        return str(int(float(stock_code)))
    except (TypeError, ValueError):
        return str(stock_code).strip()


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

    # 建立真實 ISO 交易日 trade_date：新檔已含；舊檔由無年份的 'date' + scrape 日 'full_date'
    # 反推（含 12 月→1 月跨年），確保視窗與相關性對齊以真實交易日為準 (ABC-01/02)
    if "trade_date" not in df.columns and {"date", "full_date"}.issubset(df.columns):
        fd = pd.to_datetime(df["full_date"], errors="coerce")
        mm = df["date"].astype(str).str.extract(r"(\d{1,2})/(\d{1,2})")
        month = pd.to_numeric(mm[0], errors="coerce")
        day = pd.to_numeric(mm[1], errors="coerce")
        yr = fd.dt.year - (month > fd.dt.month).astype("Int64")
        df["trade_date"] = pd.to_datetime(
            dict(year=yr, month=month, day=day), errors="coerce"
        ).dt.strftime("%Y-%m-%d")

    # 防禦性去重：同股票/分點/交易日/方向僅留一筆，避免重抓 scrape 日造成重複計算 (ABC-03)
    if {"stock_code", "broker_id", "trade_date", "side"}.issubset(df.columns):
        sort_col = "full_date" if "full_date" in df.columns else "trade_date"
        df = df.sort_values(sort_col).drop_duplicates(
            subset=["stock_code", "broker_id", "trade_date", "side"], keep="last"
        )

    # 視窗以資料中的真實交易日為錨點（非系統時鐘），取最近 N 個交易日，
    # 確保同一份資料不論哪天執行都得到相同結果 (ABC-01)
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

    # Aggregate by stock；trading_days 以真實交易日 trade_date 計（非 scrape 日）(ABC-03)
    days_col = "trade_date" if "trade_date" in broker_df.columns else "full_date"
    stock_stats = broker_df.groupby("stock_code").agg({
        "net_vol": "sum",
        "buy_vol": "sum",
        "sell_vol": "sum",
        days_col: "nunique"
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
    計算分點當天買賣超 net_vol(t) 與「未來 window 個交易日報酬」的相關性。

    對齊以真實交易日為準；未來報酬定義為 close(t+window)/close(t) - 1，
    在連續價格序列上 shift（跨的是真實交易列，而非曆日）。

    Args:
        broker_history: 券商歷史交易數據
        broker_id: 分點代碼
        stock_code: 股票代碼
        stock_prices: 股票價格數據（需包含 date, close 欄位）
        window: 未來報酬的交易日視窗（價格序列上的列數）

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

    # 以「真實交易日」作為對齊鍵（非 scrape 日 full_date），讓 net_vol 對到該股當天的價格 (ABC-02)
    if "trade_date" in broker_stock.columns:
        broker_stock["date"] = pd.to_datetime(
            broker_stock["trade_date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")
    else:
        broker_stock["date"] = pd.to_datetime(
            broker_stock["full_date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")

    # 同一交易日僅留一筆，避免重複 (ABC-03)
    broker_stock = broker_stock.drop_duplicates(subset=["date"], keep="last")

    stock_prices = stock_prices.copy()
    if "date" not in stock_prices.columns or "close" not in stock_prices.columns:
        return None

    # 在「連續價格序列」上計算未來 window 個交易日的報酬（以列為交易日；先去重排序避免缺口錯位）(ABC-04)
    stock_prices["date"] = pd.to_datetime(stock_prices["date"], errors="coerce")
    stock_prices = (
        stock_prices.dropna(subset=["date"])
        .sort_values("date")
        .drop_duplicates("date")
        .reset_index(drop=True)
    )
    # 未來報酬：close(t+window)/close(t) - 1（在價格序列上 shift，跨的是真實交易列而非曆日）
    stock_prices["fwd_return"] = (
        stock_prices["close"].shift(-window) / stock_prices["close"] - 1.0
    )
    stock_prices["date"] = stock_prices["date"].dt.strftime("%Y-%m-%d")

    # Merge：把當天 net_vol 接到對應交易日的未來報酬上
    merged = broker_stock.merge(
        stock_prices[["date", "fwd_return"]], on="date", how="inner"
    )

    # 單一定義：不論序列長短都用同一套（移除原本 rows<=window 時悄悄改用 lookback 的分支）(ABC-04)
    pair = merged[["net_vol", "fwd_return"]].copy()
    pair["net_vol"] = pd.to_numeric(pair["net_vol"], errors="coerce")
    # 同時排除 NaN 與 ±inf
    pair = pair.replace([np.inf, -np.inf], np.nan).dropna()

    if len(pair) < MIN_TRADING_DAYS:
        return None

    # Calculate Pearson correlation
    try:
        correlation = np.corrcoef(pair["net_vol"].values, pair["fwd_return"].values)[0, 1]
        if np.isnan(correlation):
            return None
        return float(correlation)
    except Exception:
        return None


def analyze_broker_correlations(
    broker_id: str,
    broker_name: str,
    broker_history: pd.DataFrame,
    days: int = 60,
    top_n: int = 10,
    as_of_date: Optional[date] = None,
    allow_fetch: bool = True
) -> Dict:
    """
    分析單個分點的完整相關性報告

    Args:
        broker_id: 分點代碼
        broker_name: 分點名稱
        broker_history: 券商歷史交易數據
        days: 分析天數
        top_n: 買超/賣超前N名
        as_of_date: 價格抓取的結束日（None 時由資料的最新交易日推算，避免 date.today() 造成不確定性）(ABC-07/FSP-04)
        allow_fetch: 是否允許在缺少本地快取時上網抓價（離線/CI 純本地測試可設 False，缺資料則優雅跳過）

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
        stock_code = _norm_code(row["stock_code"])
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
        stock_code = _norm_code(row["stock_code"])
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
    # 用 dict.fromkeys 保序去重（先買超再賣超，皆已依 net_vol 排序），取代 set() 避免迭代順序不定 (ABC-05)
    all_stocks = list(dict.fromkeys(
        top_buy["stock_code"].tolist() + top_sell["stock_code"].tolist()
    ))

    # 抓價視窗錨定於資料（而非 date.today()），確保跨日/跨機一致 (ABC-07/FSP-04)。
    # 60 列交易日約等於 84 個曆日，需額外緩衝以確保視窗夠長。
    if as_of_date is not None:
        end_date = as_of_date
    elif "trade_date" in broker_history.columns and not broker_history.empty:
        end_date = pd.to_datetime(broker_history["trade_date"]).max().date()
    else:
        end_date = date.today()
    start_date = end_date - timedelta(days=days + max(CORRELATION_WINDOWS) + 90)

    print(f"  計算相關性係數...")
    for stock_code in all_stocks:
        # Load cached stock prices；以請求範圍判斷快取是否涵蓋，避免回傳過舊/過短而凍結視窗 (FSP-01)
        stock_prices = load_stock_prices(stock_code, start_date=start_date, end_date=end_date)

        if stock_prices.empty and allow_fetch:
            # Fetch fresh data（僅在允許上網且本地快取不足時）
            stock_prices = fetch_stock_price_range(stock_code, start_date, end_date)

            if not stock_prices.empty:
                stock_prices = calculate_price_changes(stock_prices, windows=CORRELATION_WINDOWS)
                save_stock_prices(stock_code, stock_prices)

        if stock_prices.empty:
            # 缺價格資料時優雅跳過並記錄，使輸出集合對「暫時性網路失敗」具確定性 (ABC-07)
            result["correlations"].append({"stock_code": _norm_code(stock_code), "status": "no_price_data"})
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
                "stock_code": _norm_code(stock_code),
                **correlations
            }
            result["correlations"].append(corr_info)

            # Print correlation
            corr_str = ", ".join([f"{k}: {v:+.3f}" for k, v in correlations.items()])
            print(f"    {stock_code}: {corr_str}")

    # Sort correlations by absolute value of longest window correlation；
    # 加上 stock_code 次要排序鍵，使插入順序不影響結果（ABC-05）
    if result["correlations"]:
        longest_window = max(CORRELATION_WINDOWS)
        corr_key = f"corr_{longest_window}d"
        result["correlations"].sort(
            key=lambda x: (-abs(x.get(corr_key, 0)), str(x["stock_code"]))
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

    # total_trades 以真實交易日去重後的筆數計（避免重抓 scrape 日造成的重複計算）(ABC-03)
    count_col = "trade_date" if "trade_date" in broker_history.columns else "full_date"
    broker_stats = broker_history.groupby(["broker_id", "broker_name"]).agg({
        "stock_code": "nunique",
        count_col: "count"
    }).reset_index()

    broker_stats.columns = ["broker_id", "broker_name", "stocks_traded", "total_trades"]

    # Filter by minimum trades；加上 broker_id 次要排序鍵確保確定性
    broker_stats = broker_stats[broker_stats["total_trades"] >= min_trades]
    broker_stats = broker_stats.sort_values(
        ["total_trades", "broker_id"], ascending=[False, True]
    )

    return broker_stats


def main():
    """主程式"""
    print("=" * 60)
    print("Broker-Stock Correlation Analysis")
    print(f"Time: {datetime.now(TPE).isoformat()}")
    print("=" * 60)

    ensure_dirs()

    # 離線模式：設環境變數 BROKER_CORR_OFFLINE=1 時不上網抓價，缺資料則優雅跳過（純本地測試用）
    allow_fetch = os.environ.get("BROKER_CORR_OFFLINE", "") not in ("1", "true", "True")

    # Load broker history
    analysis_days = 60
    print(f"\n載入券商歷史交易數據（最近 {analysis_days} 天）...")
    broker_history = load_broker_history(days=analysis_days)

    if broker_history.empty:
        print("[ERROR] No broker history data found!")
        print("Please run update_broker.py first to collect broker data.")
        return

    print(f"載入 {len(broker_history)} 筆交易記錄")
    # 抓價結束日錨定資料最新交易日（非 date.today()），確保跨日一致 (ABC-07/FSP-04)
    as_of_date = None
    if "trade_date" in broker_history.columns and not broker_history.empty:
        as_of_date = pd.to_datetime(broker_history["trade_date"]).max().date()

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
                top_n=10,
                as_of_date=as_of_date,
                allow_fetch=allow_fetch
            )
            all_results.append(result)
        except Exception as e:
            print(f"  [ERROR] 分析失敗: {e}")
            continue

    # Export results
    output_path = os.path.join(DOCS_DIR, "broker_correlations.json")
    output_data = {
        "updated": datetime.now(TPE).isoformat(),
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
