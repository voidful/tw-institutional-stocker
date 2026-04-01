# -*- coding: utf-8 -*-
"""Update & export Taiwan institutional (三大法人) holdings data.

功能重點：
- 自動抓 TWSE/TPEX 三大法人日交易 + 外資持股；
- 以 inst_baseline.csv 為基準點，校正投信 / 自營商持股；
- 計算三大法人持股比重；
- 計算多視窗變化：5 / 20 / 60 / 120 日；
- 輸出 ranking JSON + 每檔股票時序 JSON。
"""
import json
import os
import csv
from io import StringIO
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional
import math
import requests
import pandas as pd

from utils_columns import find_col_any, normalize_columns

DATA_DIR = "data"
DOCS_DIR = os.path.join("docs", "data")
TIMESERIES_DIR = os.path.join(DOCS_DIR, "timeseries")
INST_BASELINE_PATH = os.path.join(DATA_DIR, "inst_baseline.csv")

WINDOWS = [5, 20, 60, 120]
FLOW_COLUMNS = ["date", "code", "name", "foreign_net", "trust_net", "dealer_net", "market"]
FOREIGN_COLUMNS = ["date", "code", "name", "market", "total_shares", "foreign_shares", "foreign_ratio"]
INIT_FETCH_DAYS = 60
BACKFILL_LOOKBACK_DAYS = 120


# ---------- generic helpers ----------

def ensure_dirs():
    for p in (DATA_DIR, DOCS_DIR, TIMESERIES_DIR):
        os.makedirs(p, exist_ok=True)


def get_taipei_today() -> date:
    tz = ZoneInfo("Asia/Taipei")
    return datetime.now(tz).date()


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=Sat, 6=Sun


def get_target_trade_date() -> date:
    """用台北時間的「昨天」，週末往前推到最近一個平日。"""
    today = get_taipei_today()
    target = today - timedelta(days=1)
    while is_weekend(target):
        target -= timedelta(days=1)
    return target


def get_last_date_from_csv(path: str):
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, usecols=["date"])
    if df.empty:
        return None
    return pd.to_datetime(df["date"]).dt.date.max()


def iter_trading_days(start: date, end: date):
    cur = start
    while cur <= end:
        if not is_weekend(cur):
            yield cur
        cur += timedelta(days=1)


def numeric_series(series: pd.Series, to_float: bool = False) -> pd.Series:
    s = series.astype(str)

    # 1. 去掉千分位
    s = s.str.replace(",", "", regex=False)

    # 2. 統一各種 minus / plus 符號
    s = (
        s.str.replace("\u2212", "-", regex=False)  # ‘−’
         .str.replace("\uFF0D", "-", regex=False)  # 全形『－』
         .str.replace("\uFF0B", "+", regex=False)  # 全形『＋』
         .str.strip()
    )

    # 3. 括號負數: (1234) -> -1234
    mask_paren = s.str.match(r"^\([\d\.]+\)$")
    s.loc[mask_paren] = "-" + s.loc[mask_paren].str.strip("()")

    # 4. 純缺值 token -> 0
    missing_tokens = {"", "nan", "NaN", "None", "--"}
    s = s.where(~s.isin(missing_tokens), "0")

    if to_float:
        return pd.to_numeric(s, errors="coerce").fillna(0.0)

    return pd.to_numeric(s, errors="coerce").fillna(0).astype("Int64")


def empty_flows_df() -> pd.DataFrame:
    return pd.DataFrame(columns=FLOW_COLUMNS)


def empty_foreign_df() -> pd.DataFrame:
    return pd.DataFrame(columns=FOREIGN_COLUMNS)


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = pd.NA
    return out


