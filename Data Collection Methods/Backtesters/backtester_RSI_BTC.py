from alpaca.trading.client import TradingClient
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os
import pandas as pd

# Load Keys
with open("keys.txt") as f:
    API_KEY = f.readline().strip()
    SECRET_KEY = f.readline().strip()

log_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
log_folder = f"logs/backtest/{log_date}"
os.makedirs(log_folder, exist_ok=True)
f_log = open(f"{log_folder}/log_rsi.txt", "a", buffering=1)

# Clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
historical_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)
spy_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# Parameters
INITIAL_CAPITAL = 5000
START_DATE = datetime(2025, 7, 21, tzinfo=timezone.utc)
END_DATE = datetime(2026, 7, 21, tzinfo=timezone.utc)
SCREEN_LOOKBACK_DAYS = 70
CHUNK_SIZE = 100 

cash = INITIAL_CAPITAL
positions = {}
pos_qty = {}
entry_prices = {}
highest_prices = {}
cooldown_until = {}
stop_pct_map = {}

RSI_PERIOD = 7
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 80
TAKE_PROFIT_PCT = 0.015
ATR_STOP_MULTIPLIER = 0.25
MIN_STOP_PCT = 0.004
MAX_STOP_PCT = 0.01
COOLDOWN_BARS = 5

CRYPTOS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD",
    "AVAX/USD", "DOGE/USD", "LTC/USD", "XRP/USD"
]

def compute_rsi(close_series, period=RSI_PERIOD):
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def fetch_all_daily_bars(tickers, start, end, chunk_size=CHUNK_SIZE):
    cache = {}
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            req = CryptoBarsRequest(
                symbol_or_symbols=chunk,  # Fixed: use chunk, not CRYPTOS
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                # Removed feed=DataFeed.IEX; crypto does not use IEX
            )
            df = historical_client.get_crypto_bars(req).df
        except Exception as e:
            print(f"Chunk {i // chunk_size} failed: {e}")
            continue
        if df.empty:
            continue
        for t in chunk:
            try:
                cache[t] = df.xs(t, level=0).sort_index()
            except KeyError:
                continue
    return cache

def screen_crypto(ticker, as_of_date, daily_bars_cache):
    data = daily_bars_cache.get(ticker)
    if data is None or data.empty:
        return None
    data = data[data.index.date < as_of_date.date()].tail(50)
    if len(data) < 20:
        return None
    try:
        avg_price = data["close"].tail(20).mean()
        atr = (data["high"] - data["low"]).tail(14).mean()
        atr_pct = atr / avg_price
        # Trend metric: current price relative to 50-day SMA
        trend = data["close"].iloc[-1] / data["close"].tail(50).mean()
        return {
            "ticker": ticker,
            "avg_price": avg_price,
            "atr_pct": atr_pct,
            "trend": trend,
        }
    except Exception:
        return None

def build_watchlist(crypto_list, as_of_date, daily_bars_cache, top_n=4):
    results = []
    for t in crypto_list:
        r = screen_crypto(t, as_of_date, daily_bars_cache)
        if r is None:
            continue
        # Ensure we are in an uptrend (price > 50 SMA) and volatility isn't absurd
        if r["trend"] < 1.0 or r["atr_pct"] > 0.15:
            continue
        results.append(r) 
    # Sort by strongest trend
    results.sort(key=lambda x: x["trend"], reverse=True)
    return results[:top_n]

def fetch_day_minute_bars(tickers, day_start, day_end, chunk_size=CHUNK_SIZE):
    frames = {}
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            req = CryptoBarsRequest(
                symbol_or_symbols=chunk, # Fixed: use chunk
                timeframe=TimeFrame.Minute,
                start=day_start,
                end=day_end,
            )
            df = historical_client.get_crypto_bars(req).df
        except Exception:
            continue
        if df.empty:
            continue
        for t in chunk:
            try:
                sub = df.xs(t, level=0).sort_index()
                if not sub.empty:
                    frames[t] = sub
            except KeyError:
                continue
    return frames

