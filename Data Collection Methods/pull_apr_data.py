from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from datetime import datetime, timezone, timedelta
import pandas as pd
 
# --- Config ---
TICKERS = ["NDAQ", "PSX", "PYPL", "VLO", "WDAY", "ADBE", "MPC"]
LOOKBACK_DAYS = 30
ATR_WINDOW = 14
MA_WINDOW = 50
OUTPUT_CSV = "atr_data.csv"
 
with open("keys.txt") as f:
    API_KEY = f.readline().strip()
    SECRET_KEY = f.readline().strip()
 
historical_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
 
rows = []
for ticker in TICKERS:
    try:
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS),
            end=datetime.now(timezone.utc),
            feed=DataFeed.IEX,
        )
        data = historical_client.get_stock_bars(request).df
        data = data.xs(ticker, level=0)
 
        daily_range = data["high"] - data["low"]
        atr = daily_range.tail(ATR_WINDOW).mean()
        avg_price = data["close"].tail(20).mean()
        atr_pct = atr / avg_price
 
        ma = data["close"].tail(MA_WINDOW).mean()
        last_close = data["close"].iloc[-1]
 
        # daily return series, for volatility comparison
        daily_returns = data["close"].pct_change().dropna()
 
        rows.append({
            "ticker": ticker,
            "n_days": len(data),
            "avg_price": round(avg_price, 2),
            "atr": round(atr, 4),
            "atr_pct": round(atr_pct, 5),
            "ma50": round(ma, 2),
            "last_close": round(last_close, 2),
            "daily_return_std": round(daily_returns.std(), 5),
            "max_daily_range_pct": round((daily_range / data["close"]).max(), 5),
        })
        print(f"{ticker}: atr_pct={atr_pct:.4f}")
 
    except Exception as e:
        print(f"Skipping {ticker}: {e}")
 
out = pd.DataFrame(rows)
out.to_csv(OUTPUT_CSV, index=False)
print(f"\nSaved {len(out)} rows to {OUTPUT_CSV}")
print(out.to_string(index=False))