def restore_column_from_index(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if col in df.columns:
        return df
    if isinstance(df.index, pd.MultiIndex) and col in df.index.names:
        return df.reset_index(level=col)
    if df.index.name == col:
        return df.reset_index()
    return df


def read_csv_table_with_header(text: str) -> pd.DataFrame:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    rows: list[list[str]] = []
    for line in lines:
        try:
            row = next(csv.reader([line]))
        except csv.Error:
            continue
        rows.append([str(x).replace("\ufeff", "").strip() for x in row])

    if not rows:
        return pd.DataFrame()

    header_idx = 0
    for idx, row in enumerate(rows[:40]):
        joined = "".join(row)
        has_code = ("代號" in joined) or ("證券代號" in joined)
        has_name = ("名稱" in joined) or ("證券名稱" in joined)
        if has_code and has_name:
            header_idx = idx
            break

    header = rows[header_idx]
    width = len(header)
    if width == 0:
        return pd.DataFrame()

    body: list[list[str]] = []
    for row in rows[header_idx + 1:]:
        if not any(str(x).strip() for x in row):
            continue
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        body.append(row)

    return pd.DataFrame(body, columns=header)


def read_first_html_table(text: str) -> pd.DataFrame:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(text, "html.parser")
    table = soup.find("table")
    if table is None:
        return pd.DataFrame()

    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        rows.append([cell.get_text(" ", strip=True) for cell in cells])

    if not rows:
        return pd.DataFrame()

    header_idx = 0
    for idx, row in enumerate(rows[:20]):
        joined = "".join(row)
        has_code = ("代號" in joined) or ("證券代號" in joined)
        has_name = ("名稱" in joined) or ("證券名稱" in joined)
        if has_code and has_name:
            header_idx = idx
            break

    header = [str(x).strip() for x in rows[header_idx]]
    width = len(header)
    if width == 0:
        return pd.DataFrame()

    body: list[list[str]] = []
    for row in rows[header_idx + 1:]:
        if not any(str(x).strip() for x in row):
            continue
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        body.append([str(x).strip() for x in row])

    return pd.DataFrame(body, columns=header)


def get_existing_dates(path: str) -> set[date]:
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, usecols=["date"])
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] failed reading date column from {path}: {e}")
        return set()

    if df.empty:
        return set()

    d = pd.to_datetime(df["date"], errors="coerce").dt.date.dropna()
    return set(d.tolist())


def calc_fetch_dates(
    path: str,
    target_date: date,
    init_fetch_days: int = INIT_FETCH_DAYS,
    lookback_days: int = BACKFILL_LOOKBACK_DAYS,
) -> list[date]:
    existing = get_existing_dates(path)

    if not existing:
        start = target_date - timedelta(days=init_fetch_days)
        while is_weekend(start):
            start += timedelta(days=1)
        return list(iter_trading_days(start, target_date))

    last_date = max(existing)
    forward_dates = set(iter_trading_days(last_date + timedelta(days=1), target_date))

    min_existing = min(existing)
    repair_start = max(min_existing, target_date - timedelta(days=lookback_days))
    missing_dates = {d for d in iter_trading_days(repair_start, target_date) if d not in existing}

    return sorted(forward_dates | missing_dates)


# ---------- TWSE: T86 (daily flows) ----------

def fetch_twse_t86(trade_date: date) -> pd.DataFrame:
    """三大法人買賣超統計資訊 (T86) for TWSE.

    注意：/fund/T86 是 Big5 編碼，必須用 cp950 解碼，否則欄位會是亂碼。
    """
    datestr = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/fund/T86"
    params = {
        "response": "csv",
        "date": datestr,
        "selectType": "ALLBUT0999",
    }
    resp = requests.get(url, params=params, timeout=20)

    csv_text = resp.content.decode("cp950", errors="ignore")
    df = pd.read_csv(StringIO(csv_text), header=1)

    df = df.dropna(how="all", axis=0)
    df = df.dropna(how="all", axis=1)
    df = normalize_columns(df)

    if df.empty or len(df.columns) == 0:
        return empty_flows_df()

    code_col = find_col_any(df, ["證券代號"])
    name_col = find_col_any(df, ["證券名稱"])

    col_foreign_ex_net = find_col_any(
        df,
        [
            "外陸資買賣超股數(不含外資自營商)",
            "外資及陸資(不含外資自營商)買賣超股數",
            "外資及陸資買賣超股數(不含外資自營商)",
        ],
    )
    col_foreign_self_net = find_col_any(df, ["外資自營商買賣超股數"])
    col_trust_net = find_col_any(df, ["投信買賣超股數"])
    col_dealer_net = find_col_any(
        df,
        [
            "自營商買賣超股數合計",
            "自營商買賣超股數",
        ],
    )

    df["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "")
    df["code"] = df["code"].str.strip().str.zfill(4)
    df["name"] = df[name_col].astype(str).str.strip()

    foreign_ex = numeric_series(df[col_foreign_ex_net])
    foreign_self = numeric_series(df[col_foreign_self_net])
    trust_net = numeric_series(df[col_trust_net])
    dealer_net = numeric_series(df[col_dealer_net])

    out = pd.DataFrame(
        {
            "date": trade_date,
            "code": df["code"],
            "name": df["name"],
            "foreign_net": (foreign_ex + foreign_self),
            "trust_net": trust_net,
            "dealer_net": dealer_net,
            "market": "TWSE",
        }
    )

    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask]
    return out[FLOW_COLUMNS]


