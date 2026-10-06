"""Barridos de sensibilidad y walk-forward para la estrategia ganadora.

Uso: python3 research/barrido.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos, motor, estrategias  # noqa: E402

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)

TF = "1h"
PARAMS = motor.Params(riesgo_frac=0.0075, max_posiciones=3)


def evaluar(fn, kwargs, tp_r=None, data=None, params=PARAMS, nombre=""):
    data = data or datos.cargar_todos(TF)
    senales = {}
    for s, df in data.items():
        sig = fn(df, **kwargs)
        if tp_r is not None:
            sig["tp"] = float(tp_r)
        senales[s] = sig
    return motor.simular(senales, params, nombre=nombre)


def barrido_tp():
    print("\n" + "=" * 100)
    print("1) TAKE PROFIT: ¿capar las ganancias ayuda o mata la estrategia?")
    print("=" * 100)
    filas = []
    for tp in (None, 1.5, 2.0, 3.0, 4.0, 6.0, 10.0):
        r = evaluar(estrategias.st_flip, {}, tp_r=tp,
                    nombre=f"TP {tp}R" if tp else "sin TP (solo ST flip)")
        filas.append(r.resumen())
    print(pd.DataFrame(filas).to_string(index=False))


def barrido_st():
    print("\n" + "=" * 100)
    print("2) SUPERTREND: sensibilidad a (periodo, multiplicador)")
    print("=" * 100)
    filas = []
    for p in (7, 10, 12, 14, 20):
        for m in (2.0, 2.5, 3.0, 3.5, 4.0):
            r = evaluar(estrategias.st_flip, {"st_p": p, "st_m": m}, nombre=f"ST({p},{m})")
            d = r.resumen()
            d["st_p"], d["st_m"] = p, m
            filas.append(d)
    t = pd.DataFrame(filas)
    print(t[["st_p", "st_m", "retorno_%", "max_DD_%", "Sharpe", "trades", "win_rate_%",
             "expect_R", "profit_factor"]].to_string(index=False))
    return t


def barrido_adx():
    print("\n" + "=" * 100)
    print("3) FILTRO ADX: ¿qué umbral?  (0 = sin filtro)")
    print("=" * 100)
    filas = []
    for a in (0, 15, 18, 20, 22, 25, 30):
        r = evaluar(estrategias.st_flip, {"adx_t": a, "usar_adx": a > 0}, nombre=f"ADX>{a}")
        d = r.resumen()
        d["adx_t"] = a
        filas.append(d)
    print(pd.DataFrame(filas)[["adx_t", "retorno_%", "max_DD_%", "Sharpe", "trades",
                               "win_rate_%", "expect_R", "profit_factor"]].to_string(index=False))


def barrido_trailing():
    print("\n" + "=" * 100)
    print("4) SALIDA: stop fijo en la banda vs trailing de la banda del SuperTrend")
    print("=" * 100)
    filas = []
    for tb in (False, True):
        r = evaluar(estrategias.st_flip, {"trailing_banda": tb},
                    nombre="trailing banda" if tb else "stop fijo + salida en giro")
        filas.append(r.resumen())
    print(pd.DataFrame(filas).to_string(index=False))


def barrido_largos_cortos():
    print("\n" + "=" * 100)
    print("5) LARGO / CORTO: ¿aportan los cortos?")
    print("=" * 100)
    filas = []
    for cortos in (True, False):
        data = datos.cargar_todos(TF)
        senales = {}
        for s, df in data.items():
            sig = estrategias.st_flip(df, cortos=cortos)
            if not cortos:
                sig.loc[sig["senal"] == -1, "senal"] = 0
            senales[s] = sig
        r = motor.simular(senales, PARAMS, nombre="largo+corto" if cortos else "solo largos")
        filas.append(r.resumen())
    print(pd.DataFrame(filas).to_string(index=False))


def por_simbolo():
    print("\n" + "=" * 100)
    print("6) APORTE POR SÍMBOLO (misma estrategia, base A2 sin TP)")
    print("=" * 100)
    data = datos.cargar_todos(TF)
    filas = []
    for s, df in data.items():
        senales = {s: estrategias.st_flip(df)}
        r = motor.simular(senales, motor.Params(riesgo_frac=0.0075, max_posiciones=1), nombre=s)
        d = r.resumen()
        filas.append(d)
    print(pd.DataFrame(filas).to_string(index=False))


if __name__ == "__main__":
    barrido_tp()
    barrido_st()
    barrido_adx()
    barrido_trailing()
    barrido_largos_cortos()
    por_simbolo()
