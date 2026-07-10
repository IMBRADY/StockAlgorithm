from alpaca.trading.client import TradingClient
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta
import requests
import time
import pandas as pd
from io import StringIO

API_KEY = "PKMAC2HBCTJSM6MXCHWQKFAK2H"
SECRET_KEY = "7Kexk59bKtP8VH8mCsDDWZB25HRJqQATTufAr6zdnEwh"

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True) #places orders/checks account
historical_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY) #downloads crypto prices and market data

account = trading_client.get_account()

print("!!! RUNNING BITCOIN TEST !!!")
ticker = "BTC/USD"
avgvolume = {}
diffvol = {}
prices = {}
entry_prices = {}
highest_prices = {}
request = CryptoBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=datetime.now()-timedelta(days=100), end=datetime.now())
data = historical_client.get_crypto_bars(request).df

prices[ticker] = data["close"].iloc[-1]
avgvolume[ticker] = data["volume"].tail(20).mean()
diffvol[ticker] = data["volume"].iloc[-1]/avgvolume[ticker]

print("--- Building Data Frame ---")
dataFrame = {}
positions = {}
held = [p.symbol for p in trading_client.get_all_positions()]
request = CryptoBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute, start=datetime.now()-timedelta(hours=2), end=datetime.now())
dataFrame[ticker] = historical_client.get_crypto_bars(request).df
positions[ticker] = ticker in held
if positions[ticker]:
    position = trading_client.get_open_position(ticker)
    entry_prices[ticker] = float(position.avg_entry_price)
    highest_prices[ticker] = entry_prices[ticker]

print("--- Trading ---")
macds = {}
while True: 
    print("Loop running:", datetime.now())
    account = trading_client.get_account()
    equity = float(account.equity)
    risk_per_trade = 0.01*equity
    request = CryptoBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute, start=datetime.now()-timedelta(minutes=5), end=datetime.now())
    bars = historical_client.get_crypto_bars(request).df
    if bars.empty:
        time.sleep(1)
        continue
    new_data = bars.iloc[-1]

    #Skip if already processed this bar, we can continue the while loop because we know if MACD hasnt changed, there is no buy/sell signal
    if new_data.name == dataFrame[ticker].index[-1]: # .name in a panda series is the index of the series, ie the time here
        time.sleep(1)
        continue
    dataFrame[ticker] = pd.concat([dataFrame[ticker], new_data.to_frame().T]) # new_data.to_frame().T converts the new bar into a dataframe and transposes it
    #Check MACD applicable
    if (len(dataFrame[ticker]) < 26):
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
    current_price = dataFrame[ticker]["close"].iloc[-1]

    #MACD Buy
    if previous_macd < previous_signal and current_macd > current_signal and current_price > ema30.iloc[-1] and not positions[ticker]:
        shares = risk_per_trade/(dataFrame[ticker]["close"].iloc[-1]*0.02)
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
        entry_prices[ticker] = dataFrame[ticker]["close"].iloc[-1]
        highest_prices[ticker] = entry_prices[ticker]
    
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
            continue 
    #Take Profit
    if positions[ticker] and current_price > entry_prices[ticker]*1.04:
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
        continue
    #MACD Sell
    if previous_macd > previous_signal and current_macd < current_signal and positions[ticker]:
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
    
    dataFrame[ticker] = dataFrame[ticker].tail(100)
    time.sleep(60)


# 1) Change order request method
# 2) Add ema30 parameter to buy
# 3) Add time.sleep(1) to all continues in loop to avoid rate limit errors
# idea) buy when MACD slope positive?