# ---------- TWSE: MI_QFIIS (foreign holdings) ----------

def fetch_twse_mi_qfiis(trade_date: date) -> pd.DataFrame:
    """外資及陸資投資持股統計 (MI_QFIIS) for TWSE.

    若當日查無資料或格式異常，直接回傳空 DataFrame，避免後續 find_col_any 崩潰。
    """
    datestr = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS"
    params = {
        "response": "csv",
        "date": datestr,
        "selectType": "ALLBUT0999",
    }
    resp = requests.get(url, params=params, timeout=20)

    # TWSE MI_QFIIS is Big5/CP950 encoded, not UTF-8
    csv_text = resp.content.decode("cp950", errors="ignore")

    try:
        df = pd.read_csv(StringIO(csv_text), header=1)
    except Exception:
        return empty_foreign_df()

    df = df.dropna(how="all", axis=0)
    df = df.dropna(how="all", axis=1)
    df = normalize_columns(df)

    if df.empty or len(df.columns) == 0:
        return empty_foreign_df()

    code_col = find_col_any(df, ["證券代號"])
    name_col = find_col_any(df, ["證券名稱"])
    issued_col = find_col_any(df, ["發行股數"])
    foreign_shares_col = find_col_any(df, ["全體外資及陸資持有股數"])
    foreign_ratio_col = find_col_any(df, ["全體外資及陸資持股比率"])

    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "").str.strip().str.zfill(4)
    out["name"] = df[name_col].astype(str).str.strip()

    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask]

    if out.empty:
        return empty_foreign_df()

    out["total_shares"] = numeric_series(df.loc[mask, issued_col])
    out["foreign_shares"] = numeric_series(df.loc[mask, foreign_shares_col])
    out["foreign_ratio"] = numeric_series(df.loc[mask, foreign_ratio_col], to_float=True)
    out["date"] = trade_date
    out["market"] = "TWSE"

    return out[FOREIGN_COLUMNS]


# ---------- TPEX helpers ----------

def roc_date(d: date) -> str:
    y = d.year - 1911
    return f"{y:03d}/{d.month:02d}/{d.day:02d}"


# ---------- TPEX: 三大法人 daily flows ----------

def fetch_tpex_flows(trade_date: date) -> pd.DataFrame:
    """上櫃股票三大法人買賣明細."""
    roc = roc_date(trade_date)
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
    params = {
        "d": roc,
        "l": "zh-tw",
        "o": "htm",
        "s": "0",
        "se": "EW",
        "t": "D",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.encoding = "utf-8"
    try:
        df = read_first_html_table(resp.text)
    except Exception:
        # fallback: 某些頁面結構可直接被 read_html 讀出
        tables = pd.read_html(StringIO(resp.text))
        if not tables:
            return empty_flows_df()
        df = tables[0]

    df = normalize_columns(df)
    if df.empty or len(df.columns) == 0:
        return empty_flows_df()

    code_col = find_col_any(df, ["代號"])
    name_col = find_col_any(df, ["名稱"])

    col_foreign_ex_net = find_col_any(
        df,
        [
            "外資及陸資(不含外資自營商)買賣超股數",
            "外資及陸資買賣超股數(不含外資自營商)",
            "外資及陸資買賣超股數",
        ],
    )
    col_foreign_self_net = find_col_any(df, ["外資自營商買賣超股數"])
    col_trust_net = find_col_any(df, ["投信買賣超股數"])
    col_dealer_net = find_col_any(
        df,
        [
            "自營商買賣超股數合計",
            "自營商買賣超股數",
        ],
    )

    df["code"] = df[code_col].astype(str).str.strip().str.zfill(4)
    df["name"] = df[name_col].astype(str).str.strip()

    foreign_ex = numeric_series(df[col_foreign_ex_net])
    foreign_self = numeric_series(df[col_foreign_self_net])
    trust_net = numeric_series(df[col_trust_net])
    dealer_net = numeric_series(df[col_dealer_net])

    out = pd.DataFrame(
        {
            "date": trade_date,
            "code": df["code"],
            "name": df["name"],
            "foreign_net": (foreign_ex + foreign_self),
            "trust_net": trust_net,
            "dealer_net": dealer_net,
            "market": "TPEX",
        }
    )

    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask]
    return out[FLOW_COLUMNS]


