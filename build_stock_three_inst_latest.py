# -*- coding: utf-8 -*-
"""
build_stock_three_inst_latest.py

從 docs/data/timeseries/*.json 聚合出
「每檔股票最新一筆三大法人持股比重」快照。

輸出：
- docs/data/stock_three_inst_latest.json
  格式為 list[{
      code, name, market, date,
      foreign_ratio, trust_ratio, dealer_ratio, three_inst_ratio
  }]
"""

import os
import json
from datetime import datetime

from update_all import clean_float

# 與 update_all.py 保持一致的路徑設定
DATA_DIR = "data"
DOCS_DIR = os.path.join("docs", "data")
TIMESERIES_DIR = os.path.join(DOCS_DIR, "timeseries")


def ensure_dirs():
    os.makedirs(DOCS_DIR, exist_ok=True)
    os.makedirs(TIMESERIES_DIR, exist_ok=True)


def parse_date(dstr: str) -> datetime:
    """將 'YYYY-MM-DD' 字串轉成 datetime，用來排序."""
    return datetime.strptime(dstr, "%Y-%m-%d")


def main():
    ensure_dirs()

    records = []

    # 掃描所有個股時序 JSON，例如 2330.json、2317.json ...
    for fname in os.listdir(TIMESERIES_DIR):
        if not fname.endswith(".json"):
            continue

        code = fname[:-5]  # 去掉 .json
        path = os.path.join(TIMESERIES_DIR, fname)

        try:
            with open(path, "r", encoding="utf-8") as f:
                series = json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] failed to load {path}: {e}")
            continue

        if not series:
            # 空陣列就跳過，避免前端讀到完全沒資料的股票
            print(f"[INFO] skip {code}: empty timeseries")
            continue

        # 確保依日期排序（保守做法，不假設檔案內本來就排序好）
        try:
            series_sorted = sorted(series, key=lambda r: parse_date(r["date"]))
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] failed to sort by date for {code}: {e}")
            # 如果日期壞掉，用原順序最後一筆 fallback
            series_sorted = series

        last = series_sorted[-1]

        rec = {
            "code": last.get("code", code),
            "name": last.get("name", ""),
            "market": last.get("market", ""),
            "date": last.get("date", ""),
            "foreign_ratio": clean_float(last.get("foreign_ratio", 0.0)),
            "trust_ratio": clean_float(last.get("trust_ratio", 0.0)),
            "dealer_ratio": clean_float(last.get("dealer_ratio", 0.0)),
            "three_inst_ratio": clean_float(last.get("three_inst_ratio", 0.0)),
        }

        records.append(rec)

    # 依股票代碼排序，方便 debug / 前端顯示
    records.sort(key=lambda r: (r["code"], r["market"]))

    out_path = os.path.join(DOCS_DIR, "stock_three_inst_latest.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"[INFO] wrote {len(records)} stocks to {out_path}")


if __name__ == "__main__":
    main()
