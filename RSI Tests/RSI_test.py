from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from datetime import datetime, timezone, timedelta
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
log_folder = f"logs_rsi/{log_date}"
os.makedirs(log_folder, exist_ok=True)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
historical_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

print("!!! RUNNING RSI TEST !!!")

# Params
RSI_PERIOD = 7 # duration for which average is taken. Shorter period means more recent bars have more influence
RSI_OVERSOLD = 30 # ratio of how many losses over wins dictate when its oversold and should be bought
RSI_OVERBOUGHT = 80 # ratio of how many wins over losses dictate when its overbought and should be sold
TAKE_PROFIT_PCT = 0.015
ATR_STOP_MULTIPLIER = 0.25
MIN_STOP_PCT = 0.004
MAX_STOP_PCT = 0.02
COOLDOWN_BARS = 5

def get_current_price(ticker):
    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Minute,
        start=datetime.now(timezone.utc) - timedelta(minutes=5),
        end=datetime.now(timezone.utc),
        feed=DataFeed.IEX
    )
    bars = historical_client.get_stock_bars(request).df
    if bars.empty:
        return None
    return float(bars["close"].iloc[-1])

def write_trade_log(ticker, data):
    filename = f"{log_folder}/{ticker}.csv"
    file_exists = os.path.isfile(filename)
    with open(filename, "a", buffering=1) as l:
        if not file_exists:
            l.write("timestamp,ticker,price,rsi,ema30,action,type\n")
        l.write(data + "\n")

def get_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers)
    try:
        tables = pd.read_html(StringIO(response.text))
        print("Number of tables found:", len(tables))
    except Exception as e:
        print("EXCEPTION TYPE:", type(e).__name__)
        print("EXCEPTION MESSAGE:", str(e)[:1000])
    sp500_table = tables[0]
    tickers = sp500_table["Symbol"].tolist()
    tickers = [t for t in tickers if "." not in t]
    return tickers

def screen_stock(ticker, historical_client):
    try:
        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=30),
            end=datetime.now(timezone.utc),
            feed=DataFeed.IEX
        )
        data = historical_client.get_stock_bars(request).df
        data = data.xs(ticker, level=0)
 
        avg_dollar_vol = (data["close"] * data["volume"]).tail(20).mean()
        avg_price = data["close"].tail(20).mean()
 
        daily_range = data["high"] - data["low"]
        atr = daily_range.tail(14).mean()
        atr_pct = atr / avg_price
        ma = data["close"].tail(50).mean()
        last_close = data["close"].iloc[-1]
        trend = last_close / ma

        return {
            "ticker": ticker,
            "avg_dollar_vol": avg_dollar_vol,
            "avg_price": avg_price,
            "atr_pct": atr_pct,
            "ma": ma,
            "trend": trend,
            "last_close": last_close
        }
    except Exception as e:
        print(f"Skipping {ticker}: {e}")
        return None

def build_watchlist(historical_client, top_n=7):
    tickers = get_sp500_tickers()
    results = []
    for t in tickers:
        r = screen_stock(t, historical_client)
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

def compute_rsi(close_series, period=RSI_PERIOD):
    delta = close_series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean() # Wilder's smoothing (standard RSI), equivalent to an EWM with alpha=1/period
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi

watchlist_results = build_watchlist(historical_client, 7)
tickers = [r["ticker"] for r in watchlist_results]
stop_pct_map = {}
for r in watchlist_results:
    raw_stop = r["atr_pct"] * ATR_STOP_MULTIPLIER
    stop_pct_map[r["ticker"]] = min(max(raw_stop, MIN_STOP_PCT), MAX_STOP_PCT)

print("Watchlist:", tickers)
print("Stop pct map:", {t: round(p, 4) for t, p in stop_pct_map.items()})
f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Watchlist: {tickers}\n")
f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Stop pct map: {stop_pct_map}\n")

entry_prices = {}
highest_prices = {}
dataFrame = {}
positions = {}
cooldown_until = {t: None for t in tickers}
recent_bar = {t: None for t in tickers}

