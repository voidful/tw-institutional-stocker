# -*- coding: utf-8 -*-
"""Fetch broker branch trading data from Fubon e-Broker website.

使用 Playwright 瀏覽器自動化繞過反爬蟲機制，抓取富邦網站的券商分點交易數據。

主要功能：
- fetch_broker_trading: 抓取指定股票的主力進出數據
- fetch_broker_history: 抓取特定分點在特定股票的交易歷史
"""

import os
import re
import time
from datetime import date, datetime
from typing import Optional

import pandas as pd

# Playwright import with fallback
try:
    from playwright.sync_api import sync_playwright, Browser, Page
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    print("Warning: playwright not installed. Run: pip install playwright && playwright install chromium")

# Constants
BASE_URL = "https://fubon-ebrokerdj.fbs.com.tw"
BROKER_TRADING_URL = BASE_URL + "/z/zc/zco/zco_{code}.djhtm"
BROKER_HISTORY_URL = BASE_URL + "/z/zc/zco/zco0/zco0.djhtm?a={code}&b={broker_id}"

# Browser singleton
_browser: Optional["Browser"] = None
_playwright = None


def _get_browser() -> "Browser":
    """Get or create browser instance."""
    global _browser, _playwright
    if not HAS_PLAYWRIGHT:
        raise RuntimeError("Playwright is not installed")
    
    if _browser is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
    
    return _browser


def close_browser():
    """Close browser and cleanup resources."""
    global _browser, _playwright
    if _browser:
        _browser.close()
        _browser = None
    if _playwright:
        _playwright.stop()
        _playwright = None


def _parse_number(text: str) -> int:
    """Parse number from text, handling commas and parentheses."""
    if not text or text.strip() in ("", "-"):
        return 0
    
    # Remove commas and whitespace
    text = text.strip().replace(",", "").replace(" ", "")
    
    # Handle negative numbers in parentheses like (1,234)
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    
    try:
        return int(float(text))
    except ValueError:
        return 0


