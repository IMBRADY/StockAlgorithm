CREATE TABLE bars (
    id SERIAL PRIMARY KEY,                                                  -- SERIAL generates unique integers, PRIMARY KEY uniquely identifies each row
    ticker VARCHAR(10) NOT NULL,                                            -- stores up to 10 characters, NULL means every row must have a ticker
    tf VARCHAR(10) NOT NULL,                                                -- timeframe: stores whether bar is minute, day, or hour
    ts TIMESTAMPTZ NOT NULL,                                                -- timestamp: stores the time, ex. 2026-7-24 06:00:00-07
    open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume BIGINT,  -- OHLC prices all in respective rows, volume alows storage of very large ints
    UNIQUE (ticker, timeframe, ts)                                          -- knocks out any rows with same ticker, timeframe, and timestamp
);

CREATE TABLE runs (
    run_id SERIAL PRIMARY KEY,
    strategy VARCHAR(20),                                                   -- ex. 'MACD' or 'RSI'
    mode VARCHAR(10),                                                       -- ex. 'live' or 'backtest'
    params JSONB,                                                           -- instead of making lots of columns for parameters like oversold, overbought, ema, etc, compresses it all into one json, ie, {"rsi_period":7,"oversold":35,...}
    start_time TIMESTAMPTZ,                                                 -- start and end timestamp in aforementioned format
    end_time TIMESTAMPTZ
);

CREATE TABLE executions (
    id SERIAL PRIMARY KEY,
    run_id INT REFERENCES runs(run_id),
    ticker VARCHAR(10),
    ts TIMESTAMPTZ,
    price NUMERIC, rsi NUMERIC, ema30 NUMERIC,
    action VARCHAR(10),                                                     -- BUY/SELL/NONE
    type VARCHAR(15)                                                        -- ex. RSIBUY, TAKEPROF, MACDSELL, etc
);

CREATE INDEX idx_executions_run_ticker ON executions(run_id, ticker);       -- optional, but speeds up processing. Filters data into categories based off run_id and tickers
CREATE INDEX idx_bars_ticker_ts ON bars(ticker, ts);                        -- happens automatically. more indexes -> slower writes -> faster reads