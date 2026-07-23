from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
import time
import json
import os
import pandas as pd
from io import StringIO

with open("keys.txt") as f:
    API_KEY = f.readline().strip()
    SECRET_KEY = f.readline().strip()
f = open("log_rsi.txt", "a", buffering=1)
 
log_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
log_folder = f"logs/backtest/{log_date}"
os.makedirs(log_folder, exist_ok=True)
 
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
historical_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

#Params
INITIAL_CAPITAL = 5000
START_DATE = datetime(2026, 6, 1, tzinfo=timezone.utc)
END_DATE = datetime(2026, 6, 15, tzinfo=timezone.utc)

RSI_PERIOD = 7
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
TAKE_PROFIT_PCT = 0.015
ATR_STOP_MULTIPLIER = 0.25
MIN_STOP_PCT = 0.004
MAX_STOP_PCT = 0.02
COOLDOWN_BARS = 5

def compute_rsi(close_series, period=RSI_PERIOD):
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def get_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "Chrome/138.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers)
    tables = pd.read_html(StringIO(response.text))
    tickers = tables[0]["Symbol"].tolist()
    return [t for t in tickers if "." not in t]


def screen_stock(ticker):
    try:
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=START_DATE - timedelta(days=35),
            end=START_DATE,
            feed=DataFeed.IEX,
        )
        data = historical_client.get_stock_bars(request).df
        data = data.xs(ticker, level=0)

        avg_dollar_vol = (data["close"] * data["volume"]).tail(20).mean()
        avg_price = data["close"].tail(20).mean()
        atr = (data["high"] - data["low"]).tail(14).mean()
        atr_pct = atr / avg_price
        trend = data["close"].iloc[-1] / data["close"].tail(50).mean()

        return {
            "ticker": ticker,
            "avg_dollar_vol": avg_dollar_vol,
            "avg_price": avg_price,
            "atr_pct": atr_pct,
            "trend": trend,
        }
    except Exception:
        return None


def build_watchlist(top_n=7):
    tickers = get_sp500_tickers()
    results = []
    for t in tickers:
        r = screen_stock(t)
        if r is None:
            continue
        if r["avg_dollar_vol"] < 20_000_000:
            continue
        if not (10 <= r["avg_price"] <= 300):
            continue
        if not (0.02 <= r["atr_pct"] <= 0.05):
            continue
        results.append(r)

    results.sort(key=lambda x: x["trend"], reverse=True)
    return results[:top_n]

# --- Run Screener ---
print("!!! RUNNING RSI BACKTEST !!!")
watchlist_results = build_watchlist(7)
tickers = [r["ticker"] for r in watchlist_results]
stop_pct_map = {}
for r in watchlist_results:
    raw_stop = r["atr_pct"] * ATR_STOP_MULTIPLIER
    stop_pct_map[r["ticker"]] = min(max(raw_stop, MIN_STOP_PCT), MAX_STOP_PCT)
print("Watchlist:", tickers)

# 
# ----------------------------- PUT CODE BELOW TO BACKTEST -----------------------------
# 

print("Fetching 1-minute historical data for watchlist...")
data_frames = {}
for t in tickers:
    req = StockBarsRequest(
        symbol_or_symbols=t,
        timeframe=TimeFrame.Minute,
        start=START_DATE,
        end=END_DATE,
        feed=DataFeed.IEX,
    )
    df = historical_client.get_stock_bars(req).df
    if not df.empty:
        data_frames[t] = df.xs(t, level=0).sort_index()
all_timestamps = sorted(
    list(set().union(*[df.index for df in data_frames.values()])) # Extract unified timeline across all symbols
)

print("Simulating trading execution bar-by-bar...")
cash = INITIAL_CAPITAL
positions = {t: False for t in tickers}
pos_qty = {t: 0.0 for t in tickers}
entry_prices = {t: 0.0 for t in tickers}
highest_prices = {t: 0.0 for t in tickers}
cooldown_until = {t: None for t in tickers}

trades = 0
winrate = 0
equity_curve = []

