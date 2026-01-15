import os
import math
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

# ================== AYARLAR ==================
TICKERS = {
    "AMZN": "Amazon.com Inc.",
    "NVDA": "NVIDIA Corporation",
    "TSLA": "Tesla Inc.",
    "AMD": "Advanced Micro Devices Inc.",
    "GOOGL": "Alphabet Inc. (Class A)",
    "META": "Meta Platforms Inc.",
    "AAPL": "Apple Inc.",
}

# ✅ AMZN için 2017'den başlatıyoruz (senin isteğin)
START_DATE = "2017-01-01"
END_DATE = "2026-01-01"  # None yaparsan bugüne kadar çeker

INTERVAL = "1d"
OUT_PATH = "data/all_prices_with_technicals.csv"
CSV_SEP = ";"  # TR Excel için ";" iyi
# ============================================


def compute_rsi_sma(price: pd.Series, period: int = 14) -> pd.Series:
    """RSI (SMA tabanlı)."""
    price = pd.to_numeric(price, errors="coerce")
    delta = price.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _rename_datetime_col(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    df = df.reset_index()
    if "Date" in df.columns:
        df.rename(columns={"Date": "datetime"}, inplace=True)
    elif "Datetime" in df.columns:
        df.rename(columns={"Datetime": "datetime"}, inplace=True)
    elif "index" in df.columns:
        df.rename(columns={"index": "datetime"}, inplace=True)
    else:
        raise KeyError(f"{ticker}: Tarih kolonu bulunamadı. Kolonlar: {df.columns.tolist()}")
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


def fetch_one_ticker(
    ticker: str,
    start: str,
    end: str | None,
    interval: str,
) -> pd.DataFrame:
    """
    ✅ En sağlam: auto_adjust=True
    - Close/High/Low/Open zaten adjusted gelir
    - Adj Close ile uğraşmayız
    """
    if end is None:
        end = pd.Timestamp.utcnow().strftime("%Y-%m-%d")

    df = yf.download(
        ticker,
        start=start,
        end=end,
        interval=interval,
        auto_adjust=True,     # ✅ en kritik nokta
        actions=False,
        progress=False,
        threads=False,
    )

    if df is None or df.empty:
        return pd.DataFrame()

    df = _flatten_columns(df)
    df = _rename_datetime_col(df, ticker)

    # Standart isimlere çevir
    df.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        },
        inplace=True,
    )

    keep = ["datetime", "open", "high", "low", "close", "volume"]
    missing = [c for c in keep if c not in df.columns]
    if missing:
        raise KeyError(f"{ticker}: Eksik kolonlar: {missing}. Mevcut: {df.columns.tolist()}")

    df = df[keep].copy()

    # numerik dönüşüm
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["datetime", "close"]).sort_values("datetime").reset_index(drop=True)
    return df


def sanity_check_prices(df: pd.DataFrame, ticker: str):
    """
    Uçuk hataları yakala: close çok küçük / negatif / aşırı sıçrama
    """
    if df.empty:
        return

    cmin = float(df["close"].min())
    cmax = float(df["close"].max())

    # genel: close <= 0 olamaz
    if cmin <= 0:
        raise ValueError(f"{ticker}: close <= 0 bulundu (min={cmin}). Veri bozuk olabilir.")

    # AMZN özel: 2017 sonrası adjusted close genelde çok küçük olmaz.
    if ticker == "AMZN" and cmin < 5:
        print(f"⚠️ {ticker}: close çok küçük görünüyor (min={cmin}). "
              f"Bu normal değilse split/kolon karışmış olabilir.")
        print(df.nsmallest(10, "close")[["datetime", "close"]].to_string(index=False))

    # return uç değerlerine bak
    ret = df["close"].pct_change()
    if ret.abs().max() > 2:  # %200+ günlük sıçrama -> şüpheli
        print(f"⚠️ {ticker}: aşırı günlük hareket var (max |pct|={ret.abs().max():.2f}). "
              f"Veri kaynağı/split kontrol gerekebilir.")
        print(df.loc[ret.abs().nlargest(5).index, ["datetime", "close"]].to_string(index=False))


def add_technical_indicators(one_ticker_df: pd.DataFrame, ticker: str, ticker_name: str) -> pd.DataFrame:
    """
    ✅ Tek kaynak fiyat: close (auto_adjust=True ile zaten adjusted)
    """
    df = one_ticker_df.copy()
    df = df.sort_values("datetime").reset_index(drop=True)

    df["ticker"] = ticker
    df["ticker_name"] = ticker_name

    price = pd.to_numeric(df["close"], errors="coerce")

    # SMA/EMA
    df["sma_20"] = price.rolling(window=20, min_periods=20).mean()
    df["sma_50"] = price.rolling(window=50, min_periods=50).mean()
    df["ema_20"] = price.ewm(span=20, adjust=False).mean()

    # RSI
    df["rsi_14"] = compute_rsi_sma(price, period=14)

    # MACD
    ema_12 = price.ewm(span=12, adjust=False).mean()
    ema_26 = price.ewm(span=26, adjust=False).mean()
    df["macd"] = ema_12 - ema_26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()

    # Bollinger
    m20 = price.rolling(window=20, min_periods=20).mean()
    s20 = price.rolling(window=20, min_periods=20).std()
    df["bb_middle"] = m20
    df["bb_upper"] = m20 + 2 * s20
    df["bb_lower"] = m20 - 2 * s20

    # returns
    df["daily_return"] = price.pct_change()
    df["vol_20"] = df["daily_return"].rolling(window=20, min_periods=20).std()

    # log return (daha stabil)
    df["log_return"] = np.log(price).diff()

    return df


def build_pipeline():
    if END_DATE is None:
        end = pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    else:
        end = END_DATE

    all_list = []

    for ticker, name in TICKERS.items():
        print(f"[+] {ticker} ({name}) çekiliyor... {START_DATE} -> {end}")

        df = fetch_one_ticker(ticker, START_DATE, END_DATE, INTERVAL)

        if df.empty:
            print(f"[-] {ticker}: veri gelmedi, atlanıyor.")
            continue

        sanity_check_prices(df, ticker)

        print(f"    rows={len(df)} | close min/max={df['close'].min():.4f}/{df['close'].max():.4f}")
        tech = add_technical_indicators(df, ticker, name)
        all_list.append(tech)

    if not all_list:
        raise ValueError("Hiç veri çekilemedi. İnternet/yfinance/SSL/proxy durumunu kontrol et.")

    full_df = pd.concat(all_list, ignore_index=True)
    full_df = (
        full_df.drop_duplicates(subset=["ticker", "datetime"])
        .sort_values(["ticker", "datetime"])
        .reset_index(drop=True)
    )

    out_dir = os.path.dirname(OUT_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    full_df.to_csv(OUT_PATH, index=False, encoding="utf-8", sep=CSV_SEP)
    print(f"\n[OK] Kaydedildi: {OUT_PATH}")
    print(full_df.head(5).to_string(index=False))

    return full_df


if __name__ == "__main__":
    build_pipeline()
