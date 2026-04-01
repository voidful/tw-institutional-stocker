# -*- coding: utf-8 -*-
"""Update broker branch trading data and export statistics.

每日更新券商分點交易數據，從富邦 e-Broker 網站抓取主力進出資料。
此腳本可獨立執行或由 GitHub Actions 調用。

功能：
1. 抓取熱門股票的券商分點買賣超數據
2. 統計各券商分點的績效（勝率、累計獲利）
3. 匯出 JSON 供前端顯示

使用：
  python update_broker.py         # 抓取熱門 20 支股票
  python update_broker.py --top50 # 抓取前 50 支熱門股
  python update_broker.py --all   # 抓取所有上市櫃股票 (很慢)
"""

import argparse
import os
import json
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

# 數據目錄
DATA_DIR = "data"
BROKER_DATA_DIR = os.path.join(DATA_DIR, "broker")
DOCS_DIR = os.path.join("docs", "data")

# 熱門股票清單（預設抓取）
HOT_STOCKS = [
    # 權值股
    "2330",  # 台積電
    "2317",  # 鴻海
    "2454",  # 聯發科
    "2308",  # 台達電
    "2382",  # 廣達
    "2891",  # 中信金
    "2881",  # 富邦金
    "2882",  # 國泰金
    "2303",  # 聯電
    "3711",  # 日月光投控
    # 熱門股
    "2603",  # 長榮
    "2609",  # 陽明
    "2615",  # 萬海
    "2618",  # 長榮航
    "3037",  # 欣興
    "2345",  # 智邦
    "3006",  # 晶豪科
    "2379",  # 瑞昱
    "3034",  # 聯詠
    "2412",  # 中華電
]

# 目標追蹤券商（部分匹配）
TARGET_BROKERS = [
    "凱基",
    "美林",
    "摩根",
    "高盛",
    "瑞銀",
    "野村",
    "大和",
    "麥格理",
    "元大",
    "富邦",
]


def get_all_stock_codes(limit: Optional[int] = None) -> list[str]:
    """
    從現有的 flows CSV 中取得所有股票代碼
    
    Args:
        limit: 限制數量，None 表示全部
    
    Returns:
        股票代碼清單
    """
    codes = set()
    
    for csv_file in ["twse_flows.csv", "tpex_flows.csv"]:
        csv_path = os.path.join(DATA_DIR, csv_file)
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path)
                if "code" in df.columns:
                    codes.update(df["code"].astype(str).unique())
            except Exception as e:
                print(f"Warning: Could not read {csv_file}: {e}")
    
    # 排序並過濾只保留純數字代碼（排除權證等）
    valid_codes = [c for c in codes if c.isdigit() and len(c) == 4]
    valid_codes.sort()
    
    if limit:
        return valid_codes[:limit]
    return valid_codes


def ensure_dirs():
    """確保必要目錄存在"""
    os.makedirs(BROKER_DATA_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)



def fetch_all_broker_data(stock_codes: list[str], delay: float = 1.5) -> pd.DataFrame:
    """
    抓取多支股票的券商分點數據
    
    Args:
        stock_codes: 股票代碼清單
        delay: 每次請求間隔（秒）
    
    Returns:
        合併的 DataFrame
    """
    from fetch_broker_data import fetch_broker_trading, close_browser
    
    all_data = []
    total = len(stock_codes)
    
    for i, code in enumerate(stock_codes, 1):
        print(f"[{i}/{total}] Fetching broker data for {code}...")
        try:
            df = fetch_broker_trading(code)
            if not df.empty:
                all_data.append(df)
                print(f"  -> Got {len(df)} records")
            else:
                print(f"  -> No data")
        except Exception as e:
            print(f"  -> Error: {e}")
        
        if i < total:
            time.sleep(delay)
    
    close_browser()
    
    if all_data:
        return pd.concat(all_data, ignore_index=True)
    return pd.DataFrame()


def filter_target_brokers(df: pd.DataFrame) -> pd.DataFrame:
    """過濾出目標券商的交易"""
    if df.empty:
        return df
    
    mask = df["broker_name"].apply(
        lambda x: any(target in str(x) for target in TARGET_BROKERS)
    )
    return df[mask].copy()