def _parse_percent(text: str) -> float:
    """Parse percentage from text."""
    if not text or text.strip() in ("", "-"):
        return 0.0
    
    text = text.strip().replace("%", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_broker_trading(stock_code: str, target_date: Optional[str] = None) -> pd.DataFrame:
    """
    Fetch broker branch trading data for a specific stock.
    
    Args:
        stock_code: Stock code (e.g., "2330" for TSMC)
        target_date: Target date in "MM/DD" format (e.g., "12/12"), None for latest
    
    Returns:
        DataFrame with columns:
            - date: 日期
            - stock_code: 股票代碼
            - broker_name: 分點名稱 (如 "凱基-台北")
            - broker_id: 分點代碼
            - buy_vol: 買進張數
            - sell_vol: 賣出張數
            - net_vol: 買賣超張數
            - pct: 佔比(%)
            - rank: 排名
            - side: "buy" (買超) 或 "sell" (賣超)
    """
    if not HAS_PLAYWRIGHT:
        raise RuntimeError("Playwright is not installed")
    
    browser = _get_browser()
    page = browser.new_page()
    
    try:
        url = BROKER_TRADING_URL.format(code=stock_code)
        page.goto(url, wait_until="networkidle", timeout=30000)
        
        # Wait for table to load
        page.wait_for_selector("table.t01", timeout=10000)
        
        # If target_date specified, try to select it
        if target_date:
            try:
                # Find and select date from dropdown
                select = page.query_selector("select")
                if select:
                    options = select.query_selector_all("option")
                    for opt in options:
                        if target_date in (opt.get_attribute("value") or ""):
                            select.select_option(value=opt.get_attribute("value"))
                            page.wait_for_load_state("networkidle")
                            break
            except Exception:
                pass  # Use default date if selection fails
        
        # Parse the page content
        records = []
        
        # Find the t01 table which contains the broker data
        table = page.query_selector("table.t01")
        if not table:
            return pd.DataFrame()
        
        rows = table.query_selector_all("tr")
        
        # Get the displayed date from page header
        # Look for pattern like "12/12" in the page content
        date_text = ""
        for row in rows[:5]:
            row_text = row.inner_text()
            match = re.search(r"(\d{1,2}/\d{1,2})", row_text)
            if match:
                date_text = match.group(1)
                break
        
        # Find the header row (contains "買超券商" and "賣超券商")
        data_start_idx = 0
        for i, row in enumerate(rows):
            cells = row.query_selector_all("td")
            if len(cells) >= 10:
                cell_texts = [c.inner_text().strip() for c in cells]
                if "買超券商" in cell_texts[0] and "賣超券商" in cell_texts[5]:
                    data_start_idx = i + 1
                    break
        
        # Parse data rows
        # Each row has 10 cells: [買超券商, 買進, 賣出, 買超, 佔比, 賣超券商, 買進, 賣出, 賣超, 佔比]
        rank = 0
        for row in rows[data_start_idx:]:
            cells = row.query_selector_all("td")
            if len(cells) < 10:
                continue
            
            rank += 1
            
            # Parse buy side (cells 0-4)
            buy_broker_cell = cells[0]
            buy_broker_link = buy_broker_cell.query_selector("a")
            if buy_broker_link:
                buy_broker_name = buy_broker_link.inner_text().strip()
                href = buy_broker_link.get_attribute("href") or ""
                match = re.search(r"b=([^&]+)", href)
                buy_broker_id = match.group(1) if match else ""
            else:
                buy_broker_name = buy_broker_cell.inner_text().strip()
                buy_broker_id = ""
            
            if buy_broker_name and buy_broker_name != "買超券商":
                buy_vol = _parse_number(cells[1].inner_text())
                sell_vol = _parse_number(cells[2].inner_text())
                net_vol = _parse_number(cells[3].inner_text())
                pct = _parse_percent(cells[4].inner_text())
                
                records.append({
                    "date": date_text,
                    "stock_code": stock_code,
                    "broker_name": buy_broker_name,
                    "broker_id": buy_broker_id,
                    "buy_vol": buy_vol,
                    "sell_vol": sell_vol,
                    "net_vol": net_vol,
                    "pct": pct,
                    "rank": rank,
                    "side": "buy"
                })
            
            # Parse sell side (cells 5-9)
            sell_broker_cell = cells[5]
            sell_broker_link = sell_broker_cell.query_selector("a")
            if sell_broker_link:
                sell_broker_name = sell_broker_link.inner_text().strip()
                href = sell_broker_link.get_attribute("href") or ""
                match = re.search(r"b=([^&]+)", href)
                sell_broker_id = match.group(1) if match else ""
            else:
                sell_broker_name = sell_broker_cell.inner_text().strip()
                sell_broker_id = ""
            
            if sell_broker_name and sell_broker_name != "賣超券商":
                buy_vol = _parse_number(cells[6].inner_text())
                sell_vol = _parse_number(cells[7].inner_text())
                net_vol = _parse_number(cells[8].inner_text())  # This is negative (賣超)
                pct = _parse_percent(cells[9].inner_text())
                
                records.append({
                    "date": date_text,
                    "stock_code": stock_code,
                    "broker_name": sell_broker_name,
                    "broker_id": sell_broker_id,
                    "buy_vol": buy_vol,
                    "sell_vol": sell_vol,
                    "net_vol": -abs(net_vol),  # Ensure negative for sell side
                    "pct": pct,
                    "rank": rank,
                    "side": "sell"
                })
        
        return pd.DataFrame(records)
    
    finally:
        page.close()




def fetch_broker_history(stock_code: str, broker_id: str, days: int = 20) -> pd.DataFrame:
    """
    Fetch trading history for a specific broker branch on a specific stock.
    
    Args:
        stock_code: Stock code (e.g., "2330")
        broker_id: Broker branch ID (e.g., "1020")
        days: Number of days of history to fetch (default 20)
    
    Returns:
        DataFrame with columns:
            - date: 日期
            - stock_code: 股票代碼
            - broker_id: 分點代碼
            - buy_vol: 買進張數
            - sell_vol: 賣出張數
            - net_vol: 買賣超張數
            - price: 成交均價
    """
    if not HAS_PLAYWRIGHT:
        raise RuntimeError("Playwright is not installed")
    
    browser = _get_browser()
    page = browser.new_page()
    
    try:
        url = BROKER_HISTORY_URL.format(code=stock_code, broker_id=broker_id)
        page.goto(url, wait_until="networkidle", timeout=30000)
        
        # Wait for table to load
        page.wait_for_selector("table.t01", timeout=10000)
        
        records = []
        
        # Find main data table
        table = page.query_selector("table.t01")
        if not table:
            return pd.DataFrame()
        
        rows = table.query_selector_all("tr")
        
        for row in rows[1:days+1]:  # Skip header, limit to 'days' records
            cells = row.query_selector_all("td")
            if len(cells) < 4:
                continue
            
            date_text = cells[0].inner_text().strip()
            buy_vol = _parse_number(cells[1].inner_text())
            sell_vol = _parse_number(cells[2].inner_text())
            net_vol = _parse_number(cells[3].inner_text())
            price = _parse_percent(cells[4].inner_text()) if len(cells) > 4 else 0.0
            
            records.append({
                "date": date_text,
                "stock_code": stock_code,
                "broker_id": broker_id,
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "net_vol": net_vol,
                "price": price
            })
        
        return pd.DataFrame(records)
    
    finally:
        page.close()


def fetch_multiple_stocks(stock_codes: list[str], delay: float = 1.0) -> pd.DataFrame:
    """
    Fetch broker trading data for multiple stocks.
    
    Args:
        stock_codes: List of stock codes
        delay: Delay between requests in seconds (to avoid rate limiting)
    
    Returns:
        Combined DataFrame with all broker trading data
    """
    all_data = []
    
    for code in stock_codes:
        try:
            df = fetch_broker_trading(code)
            all_data.append(df)
            time.sleep(delay)
        except Exception as e:
            print(f"Error fetching {code}: {e}")
            continue
    
    if all_data:
        return pd.concat(all_data, ignore_index=True)
    return pd.DataFrame()


# Cleanup on module unload
import atexit
atexit.register(close_browser)


if __name__ == "__main__":
    # Test fetching broker data for TSMC (2330)
    print("Fetching broker trading data for 2330...")
    df = fetch_broker_trading("2330")
    print(f"Got {len(df)} records")
    print(df.head(10))
    
    close_browser()