# --- Run Screener ---
print("!!! RUNNING RSI CRYPTO BACKTEST !!!")

print(f"Fetching daily bars for screening...")
daily_bars_cache = fetch_all_daily_bars(
    CRYPTOS,
    start=START_DATE - timedelta(days=SCREEN_LOOKBACK_DAYS),
    end=END_DATE,
)
print(f"Daily bar cache ready: {len(daily_bars_cache)} tickers.")
print("Simulating trading...")

trades = 0
wincash = 0
losecash = 0
winrate = 0
equity_curve = []

# Loop variables
current_day = START_DATE

while current_day < END_DATE:
    day_start = current_day
    day_end = day_start + timedelta(days=1)
    
    print(f"\n--- Rescreening for Trading Session: {day_start.strftime('%Y-%m-%d')} ---")
    
    # 1. Screen for top trending cryptos today
    watchlist_results = build_watchlist(CRYPTOS, day_start, daily_bars_cache)
    active_tickers = [r["ticker"] for r in watchlist_results]
    
    if not active_tickers:
        print("No trending tickers found today. Skipping.")
        current_day += timedelta(days=1)
        continue

    # 2. Update Stop Loss percentages for today's active tickers
    for r in watchlist_results:
        raw_stop = r["atr_pct"] * ATR_STOP_MULTIPLIER
        stop_pct_map[r["ticker"]] = min(max(raw_stop, MIN_STOP_PCT), MAX_STOP_PCT)

    # 3. Initialize state tracking
    for t in active_tickers:
        positions.setdefault(t, False)
        pos_qty.setdefault(t, 0.0)
        entry_prices.setdefault(t, 0.0)
        highest_prices.setdefault(t, 0.0)
        cooldown_until.setdefault(t, None)

    print(f"Active Tickers Today: {active_tickers}")
    day_data_frames = fetch_day_minute_bars(active_tickers, day_start, day_end)

    if day_data_frames:
        day_timestamps = sorted(
            list(set().union(*[df.index for df in day_data_frames.values()]))
        )
        
        for bar_time in day_timestamps:
            current_equity = cash
            for t in active_tickers:
                if positions[t] and t in day_data_frames and bar_time in day_data_frames[t].index:
                    current_equity += pos_qty[t] * day_data_frames[t].loc[bar_time, "close"]

            remaining_slots = sum(1 for t in active_tickers if not positions[t])
            risk_per_trade = current_equity / remaining_slots if remaining_slots > 0 else 0

            for ticker in active_tickers:
                if ticker not in day_data_frames or bar_time not in day_data_frames[ticker].index:
                    continue

                df_history = day_data_frames[ticker].loc[:bar_time]
                if len(df_history) < RSI_PERIOD + 2:
                    continue

                current_price = float(df_history["close"].iloc[-1])
                ema30 = df_history["close"].ewm(span=30, adjust=False).mean().iloc[-1]
                rsi_series = compute_rsi(df_history["close"], RSI_PERIOD)
                current_rsi = float(rsi_series.iloc[-1])
                previous_rsi = float(rsi_series.iloc[-2])

                in_cooldown = (
                    cooldown_until[ticker] is not None
                    and bar_time < cooldown_until[ticker]
                )

                # --------------- BUY SIGNAL ---------------
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
                        print(f"[{bar_time}] BUY {ticker} at ${current_price:.2f} (RSI: {current_rsi:.1f})")

                # --------------- SELL SIGNALS ---------------
                elif positions[ticker]:
                    highest_prices[ticker] = max(highest_prices[ticker], current_price)
                    stop_pct = stop_pct_map.get(ticker, 0.01)

                    # Stop Loss Exit
                    if current_price < highest_prices[ticker] * (1 - stop_pct):
                        sold_val = pos_qty[ticker] * current_price
                        cash += sold_val
                        positions[ticker] = False
                        cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                        trades += 1
                        print(f"[{bar_time}] STOP LOSS {ticker} at ${current_price:.2f}")
                        losecash += (entry_prices[ticker] - current_price) * pos_qty[ticker]

                    # Take Profit Exit
                    elif current_price > entry_prices[ticker] * (1 + TAKE_PROFIT_PCT):
                        sold_val = pos_qty[ticker] * current_price
                        cash += sold_val
                        positions[ticker] = False
                        cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                        trades += 1
                        winrate += 1
                        print(f"[{bar_time}] TAKE PROFIT {ticker} at ${current_price:.2f}")
                        wincash += (current_price - entry_prices[ticker]) * pos_qty[ticker]

                    # RSI Overbought Exit
                    elif current_rsi >= RSI_OVERBOUGHT and current_price > entry_prices[ticker]:
                        sold_val = pos_qty[ticker] * current_price
                        cash += sold_val
                        positions[ticker] = False
                        cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                        trades += 1
                        if current_price > entry_prices[ticker]:
                            winrate += 1
                            wincash += (current_price - entry_prices[ticker]) * pos_qty[ticker]
                        else:
                            losecash += (entry_prices[ticker] - current_price) * pos_qty[ticker]
                        print(f"[{bar_time}] RSI SELL {ticker} at ${current_price:.2f} (RSI: {current_rsi:.1f})")

            equity_curve.append({"timestamp": bar_time, "equity": current_equity})
            
    # Iterate to next day (prevents the infinite loop!)
    current_day += timedelta(days=1)

