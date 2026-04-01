# 券商分點買賣超與股票相關性分析

## 功能說明

本系統提供以下功能：

### 1. 分點買賣超統計
- 統計各券商分點的買超前10名股票
- 統計各券商分點的賣超前10名股票
- 包含總買賣超張數、交易天數、平均日買賣超等資訊

### 2. 相關性分析（待實現）
- 計算分點交易量與股票收盤價漲跌幅的相關性
- 支援多個時間窗口：15天、30天、45天、60天
- 使用Pearson相關係數

## 使用方法

### 安裝依賴

```bash
pip install pandas numpy requests
```

### 執行分析

```bash
# 分析分點買賣超統計（最近60天）
python analyze_broker_stats.py

# 相關性分析（需要網路環境支援）
python analyze_broker_correlation.py
```

### 獲取股票價格數據

```bash
# 獲取單支股票的歷史收盤價
python fetch_stock_prices.py
```

## 數據輸出

### 分點統計數據
- **輸出文件**: `docs/data/broker_stats.json`
- **數據結構**:

```json
{
  "updated": "2025-12-26T03:27:19.867390",
  "analysis_days": 60,
  "date_range": {
    "start": "2025-12-15T00:00:00",
    "end": "2025-12-26T00:00:00"
  },
  "brokers_analyzed": 30,
  "total_active_brokers": 36,
  "results": [
    {
      "broker_id": "1470",
      "broker_name": "台灣摩根士丹利",
      "top_buy_stocks": [
        {
          "rank": 1,
          "stock_code": 2303,
          "stock_name": "聯電",
          "total_net_vol": 116750,
          "total_buy_vol": 155512,
          "total_sell_vol": 38762,
          "trading_days": 12,
          "avg_net_vol": 9729.17
        }
      ],
      "top_sell_stocks": [
        {
          "rank": 1,
          "stock_code": 2382,
          "stock_name": "廣達",
          "total_net_vol": -21730,
          "total_buy_vol": 11388,
          "total_sell_vol": 33118,
          "trading_days": 12,
          "avg_net_vol": -1810.83
        }
      ]
    }
  ]
}
```

### 相關性分析數據
- **輸出文件**: `docs/data/broker_correlations.json`
- **數據結構**:

```json
{
  "updated": "2025-12-26T...",
  "analysis_days": 60,
  "correlation_windows": [15, 30, 45, 60],
  "brokers_analyzed": 20,
  "results": [
    {
      "broker_id": "1470",
      "broker_name": "台灣摩根士丹利",
      "top_buy_stocks": [...],
      "top_sell_stocks": [...],
      "correlations": [
        {
          "stock_code": 2330,
          "corr_15d": 0.6543,
          "corr_30d": 0.7234,
          "corr_45d": 0.6891,
          "corr_60d": 0.7012
        }
      ]
    }
  ]
}
```

## 數據欄位說明

### 分點統計欄位
- `broker_id`: 券商分點代碼
- `broker_name`: 券商分點名稱
- `stock_code`: 股票代碼
- `stock_name`: 股票名稱
- `total_net_vol`: 總買賣超張數（正數為買超，負數為賣超）
- `total_buy_vol`: 總買進張數
- `total_sell_vol`: 總賣出張數
- `trading_days`: 交易天數
- `avg_net_vol`: 平均每日買賣超張數
- `rank`: 排名

### 相關性欄位
- `corr_15d`: 15天收盤價漲跌幅相關係數
- `corr_30d`: 30天收盤價漲跌幅相關係數
- `corr_45d`: 45天收盤價漲跌幅相關係數
- `corr_60d`: 60天收盤價漲跌幅相關係數

相關係數範圍：-1 到 1
- 接近 1：正相關（分點買超時，股價傾向上漲）
- 接近 -1：負相關（分點買超時，股價傾向下跌）
- 接近 0：無相關性

## 整合到現有系統

### 更新 GitHub Actions workflow

在 `.github/workflows/update.yml` 中新增：

```yaml
- name: Analyze broker statistics
  run: python analyze_broker_stats.py

# 如果需要相關性分析（需要網路環境支援）
- name: Analyze broker correlations
  run: python analyze_broker_correlation.py
  continue-on-error: true  # 網路問題不中斷流程
```

### 前端展示

可以在 `docs/index.html` 中新增分頁來展示：
1. 各分點買超/賣超前10名股票
2. 分點與股票的相關性排名
3. 互動式圖表顯示相關性係數

## 注意事項

1. **網路限制**: 如果在無法訪問 TWSE/TPEX API 的環境中執行，`analyze_broker_correlation.py` 無法獲取股票價格數據
2. **數據更新**: 建議每天與券商數據同步更新
3. **效能考量**: 完整相關性分析需要下載大量股票價格數據，可能耗時較久
4. **數據準確性**: 相關性分析需要足夠的交易天數（建議至少10天）才有參考價值

## 技術架構

```
analyze_broker_stats.py
  ├─ 載入券商歷史數據 (broker_history.csv)
  ├─ 統計各分點買賣超排名
  ├─ 輸出 broker_stats.json
  └─ 快速執行，不需網路

analyze_broker_correlation.py
  ├─ 載入券商歷史數據
  ├─ 獲取股票收盤價 (fetch_stock_prices.py)
  ├─ 計算收盤價漲跌幅
  ├─ 計算相關係數
  └─ 輸出 broker_correlations.json

fetch_stock_prices.py
  ├─ 從 TWSE API 獲取上市股票價格
  ├─ 從 TPEX API 獲取上櫃股票價格
  ├─ 計算多時間窗口漲跌幅
  └─ 存儲到 data/prices/
```

## 未來改進方向

1. 支援更多相關性指標（Spearman、Kendall等）
2. 新增視覺化圖表（散點圖、熱力圖等）
3. 實現分點績效追蹤（勝率、累計報酬等）
4. 支援自定義時間窗口
5. 新增即時數據更新機制