print("--- Syncing Data ---")
pos_map = {p.symbol: p for p in trading_client.get_all_positions()}
for t in tickers:
    request = StockBarsRequest(
        symbol_or_symbols=t, timeframe=TimeFrame.Minute,
        start=datetime.now(timezone.utc) - timedelta(days=2),
        end=datetime.now(timezone.utc), feed=DataFeed.IEX
    )
    dataFrame[t] = historical_client.get_stock_bars(request).df
    dataFrame[t] = dataFrame[t].xs(t, level=0)
 
    if t in pos_map:
        position = pos_map[t]
        entry_prices[t] = float(position.avg_entry_price)
        highest_prices[t] = entry_prices[t]
        positions[t] = True
        print(f"{t}: existing position")
    else:
        positions[t] = False

print("--- Trading ---")
starting_equity = float(trading_client.get_account().equity)
spy_start_price = get_current_price("SPY")
f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Beginning Trading\n")
f.flush()
trades = 0
winrate = 0

try:
    while True:
        try:
            print("Loop running:", datetime.now(timezone.utc))
            account = trading_client.get_account()
            clock = trading_client.get_clock()
            if not clock.is_open:
                print("Market closed")
                time.sleep(300)
                break
            equity = float(account.equity)
            for ticker in tickers:
                try:
                    action = False
                    remaining = sum(1 for t in tickers if not positions[t])
                    risk_per_trade = equity / remaining if remaining > 0 else 0
 
                    request = StockBarsRequest(
                        symbol_or_symbols=ticker, timeframe=TimeFrame.Minute,
                        start=datetime.now(timezone.utc) - timedelta(minutes=20),
                        end=datetime.now(timezone.utc), feed=DataFeed.IEX
                    )
                    bars = historical_client.get_stock_bars(request).df
                    if bars.empty:
                        time.sleep(1)
                        continue
                    bars = bars.xs(ticker, level=0)
                    if len(bars) < 2:
                        continue
                    new_data = bars.iloc[-2]
                    bar_time = bars.index[-2]
 
                    if bar_time == recent_bar[ticker]:
                        time.sleep(1)
                        continue
                    dataFrame[ticker] = pd.concat([dataFrame[ticker], new_data.to_frame().T])
                    recent_bar[ticker] = bar_time
                    dataFrame[ticker] = dataFrame[ticker][~dataFrame[ticker].index.duplicated(keep="last")]
 
                    if len(dataFrame[ticker]) < RSI_PERIOD + 2:
                        print("Not enough data")
                        time.sleep(1)
                        continue
 
                    ema30 = dataFrame[ticker]["close"].ewm(span=30, adjust=False).mean()
                    rsi = compute_rsi(dataFrame[ticker]["close"], RSI_PERIOD)
                    current_rsi = rsi.iloc[-1]
                    previous_rsi = rsi.iloc[-2]
                    current_price = new_data["close"]
 
                    in_cooldown = cooldown_until[ticker] is not None and bar_time < cooldown_until[ticker]
 
                    print(f"{ticker} : RSI:", current_rsi, "Price:", current_price, "EMA30:", ema30.iloc[-1])
                    f.write(f"[{datetime.now(timezone.utc)}] [main/INFO] [{ticker}]: RSI: {current_rsi}, Price: {current_price}, EMA30: {ema30.iloc[-1]}\n")
 
                    # ------------------- BUY SIGNAL -------------------
                    
                    if (previous_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD and current_price > ema30.iloc[-1] and not in_cooldown and not positions[ticker] and risk_per_trade > 0):
                        print("RSI BUY SIGNAL: ", ticker)
                        order = MarketOrderRequest(
                            symbol=ticker,
                            notional=round(risk_per_trade, 2),
                            side=OrderSide.BUY,
                            time_in_force=TimeInForce.DAY
                        )
                        trading_client.submit_order(order)
                        positions[ticker] = True
                        entry_prices[ticker] = current_price
                        highest_prices[ticker] = entry_prices[ticker]
                        f.write(f"[{datetime.now(timezone.utc)}] [main/BUY]: RSI Buy, At price: {current_price}\n")
                        write_trade_log(
                            ticker,
                            f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_rsi},{ema30.iloc[-1]},BUY,RSIBUY"
                        )
 
                    # ------------------- SELL SIGNALS -------------------

                    elif positions[ticker]:
                        highest_prices[ticker] = max(highest_prices[ticker], current_price)
                        stop_pct = stop_pct_map.get(ticker, 0.01)
 
                        # ATR-scaled trailing stop
                        if current_price < highest_prices[ticker] * (1 - stop_pct):
                            action = True
                            position = trading_client.get_open_position(ticker)
                            f.write(f"[{datetime.now(timezone.utc)}] [main/SELL] [{ticker}]: Stop Loss ({stop_pct:.4f}), At price: {current_price}\n")
                            write_trade_log(
                                ticker,
                                f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_rsi},{ema30.iloc[-1]},SELL,STOPLOSS"
                            )
                            order = MarketOrderRequest(symbol=ticker, qty=position.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                            trading_client.submit_order(order)
                            positions[ticker] = False
                            cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                            trades += 1
                            continue
 
                        # Fixed take-profit backstop

                        if current_price > entry_prices.get(ticker, 0) * (1 + TAKE_PROFIT_PCT):
                            action = True
                            position = trading_client.get_open_position(ticker)
                            f.write(f"[{datetime.now(timezone.utc)}] [main/SELL] [{ticker}]: Take Profit, At price: {current_price}\n")
                            write_trade_log(
                                ticker,
                                f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_rsi},{ema30.iloc[-1]},SELL,TAKEPROF"
                            )
                            order = MarketOrderRequest(symbol=ticker, qty=position.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                            trading_client.submit_order(order)
                            positions[ticker] = False
                            cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                            trades += 1
                            winrate += 1
                            continue
 
                        # RSI SELL: leading exit signal -- overbought reached

                        if current_rsi >= RSI_OVERBOUGHT:
                            action = True
                            position = trading_client.get_open_position(ticker)
                            f.write(f"[{datetime.now(timezone.utc)}] [main/SELL] [{ticker}]: RSI Overbought Sell, At price: {current_price}\n")
                            write_trade_log(
                                ticker,
                                f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_rsi},{ema30.iloc[-1]},SELL,RSISELL"
                            )
                            order = MarketOrderRequest(symbol=ticker, qty=position.qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                            trading_client.submit_order(order)
                            positions[ticker] = False
                            cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                            trades += 1
                            if current_price > entry_prices[ticker]:
                                winrate += 1
                            continue
 
                    if not action:
                        write_trade_log(
                            ticker,
                            f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_rsi},{ema30.iloc[-1]},NONE,HOLD"
                        )
                    else:
                        write_trade_log(
                            ticker,
                            f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_rsi},{ema30.iloc[-1]},NONE,NOTHOLD"
                        )
 
                    dataFrame[ticker] = dataFrame[ticker].tail(100)
 
                except Exception as e:
                    print(f"ERROR on {ticker}:", repr(e))
                    f.write(f"[{datetime.now(timezone.utc)}] [main/ERROR] [{ticker}]: {repr(e)}\n")
                    continue
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("ERROR in loop:", repr(e))
            time.sleep(5)
            continue
        sleep_time = 60 - datetime.now(timezone.utc).second - datetime.now(timezone.utc).microsecond / 1000000
        time.sleep(sleep_time)
