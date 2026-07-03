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
import time
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

# 三大法人「持股比重變化」與「買賣超」排行共用的檢查點（視窗）。
# 5 / 10 / 30 日可對照富邦 zgk 頁面驗證，20 日為本程式自行算出。
WINDOWS = [5, 10, 20, 30]
FLOW_COLUMNS = ["date", "code", "name", "foreign_net", "trust_net", "dealer_net", "market"]
FOREIGN_COLUMNS = ["date", "code", "name", "market", "total_shares", "foreign_shares", "foreign_ratio"]
FLOW_NUMERIC_COLUMNS = ["foreign_net", "trust_net", "dealer_net"]
FOREIGN_NUMERIC_COLUMNS = ["total_shares", "foreign_shares", "foreign_ratio"]
FLOW_MIN_ROWS_BY_MARKET = {"TWSE": 1000, "TPEX": 700}
INIT_FETCH_DAYS = 60
BACKFILL_LOOKBACK_DAYS = 120

# TPEX 新版三大法人買賣明細 JSON API。
# 舊版 /web/stock/3insti/daily_trade/3itrade_hedge_result.php 已於 2025/12 失效，
# 自該日起上櫃三大法人買賣超無法更新；改用此端點。
TPEX_DAILY_TRADE_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"


# ---------- generic helpers ----------