for step, bar_time in enumerate(all_timestamps):
    current_equity = cash
    for t in tickers:
        if positions[t] and bar_time in data_frames[t].index:
            current_equity += (
                pos_qty[t] * data_frames[t].loc[bar_time, "close"]
            )

    remaining_slots = sum(1 for t in tickers if not positions[t])
    risk_per_trade = (
        current_equity / remaining_slots if remaining_slots > 0 else 0
    )

    for ticker in tickers:
        if (
            ticker not in data_frames
            or bar_time not in data_frames[ticker].index
        ):
            continue
        df_history = data_frames[ticker].loc[:bar_time]
        if len(df_history) < RSI_PERIOD + 2:
            continue

        current_price = float(df_history["close"].iloc[-1])
        ema30 = (
            df_history["close"].ewm(span=30, adjust=False).mean().iloc[-1]
        )
        rsi_series = compute_rsi(df_history["close"], RSI_PERIOD)
        current_rsi = float(rsi_series.iloc[-1])
        previous_rsi = float(rsi_series.iloc[-2])

        in_cooldown = (
            cooldown_until[ticker] is not None
            and bar_time < cooldown_until[ticker]
        )

        # ------------------- BUY SIGNAL -------------------
        if (
            previous_rsi < RSI_OVERSOLD
            and current_rsi >= RSI_OVERSOLD
            and current_price > ema30
            and not in_cooldown
            and not positions[ticker]
            and risk_per_trade > 0
        ):
            allocated_cash = min(cash, risk_per_trade)
            shares = allocated_cash / current_price
            if shares > 0:
                pos_qty[ticker] = shares
                cash -= allocated_cash
                positions[ticker] = True
                entry_prices[ticker] = current_price
                highest_prices[ticker] = current_price
                print(
                    f"[{bar_time}] BUY {ticker} at ${current_price:.2f} (RSI: {current_rsi:.1f})"
                )

        # ------------------- SELL SIGNALS -------------------
        elif positions[ticker]:
            highest_prices[ticker] = max(
                highest_prices[ticker], current_price
            )
            stop_pct = stop_pct_map.get(ticker, 0.01)

            # Stop Loss Exit
            if current_price < highest_prices[ticker] * (1 - stop_pct):
                sold_val = pos_qty[ticker] * current_price
                cash += sold_val
                positions[ticker] = False
                cooldown_until[ticker] = bar_time + timedelta(
                    minutes=COOLDOWN_BARS
                )
                trades += 1
                print(
                    f"[{bar_time}] STOP LOSS {ticker} at ${current_price:.2f}"
                )

            # Take Profit Exit
            elif (
                current_price > entry_prices[ticker] * (1 + TAKE_PROFIT_PCT)
            ):
                sold_val = pos_qty[ticker] * current_price
                cash += sold_val
                positions[ticker] = False
                cooldown_until[ticker] = bar_time + timedelta(
                    minutes=COOLDOWN_BARS
                )
                trades += 1
                winrate += 1
                print(
                    f"[{bar_time}] TAKE PROFIT {ticker} at ${current_price:.2f}"
                )

            # RSI Overbought Exit
            elif current_rsi >= RSI_OVERBOUGHT:
                sold_val = pos_qty[ticker] * current_price
                cash += sold_val
                positions[ticker] = False
                cooldown_until[ticker] = bar_time + timedelta(
                    minutes=COOLDOWN_BARS
                )
                trades += 1
                if current_price > entry_prices[ticker]:
                    winrate += 1
                print(
                    f"[{bar_time}] RSI SELL {ticker} at ${current_price:.2f} (RSI: {current_rsi:.1f})"
                )

    equity_curve.append({"timestamp": bar_time, "equity": current_equity})

# --- Generate Summary Report ---
final_equity = current_equity
algo_return = ((final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
win_pct = (winrate / trades * 100) if trades > 0 else 0.0

try:
    spy_request = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Minute,
        start=START_DATE,
        end=END_DATE,
        feed=DataFeed.IEX,
    )
    spy_df = historical_client.get_stock_bars(spy_request).df.xs(
        "SPY", level=0
    )

    spy_start_price = float(spy_df["close"].iloc[0])
    spy_end_price = float(spy_df["close"].iloc[-1])
    spy_return = ((spy_end_price - spy_start_price) / spy_start_price) * 100
    alpha = algo_return - spy_return

    spy_line = f"SPY Return:       {spy_return:.2f}%\nAlpha vs SPY:     {alpha:.2f}%"
except Exception as e:
    spy_line = f"SPY Return:       N/A ({e})\nAlpha vs SPY:     N/A"

report_content = f"""
====================================
  RSI BACKTEST PERFORMANCE REPORT
====================================
Period: {START_DATE.strftime('%Y-%m-%d')} to {END_DATE.strftime('%Y-%m-%d')}
Starting Balance: ${INITIAL_CAPITAL:,.2f}
Ending Balance:   ${final_equity:,.2f}

Total Trades:     {trades}
Win Rate:         {win_pct:.2f}%

Algorithm Return: {algo_return:.2f}%
{spy_line}
====================================
"""

print(report_content)

report_file = Path(log_folder) / "report.txt"
report_file.write_text(report_content)