"""Barrido amplio + validación walk-forward (in-sample / out-of-sample).

Criterio de selección: no el mejor en muestra, sino el que aguanta en
  1) las dos mitades del periodo, 2) los 5 símbolos, 3) vecindario de parámetros.

Uso: python3 research/walkforward.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos, motor, estrategias  # noqa: E402

pd.set_option("display.width", 260)
pd.set_option("display.max_columns", 50)
pd.set_option("display.max_rows", 300)

TF = "1h"
DATA = datos.cargar_todos(TF)
FECHAS = DATA["BTC/USDT:USDT"]["datetime"]
T0, T1 = FECHAS.iloc[0], FECHAS.iloc[-1]
CORTE = T0 + (T1 - T0) * 0.6          # 60% in-sample / 40% out-of-sample

_cache = {}


def senales_st(**kw):
    """Cachea el cálculo de indicadores por combinación (lento)."""
    clave = tuple(sorted((k, v) for k, v in kw.items() if k in
                         ("st_p", "st_m", "adx_p", "adx_t", "ema_p", "usar_adx",
                          "usar_ema", "cortos", "trailing_banda")))
    if clave not in _cache:
        _cache[clave] = {s: estrategias.st_flip(df, **kw) for s, df in DATA.items()}
    return _cache[clave]


def correr(kw, tp_r=None, desde=None, hasta=None, params=None, data=None):
    sen = senales_st(**kw)
    out = {}
    for s, sig in sen.items():
        d = sig
        if desde is not None or hasta is not None:
            m = pd.Series(True, index=d.index)
            if desde is not None:
                m &= d["datetime"] >= desde
            if hasta is not None:
                m &= d["datetime"] <= hasta
            d = d[m].reset_index(drop=True)
        if tp_r is not None:
            d = d.copy()
            d["tp"] = float(tp_r)
        out[s] = d
    return motor.simular(out, params or motor.Params(), nombre=str(kw))


def main():
    print(f"Datos: {T0} → {T1}  | corte IS/OOS: {CORTE}")

    grid = list(itertools.product(
        [10, 14, 20],        # st_p
        [3.0, 3.5],          # st_m
        [18.0, 20.0, 22.0],  # adx_t
        [None, 6.0],         # tp en R
    ))
    filas = []
    for st_p, st_m, adx_t, tp in grid:
        kw = dict(st_p=st_p, st_m=st_m, adx_t=adx_t, usar_adx=True, usar_ema=True,
                  cortos=True)
        r_full = correr(kw, tp_r=tp)
        r_is = correr(kw, tp_r=tp, hasta=CORTE)
        r_oos = correr(kw, tp_r=tp, desde=CORTE)
        f, i, o = r_full.resumen(), r_is.resumen(), r_oos.resumen()
        filas.append({
            "st_p": st_p, "st_m": st_m, "adx": adx_t, "tp": tp or 0,
            "ret_full": f.get("retorno_%"), "DD_full": f.get("max_DD_%"),
            "sharpe_full": f.get("Sharpe"), "exp_R_full": f.get("expect_R"),
            "trades_full": f.get("trades"),
            "ret_IS": i.get("retorno_%"), "sharpe_IS": i.get("Sharpe"),
            "trades_IS": i.get("trades"),
            "ret_OOS": o.get("retorno_%"), "sharpe_OOS": o.get("Sharpe"),
            "DD_OOS": o.get("max_DD_%"), "trades_OOS": o.get("trades"),
        })
    t = pd.DataFrame(filas)
    t["score"] = t[["ret_full", "ret_IS", "ret_OOS"]].min(axis=1)  # robustez: el peor de los tres
    print("\n=== BARRIDO (ordenado por robustez: peor retorno entre full/IS/OOS) ===")
    print(t.sort_values("score", ascending=False).to_string(index=False))
    return t


if __name__ == "__main__":
    main()
