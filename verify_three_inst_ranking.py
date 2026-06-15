# -*- coding: utf-8 -*-
"""對照富邦 zgk 頁面，驗證本程式算出的三大法人 N 日買賣超是否正確。

資料來源（富邦 e-Broker DJ）：
    https://fubon-ebrokerdj.fbs.com.tw/z/zg/zgk.djhtm?A=D{inst}&B={market}&C={window}
參數：
    A 第二碼 = 法人別：F=外資、D=投信、B=自營商
    B        = 市場：0=上市(TWSE)、1=上櫃(TPEX)
    C        = 視窗天數：1 / 5 / 10 / 30（富邦無 20 日，故 20 日由本程式自算、不在此驗證）

對每個 (法人, 市場, 視窗)：
    1. 抓富邦排行（買超 + 賣超）得到 {股票代碼: 超張數(張)}；
    2. 由 data/twse_flows.csv / tpex_flows.csv 算出「最近 N 個交易日」該法人累計買賣超(張)；
    3. 逐檔比對，超過容差(--tol 張)即列為不符，並輸出通過率。

用法：
    python verify_three_inst_ranking.py
    python verify_three_inst_ranking.py --windows 5 10 30 --tol 1
"""
import argparse
import os
import re
import sys

import pandas as pd

from update_all import http_get_bytes

DATA_DIR = "data"
ZGK_URL = "https://fubon-ebrokerdj.fbs.com.tw/z/zg/zgk.djhtm"

# 法人別 -> (A 第二碼, 本地欄位)
INST = {
    "外資": ("F", "foreign_net"),
    "投信": ("D", "trust_net"),
    "自營商": ("B", "dealer_net"),
}
# 市場 -> (B 參數, 本地 flows 檔)
MARKET = {
    "上市": ("0", os.path.join(DATA_DIR, "twse_flows.csv")),
    "上櫃": ("1", os.path.join(DATA_DIR, "tpex_flows.csv")),
}

_CODE_NAME = re.compile(r"^(\d{4,6}[A-Z]?)(\D.+)$")
_INT = re.compile(r"^-?[\d,]+$")
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def fetch_fubon_ranking(inst_code: str, market_code: str, window: int) -> dict:
    """回傳 {code: 超張數(張)}；買超為正、賣超為負。"""
    params = {"A": "D" + inst_code, "B": market_code, "C": str(window)}
    content = http_get_bytes(ZGK_URL, params=params)
    txt = content.decode("big5", "ignore")
    cells = [_TAG.sub("", c).replace("\xa0", " ").strip() for c in _TD.findall(txt)]

    result = {}
    for i in range(len(cells) - 1):
        m = _CODE_NAME.match(cells[i])
        if not m:
            continue
        nxt = cells[i + 1]
        if not _INT.match(nxt):
            continue
        code = m.group(1)
        try:
            lots = int(nxt.replace(",", ""))
        except ValueError:
            continue
        # 同一代碼理論上只會出現在買超或賣超一側
        result[code] = lots
    return result


def our_ranking(flows_path: str, field: str, window: int):
    """回傳 ({code: 累計買賣超(張)}, date_range_str)。"""
    if not os.path.exists(flows_path):
        return {}, "(no file)"
    df = pd.read_csv(flows_path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["code"] = df["code"].astype(str).str.strip()
    df = df.dropna(subset=["date", "code"])
    df[field] = pd.to_numeric(df[field], errors="coerce").fillna(0.0)

    all_dates = sorted(df["date"].unique())
    win = all_dates[-window:]
    sub = df[df["date"].isin(set(win))]
    agg = sub.groupby("code")[field].sum()
    lots = {c: int(round(v / 1000.0)) for c, v in agg.items()}
    rng = f"{win[0]} ~ {win[-1]} ({len(win)}d)"
    return lots, rng


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, nargs="+", default=[5, 10, 30])
    ap.add_argument("--tol", type=int, default=1, help="容差（張），預設 1（四捨五入誤差）")
    ap.add_argument("--show", type=int, default=8, help="每組最多列出幾筆不符")
    args = ap.parse_args()

    print("=" * 70)
    print("三大法人 N 日買賣超 — 對照富邦 zgk 驗證")
    print("=" * 70)

    overall_ok = True
    for window in args.windows:
        if window not in (1, 5, 10, 30):
            print(f"\n[SKIP] {window} 日：富邦 zgk 無此視窗（20 日為本程式自算，無對照來源）")
            continue
        for mname, (mcode, fpath) in MARKET.items():
            for iname, (icode, field) in INST.items():
                try:
                    fubon = fetch_fubon_ranking(icode, mcode, window)
                except Exception as e:  # noqa: BLE001
                    print(f"\n[{window}d {mname} {iname}] 富邦抓取失敗：{e}")
                    overall_ok = False
                    continue
                ours, rng = our_ranking(fpath, field, window)
                if not fubon:
                    print(f"\n[{window}d {mname} {iname}] 富邦無資料")
                    continue

                checked = matched = 0
                mism = []
                for code, fval in fubon.items():
                    if code not in ours:
                        mism.append((code, fval, None))
                        continue
                    checked += 1
                    if abs(ours[code] - fval) <= args.tol:
                        matched += 1
                    else:
                        mism.append((code, fval, ours[code]))

                rate = (matched / checked * 100) if checked else 0.0
                status = "OK " if (checked and matched == checked) else "DIFF"
                if status != "OK ":
                    overall_ok = False
                print(
                    f"\n[{status}] {window}d {mname} {iname}  "
                    f"富邦{len(fubon)}檔 比對{checked} 相符{matched} ({rate:.0f}%)  本地視窗:{rng}"
                )
                for code, fval, oval in mism[: args.show]:
                    o = "缺" if oval is None else f"{oval:+,}"
                    print(f"     {code}: 富邦={fval:+,} 張  本地={o} 張")
                if len(mism) > args.show:
                    print(f"     ...（另有 {len(mism) - args.show} 筆）")

    print("\n" + "=" * 70)
    print("結論：", "全部相符 ✅" if overall_ok else "有差異，請檢查上方 DIFF 項 ⚠️")
    print("=" * 70)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
