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

with open("keys.txt") as f: #Everything under with open umbrella will happen while file is being read, then closed after
    API_KEY = f.readline().strip()
    SECRET_KEY = f.readline().strip()  
f = open("log.txt", "a", buffering=1) # bufferings lets log refresh every line instead of all at once (prevents accidental crash not saving log)

log_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
log_folder = f"logs/{log_date}"
os.makedirs(log_folder, exist_ok=True)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True) #places orders/checks account
historical_client = StockHistoricalDataClient(API_KEY, SECRET_KEY) #downloads stock prices and market data

print("!!! RUNNING MACD TEST !!!")
atr_range = {} 

def get_current_price(ticker):
    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Minute,
        start=datetime.now(timezone.utc)-timedelta(minutes=5),
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
            l.write("timestamp,ticker,price,macd,signal,ema30,action,type\n")
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
        tables = pd.read_html(StringIO(response.text)) # Whenever you have a big block of html text, pandas tries to interpret HTML as a file path, so we need to add StringIO
        print("Number of tables found:", len(tables))
    except Exception as e:
        print("EXCEPTION TYPE:", type(e).__name__)
        print("EXCEPTION MESSAGE:", str(e)[:1000])
    print("Found", len(tables), "tables")
    sp500_table = tables[0] # only first table because table 1 lists symbol, security, GICS Sector, etc. (you can look on wiki it is the first table)
    tickers = sp500_table["Symbol"].tolist() # we replace . with - because some apis expect BRK-B but is listed as BRK.B on wikis
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
        atr = daily_range.tail(14).mean() # mean of high-low, measure on average of how much stock moves in a day
        atr_pct = atr / avg_price # percent atr, percent it moves on avg each day
        ma = data["close"].tail(50).mean()
        last_close = data["close"].iloc[-1]
        trend = last_close/ma

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
    
def build_watchlist(historical_client, top_n=7): # top n chooses number of tickers to look at
    tickers = get_sp500_tickers()
    results = []
    for t in tickers:
        r = screen_stock(t, historical_client)
        if r is None:
            continue
        # Hard filters
        if r["avg_dollar_vol"] < 20_000_000:
            continue
        if not (10 <= r["avg_price"] <= 300):
            continue
        if not (0.02 <= r["atr_pct"] <= 0.05):
            continue
        if r["ma"] > r["last_close"]:
            continue
        atr_range[t] = r["atr_pct"]
        results.append(r)

    results.sort(key=lambda x: x["trend"], reverse=True)
    return [r["ticker"] for r in results[:top_n]]

tickers = build_watchlist(historical_client, 7)
print("Watchlist:", tickers)
f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Watchlist: {tickers}\n")

prices = {}
avgvolume = {}
diffvol = {}
entry_prices = {}
highest_prices = {}
dataFrame = {}
positions = {}
cooldown_until = {t: None for t in tickers}
recent_bar = {t: None for t in tickers}
COOLDOWN_BARS = 5 # stops bot from constantly reentering and selling if conditions met

