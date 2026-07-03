# -*- coding: utf-8 -*-
"""Update broker branch trading data and export statistics.

每日更新券商分點交易數據，從富邦 e-Broker 網站抓取主力進出資料。
此腳本可獨立執行或由 GitHub Actions 調用。

功能：
1. 抓取熱門股票的券商分點買賣超數據
2. 統計各券商分點的績效（勝率、累計獲利）
3. 匯出 JSON 供前端顯示

使用：
  python update_broker.py         # 抓取預設熱門 20 支股票（HOT_STOCKS 清單）
  python update_broker.py --top50 # 抓取代碼最小的前 50 支上市櫃股票（依代碼排序，非熱門度）
  python update_broker.py --all   # 抓取所有上市櫃股票 (很慢)
"""

import argparse
import os
import json
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

# 台灣時區：CI 在 UTC 執行，凡是「蓋日期戳記 / 檔名 / 比對日期」都需用台北時間避免差一天 (UB-04)
TPE = ZoneInfo("Asia/Taipei")


def _ensure_trade_date(df: pd.DataFrame) -> pd.DataFrame:
    """確保 DataFrame 具備真實 ISO 交易日欄位 trade_date。

    新抓的資料 fetch_broker_data 已直接帶 trade_date；
    為相容舊的 on-disk 檔案（僅有無年份的 'date' + scrape 日 'full_date'），
    在缺少 trade_date 時由 date + full_date 反推（含 12 月→1 月跨年）。
    """
    df = df.copy()
    need = "trade_date" not in df.columns or df["trade_date"].isna().any() or (df.get("trade_date") == "").any()
    if need and {"date", "full_date"}.issubset(df.columns):
        fd = pd.to_datetime(df["full_date"], errors="coerce")
        mm = df["date"].astype(str).str.extract(r"(\d{1,2})/(\d{1,2})")
        month = pd.to_numeric(mm[0], errors="coerce")
        day = pd.to_numeric(mm[1], errors="coerce")
        # 交易月份大於 scrape 月份 → 屬於前一年（12 月交易在 1 月被抓）
        yr = fd.dt.year - (month > fd.dt.month).astype("Int64")
        reconstructed = pd.to_datetime(
            dict(year=yr, month=month, day=day), errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        if "trade_date" in df.columns:
            df["trade_date"] = df["trade_date"].where(
                df["trade_date"].notna() & (df["trade_date"] != ""), reconstructed
            )
        else:
            df["trade_date"] = reconstructed
    return df

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

    注意：limit 是「依股票代碼排序後取前 N 支」，並非依成交量/熱門度排序
    （flows CSV 沒有成交量欄位可排名），故 --top50/--top100 取到的是代碼最小的
    N 支（含 0050 等 ETF），與「熱門」無關 (UB-07)。

    Args:
        limit: 限制數量，None 表示全部

    Returns:
        股票代碼清單（依代碼遞增排序）
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
        "updated": datetime.now(TPE).isoformat(),
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
        "updated": datetime.now(TPE).isoformat(),
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
        "updated": datetime.now(TPE).isoformat(),
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
    # 添加 scrape 日期戳記（full_date 僅作為抓取稽核欄位；用台北時間避免 UTC 差一天 UB-04）
    today = datetime.now(TPE).date().isoformat()
    new_trades = new_trades.copy()
    new_trades["full_date"] = today
    # 確保新資料帶真實 ISO 交易日 trade_date（fetch 已提供；缺漏時反推）
    new_trades = _ensure_trade_date(new_trades)

    # 載入歷史數據
    if os.path.exists(history_path):
        history = pd.read_csv(history_path)
        history = _ensure_trade_date(history)
        # 合併（不再只靠 full_date==today 去重，改以真實交易日去重）
        combined = pd.concat([history, new_trades], ignore_index=True)
    else:
        combined = new_trades

    # 以真實交易識別去重：同一檔股票、同一分點、同一交易日、同一買賣方向只保留最後一次抓取
    # （keep="last"：較晚抓取的修正值勝出）。這修正了週末/假日重抓造成的重複計算 (UB-01)
    if "trade_date" in combined.columns:
        combined = combined.sort_values("full_date").drop_duplicates(
            subset=["stock_code", "broker_name", "trade_date", "side"], keep="last"
        )

    # 只保留最近 60 個交易日：以資料中的最新交易日為錨點（非系統時鐘），確保同樣的資料
    # 不論哪天執行都得到相同視窗 (UB-02)。並以真實交易日 trade_date 計算，而非 scrape 日。
    if "trade_date" in combined.columns:
        td = pd.to_datetime(combined["trade_date"], errors="coerce")
        valid_days = sorted(td.dropna().unique())
        if valid_days:
            keep_days = set(valid_days[-60:])
            combined = combined[td.isin(keep_days)]
    elif "full_date" in combined.columns:
        # 後備：若無 trade_date，至少以資料錨定 full_date 視窗（避免時鐘相依）
        fd = pd.to_datetime(combined["full_date"], errors="coerce")
        cutoff = fd.max() - timedelta(days=60)
        combined = combined[fd >= cutoff]

    combined = combined.reset_index(drop=True)

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

    # 確保有真實交易日欄位後，以「真實交易日」彙總（而非 scrape 日 full_date），
    # 避免週末/假日重抓導致 cumsum 重複計算同一筆交易 (UB-01)
    target_df = _ensure_trade_date(target_df)
    if "trade_date" not in target_df.columns:
        print("No trade_date available for trends")
        return

    # 按券商和真實交易日彙總
    daily = target_df.groupby(["broker_name", "trade_date"]).agg({
        "net_vol": "sum"
    }).reset_index()

    daily = daily.sort_values(["broker_name", "trade_date"])

    # 計算累計買賣超
    brokers_data = {}
    for broker_name in daily["broker_name"].unique():
        broker_df = daily[daily["broker_name"] == broker_name].copy()
        broker_df = broker_df.sort_values("trade_date")
        broker_df["cumulative"] = broker_df["net_vol"].cumsum()

        brokers_data[broker_name] = broker_df[["trade_date", "net_vol", "cumulative"]].rename(
            columns={"trade_date": "date"}
        ).to_dict(orient="records")
    
    result = {
        "updated": datetime.now(TPE).isoformat(),
        "brokers": brokers_data
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"Saved broker trends to {output_path}")




def main():
    """主程式"""
    parser = argparse.ArgumentParser(description="更新券商分點交易數據")
    parser.add_argument("--all", action="store_true", help="抓取所有上市櫃股票（約 1700+，需數小時）")
    parser.add_argument("--top50", action="store_true", help="抓取代碼最小的前 50 支上市櫃股票（依代碼排序，非熱門度）")
    parser.add_argument("--top100", action="store_true", help="抓取代碼最小的前 100 支上市櫃股票（依代碼排序，非熱門度）")
    parser.add_argument("--delay", type=float, default=1.5, help="每次請求間隔秒數（預設 1.5）")
    args = parser.parse_args()
    
    print("=" * 60)
    print("Update Broker Trading Data")
    print(f"Time: {datetime.now(TPE).isoformat()}")
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
        raise SystemExit("[ERROR] No broker trades fetched; failing update to avoid silent stale data.")
    
    print(f"\nTotal records fetched: {len(all_trades)}")
    
    # 2. 儲存原始數據到 CSV（檔名日期用台北時間，避免 UTC 差一天 UB-04）
    today = datetime.now(TPE).date().isoformat()
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
    # 報告真實「交易日」數（trade_date），而非 scrape 日數 (UB-06)
    trading_days = history_df["trade_date"].nunique() if "trade_date" in history_df.columns else history_df["full_date"].nunique()
    print(f"Broker history: {len(history_df)} records from {trading_days} trading days")
    
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