# ---------- TPEX: 外資持股比例 (QFII) ----------

def fetch_tpex_qfii(trade_date: date) -> pd.DataFrame:
    """僑外資及陸資持股統計 (上櫃)."""
    url = "https://www.tpex.org.tw/web/stock/3insti/qfii/qfii_result.php"
    params = {
        "d": roc_date(trade_date),
        "l": "zh-tw",
        "o": "data",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.encoding = "utf-8"
    try:
        df = read_csv_table_with_header(resp.text)
        if df.empty:
            df = pd.read_csv(
                StringIO(resp.text),
                engine="python",
                on_bad_lines="skip",
            )
    except Exception:
        return empty_foreign_df()

    df = df.dropna(how="all", axis=0)
    df = df.dropna(how="all", axis=1)
    df = normalize_columns(df)
    if df.empty or len(df.columns) == 0:
        return empty_foreign_df()

    code_col = find_col_any(df, ["證券代號", "代號"])
    name_col = find_col_any(df, ["證券名稱", "名稱"])
    shares_col = find_col_any(df, ["發行股數"])
    foreign_shares_col = find_col_any(df, ["僑外資及陸資持有股數"])
    foreign_ratio_col = find_col_any(df, ["僑外資及陸資持股比率"])

    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.strip().str.zfill(4)
    out["name"] = df[name_col].astype(str).str.strip()

    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask]

    if out.empty:
        return empty_foreign_df()

    out["total_shares"] = numeric_series(df.loc[mask, shares_col])
    out["foreign_shares"] = numeric_series(df.loc[mask, foreign_shares_col])
    out["foreign_ratio"] = numeric_series(df.loc[mask, foreign_ratio_col], to_float=True)
    out["date"] = trade_date
    out["market"] = "TPEX"

    return out[FOREIGN_COLUMNS]


# ---------- history append helpers ----------

def append_history(df_new: pd.DataFrame, path: str, key_cols: list[str]) -> pd.DataFrame:
    if df_new.empty:
        if os.path.exists(path):
            return pd.read_csv(path)
        return df_new.copy()

    df_new = ensure_columns(df_new, key_cols)
    df_new = df_new.copy()
    df_new["date"] = pd.to_datetime(df_new["date"], errors="coerce").dt.date
    df_new = df_new.dropna(subset=["date"])

    if os.path.exists(path):
        df_old = pd.read_csv(path)
        df_old = ensure_columns(df_old, key_cols)
        df_old["date"] = pd.to_datetime(df_old["date"], errors="coerce").dt.date
        df_old = df_old.dropna(subset=["date"])
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_all = df_new

    df_all = df_all.drop_duplicates(subset=key_cols).sort_values(["date", "code"])
    df_all.to_csv(path, index=False, date_format="%Y-%m-%d")
    return df_all


# ---------- model: holdings estimation ----------

def build_foreign_master(twse: pd.DataFrame, tpex: pd.DataFrame) -> pd.DataFrame:
    all_df = pd.concat([twse, tpex], ignore_index=True)
    if all_df.empty:
        return all_df
    all_df = restore_column_from_index(all_df, "code")
    all_df = ensure_columns(all_df, ["code", "date"])
    all_df = all_df.dropna(subset=["code", "date"])
    if all_df.empty:
        return all_df
    all_df = all_df.sort_values(["code", "date"])
    all_df["date"] = pd.to_datetime(all_df["date"], errors="coerce").dt.date
    all_df = all_df.dropna(subset=["date"])
    if all_df.empty:
        return all_df
    all_df = (
        all_df.set_index(["code", "date"])
        .sort_index()
        .groupby(level=0)
        .ffill()
        .reset_index()
    )
    return all_df