print("--- Syncing Data ---")
def sync_positions_from_alpaca():
    positions = trading_client.get_all_positions()
    data = {}
    for p in positions:
        data[p.symbol] = {
            "qty": p.qty,
            "avg_entry_price": p.avg_entry_price
        }
    with open("positions.json", "w") as f:
        json.dump(data, f, indent=2)
    return data

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
macds = {}
trades = 0
winrate = 0
try: # when keyboard interruption, process loop first
    while True: 
        try: # catch general errors without killing whole bot
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
                    if remaining == 0:
                        risk_per_trade = 0
                    else:
                        risk_per_trade = equity/remaining
                    request = StockBarsRequest(
                        symbol_or_symbols=ticker, timeframe=TimeFrame.Minute,
                        start=datetime.now(timezone.utc) - timedelta(minutes=20),
                        end=datetime.now(timezone.utc), feed=DataFeed.IEX
                    )
                    bars = historical_client.get_stock_bars(request).df
                    if bars.empty:
                        print("Bars empty")
                        time.sleep(1)
                        continue
                    bars = bars.xs(ticker, level=0)
                    if len(bars) < 2:
                        print(f"{ticker}: not enough bars yet")
                        continue
                    new_data = bars.iloc[-2]
                    bar_time = bars.index[-2]

                    #Skip if already processed this bar, we can continue the while loop because we know if MACD hasnt changed, there is no buy/sell signal
                    if bar_time == recent_bar[ticker]: # check if most recent bar in pandas table is what we are grabbing currently
                        print("Bar already processed")
                        sleep_time = 60 - datetime.now(timezone.utc).second - datetime.now(timezone.utc).microsecond/1000000
                        time.sleep(1)
                        continue
                    dataFrame[ticker] = pd.concat([dataFrame[ticker], new_data.to_frame().T]) # new_data.to_frame().T converts the new bar into a dataframe and transposes it
                    
                    recent_bar[ticker] = bar_time
                    dataFrame[ticker] = dataFrame[ticker][~dataFrame[ticker].index.duplicated(keep="last")] # drops duplicates

                    #Check MACD applicable
                    if (len(dataFrame[ticker]) < 26):
                        print("Not enough data")
                        time.sleep(1)
                        continue
                    #MACD Calculation
                    ema12 = dataFrame[ticker]["close"].ewm(span=12, adjust=False).mean() #.ewm - exponential moving window (heavier weight on recent prices)
                    ema26 = dataFrame[ticker]["close"].ewm(span=26, adjust=False).mean() # we want to create a table of emas hence we do not use iloc
                    ema30 = dataFrame[ticker]["close"].ewm(span=30, adjust=False).mean()
                    macd = ema12 - ema26
                    signal = macd.ewm(span=9, adjust=False).mean()
                    histogram = macd - signal # If histogram positive, it means macd is above signal line, which is bullish. If histogram is negative, it means macd is below signal line, which is bearish.
                    current_macd = macd.iloc[-1]
                    previous_macd = macd.iloc[-2]
                    current_signal = signal.iloc[-1]
                    previous_signal = signal.iloc[-2]
                    macds[ticker] = {
                    "current_macd": current_macd,
                    "previous_macd": previous_macd,
                    "current_signal": current_signal,
                    "previous_signal": previous_signal }
                    current_price = new_data["close"]

                    #Rolling Vol Calculation
                    rolling_avg_vol = dataFrame[ticker]["volume"].tail(20).mean()
                    current_vol = new_data["volume"]

                    #Cooldown Check
                    in_cooldown = cooldown_until[ticker] is not None and bar_time < cooldown_until[ticker]

                    print(f"{ticker} : EMA30:", ema30.iloc[-1], "Current MACD:", current_macd, "Current Price:", current_price, "Current Equity:", equity)
                    f.write(f"[{datetime.now(timezone.utc)}] [main/INFO] [{ticker}]: EMA30: {ema30.iloc[-1]}, Current MACD: {current_macd}, Current Price: {current_price}, Current Equity: {equity}\n")
                    print("Local:", datetime.now())
                    print("Bar time:", bars.index[-1])

                    #MACD Buy
                    if current_macd > current_signal and current_macd > 0 and (current_macd-previous_macd) > 0 and not in_cooldown and not positions[ticker] and risk_per_trade > 0:
                        print("BUY SIGNAL: ", ticker)
                        order = MarketOrderRequest(
                            symbol=ticker,
                            notional=round(risk_per_trade,2),
                            side=OrderSide.BUY,
                            time_in_force=TimeInForce.DAY
                        )
                        trading_client.submit_order(order)
                        print("ORDER SENT")
                        positions[ticker] = True
                        entry_prices[ticker] = current_price
                        highest_prices[ticker] = entry_prices[ticker]
                        f.write(f"[{datetime.now(timezone.utc)}] [main/BUY]: MACD Buy, At price: {current_price}\n")
                        write_trade_log(
                            ticker,
                            f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_macd},{current_signal},{ema30.iloc[-1]},BUY,MACDBUY"
                        )
                    
                    #Stop Loss (Trailing Stop)
                    elif positions[ticker]:
                        highest_prices[ticker] = max(highest_prices[ticker], current_price)
                        current_atr_pct = atr_range.get(ticker, 0.025) # if val not found choose 0.025 atr_pct
                        stop_distance = current_atr_pct*0.25
                        if current_price < highest_prices[ticker]*(1-stop_distance): # we can measure atr_pct because more volatile stocks wont sell early due to noise, rather we sell if we leave a true range of the stock based on volatility
                            action = True
                            position = trading_client.get_open_position(ticker)
                            print("STOP LOSS: ", ticker)
                            f.write(f"[{datetime.now(timezone.utc)}] [main/SELL] [{ticker}]: Stop Loss, At price: {current_price}\n")
                            write_trade_log(
                                ticker,
                                f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_macd},{current_signal},{ema30.iloc[-1]},SELL,STOPLOSS"
                            )
                            order = MarketOrderRequest(
                                symbol=ticker,
                                qty=position.qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            )
                            trading_client.submit_order(order)
                            print("ORDER SENT")
                            positions[ticker] = False
                            cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                            trades += 1
                            continue 
                        #Take Profit
                        if current_price > entry_prices.get(ticker, 0)*1.015:
                            action = True
                            position = trading_client.get_open_position(ticker)
                            f.write(f"[{datetime.now(timezone.utc)}] [main/SELL] [{ticker}]: Take Profit, At price: {current_price}\n")
                            write_trade_log(
                                ticker,
                                f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_macd},{current_signal},{ema30.iloc[-1]},SELL,TAKEPROF"
                            )
                            print("TAKE PROFIT: ", ticker)
                            order = MarketOrderRequest(
                                symbol=ticker,
                                qty=position.qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            )
                            trading_client.submit_order(order)
                            print("ORDER SENT")
                            positions[ticker] = False
                            cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                            trades += 1
                            winrate += 1                            
                            continue
                        #MACD Sell
                        if previous_macd > previous_signal and current_macd < current_signal:
                            action = True
                            position = trading_client.get_open_position(ticker)
                            f.write(f"[{datetime.now(timezone.utc)}] [main/SELL] [{ticker}]: MACD Sell, At price: {current_price}\n")
                            write_trade_log(
                                ticker,
                                f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_macd},{current_signal},{ema30.iloc[-1]},SELL,MACDSELL"
                            )
                            order = MarketOrderRequest(
                                symbol=ticker,
                                qty=position.qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            )
                            trading_client.submit_order(order)
                            print("ORDER SENT")
                            positions[ticker] = False
                            cooldown_until[ticker] = bar_time + timedelta(minutes=COOLDOWN_BARS)
                            trades += 1
                            if current_price > entry_prices[ticker]:
                                winrate += 1                            
                            continue

                    if not action:
                        write_trade_log(
                            ticker,
                            f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_macd},{current_signal},{ema30.iloc[-1]},NONE,HOLD"
                        )
                    else:
                        write_trade_log(
                            ticker,
                            f"{datetime.now(timezone.utc)},{ticker},{current_price},{current_macd},{current_signal},{ema30.iloc[-1]},NONE,NOTHOLD"
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
        sleep_time = 60 - datetime.now(timezone.utc).second - datetime.now(timezone.utc).microsecond/1000000
        time.sleep(sleep_time)
except KeyboardInterrupt:
    print("Stopped Manually")
finally:
    ending_equity = float(trading_client.get_account().equity)
    algorithm_return = ((ending_equity - starting_equity) / starting_equity) * 100
    spy_end_price = get_current_price("SPY")
    if spy_start_price is not None and spy_end_price is not None:
        spy_return = ((spy_end_price - spy_start_price) / spy_start_price) * 100
        alpha = algorithm_return - spy_return
        spy_line = f"SPY Return: {spy_return:.2f}%\nAlpha vs SPY: {alpha:.2f}%"
    else:
        spy_line = "SPY Return: N/A (no price data)\nAlpha vs SPY: N/A"

    report_path = f"{log_folder}/report.txt"
    win_pct = (winrate / trades * 100) if trades > 0 else 0.0
    try:
        with open(report_path, "a") as report:
            report.write(f"""
============================
Daily Algorithm Report
Date: {datetime.now(timezone.utc)}

Starting Equity: ${starting_equity:.2f}
Ending Equity: ${ending_equity:.2f}

Num Trades: {trades}
Winrate: {win_pct:.2f}%

Algorithm Return: {algorithm_return:.2f}%

SPY Return: {spy_return:.2f}%

Alpha vs SPY: {(algorithm_return - spy_return):.2f}%

============================
""")
    except Exception as e:
        print("FAILED TO WRITE REPORT:", repr(e))
    try:
        final_positions = trading_client.get_all_positions()
        data = {p.symbol: {"qty": p.qty, "avg_entry_price": p.avg_entry_price} for p in final_positions}
        with open("positions.json", "w") as pf:
            json.dump(data, pf, indent=2)
        print("Data saved to file")
    except Exception as e:
        print("FAILED TO SAVE POSITIONS:", repr(e))

# 1) Add wins and trade # to report

#NOTE: CRYPTO USES GTC NOT DAY