def http_get_bytes(url: str, params: Optional[dict] = None, timeout: int = 25) -> bytes:
    """GET 回傳 response bytes；requests 失敗時退回 curl。

    某些環境下 Python 的 SSL stack 對 www.tpex.org.tw 會丟 SSLError（握手失
    敗），但系統 curl 可正常連線。為了讓資料在本機與 CI 都能穩定抓取，這裡在
    requests 失敗時改用 curl 作為後援。
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, params=params, timeout=timeout, headers=headers)
        return resp.content
    except Exception as exc:  # noqa: BLE001  (SSLError / ConnectionError 等)
        import subprocess
        import urllib.parse

        full = url + (("?" + urllib.parse.urlencode(params)) if params else "")
        try:
            out = subprocess.run(
                ["curl", "-sS", "--max-time", str(timeout),
                 "-H", "User-Agent: Mozilla/5.0", full],
                capture_output=True, check=True,
            )
            return out.stdout
        except Exception:
            raise exc


def ensure_dirs():
    for p in (DATA_DIR, DOCS_DIR, TIMESERIES_DIR):
        os.makedirs(p, exist_ok=True)


def get_taipei_today() -> date:
    tz = ZoneInfo("Asia/Taipei")
    return datetime.now(tz).date()


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=Sat, 6=Sun


def get_target_trade_date() -> date:
    """用台北時間的「今天」，週末往前推到最近一個平日。

    GitHub Actions 在台北晚間執行，TWSE/TPEX 當日三大法人買賣超此時已發布；
    若仍抓昨天，資料會天然落後一天，且連續空回應時不容易被注意到。
    """
    target = get_taipei_today()
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


def normalize_table(
    df: pd.DataFrame,
    columns: list[str],
    numeric_columns: list[str],
) -> pd.DataFrame:
    """Return a CSV/JSON-safe table with stable columns and no numeric NA."""
    out = restore_column_from_index(df.copy(), "code")
    out = ensure_columns(out, columns)
    out = out[columns].copy()

    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    if "code" in out.columns:
        out["code"] = out["code"].astype(str).str.strip()
        out.loc[out["code"].isin(["", "nan", "None", "<NA>"]), "code"] = pd.NA
        valid_code = out["code"].astype(str).str.match(r"^\d{4,6}[A-Z]*$")
        not_dummy_code = ~out["code"].astype(str).str.fullmatch(r"0+")
        out = out[valid_code & not_dummy_code].copy()
    for col in ("name", "market"):
        if col in out.columns:
            out[col] = out[col].fillna("").astype(str).str.strip()
    if {"code", "name"}.issubset(out.columns):
        missing_name = out["name"].isin(["", "nan", "None", "<NA>"])
        out.loc[missing_name, "name"] = out.loc[missing_name, "code"]

    for col in numeric_columns:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)

    out = out.dropna(subset=[c for c in ("date", "code") if c in out.columns])
    return out


def normalize_flow_table(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_table(df, FLOW_COLUMNS, FLOW_NUMERIC_COLUMNS)


def normalize_foreign_table(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_table(df, FOREIGN_COLUMNS, FOREIGN_NUMERIC_COLUMNS)


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

def flow_corrupt_mask(out: pd.DataFrame, total: pd.Series) -> pd.Series:
    """標記「官方三大法人合計欄位有值，但 外資+投信+自營 ≠ 該合計」的列。

    這類列通常是回應被截斷或單列解析異常（某成分被讀成 0），會讓買賣超算錯。
    官方合計為 0（少數列合計欄留白）者不在檢查範圍——其三個成分仍可信。
    """
    s = (
        pd.to_numeric(out["foreign_net"], errors="coerce").fillna(0)
        + pd.to_numeric(out["trust_net"], errors="coerce").fillna(0)
        + pd.to_numeric(out["dealer_net"], errors="coerce").fillna(0)
    )
    total = pd.to_numeric(total, errors="coerce").fillna(0)
    return (total != 0) & (s != total)


def _roc_title_date(text: str) -> Optional[date]:
    """從 TWSE/TPEX 報表標題抓出『115年06月04日』形式的日期（民國轉西元）。"""
    import re

    m = re.search(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text[:300])
    if not m:
        return None
    try:
        return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_twse_t86(csv_text: str, trade_date: date):
    """解析 T86 CSV，回傳 (out_df, corrupt_mask, status)。

    status: "ok" | "empty"(該日無資料) | "wrongdate"(回應日期 != 請求日期，需重抓)。
    """
    # 防呆：rapid bulk 抓取時 TWSE 偶爾會回「別天」的資料，且該回應自洽（合計對得上），
    # 單純比對 sum==total 抓不到，必須比對報表標題日期。
    title_date = _roc_title_date(csv_text)
    if title_date is not None and title_date != trade_date:
        return empty_flows_df(), None, "wrongdate"

    df = pd.read_csv(StringIO(csv_text), header=1)
    df = df.dropna(how="all", axis=0).dropna(how="all", axis=1)
    df = normalize_columns(df)
    if df.empty or len(df.columns) == 0:
        return empty_flows_df(), None, "empty"

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
    col_dealer_net = find_col_any(df, ["自營商買賣超股數合計", "自營商買賣超股數"])
    col_total_net = find_col_any(df, ["三大法人買賣超股數"], required=False)

    df["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "")
    df["code"] = df["code"].str.strip().str.zfill(4)
    df["name"] = df[name_col].astype(str).str.strip()

    foreign_ex = numeric_series(df[col_foreign_ex_net])
    foreign_self = numeric_series(df[col_foreign_self_net])

    out = pd.DataFrame(
        {
            "date": trade_date,
            "code": df["code"],
            "name": df["name"],
            "foreign_net": (foreign_ex + foreign_self),
            "trust_net": numeric_series(df[col_trust_net]),
            "dealer_net": numeric_series(df[col_dealer_net]),
            "market": "TWSE",
        }
    )

    if col_total_net is not None:
        corrupt = flow_corrupt_mask(out, numeric_series(df[col_total_net]))
    else:
        corrupt = pd.Series(False, index=out.index)

    mask = out["code"].str.match(r"^\d{4,6}[A-Z]*$")
    return out[mask][FLOW_COLUMNS], corrupt[mask], "ok"


def fetch_twse_t86(trade_date: date, attempts: int = 3) -> pd.DataFrame:
    """三大法人買賣超統計資訊 (T86) for TWSE.

    - /fund/T86 是 Big5 編碼，必須用 cp950 解碼。
    - 以「外資+投信+自營 == 官方三大法人合計」做完整性檢查；若有列對不起來
      （多半是回應被截斷／單列解析異常），重抓最多 attempts 次；仍失敗則捨棄
      該些壞列並警告，避免把錯誤的買賣超寫進歷史。
    """
    url = "https://www.twse.com.tw/fund/T86"
    params = {"response": "csv", "date": trade_date.strftime("%Y%m%d"), "selectType": "ALLBUT0999"}

    last_out = empty_flows_df()
    last_corrupt = None
    for attempt in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=20)
            out, corrupt, status = _parse_twse_t86(
                resp.content.decode("cp950", errors="ignore"), trade_date
            )
        except Exception as e:  # noqa: BLE001
            if attempt == attempts - 1:
                raise
            time.sleep(0.8)
            continue

        if status == "wrongdate":
            if attempt < attempts - 1:
                print(f"[WARN] TWSE T86 {trade_date}: 回應為他日資料，重抓 ...")
                time.sleep(1.0)
                continue
            print(f"[WARN] TWSE T86 {trade_date}: 重抓後仍非當日資料，捨棄")
            return empty_flows_df()
        if status == "empty":
            return out  # 該日無資料（假日等），重抓也沒用
        if corrupt is None or not bool(corrupt.any()):
            return out  # 全部對得起來
        last_out, last_corrupt = out, corrupt
        if attempt < attempts - 1:
            print(f"[WARN] TWSE T86 {trade_date}: {int(corrupt.sum())} 列合計對不上，重抓 ...")
            time.sleep(0.8)

    bad = int(last_corrupt.sum()) if last_corrupt is not None else 0
    print(f"[WARN] TWSE T86 {trade_date}: 重抓後仍有 {bad} 列異常，捨棄該些列")
    return last_out[~last_corrupt.values] if last_corrupt is not None else last_out


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

    mask = out["code"].str.match(r"^\d{4,6}[A-Z]*$")
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
    """上櫃股票三大法人買賣明細（TPEX 新版 JSON API）。

    回傳 24 欄，三個一組（買進股數 / 賣出股數 / 買賣超股數）：
      [0] 代號 [1] 名稱
      [2..4]   外資及陸資(不含外資自營商)
      [5..7]   外資自營商
      [8..10]  外資及陸資合計      -> foreign_net (idx 10)
      [11..13] 投信               -> trust_net   (idx 13)
      [14..16] 自營商(自行買賣)
      [17..19] 自營商(避險)
      [20..22] 自營商合計          -> dealer_net  (idx 22)
      [23]     三大法人買賣超股數合計（驗證用）
    經驗證：foreign_net + trust_net + dealer_net == idx23。
    """
    params = {
        "type": "Daily",
        "sect": "EW",
        "date": roc_date(trade_date),
        "id": "",
        "response": "json",
    }
    try:
        content = http_get_bytes(TPEX_DAILY_TRADE_URL, params=params)
        data = json.loads(content.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return empty_flows_df()

    tables = data.get("tables") or []
    if not tables:
        return empty_flows_df()
    # 防呆：回應日期需與請求日期相符，避免抓到他日資料。
    resp_date = str(tables[0].get("date") or data.get("date") or "").strip()
    if resp_date and resp_date != roc_date(trade_date):
        print(f"[WARN] TPEX flows {trade_date}: 回應日期 {resp_date} 非當日，捨棄")
        return empty_flows_df()
    rows = tables[0].get("data") or []
    if not rows:
        return empty_flows_df()

    records = []
    for r in rows:
        if not r or len(r) < 24:
            continue
        records.append(
            {
                "code": str(r[0]).strip().strip("=").strip('"'),
                "name": str(r[1]).strip(),
                "foreign_raw": r[10],
                "trust_raw": r[13],
                "dealer_raw": r[22],
                "total_raw": r[23],  # 三大法人買賣超股數合計（驗證用）
            }
        )
    if not records:
        return empty_flows_df()

    raw = pd.DataFrame(records)
    out = pd.DataFrame(
        {
            "date": trade_date,
            "code": raw["code"].astype(str).str.strip().str.zfill(4),
            "name": raw["name"],
            "foreign_net": numeric_series(raw["foreign_raw"]),
            "trust_net": numeric_series(raw["trust_raw"]),
            "dealer_net": numeric_series(raw["dealer_raw"]),
            "market": "TPEX",
        }
    )

    # 完整性檢查：外資+投信+自營 應等於官方合計；對不上的列捨棄並警告。
    corrupt = flow_corrupt_mask(out, numeric_series(raw["total_raw"]))
    if bool(corrupt.any()):
        print(f"[WARN] TPEX flows {trade_date}: {int(corrupt.sum())} 列合計對不上，捨棄該些列")
        out = out[~corrupt]

    mask = out["code"].str.match(r"^\d{4,6}[A-Z]*$")
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

    mask = out["code"].str.match(r"^\d{4,6}[A-Z]*$")
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

def append_history(
    df_new: pd.DataFrame,
    path: str,
    key_cols: list[str],
    columns: Optional[list[str]] = None,
    numeric_columns: Optional[list[str]] = None,
) -> pd.DataFrame:
    columns = columns or key_cols
    numeric_columns = numeric_columns or []
    if df_new.empty:
        if os.path.exists(path):
            return normalize_table(pd.read_csv(path), columns, numeric_columns)
        return normalize_table(df_new.copy(), columns, numeric_columns)

    df_new = normalize_table(df_new, columns, numeric_columns)

    if os.path.exists(path):
        df_old = normalize_table(pd.read_csv(path), columns, numeric_columns)
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_all = df_new

    # keep='last'：重抓同一 (date, code, market) 時，讓「新值」覆蓋舊值，
    # 以便修正過去抓錯的資料（例如 dealer_net 欄位解析錯誤）。日常更新不會重抓
    # 既有日期，因此此設定對日常流程無影響。
    df_all = df_all.drop_duplicates(subset=key_cols, keep="last").sort_values(["date", "code"])
    df_all = normalize_table(df_all, columns, numeric_columns)
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
    merged = merged.sort_values(["code", "market", "date"]).reset_index(drop=True)

    # 外資持股資料有時比買賣超晚發布；若同一股票/市場當日缺持股，沿用最近有效值，
    # 避免最新交易日的 foreign_ratio / total_shares 被空值清成 0 造成 ranking 失真。
    for col in ("total_shares", "foreign_ratio"):
        merged[col] = pd.to_numeric(merged[col], errors="coerce")
        merged[col] = merged.groupby(["code", "market"])[col].ffill()

    # total_shares 先轉 float，避免後面 replace/where 中 extension array 爆炸
    merged["total_shares"] = merged["total_shares"].fillna(0.0)
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

    # 以「全市場交易日軸」對齊 w 日變化，而非用 DataFrame 列位移（diff(periods=w)
    # 會把列數當天數，對有缺漏交易日的個股算錯，且結果會隨「哪些日成功抓到」而變）。
    # 作法：把比重攤成 code × date 寬表，補上完整交易日軸並在日期方向 ffill（持股
    # 在沒交易的日子沿用前值），再沿日期軸位移 w 個交易日相減。
    wide = merged.pivot_table(
        index="code", columns="date", values="three_inst_ratio_est", aggfunc="last"
    ).sort_index(axis=1)
    wide_ff = wide.ffill(axis=1)

    for w in windows:
        col = f"three_inst_ratio_change_{w}"
        diff_w = wide_ff - wide_ff.shift(periods=w, axis=1)
        long_w = (
            diff_w.stack()
            .dropna()
            .rename(col)
            .reset_index()  # columns: code, date, col
        )
        merged = merged.merge(long_w, on=["code", "date"], how="left")
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

def export_netbuy_rankings(
    flows_all: pd.DataFrame,
    windows: list[int],
    out_dir: str = DOCS_DIR,
    top_n: int = 200,
):
    """三大法人「N 日累計買賣超」排行。

    直接用官方每日買賣超（外資合計 / 投信 / 自營商合計）在「最近 N 個交易日」加總，
    不依賴外資持股，也不做任何估計，因此可直接對照富邦 zgk 頁面（C=5/10/30）驗證。

    單位：張（= 股 / 1000，四捨五入），與富邦頁面一致。
    每個視窗輸出買超(up) / 賣超(down) 各 top_n 檔，欄位含外資、投信、自營、合計。
    """
    if flows_all is None or flows_all.empty:
        return

    df = flows_all.copy()
    df = restore_column_from_index(df, "code")
    df = ensure_columns(df, FLOW_COLUMNS)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["code"] = df["code"].astype(str).str.strip()
    df = df.dropna(subset=["date", "code"])
    df = df[df["code"] != ""]
    if df.empty:
        return
    for c in ("foreign_net", "trust_net", "dealer_net"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    # 每檔股票最新一筆的名稱 / 市場
    meta = (
        df.sort_values("date")
        .groupby("code")
        .agg(name=("name", "last"), market=("market", "last"))
        .reset_index()
    )

    all_dates = sorted(df["date"].unique())
    latest_date = all_dates[-1]
    updated = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    os.makedirs(out_dir, exist_ok=True)

    def to_lots(value) -> int:
        return int(round(float(value) / 1000.0))

    def to_records(sorted_df: pd.DataFrame) -> list[dict]:
        records = []
        for rank, (_, row) in enumerate(sorted_df.iterrows(), start=1):
            records.append(
                {
                    "rank": rank,
                    "code": row["code"],
                    "name": row.get("name", "") or "",
                    "market": row.get("market", "") or "",
                    "foreign": to_lots(row["foreign"]),
                    "trust": to_lots(row["trust"]),
                    "dealer": to_lots(row["dealer"]),
                    "total": to_lots(row["total"]),
                }
            )
        return records

    for w in windows:
        win_dates = all_dates[-w:]  # 最近 w 個交易日
        sub = df[df["date"].isin(set(win_dates))]
        if sub.empty:
            continue
        agg = (
            sub.groupby("code")
            .agg(
                foreign=("foreign_net", "sum"),
                trust=("trust_net", "sum"),
                dealer=("dealer_net", "sum"),
            )
            .reset_index()
        )
        agg["total"] = agg["foreign"] + agg["trust"] + agg["dealer"]
        agg = agg.merge(meta, on="code", how="left")

        # 穩定排序：以合計為主鍵，code 為次鍵，避免同值列在不同 pandas 版本間飄動。
        up = agg.sort_values(["total", "code"], ascending=[False, True]).head(top_n)
        down = agg.sort_values(["total", "code"], ascending=[True, True]).head(top_n)

        for side, ranked in (("up", up), ("down", down)):
            payload = {
                "updated": updated,
                "metric": "net_buy_sell",
                "window": w,
                "unit": "張",
                "side": side,
                "trading_days": len(win_dates),
                "date_range": {
                    "start": win_dates[0].strftime("%Y-%m-%d"),
                    "end": latest_date.strftime("%Y-%m-%d"),
                },
                "data": to_records(ranked),
            }
            path = os.path.join(out_dir, f"top_three_inst_netbuy_{w}_{side}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)


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


# ---------- validation ----------

def validate_no_nulls(df: pd.DataFrame, columns: list[str], label: str):
    missing_cols = [col for col in columns if col not in df.columns]
    if missing_cols:
        raise RuntimeError(f"{label}: missing columns {missing_cols}")

    null_counts = df[columns].isna().sum()
    bad = {col: int(count) for col, count in null_counts.items() if int(count) > 0}
    if bad:
        raise RuntimeError(f"{label}: null values found {bad}")


def validate_flow_history(df: pd.DataFrame, market: str, target_date: date):
    label = f"{market} flows"
    df = normalize_flow_table(df)
    if df.empty:
        raise RuntimeError(f"{label}: empty history")

    validate_no_nulls(df, FLOW_COLUMNS, label)
    latest = pd.to_datetime(df["date"], errors="coerce").dt.date.max()
    if latest != target_date:
        raise RuntimeError(f"{label}: latest date {latest} != target date {target_date}")

    today_rows = df[df["date"] == target_date]
    min_rows = FLOW_MIN_ROWS_BY_MARKET.get(market, 1)
    if len(today_rows) < min_rows:
        raise RuntimeError(
            f"{label}: only {len(today_rows)} rows for {target_date}, expected >= {min_rows}"
        )


def validate_foreign_history(df: pd.DataFrame, market: str):
    label = f"{market} foreign holdings"
    df = normalize_foreign_table(df)
    if df.empty:
        raise RuntimeError(f"{label}: empty history")
    validate_no_nulls(df, FOREIGN_COLUMNS, label)


def validate_json_file(path: str):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if "NaN" in text or "Infinity" in text or "-Infinity" in text:
        raise RuntimeError(f"{path}: invalid JSON numeric token")
    json.loads(text)


def validate_exports(out_dir: str, windows: list[int]):
    required = []
    for w in windows:
        required.extend(
            [
                os.path.join(out_dir, f"top_three_inst_netbuy_{w}_up.json"),
                os.path.join(out_dir, f"top_three_inst_netbuy_{w}_down.json"),
                os.path.join(out_dir, f"top_three_inst_change_{w}_up.json"),
                os.path.join(out_dir, f"top_three_inst_change_{w}_down.json"),
            ]
        )

    missing = [path for path in required if not os.path.exists(path)]
    if missing:
        raise RuntimeError(f"missing export files: {missing}")

    for path in required:
        validate_json_file(path)


def validate_daily_update(
    twse_flows_all: pd.DataFrame,
    tpex_flows_all: pd.DataFrame,
    twse_foreign_all: pd.DataFrame,
    tpex_foreign_all: pd.DataFrame,
    target_date: date,
):
    validate_flow_history(twse_flows_all, "TWSE", target_date)
    validate_flow_history(tpex_flows_all, "TPEX", target_date)
    validate_foreign_history(twse_foreign_all, "TWSE")
    validate_foreign_history(tpex_foreign_all, "TPEX")
    validate_exports(DOCS_DIR, WINDOWS)


def write_history_table(df: pd.DataFrame, path: str, columns: list[str]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = df[columns].copy()
    df.to_csv(path, index=False, date_format="%Y-%m-%d")


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
                twse_new,
                twse_flows_path,
                ["date", "code", "market"],
                FLOW_COLUMNS,
                FLOW_NUMERIC_COLUMNS,
            )
        else:
            twse_flows_all = normalize_flow_table(
                pd.read_csv(twse_flows_path) if os.path.exists(twse_flows_path) else empty_flows_df()
            )

        if not tpex_new.empty:
            tpex_flows_all = append_history(
                tpex_new,
                tpex_flows_path,
                ["date", "code", "market"],
                FLOW_COLUMNS,
                FLOW_NUMERIC_COLUMNS,
            )
        else:
            tpex_flows_all = normalize_flow_table(
                pd.read_csv(tpex_flows_path) if os.path.exists(tpex_flows_path) else empty_flows_df()
            )
    else:
        print("[INFO] no new flows fetched.")
        twse_flows_all = normalize_flow_table(
            pd.read_csv(twse_flows_path) if os.path.exists(twse_flows_path) else empty_flows_df()
        )
        tpex_flows_all = normalize_flow_table(
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
            twse_foreign_new,
            twse_foreign_path,
            ["date", "code", "market"],
            FOREIGN_COLUMNS,
            FOREIGN_NUMERIC_COLUMNS,
        )
    else:
        twse_foreign_all = normalize_foreign_table(
            pd.read_csv(twse_foreign_path) if os.path.exists(twse_foreign_path) else empty_foreign_df()
        )

    if foreign_new_list_tpex:
        tpex_foreign_new = pd.concat(foreign_new_list_tpex, ignore_index=True)
        tpex_foreign_all = append_history(
            tpex_foreign_new,
            tpex_foreign_path,
            ["date", "code", "market"],
            FOREIGN_COLUMNS,
            FOREIGN_NUMERIC_COLUMNS,
        )
    else:
        tpex_foreign_all = normalize_foreign_table(
            pd.read_csv(tpex_foreign_path) if os.path.exists(tpex_foreign_path) else empty_foreign_df()
        )

    twse_flows_all = normalize_flow_table(twse_flows_all)
    tpex_flows_all = normalize_flow_table(tpex_flows_all)
    twse_foreign_all = normalize_foreign_table(twse_foreign_all)
    tpex_foreign_all = normalize_foreign_table(tpex_foreign_all)

    write_history_table(twse_flows_all, twse_flows_path, FLOW_COLUMNS)
    write_history_table(tpex_flows_all, tpex_flows_path, FLOW_COLUMNS)
    write_history_table(twse_foreign_all, twse_foreign_path, FOREIGN_COLUMNS)
    write_history_table(tpex_foreign_all, tpex_foreign_path, FOREIGN_COLUMNS)

    if twse_flows_all.empty and tpex_flows_all.empty:
        print("[WARN] no flows history available, aborting model/export.")
        return

    flows_all = pd.concat(
        [df for df in (twse_flows_all, tpex_flows_all) if not df.empty],
        ignore_index=True,
    )

    # 三大法人 N 日買賣超排行：純官方日買賣超加總，不依賴外資持股，
    # 即使外資持股當日抓取失敗也照樣產出，並可對照富邦驗證。
    export_netbuy_rankings(flows_all, windows=WINDOWS, out_dir=DOCS_DIR)
    print("[INFO] exported three-inst net buy/sell rankings:", WINDOWS)

    if twse_foreign_all.empty and tpex_foreign_all.empty:
        print("[WARN] no foreign holdings history available, aborting holdings model/export.")
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
    validate_daily_update(
        twse_flows_all,
        tpex_flows_all,
        twse_foreign_all,
        tpex_foreign_all,
        target_date,
    )

    print("[INFO] update_all.py completed successfully.")


if __name__ == "__main__":
    main()
