"""Carga de datos históricos e indicadores técnicos para investigación.

Los CSV viven en la raíz del repo (historial_<SYMBOL>_<TF>.csv) y fueron
descargados de OKX (swaps USDT). Este módulo NO necesita red: trabaja 100%
offline con lo que ya está versionado.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

SIMBOLOS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
            "XRP/USDT:USDT", "DOGE/USDT:USDT"]

# Especificación de contratos OKX (swap perpetuo USDT):
#   ctVal = valor del contrato, lotSz = paso mínimo (en contratos),
#   minSz = tamaño mínimo de orden (en contratos).
# Son los valores estándar de OKX; el bot en vivo los lee del exchange.
CONTRACT_SIZE = {          # ctVal
    "BTC/USDT:USDT": 0.01,
    "ETH/USDT:USDT": 0.1,
    "SOL/USDT:USDT": 1.0,
    "XRP/USDT:USDT": 100.0,
    "DOGE/USDT:USDT": 10.0,
}
LOT_SIZE = {
    "BTC/USDT:USDT": 0.01,
    "ETH/USDT:USDT": 0.01,
    "SOL/USDT:USDT": 1.0,
    "XRP/USDT:USDT": 1.0,
    "DOGE/USDT:USDT": 1.0,
}
MIN_SIZE = {
    "BTC/USDT:USDT": 0.01,
    "ETH/USDT:USDT": 0.01,
    "SOL/USDT:USDT": 1.0,
    "XRP/USDT:USDT": 1.0,
    "DOGE/USDT:USDT": 1.0,
}


def ruta_csv(symbol: str, tf: str) -> Path:
    return ROOT / f"historial_{symbol.replace('/', '_').replace(':', '_')}_{tf}.csv"


def cargar(symbol: str, tf: str = "1h") -> Optional[pd.DataFrame]:
    """Carga velas de un símbolo. Devuelve DataFrame indexado por timestamp (ms)."""
    p = ruta_csv(symbol, tf)
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["datetime"])
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    return df.reset_index(drop=True)


def cargar_todos(tf: str = "1h") -> Dict[str, pd.DataFrame]:
    out = {}
    for s in SIMBOLOS:
        df = cargar(s, tf)
        if df is not None:
            out[s] = df
    return out


# ===========================================================================
# INDICADORES (Wilder estándar)
# ===========================================================================

def atr(df: pd.DataFrame, periodo: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / periodo, adjust=False).mean()


def _dm_adx(df: pd.DataFrame, periodo: int = 14) -> pd.DataFrame:
    high, low = df["high"], df["low"]
    up, down = high.diff(), -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atrw = atr(df, periodo).replace(0, np.nan)
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / periodo, adjust=False).mean() / atrw
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / periodo, adjust=False).mean() / atrw
    di_sum = (pdi + mdi).replace(0, np.nan)
    dx = (100 * (pdi - mdi).abs() / di_sum).fillna(0.0)
    adx = dx.ewm(alpha=1 / periodo, adjust=False).mean()
    # período de estabilización: ~3x el periodo (dos etapas de suavizado)
    adx.iloc[: periodo * 3] = np.nan
    return pd.DataFrame({"plus_di": pdi, "minus_di": mdi, "adx": adx})


def adx(df: pd.DataFrame, periodo: int = 14) -> pd.Series:
    return _dm_adx(df, periodo)["adx"]


def supertrend(df: pd.DataFrame, periodo: int = 10, multiplicador: float = 3.0) -> pd.DataFrame:
    """SuperTrend clásico (ATR Wilder sobre TR). Devuelve bandas + dirección."""
    high, low, close = df["high"], df["low"], df["close"]
    hl2 = (high + low) / 2
    atrw = atr(df, periodo)
    upper_basic = (hl2 + multiplicador * atrw).values
    lower_basic = (hl2 - multiplicador * atrw).values
    c = close.values
    n = len(df)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    st = np.ones(n, dtype=bool)          # True = alcista
    if n > periodo:
        upper[periodo] = upper_basic[periodo]
        lower[periodo] = lower_basic[periodo]
        for i in range(periodo + 1, n):
            upper[i] = upper_basic[i] if (upper_basic[i] < upper[i - 1] or c[i - 1] > upper[i - 1]) else upper[i - 1]
            lower[i] = lower_basic[i] if (lower_basic[i] > lower[i - 1] or c[i - 1] < lower[i - 1]) else lower[i - 1]
            if st[i - 1]:
                st[i] = not (c[i] < lower[i])
            else:
                st[i] = c[i] > upper[i]
    upper[:periodo] = np.nan
    lower[:periodo] = np.nan
    return pd.DataFrame({"st_upper": upper, "st_lower": lower, "st_dir": st}, index=df.index)


def ema(serie: pd.Series, periodo: int) -> pd.Series:
    return serie.ewm(span=periodo, adjust=False).mean()


def rsi(serie: pd.Series, periodo: int = 14) -> pd.Series:
    delta = serie.diff()
    gan = delta.clip(lower=0).ewm(alpha=1 / periodo, adjust=False).mean()
    per = (-delta.clip(upper=0)).ewm(alpha=1 / periodo, adjust=False).mean()
    rs = gan / per.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def donchian(df: pd.DataFrame, periodo: int) -> pd.DataFrame:
    """Canal de Donchian desplazado 1 vela (sin look-ahead)."""
    hi = df["high"].rolling(periodo).max().shift(1)
    lo = df["low"].rolling(periodo).min().shift(1)
    return pd.DataFrame({"dc_high": hi, "dc_low": lo})


def bollinger(serie: pd.Series, periodo: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = serie.rolling(periodo).mean()
    sd = serie.rolling(periodo).std(ddof=0)
    return pd.DataFrame({"bb_mid": mid, "bb_up": mid + mult * sd, "bb_lo": mid - mult * sd})


def indicadores_base(df: pd.DataFrame, periodo_st: int = 10, mult_st: float = 3.0,
                     periodo_adx: int = 14, periodo_ema: int = 200) -> pd.DataFrame:
    out = df.copy()
    st = supertrend(out, periodo_st, mult_st)
    out["st_upper"], out["st_lower"], out["st_dir"] = st["st_upper"], st["st_lower"], st["st_dir"]
    dm = _dm_adx(out, periodo_adx)
    out["adx"], out["plus_di"], out["minus_di"] = dm["adx"], dm["plus_di"], dm["minus_di"]
    out["atr"] = atr(out, periodo_adx)
    out["atr_pct"] = out["atr"] / out["close"] * 100
    out["ema200"] = ema(out["close"], periodo_ema)
    out["ema50"] = ema(out["close"], 50)
    out["ema20"] = ema(out["close"], 20)
    out["rsi"] = rsi(out["close"], 14)
    return out
