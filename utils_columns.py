# -*- coding: utf-8 -*-
"""Utility helpers for locating TWSE/TPEX column names robustly."""
from typing import Iterable
import pandas as pd


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize DataFrame column names.

    - 若為 MultiIndex，將各層級以字串串接成單一欄位名稱。
    - 全部 strip 前後空白。
    """
    df = df.copy()
    cols = df.columns
    if isinstance(cols, pd.MultiIndex):
        new_cols = []
        for col in cols:
            # col 是 tuple，例如 (標題, 類別, 欄名)
            parts = [str(x).strip() for x in col if x is not None and str(x).strip() != ""]
            # 串成一個字串，例如 "外資及陸資(不含外資自營商)買賣超股數"
            new_cols.append("".join(parts))
        df.columns = new_cols
    else:
        df.columns = [str(c).strip() for c in cols]
    return df


def find_col_any(df: pd.DataFrame, candidates: Iterable[str], required: bool = True) -> str:
    """Return the column best matching one of the candidate names.

    解析順序（為了避免「父字串」欄位搶先命中）：

    1. **完全相等**（依候選字優先序）。例如 T86 同時有
       ``外資自營商買賣超股數`` 與 ``自營商買賣超股數`` 兩欄；舊版用單純子字串
       比對時，``自營商買賣超股數`` 會被排在前面的 ``外資自營商買賣超股數``
       搶走，導致 ``dealer_net`` 幾乎永遠抓到 0。改為「完全相等優先」即可正確
       命中真正的自營商合計欄位。
    2. **子字串比對**（依候選字優先序）；同一候選字命中多欄時取「最短」者，
       最短者通常最貼近候選字本身（如 ``自營商買賣超股數`` 而非
       ``外資自營商買賣超股數``）。

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame whose columns will be searched.
    candidates : Iterable[str]
        Candidate column names / keyword substrings, in priority order.
    required : bool, default True
        If True, raise KeyError when no column is found. Otherwise return None.
    """
    cols = [str(c).strip() for c in df.columns]
    candidates = [str(k).strip() for k in candidates]

    # 1) 完全相等優先（候選字優先序）
    for kw in candidates:
        for c in cols:
            if c == kw:
                return c

    # 2) 子字串退而求其次（候選字優先序；同候選字多命中取最短欄名）
    for kw in candidates:
        matches = [c for c in cols if kw in c]
        if matches:
            return min(matches, key=len)

    if required:
        raise KeyError(f"找不到欄位，候選關鍵字={candidates}, 實際欄位={cols}")
    return None