def aggregate_broker_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    彙總券商分點統計
    
    Returns:
        DataFrame with columns:
            - broker_name: 券商名稱
            - total_net_vol: 總買賣超張數
            - buy_count: 買超次數
            - sell_count: 賣超次數
            - stocks_traded: 交易股票數
            - avg_net_vol: 平均買賣超
    """
    if df.empty:
        return pd.DataFrame()
    
    stats = df.groupby("broker_name").agg({
        "net_vol": ["sum", "mean"],
        "side": lambda x: (x == "buy").sum(),
        "stock_code": "nunique"
    })
    
    stats.columns = ["total_net_vol", "avg_net_vol", "buy_count", "stocks_traded"]
    stats = stats.reset_index()
    stats["sell_count"] = df.groupby("broker_name").apply(
        lambda x: (x["side"] == "sell").sum(), include_groups=False
    ).values
    
    # 排序：按絕對買賣超量
    stats["abs_net_vol"] = stats["total_net_vol"].abs()
    stats = stats.sort_values("abs_net_vol", ascending=False)
    stats = stats.drop(columns=["abs_net_vol"])
    
    return stats


def aggregate_stock_broker_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """
    建立股票-券商矩陣，顯示每支股票各券商的買賣超
    
    Returns:
        Pivot table: rows=stock_code, columns=broker_name, values=net_vol
    """
    if df.empty:
        return pd.DataFrame()
    
    # 只取目標券商
    target_df = filter_target_brokers(df)
    if target_df.empty:
        return pd.DataFrame()
    
    pivot = target_df.pivot_table(
        index="stock_code",
        columns="broker_name",
        values="net_vol",
        aggfunc="sum",
        fill_value=0
    )
    
    return pivot


def export_broker_ranking(df: pd.DataFrame, output_path: str):
    """匯出券商排名 JSON"""
    if df.empty:
        return
    
    result = {
        "updated": datetime.now().isoformat(),
        "data": df.to_dict(orient="records")
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved broker ranking to {output_path}")


def export_broker_trades(df: pd.DataFrame, output_path: str):
    """匯出今日分點交易明細 JSON"""
    if df.empty:
        return
    
    # 轉換為可序列化格式
    records = df.to_dict(orient="records")
    
    result = {
        "updated": datetime.now().isoformat(),
        "count": len(records),
        "data": records
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved {len(records)} trades to {output_path}")


def export_target_broker_trades(df: pd.DataFrame, output_path: str):
    """匯出目標券商交易 JSON"""
    target_df = filter_target_brokers(df)
    if target_df.empty:
        print("No target broker trades found")
        return
    
    # 按券商和股票分組
    grouped = target_df.groupby("broker_name").apply(
        lambda g: g[["stock_code", "net_vol", "side", "pct", "rank"]].to_dict(orient="records"),
        include_groups=False
    ).to_dict()
    
    result = {
        "updated": datetime.now().isoformat(),
        "brokers": grouped
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved target broker trades to {output_path}")


def build_broker_history(new_trades: pd.DataFrame, history_path: str) -> pd.DataFrame:
    """
    累積券商歷史交易數據
    
    Args:
        new_trades: 今日新交易數據
        history_path: 歷史數據 CSV 路徑
    
    Returns:
        合併後的歷史數據
    """
    # 添加完整日期
    today = date.today().isoformat()
    new_trades = new_trades.copy()
    new_trades["full_date"] = today
    
    # 載入歷史數據
    if os.path.exists(history_path):
        history = pd.read_csv(history_path)
        # 移除今天的舊數據（如果有）
        history = history[history["full_date"] != today]
        # 合併
        combined = pd.concat([history, new_trades], ignore_index=True)
    else:
        combined = new_trades
    
    # 只保留最近 60 天
    if "full_date" in combined.columns:
        combined["full_date"] = pd.to_datetime(combined["full_date"])
        cutoff = datetime.now() - timedelta(days=60)
        combined = combined[combined["full_date"] >= cutoff]
        combined["full_date"] = combined["full_date"].dt.strftime("%Y-%m-%d")
    
    # 儲存
    combined.to_csv(history_path, index=False, encoding="utf-8-sig")
    
    return combined


def export_broker_trends(history_df: pd.DataFrame, output_path: str):
    """
    匯出券商買賣超趨勢數據供前端繪圖
    
    生成格式：
    {
        "updated": "...",
        "brokers": {
            "摩根大通": [
                {"date": "2024-12-01", "net_vol": 1234, "cumulative": 5678},
                ...
            ]
        }
    }
    """
    if history_df.empty:
        return
    
    # 只處理目標券商
    target_df = filter_target_brokers(history_df)
    if target_df.empty:
        print("No target broker history found")
        return
    
    # 按券商和日期彙總
    daily = target_df.groupby(["broker_name", "full_date"]).agg({
        "net_vol": "sum"
    }).reset_index()
    
    daily = daily.sort_values(["broker_name", "full_date"])
    
    # 計算累計買賣超
    brokers_data = {}
    for broker_name in daily["broker_name"].unique():
        broker_df = daily[daily["broker_name"] == broker_name].copy()
        broker_df = broker_df.sort_values("full_date")
        broker_df["cumulative"] = broker_df["net_vol"].cumsum()
        
        brokers_data[broker_name] = broker_df[["full_date", "net_vol", "cumulative"]].rename(
            columns={"full_date": "date"}
        ).to_dict(orient="records")
    
    result = {
        "updated": datetime.now().isoformat(),
        "brokers": brokers_data
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved broker trends to {output_path}")




def main():
    """主程式"""
    parser = argparse.ArgumentParser(description="更新券商分點交易數據")
    parser.add_argument("--all", action="store_true", help="抓取所有上市櫃股票（約 1700+，需數小時）")
    parser.add_argument("--top50", action="store_true", help="抓取前 50 支熱門股")
    parser.add_argument("--top100", action="store_true", help="抓取前 100 支熱門股")
    parser.add_argument("--delay", type=float, default=1.5, help="每次請求間隔秒數（預設 1.5）")
    args = parser.parse_args()
    
    print("=" * 60)
    print("Update Broker Trading Data")
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 60)
    
    ensure_dirs()
    
    # 決定要抓取的股票清單
    if args.all:
        stock_codes = get_all_stock_codes()
        print(f"\n[MODE] Full crawl: {len(stock_codes)} stocks")
    elif args.top100:
        stock_codes = get_all_stock_codes(limit=100)
        print(f"\n[MODE] Top 100 stocks")
    elif args.top50:
        stock_codes = get_all_stock_codes(limit=50)
        print(f"\n[MODE] Top 50 stocks")
    else:
        stock_codes = HOT_STOCKS
        print(f"\n[MODE] Hot stocks: {len(stock_codes)} stocks")
    
    # 1. 抓取分點數據
    print(f"\nFetching broker data for {len(stock_codes)} stocks...")
    all_trades = fetch_all_broker_data(stock_codes, delay=args.delay)
    
    if all_trades.empty:
        print("[WARN] No broker trades fetched, aborting.")
        return
    
    print(f"\nTotal records fetched: {len(all_trades)}")
    
    # 2. 儲存原始數據到 CSV
    today = date.today().isoformat()
    csv_path = os.path.join(BROKER_DATA_DIR, f"broker_trades_{today}.csv")
    all_trades.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"Saved raw data to {csv_path}")
    
    # 3. 統計券商排名
    broker_stats = aggregate_broker_stats(all_trades)
    print(f"\nBroker stats: {len(broker_stats)} brokers")
    
    # 4. 匯出 JSON 供前端使用
    # 4.1 券商排名
    ranking_path = os.path.join(DOCS_DIR, "broker_ranking.json")
    export_broker_ranking(broker_stats, ranking_path)
    
    # 4.2 今日交易明細
    trades_path = os.path.join(DOCS_DIR, "broker_trades_latest.json")
    export_broker_trades(all_trades, trades_path)
    
    # 4.3 目標券商交易
    target_path = os.path.join(DOCS_DIR, "target_broker_trades.json")
    export_target_broker_trades(all_trades, target_path)
    
    # 4.4 累積歷史數據並生成趨勢圖數據
    history_path = os.path.join(BROKER_DATA_DIR, "broker_history.csv")
    history_df = build_broker_history(all_trades, history_path)
    print(f"Broker history: {len(history_df)} records from {history_df['full_date'].nunique()} days")
    
    # 4.5 匯出券商趨勢數據
    trends_path = os.path.join(DOCS_DIR, "broker_trends.json")
    export_broker_trends(history_df, trends_path)

    
    # 5. 輸出統計摘要
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Stocks crawled: {len(stock_codes)}")
    print(f"Total trades: {len(all_trades)}")
    print(f"Buy side: {len(all_trades[all_trades['side'] == 'buy'])}")
    print(f"Sell side: {len(all_trades[all_trades['side'] == 'sell'])}")
    print(f"Unique brokers: {all_trades['broker_name'].nunique()}")
    
    # 顯示前 10 名買超券商
    print("\nTop 10 Net Buy Brokers:")
    top_buy = broker_stats.head(10)
    for _, row in top_buy.iterrows():
        print(f"  {row['broker_name']}: {row['total_net_vol']:+,} 張")
    
    print("\n[INFO] update_broker.py completed successfully.")


if __name__ == "__main__":
    main()

