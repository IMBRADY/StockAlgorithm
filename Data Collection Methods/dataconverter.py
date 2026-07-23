import pandas as pd
import matplotlib.pyplot as plt
import sqlite3


df = pd.read_csv("logs/2026-07-20/WDAY.csv")

# Creates window named fig, with two axes named ax1 and ax2
# 2 rows, 1 col, we choose 12x8in window on same x axis
fig, (ax1,ax2) = plt.subplots(2,1,figsize=(12,8), sharex=True) 

# Stock Price
ax1.plot(df["timestamp"].str[11:19], df["price"], linestyle = 'dashed', color = 'black')
ax1.set_title("Day Stock Price")
ax1.set_ylabel("Price")

buy = df[df["action"] == "BUY"]
sell = df[df["action"] == "SELL"]
ax1.scatter( # creates scatter plots of specific x,y
    buy["timestamp"].str[11:19], # where to place x values
    buy["price"], # where to place y values (matches price graph)
    marker="^",
    color="green",
    s=100, # size of marker
    label="BUY"
)
ax1.scatter(
    sell["timestamp"].str[11:19],
    sell["price"],
    marker="v",
    color="red",
    s=100,
    label="SELL"
)

# MACD
ax2.plot(df["timestamp"].str[11:19], df["macd"], linestyle = 'dashed', color = 'b')
ax2.plot(df["timestamp"].str[11:19], df["signal"], linestyle = 'dashed', color = 'r')
ax2.axhline(0, color="black", linewidth=0.5)
ax2.set_title("MACD")
ax2.set_ylabel("Value")

ax1.legend()

plt.show()