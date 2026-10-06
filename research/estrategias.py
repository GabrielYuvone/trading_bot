"""Definición de estrategias candidatas.

Cada función devuelve un DataFrame con columnas:
    datetime, open, high, low, close,
    senal    (1 = long, -1 = short, 0 = nada; se entra en la apertura siguiente),
    sl       (precio del stop inicial),
    trail    (opcional: stop dinámico monótono),
    salida_l / salida_s (opcional: cerrar la posición larga/corta),
    fuerza   (opcional: prioridad si hay más señales que huecos).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos  # noqa: E402


def _base(df: pd.DataFrame) -> pd.DataFrame:
    out = df[["datetime", "open", "high", "low", "close"]].copy()
    out["senal"] = 0.0
    out["sl"] = np.nan
    out["salida_l"] = False
    out["salida_s"] = False
    out["fuerza"] = 0.0
    return out


# ---------------------------------------------------------------------------
# A) SuperTrend: giro de tendencia (estrategia actual del bot)
# ---------------------------------------------------------------------------

def st_flip(df: pd.DataFrame, st_p: int = 10, st_m: float = 3.0, adx_p: int = 14,
            adx_t: float = 22.0, ema_p: int = 200, usar_adx: bool = True,
            usar_ema: bool = True, cortos: bool = True, trailing_banda: bool = False,
            **_) -> pd.DataFrame:
    d = datos.indicadores_base(df, st_p, st_m, adx_p, ema_p)
    out = _base(d)
    prev = d["st_dir"].shift(1)
    giro_alc = (~prev.fillna(False)) & d["st_dir"]
    giro_baj = prev.fillna(False) & (~d["st_dir"])
    filtro = (d["adx"] > adx_t) if usar_adx else pd.Series(True, index=d.index)
    for i in range(1, len(d)):
        if not bool(filtro.iloc[i]):
            continue
        g_a, g_b = bool(giro_alc.iloc[i]), bool(giro_baj.iloc[i])
        if not (g_a or g_b):
            continue
        if g_a:
            if usar_ema and d["close"].iloc[i] <= d["ema200"].iloc[i]:
                continue
            out.loc[out.index[i], "senal"] = 1
            out.loc[out.index[i], "sl"] = d["st_lower"].iloc[i]
        elif cortos:
            if usar_ema and d["close"].iloc[i] >= d["ema200"].iloc[i]:
                continue
            out.loc[out.index[i], "senal"] = -1
            out.loc[out.index[i], "sl"] = d["st_upper"].iloc[i]
    out["salida_l"] = giro_baj.values
    out["salida_s"] = giro_alc.values
    if trailing_banda:
        out["trail_l"] = d["st_lower"].values
        out["trail_s"] = d["st_upper"].values
    out["fuerza"] = d["adx"].fillna(0).values
    return out


# ---------------------------------------------------------------------------
# B) SuperTrend: continuación / pullback a la banda
# ---------------------------------------------------------------------------

def st_pullback(df: pd.DataFrame, st_p: int = 10, st_m: float = 3.0, adx_p: int = 14,
                adx_t: float = 20.0, ema_p: int = 200, toque_pct: float = 0.005,
                ventana: int = 6, atr_sl: float = 0.5, atr_trail: float = 3.0,
                cortos: bool = True, **_) -> pd.DataFrame:
    """Tendencia confirmada (ST + EMA200 + ADX) con entrada en retroceso a la banda."""
    d = datos.indicadores_base(df, st_p, st_m, adx_p, ema_p)
    d["min_low"] = d["low"].rolling(ventana).min()
    d["max_high"] = d["high"].rolling(ventana).max()
    out = _base(d)

    banda = np.where(d["st_dir"], d["st_lower"], d["st_upper"])
    cerca = (d["close"] - banda).abs() / d["close"] <= toque_pct
    cond_alc = d["st_dir"] & cerca & (d["close"] > d["ema200"]) & (d["adx"] > adx_t)
    cond_baj = (~d["st_dir"]) & cerca & (d["close"] < d["ema200"]) & (d["adx"] > adx_t)
    out["senal"] = np.where(cond_alc, 1.0, np.where(cond_baj & cortos, -1.0, 0.0))
    out["sl"] = np.where(out["senal"] == 1,
                         np.minimum(banda, d["min_low"]) - atr_sl * d["atr"],
                         np.where(out["senal"] == -1,
                                  np.maximum(banda, d["max_high"]) + atr_sl * d["atr"], np.nan))
    out["trail"] = np.where(d["st_dir"], d["close"] - atr_trail * d["atr"],
                            d["close"] + atr_trail * d["atr"])
    out["salida_l"] = (~d["st_dir"]).values
    out["salida_s"] = d["st_dir"].values
    out["fuerza"] = d["adx"].fillna(0).values
    return out


# ---------------------------------------------------------------------------
# C) Breakout de Donchian (tortuga) + trailing ATR
# ---------------------------------------------------------------------------

def donchian_breakout(df: pd.DataFrame, n_entrada: int = 48, n_salida: int = 24,
                      atr_mult: float = 3.0, atr_sl: float = 2.5, adx_p: int = 14,
                      adx_t: float = 18.0, ema_p: int = 200, usar_adx: bool = True,
                      usar_ema: bool = True, cortos: bool = True, **_) -> pd.DataFrame:
    d = datos.indicadores_base(df, 10, 3.0, adx_p, ema_p)
    ent, sal = datos.donchian(d, n_entrada), datos.donchian(d, n_salida)
    d["dc_high"], d["dc_low"] = ent["dc_high"], ent["dc_low"]
    d["ex_high"], d["ex_low"] = sal["dc_high"], sal["dc_low"]
    out = _base(d)

    alc = (d["high"] > d["dc_high"]) & (d["adx"] > adx_t if usar_adx else True)
    baj = (d["low"] < d["dc_low"]) & (d["adx"] > adx_t if usar_adx else True)
    if usar_ema:
        alc &= d["close"] > d["ema200"]
        baj &= d["close"] < d["ema200"]
    out["senal"] = np.where(alc, 1.0, np.where(baj & cortos, -1.0, 0.0))
    out["sl"] = np.where(out["senal"] == 1, d["close"] - atr_sl * d["atr"],
                         np.where(out["senal"] == -1, d["close"] + atr_sl * d["atr"], np.nan))
    out["trail"] = np.where(out["senal"] == 1, d["close"] - atr_mult * d["atr"],
                            np.where(out["senal"] == -1, d["close"] + atr_mult * d["atr"],
                                     d["close"] - atr_mult * d["atr"]))
    # el trailing se aplica según el lado de la posición abierta en el motor
    out["trail_l"] = (d["close"] - atr_mult * d["atr"]).values
    out["trail_s"] = (d["close"] + atr_mult * d["atr"]).values
    out["salida_l"] = (d["close"] < d["ex_low"]).values
    out["salida_s"] = (d["close"] > d["ex_high"]).values
    out["fuerza"] = d["adx"].fillna(0).values
    return out


# ---------------------------------------------------------------------------
# D) Cruce de medias móviles + trailing ATR
# ---------------------------------------------------------------------------

def ema_cruce(df: pd.DataFrame, rapida: int = 50, lenta: int = 200, atr_mult: float = 3.0,
              atr_sl: float = 2.5, adx_p: int = 14, adx_t: float = 18.0,
              usar_adx: bool = True, cortos: bool = True, **_) -> pd.DataFrame:
    d = datos.indicadores_base(df, 10, 3.0, adx_p, 200)
    d["ema_r"] = datos.ema(d["close"], rapida)
    d["ema_l"] = datos.ema(d["close"], lenta)
    out = _base(d)
    arriba = d["ema_r"] > d["ema_l"]
    cruce_alc = arriba & (~arriba.shift(1).fillna(False))
    cruce_baj = (~arriba) & arriba.shift(1).fillna(False)
    fa = (d["adx"] > adx_t) if usar_adx else pd.Series(True, index=d.index)
    out["senal"] = np.where(cruce_alc & fa, 1.0, np.where(cruce_baj & fa & cortos, -1.0, 0.0))
    out["sl"] = np.where(out["senal"] == 1, d["close"] - atr_sl * d["atr"],
                         np.where(out["senal"] == -1, d["close"] + atr_sl * d["atr"], np.nan))
    out["trail_l"] = (d["close"] - atr_mult * d["atr"]).values
    out["trail_s"] = (d["close"] + atr_mult * d["atr"]).values
    out["salida_l"] = (cruce_baj).values
    out["salida_s"] = (cruce_alc).values
    out["fuerza"] = d["adx"].fillna(0).values
    return out


# ---------------------------------------------------------------------------
# E) Momentum de serie temporal
# ---------------------------------------------------------------------------

def momentum(df: pd.DataFrame, lookback_h: int = 24 * 14, ema_p: int = 200,
             atr_mult: float = 4.0, atr_sl: float = 3.0, rebalance_h: int = 24 * 7,
             cortos: bool = True, **_) -> pd.DataFrame:
    d = datos.indicadores_base(df, 10, 3.0, 14, ema_p)
    d["mom"] = d["close"].pct_change(lookback_h)
    out = _base(d)
    ultima = -10 ** 9
    for i in range(lookback_h + 2, len(d)):
        if i - ultima < rebalance_h:
            continue
        mom = d["mom"].iloc[i]
        if not np.isfinite(mom):
            continue
        px, ema = d["close"].iloc[i], d["ema200"].iloc[i]
        if mom > 0 and px > ema:
            out.loc[out.index[i], "senal"] = 1
            out.loc[out.index[i], "sl"] = px - atr_sl * d["atr"].iloc[i]
            ultima = i
        elif cortos and mom < 0 and px < ema:
            out.loc[out.index[i], "senal"] = -1
            out.loc[out.index[i], "sl"] = px + atr_sl * d["atr"].iloc[i]
            ultima = i
    out["trail_l"] = (d["close"] - atr_mult * d["atr"]).values
    out["trail_s"] = (d["close"] + atr_mult * d["atr"]).values
    out["fuerza"] = d["mom"].abs().fillna(0).values
    return out


# ---------------------------------------------------------------------------
# F) Reversión a la media en rangos (Bollinger + RSI), solo con ADX bajo
# ---------------------------------------------------------------------------

def mean_reversion(df: pd.DataFrame, bb_p: int = 20, bb_m: float = 2.0,
                   adx_t: float = 18.0, atr_sl: float = 1.5, ema_p: int = 200,
                   cortos: bool = True, **_) -> pd.DataFrame:
    d = datos.indicadores_base(df, 10, 3.0, 14, ema_p)
    d = pd.concat([d, datos.bollinger(d["close"], bb_p, bb_m)], axis=1)
    out = _base(d)
    rango = d["adx"] < adx_t
    alc = rango & (d["close"] <= d["bb_lo"]) & (d["rsi"] < 40)
    baj = rango & (d["close"] >= d["bb_up"]) & (d["rsi"] > 60) & cortos
    out["senal"] = np.where(alc, 1.0, np.where(baj, -1.0, 0.0))
    out["sl"] = np.where(out["senal"] == 1, d["close"] - atr_sl * d["atr"],
                         np.where(out["senal"] == -1, d["close"] + atr_sl * d["atr"], np.nan))
    cruza_arriba = (d["close"] >= d["bb_mid"]) & (d["close"].shift(1) < d["bb_mid"])
    cruza_abajo = (d["close"] <= d["bb_mid"]) & (d["close"].shift(1) > d["bb_mid"])
    out["salida_l"] = cruza_arriba.values
    out["salida_s"] = cruza_abajo.values
    out["fuerza"] = (adx_t - d["adx"]).fillna(0).values
    return out
