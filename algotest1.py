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
import pandas as pd
from io import StringIO

with open("keys.txt") as f: #Everything under with open umbrella will happen while file is being read, then closed after
    API_KEY = f.readline().strip()
    SECRET_KEY = f.readline().strip()  
f = open("log.txt", "a")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True) #places orders/checks account
historical_client = StockHistoricalDataClient(API_KEY, SECRET_KEY) #downloads stock prices and market data

account = trading_client.get_account()

print("!!! RUNNING STOCK TEST !!!")
ticker = "NVDA"
avgvolume = {}
diffvol = {}
prices = {}
entry_prices = {}
highest_prices = {}
request = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=datetime.now(timezone.utc)-timedelta(days=100), end=datetime.now(timezone.utc), feed=DataFeed.IEX)
data = historical_client.get_stock_bars(request).df
data = data.xs(ticker, level=0) 

prices[ticker] = data["close"].iloc[-1]
avgvolume[ticker] = data["volume"].tail(20).mean()
diffvol[ticker] = data["volume"].iloc[-1]/avgvolume[ticker]

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

dataFrame = {}
positions = {}
pos_map = {p.symbol: p for p in trading_client.get_all_positions()}
request = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute, start=datetime.now(timezone.utc)-timedelta(days=2), end=datetime.now(timezone.utc), feed=DataFeed.IEX)
dataFrame[ticker] = historical_client.get_stock_bars(request).df
dataFrame[ticker] = dataFrame[ticker].xs(ticker, level=0)
if ticker in pos_map:
    position = pos_map[ticker]
    entry_prices[ticker] = float(position.avg_entry_price)
    highest_prices[ticker] = entry_prices[ticker]
    positions[ticker] = True
    print("Positions: ", positions[ticker])
else:
    positions[ticker] = False

print("--- Trading ---")
f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Beginning Trading\n")
macds = {}
recent_bar = None
clock = trading_client.get_clock()
try: # when keyboard interruption, process loop first
    while True: 
        try: # catch general errors without killing whole bot
            print("Loop running:", datetime.now(timezone.utc))
            account = trading_client.get_account()
            clock = trading_client.get_clock()
            if not clock.is_open:
                print("Market closed")
                time.sleep(300)
                continue
            equity = float(account.equity)
            risk_per_trade = 0.01*equity
            request = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute, start=datetime.now(timezone.utc)-timedelta(minutes=5), end=datetime.now(timezone.utc), feed=DataFeed.IEX)
            bars = historical_client.get_stock_bars(request).df
            if bars.empty:
                print("Bars empty")
                time.sleep(1)
                continue
            bars = bars.xs(ticker, level=0)
            new_data = bars.iloc[-2]
            bar_time = bars.index[-2]

            #Skip if already processed this bar, we can continue the while loop because we know if MACD hasnt changed, there is no buy/sell signal
            if bar_time == recent_bar: # .name in a panda series is the index of the series, ie the time here
                print("Bar already processed")
                sleep_time = 60 - datetime.now(timezone.utc).second - datetime.now(timezone.utc).microsecond/1000000
                time.sleep(sleep_time)
                continue
            dataFrame[ticker] = pd.concat([dataFrame[ticker], new_data.to_frame().T]) # new_data.to_frame().T converts the new bar into a dataframe and transposes it
            
            recent_bar = bar_time
            dataFrame[ticker] = dataFrame[ticker].drop_duplicates()

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
            f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Beginning Trading\n")

            print("EMA30:", ema30.iloc[-1], "Current MACD:", current_macd, "Current Price:", current_price, "Current Equity:", equity)
            print(dataFrame[ticker].tail())
            print("Local:", datetime.now())
            print("Bar time:", bars.index[-1])

            #MACD Buy
            if current_macd > current_signal and (current_macd-previous_macd) > 0 and new_data["close"] > ema30.iloc[-1] and not positions[ticker]:
                shares = round(risk_per_trade/(current_price*0.02), 6)
                print("BUY SIGNAL: ", ticker)
                order = MarketOrderRequest(
                    symbol=ticker,
                    qty=shares,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(order)
                print("ORDER SENT")
                positions[ticker] = True
                entry_prices[ticker] = current_price
                highest_prices[ticker] = entry_prices[ticker]
                f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Beginning Trading\n")
            
            #Stop Loss (Trailing Stop)
            if positions[ticker]:
                highest_prices[ticker] = max(highest_prices[ticker], current_price)
                if current_price < highest_prices[ticker]*0.98:
                    position = trading_client.get_open_position(ticker)
                    print("STOP LOSS: ", ticker)
                    order = MarketOrderRequest(
                        symbol=ticker,
                        qty=position.qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY
                    )
                    trading_client.submit_order(order)
                    print("ORDER SENT")
                    positions[ticker] = False
                    f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Beginning Trading\n")
                    continue 
                #Take Profit
                if current_price > entry_prices.get(ticker, 0)*1.04:
                    position = trading_client.get_open_position(ticker)
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
                    f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Beginning Trading\n")
                    continue
                #MACD Sell
                if previous_macd > previous_signal and current_macd < current_signal:
                    position = trading_client.get_open_position(ticker)
                    print("SELL SIGNAL: ", ticker)
                    order = MarketOrderRequest(
                        symbol=ticker,
                        qty=position.qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY
                    )
                    trading_client.submit_order(order)
                    print("ORDER SENT")
                    positions[ticker] = False
                    f.write(f"[{datetime.now(timezone.utc)}] [main/INFO]: Beginning Trading\n")
            
            dataFrame[ticker] = dataFrame[ticker].tail(100)
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
    f.close()
    positions = trading_client.get_all_positions()
    data = {p.symbol: {"qty": p.qty, "avg_entry_price": p.avg_entry_price} for p in positions}
    with open("positions.json", "w") as f:
        json.dump(data, f, indent=2) #dump with file handler f and indentation 2
    print("Data saved to file")

# 1) Change order request method
# 2) Add ema30 parameter to buy
# 3) Add time.sleep(1) to all continues in loop to avoid rate limit errors
# idea) Trading multiple shares (implement something so that if curr >>> ema, buy more shares, if curr > ema, buy less shares)

#NOTE: CRYPTO USES GTC NOT DAY