def build_estimated_holdings(
    flows: pd.DataFrame,
    foreign_master: pd.DataFrame,
    baseline: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """建立三大法人持股估計，支援 baseline 校正。"""
    flows = restore_column_from_index(flows.copy(), "code")
    foreign_master = restore_column_from_index(foreign_master.copy(), "code")

    flows = ensure_columns(flows, ["date", "code", "market", "trust_net", "dealer_net"])
    foreign_master = ensure_columns(
        foreign_master, ["date", "code", "market", "total_shares", "foreign_ratio"]
    )

    flows["date"] = pd.to_datetime(flows["date"], errors="coerce").dt.date
    foreign_master["date"] = pd.to_datetime(foreign_master["date"], errors="coerce").dt.date
    flows = flows.dropna(subset=["date", "code", "market"])
    foreign_master = foreign_master.dropna(subset=["date", "code", "market"])
    if flows.empty:
        return flows

    merged = flows.merge(
        foreign_master[
            [
                "date",
                "code",
                "market",
                "total_shares",
                "foreign_ratio",
            ]
        ],
        on=["date", "code", "market"],
        how="left",
    )

    if baseline is not None and not baseline.empty and "date" in baseline.columns:
        base = restore_column_from_index(baseline.copy(), "code")
        base = ensure_columns(base, ["date", "code", "trust_shares_base", "dealer_shares_base"])
        base["date"] = pd.to_datetime(
            base["date"], format="%Y-%m-%d", errors="coerce"
        )
        base = base.dropna(subset=["date"])
        if not base.empty:
            base["date"] = base["date"].dt.date
            merged = merged.merge(
                base[["date", "code", "trust_shares_base", "dealer_shares_base"]],
                on=["date", "code"],
                how="left",
            )
        else:
            merged["trust_shares_base"] = pd.NA
            merged["dealer_shares_base"] = pd.NA
    else:
        merged["trust_shares_base"] = pd.NA
        merged["dealer_shares_base"] = pd.NA

    merged = restore_column_from_index(merged, "code")
    merged = ensure_columns(
        merged,
        [
            "code",
            "date",
            "trust_net",
            "dealer_net",
            "total_shares",
            "foreign_ratio",
            "trust_shares_base",
            "dealer_shares_base",
        ],
    )
    merged = merged.dropna(subset=["code", "date"])
    if merged.empty:
        return merged

    merged["code"] = merged["code"].astype(str).str.strip()
    merged = merged.sort_values(["code", "date"]).reset_index(drop=True)

    # total_shares 先轉 float，避免後面 replace/where 中 extension array 爆炸
    merged["total_shares"] = pd.to_numeric(
        merged["total_shares"], errors="coerce"
    ).fillna(0.0)
    merged["trust_net"] = pd.to_numeric(merged["trust_net"], errors="coerce").fillna(0.0)
    merged["dealer_net"] = pd.to_numeric(merged["dealer_net"], errors="coerce").fillna(0.0)

    merged["trust_cum"] = merged.groupby("code")["trust_net"].cumsum()
    merged["dealer_cum"] = merged.groupby("code")["dealer_net"].cumsum()

    # baseline 轉數值，避免 NAType
    base_trust = pd.to_numeric(merged["trust_shares_base"], errors="coerce")
    base_dealer = pd.to_numeric(merged["dealer_shares_base"], errors="coerce")

    base_trust_ff = base_trust.groupby(merged["code"]).ffill().fillna(0.0)
    base_dealer_ff = base_dealer.groupby(merged["code"]).ffill().fillna(0.0)

    trust_cum_at_base = (
        merged["trust_cum"]
        .where(base_trust.notna())
        .groupby(merged["code"])
        .ffill()
        .fillna(0.0)
    )
    dealer_cum_at_base = (
        merged["dealer_cum"]
        .where(base_dealer.notna())
        .groupby(merged["code"])
        .ffill()
        .fillna(0.0)
    )

    merged["trust_shares_est"] = base_trust_ff + (merged["trust_cum"] - trust_cum_at_base)
    merged["dealer_shares_est"] = base_dealer_ff + (merged["dealer_cum"] - dealer_cum_at_base)

    # 若沒有任何 baseline，退化為純 cumsum 模型
    no_base_by_code = (
        (base_trust_ff == 0.0) & (base_dealer_ff == 0.0)
    ).groupby(merged["code"]).transform("all")
    merged.loc[no_base_by_code, "trust_shares_est"] = merged.loc[no_base_by_code, "trust_cum"]
    merged.loc[no_base_by_code, "dealer_shares_est"] = merged.loc[no_base_by_code, "dealer_cum"]

    # total_shares 已在前面轉成 float 並 fillna(0.0)
    denom = merged["total_shares"].astype("float64")
    valid = denom > 0.0

    # 先給預設 0，只有有總股數資訊時才算比重
    merged["trust_ratio_est"] = 0.0
    merged["dealer_ratio_est"] = 0.0

    merged.loc[valid, "trust_ratio_est"] = (
            merged.loc[valid, "trust_shares_est"].astype(float) / denom[valid] * 100.0
    )
    merged.loc[valid, "dealer_ratio_est"] = (
            merged.loc[valid, "dealer_shares_est"].astype(float) / denom[valid] * 100.0
    )

    merged["foreign_ratio"] = merged["foreign_ratio"].fillna(0.0)

    merged["three_inst_ratio_est"] = (
            merged["foreign_ratio"] + merged["trust_ratio_est"] + merged["dealer_ratio_est"]
    )
    return merged


def add_change_metrics(merged: pd.DataFrame, windows: list[int]) -> pd.DataFrame:
    merged = restore_column_from_index(merged.copy(), "code")
    merged = ensure_columns(merged, ["code", "date", "three_inst_ratio_est"])
    merged["date"] = pd.to_datetime(merged["date"], errors="coerce").dt.date
    merged = merged.dropna(subset=["date"])
    if merged.empty:
        for w in windows:
            merged[f"three_inst_ratio_change_{w}"] = pd.NA
        return merged

    merged["code"] = merged["code"].astype(str).str.strip()
    if (merged["code"] == "").all():
        for w in windows:
            merged[f"three_inst_ratio_change_{w}"] = pd.NA
        return merged

    merged["three_inst_ratio_est"] = pd.to_numeric(
        merged["three_inst_ratio_est"], errors="coerce"
    ).fillna(0.0)
    merged = merged.sort_values(["code", "date"]).reset_index(drop=True)
    by_code = merged.groupby("code")["three_inst_ratio_est"]

    for w in windows:
        col = f"three_inst_ratio_change_{w}"
        merged[col] = by_code.diff(periods=w)
    return merged


# ---------- export JSON ----------

def export_change_rankings(
    merged: pd.DataFrame, windows: list[int], out_dir: str = DOCS_DIR
):
    if merged.empty or "date" not in merged.columns:
        return
    latest_date = pd.to_datetime(merged["date"]).dt.date.max()
    if pd.isna(latest_date):
        return
    latest = merged[merged["date"] == latest_date].copy()

    import json
    os.makedirs(out_dir, exist_ok=True)

    for w in windows:
        col = f"three_inst_ratio_change_{w}"
        if col not in latest.columns:
            continue
        tmp = latest[latest[col].notna()].copy()
        if tmp.empty:
            continue

        up = tmp.sort_values(col, ascending=False).head(200)
        down = tmp.sort_values(col, ascending=True).head(200)

        def to_dict_list(df: pd.DataFrame):
            cols = ["code", "name", "market", "three_inst_ratio_est", col]
            records = []
            for _, row in df[cols].iterrows():
                records.append(
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "market": row["market"],
                        "three_inst_ratio": float(row["three_inst_ratio_est"]),
                        "change": float(row[col]),
                    }
                )
            return records

        up_json = to_dict_list(up)
        down_json = to_dict_list(down)

        up_path = os.path.join(out_dir, f"top_three_inst_change_{w}_up.json")
        down_path = os.path.join(out_dir, f"top_three_inst_change_{w}_down.json")

        with open(up_path, "w", encoding="utf-8") as f:
            json.dump(up_json, f, ensure_ascii=False, indent=2)
        with open(down_path, "w", encoding="utf-8") as f:
            json.dump(down_json, f, ensure_ascii=False, indent=2)