# Sell off any remaining positions at the end of the backtest
for t in CRYPTOS:
    if positions.get(t, False):
        final_price = float(day_data_frames[t]["close"].iloc[-1])
        cash += pos_qty[t] * final_price
        positions[t] = False

# --- Generate Summary Report ---
final_equity = cash
algo_return = ((final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
win_pct = (winrate / trades * 100) if trades > 0 else 0.0

try:
    # Use spy_client & StockBarsRequest (Fixed SPY logic)
    spy_request = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Day, 
        start=START_DATE,
        end=END_DATE,
        feed=DataFeed.IEX,
    )
    spy_df = spy_client.get_stock_bars(spy_request).df
    if not spy_df.empty:
        spy_df = spy_df.xs("SPY", level=0)

    spy_start_price = float(spy_df["close"].iloc[0])
    spy_end_price = float(spy_df["close"].iloc[-1])
    spy_return = ((spy_end_price - spy_start_price) / spy_start_price) * 100
    alpha = algo_return - spy_return
    spy_line = f"SPY Return:       {spy_return:.2f}%\nAlpha vs SPY:     {alpha:.2f}%"
except Exception as e:
    spy_line = f"SPY Return:       N/A ({e})\nAlpha vs SPY:     N/A"

# Safe division for averages
avgwincash = (wincash / winrate) if winrate > 0 else 0
loss_count = (trades - winrate)
avglosecash = (losecash / loss_count) if loss_count > 0 else 0

report_content = f"""
====================================
  RSI BACKTEST PERFORMANCE REPORT
====================================
Period: {START_DATE.strftime('%Y-%m-%d')} to {END_DATE.strftime('%Y-%m-%d')}
Starting Balance: ${INITIAL_CAPITAL:,.2f}
Ending Balance:   ${final_equity:,.2f}

Total Trades:     {trades}
Win Rate:         {win_pct:.2f}%
Avg Money Earned on Win: ${avgwincash:.2f}
Avg Money Lost on Loss:  ${avglosecash:.2f}

Algorithm Return: {algo_return:.2f}%
{spy_line}
====================================
"""

print(report_content)
f_log.write(report_content)
f_log.close()

report_file = Path(log_folder) / "report.txt"
report_file.write_text(report_content)