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
    """Return the first column whose name contains any candidate substring.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame whose columns will be searched.
    candidates : Iterable[str]
        Keyword substrings; the first column whose name contains any of them
        will be returned.
    required : bool, default True
        If True, raise KeyError when no column is found. Otherwise return None.
    """
    cols = [str(c).strip() for c in df.columns]
    for kw in candidates:
        for c in cols:
            if kw in c:
                return c
    if required:
        raise KeyError(f"找不到欄位，候選關鍵字={list(candidates)}, 實際欄位={cols}")
    return None