except KeyboardInterrupt:
    print("Stopped Manually")
finally:
    ending_equity = float(trading_client.get_account().equity)
    algorithm_return = ((ending_equity - starting_equity) / starting_equity) * 100
 
    spy_end_price = get_current_price("SPY")
    if spy_start_price is not None and spy_end_price is not None:
        spy_return = ((spy_end_price - spy_start_price) / spy_start_price) * 100
        spy_line = f"SPY Return: {spy_return:.2f}%\nAlpha vs SPY: {(algorithm_return - spy_return):.2f}%"
    else:
        spy_line = "SPY Return: N/A (no price data)\nAlpha vs SPY: N/A"
 
    report_path = f"{log_folder}/report.txt"
    win_pct = (winrate / trades * 100) if trades > 0 else 0.0
    try:
        with open(report_path, "a") as report:
            report.write(f"""
============================
Daily RSI Algorithm Report
Date: {datetime.now(timezone.utc)}
 
Starting Equity: ${starting_equity:.2f}
Ending Equity: ${ending_equity:.2f}
 
Num Trades: {trades}
Winrate: {win_pct:.2f}%
 
Algorithm Return: {algorithm_return:.2f}%
 
{spy_line}
 
============================
""")
    except Exception as e:
        print("FAILED TO WRITE REPORT:", repr(e))
 
    try:
        final_positions = trading_client.get_all_positions()
        data = {p.symbol: {"qty": p.qty, "avg_entry_price": p.avg_entry_price} for p in final_positions}
        with open("positions_rsi.json", "w") as pf:
            json.dump(data, pf, indent=2)
        print("Data saved to file")
    except Exception as e:
        print("FAILED TO SAVE POSITIONS:", repr(e))