def clean_float(val, default: float = 0.0) -> float:
    """把 NaN / inf / 非數字 清成 safe float，避免寫出非法 JSON。"""
    if val is None:
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def export_timeseries_by_code(
    merged: pd.DataFrame,
    out_root: str = TIMESERIES_DIR,
    primary_window: int = 20,
):
    os.makedirs(out_root, exist_ok=True)

    merged = restore_column_from_index(merged.copy(), "code")
    merged = ensure_columns(merged, ["code", "date"])
    merged = merged.dropna(subset=["code", "date"])
    if merged.empty:
        return

    merged = merged.sort_values(["code", "date"])
    col_change = f"three_inst_ratio_change_{primary_window}"

    for code, g in merged.groupby("code"):
        records = []
        for _, row in g.iterrows():
            date_str = (
                row["date"].strftime("%Y-%m-%d")
                if not isinstance(row["date"], str)
                else row["date"]
            )

            rec = {
                "date": date_str,
                "code": row.get("code", code),
                "name": row.get("name", ""),
                "market": row.get("market", ""),
                "foreign_ratio": clean_float(row.get("foreign_ratio", 0.0)),
                "trust_ratio": clean_float(row.get("trust_ratio_est", 0.0)),
                "dealer_ratio": clean_float(row.get("dealer_ratio_est", 0.0)),
                "three_inst_ratio": clean_float(row.get("three_inst_ratio_est", 0.0)),
            }

            if col_change in g.columns:
                rec[col_change] = clean_float(row.get(col_change, 0.0))

            records.append(rec)

        out_path = os.path.join(out_root, f"{code}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)


# ---------- main orchestration ----------

def main():
    ensure_dirs()

    twse_flows_path = os.path.join(DATA_DIR, "twse_flows.csv")
    tpex_flows_path = os.path.join(DATA_DIR, "tpex_flows.csv")
    twse_foreign_path = os.path.join(DATA_DIR, "twse_foreign.csv")
    tpex_foreign_path = os.path.join(DATA_DIR, "tpex_foreign.csv")

    target_date = get_target_trade_date()
    print(f"[INFO] target trade date (Taipei): {target_date}")

    flow_days_twse = calc_fetch_dates(twse_flows_path, target_date)
    flow_days_tpex = calc_fetch_dates(tpex_flows_path, target_date)
    flow_days_twse_set = set(flow_days_twse)
    flow_days_tpex_set = set(flow_days_tpex)
    flow_days = sorted(flow_days_twse_set | flow_days_tpex_set)

    foreign_days_twse = calc_fetch_dates(twse_foreign_path, target_date)
    foreign_days_tpex = calc_fetch_dates(tpex_foreign_path, target_date)
    foreign_days_twse_set = set(foreign_days_twse)
    foreign_days_tpex_set = set(foreign_days_tpex)
    foreign_days = sorted(foreign_days_twse_set | foreign_days_tpex_set)

    if flow_days:
        print(
            f"[INFO] flows fetch plan: {flow_days[0]} -> {flow_days[-1]} "
            f"(TWSE={len(flow_days_twse_set)}, TPEX={len(flow_days_tpex_set)}, union={len(flow_days)})"
        )
    else:
        print("[INFO] flows fetch plan: no missing/new trade date.")

    if foreign_days:
        print(
            f"[INFO] foreign fetch plan: {foreign_days[0]} -> {foreign_days[-1]} "
            f"(TWSE={len(foreign_days_twse_set)}, TPEX={len(foreign_days_tpex_set)}, union={len(foreign_days)})"
        )
    else:
        print("[INFO] foreign fetch plan: no missing/new trade date.")

    # --- update flows ---
    flows_new_list = []
    for d in flow_days:
        print(f"[INFO] fetching flows for {d} ...")
        if d in flow_days_twse_set:
            try:
                twse_df = fetch_twse_t86(d)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] TWSE T86 fetch failed at {d}: {e}")
                twse_df = empty_flows_df()
            if not twse_df.empty:
                flows_new_list.append(twse_df)

        if d in flow_days_tpex_set:
            try:
                tpex_df = fetch_tpex_flows(d)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] TPEX flows fetch failed at {d}: {e}")
                tpex_df = empty_flows_df()
            if not tpex_df.empty:
                flows_new_list.append(tpex_df)

    if flows_new_list:
        flows_new = pd.concat(flows_new_list, ignore_index=True)
        twse_new = flows_new[flows_new["market"] == "TWSE"].copy()
        tpex_new = flows_new[flows_new["market"] == "TPEX"].copy()

        if not twse_new.empty:
            twse_flows_all = append_history(
                twse_new, twse_flows_path, ["date", "code", "market"]
            )
        else:
            twse_flows_all = (
                pd.read_csv(twse_flows_path) if os.path.exists(twse_flows_path) else empty_flows_df()
            )

        if not tpex_new.empty:
            tpex_flows_all = append_history(
                tpex_new, tpex_flows_path, ["date", "code", "market"]
            )
        else:
            tpex_flows_all = (
                pd.read_csv(tpex_flows_path) if os.path.exists(tpex_flows_path) else empty_flows_df()
            )
    else:
        print("[INFO] no new flows fetched.")
        twse_flows_all = (
            pd.read_csv(twse_flows_path) if os.path.exists(twse_flows_path) else empty_flows_df()
        )
        tpex_flows_all = (
            pd.read_csv(tpex_flows_path) if os.path.exists(tpex_flows_path) else empty_flows_df()
        )

    # --- update foreign holdings ---
    foreign_new_list_twse = []
    foreign_new_list_tpex = []

    for d in foreign_days:
        print(f"[INFO] fetching foreign holdings for {d} ...")
        if d in foreign_days_twse_set:
            try:
                twse_f = fetch_twse_mi_qfiis(d)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] TWSE MI_QFIIS fetch failed at {d}: {e}")
                twse_f = empty_foreign_df()
            if not twse_f.empty:
                foreign_new_list_twse.append(twse_f)

        if d in foreign_days_tpex_set:
            try:
                tpex_f = fetch_tpex_qfii(d)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] TPEX QFII fetch failed at {d}: {e}")
                tpex_f = empty_foreign_df()
            if not tpex_f.empty:
                foreign_new_list_tpex.append(tpex_f)

    if foreign_new_list_twse:
        twse_foreign_new = pd.concat(foreign_new_list_twse, ignore_index=True)
        twse_foreign_all = append_history(
            twse_foreign_new, twse_foreign_path, ["date", "code", "market"]
        )
    else:
        twse_foreign_all = (
            pd.read_csv(twse_foreign_path) if os.path.exists(twse_foreign_path) else empty_foreign_df()
        )

    if foreign_new_list_tpex:
        tpex_foreign_new = pd.concat(foreign_new_list_tpex, ignore_index=True)
        tpex_foreign_all = append_history(
            tpex_foreign_new, tpex_foreign_path, ["date", "code", "market"]
        )
    else:
        tpex_foreign_all = (
            pd.read_csv(tpex_foreign_path) if os.path.exists(tpex_foreign_path) else empty_foreign_df()
        )

    twse_flows_all = ensure_columns(restore_column_from_index(twse_flows_all, "code"), FLOW_COLUMNS)
    tpex_flows_all = ensure_columns(restore_column_from_index(tpex_flows_all, "code"), FLOW_COLUMNS)
    twse_foreign_all = ensure_columns(restore_column_from_index(twse_foreign_all, "code"), FOREIGN_COLUMNS)
    tpex_foreign_all = ensure_columns(restore_column_from_index(tpex_foreign_all, "code"), FOREIGN_COLUMNS)

    if twse_flows_all.empty and tpex_flows_all.empty:
        print("[WARN] no flows history available, aborting model/export.")
        return

    flows_all = pd.concat(
        [df for df in (twse_flows_all, tpex_flows_all) if not df.empty],
        ignore_index=True,
    )
    if twse_foreign_all.empty and tpex_foreign_all.empty:
        print("[WARN] no foreign holdings history available, aborting model/export.")
        return

    foreign_master = build_foreign_master(twse_foreign_all, tpex_foreign_all)
    if foreign_master.empty:
        print("[WARN] foreign_master is empty, aborting model/export.")
        return

    # baseline 校正
    if os.path.exists(INST_BASELINE_PATH):
        baseline_df = pd.read_csv(INST_BASELINE_PATH, comment="#")
        if baseline_df.empty:
            baseline_df = None
    else:
        baseline_df = None

    merged = build_estimated_holdings(flows_all, foreign_master, baseline=baseline_df)
    merged = add_change_metrics(merged, windows=WINDOWS)

    export_change_rankings(merged, windows=WINDOWS, out_dir=DOCS_DIR)
    export_timeseries_by_code(merged, out_root=TIMESERIES_DIR, primary_window=20)

    print("[INFO] update_all.py completed successfully.")


if __name__ == "__main__":
    main()
