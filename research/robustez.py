"""Última verificación antes de decidir:
   1) vecindario de parámetros (¿estamos en una meseta o en un pico?)
   2) sensibilidad a costos (fee/slippage/funding)
   3) comportamiento de los cortos en las caídas del periodo

Uso: python3 research/robustez.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos, motor, estrategias  # noqa: E402

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_rows", 200)

DATA = datos.cargar_todos("1h")
FECHAS = DATA["BTC/USDT:USDT"]["datetime"]
T0, T1 = FECHAS.iloc[0], FECHAS.iloc[-1]
CORTE = T0 + (T1 - T0) * 0.6
_cache = {}


def sigs(st_p, st_m, adx_t):
    k = (st_p, st_m, adx_t)
    if k not in _cache:
        _cache[k] = {s: estrategias.st_flip(df, st_p=st_p, st_m=st_m, adx_t=adx_t)
                     for s, df in DATA.items()}
    return _cache[k]


def correr(st_p, st_m, adx_t, params, desde=None, hasta=None):
    out = {}
    for s, sig in sigs(st_p, st_m, adx_t).items():
        d = sig
        if desde is not None or hasta is not None:
            m = pd.Series(True, index=d.index)
            if desde is not None:
                m &= d["datetime"] >= desde
            if hasta is not None:
                m &= d["datetime"] <= hasta
            d = d[m].reset_index(drop=True)
        out[s] = d
    return motor.simular(out, params)


def vecindario():
    print("\n" + "=" * 110)
    print("1) VECINDARIO DE PARÁMETROS (meseta = robusto; pico aislado = sobreajuste)")
    print("=" * 110)
    filas = []
    for st_p, st_m, adx_t in itertools.product([14, 16, 20, 24], [3.0, 3.5, 4.0], [18.0, 20.0, 22.0]):
        p = motor.Params(riesgo_frac=0.0075, max_posiciones=3)
        f = correr(st_p, st_m, adx_t, p).resumen()
        i = correr(st_p, st_m, adx_t, p, hasta=CORTE).resumen()
        o = correr(st_p, st_m, adx_t, p, desde=CORTE).resumen()
        filas.append({"st_p": st_p, "st_m": st_m, "adx": adx_t,
                      "ret_%": f.get("retorno_%"), "DD_%": f.get("max_DD_%"),
                      "Sharpe": f.get("Sharpe"), "exp_R": f.get("expect_R"),
                      "trades": f.get("trades"),
                      "ret_IS": i.get("retorno_%"), "ret_OOS": o.get("retorno_%")})
    t = pd.DataFrame(filas)
    print(t.to_string(index=False))
    print(f"\n  mediana retorno: {t['ret_%'].median():+.1f}% · "
          f"combos con retorno > 0: {(t['ret_%'] > 0).sum()}/{len(t)} · "
          f"combos con OOS > 0: {(t['ret_OOS'] > 0).sum()}/{len(t)}")
    return t


def costos():
    print("\n" + "=" * 110)
    print("2) SENSIBILIDAD A COSTOS (ST 20/3.5, ADX>20)")
    print("=" * 110)
    filas = []
    for fee, slip, fund in [(0.0002, 0.0001, 0.0),      # optimista (maker, sin funding)
                            (0.0005, 0.0003, 0.0001),   # base
                            (0.0005, 0.0006, 0.0001),   # slippage alto
                            (0.0010, 0.0006, 0.0002)]:  # pesimista
        c = motor.Costos(fee=fee, slippage=slip, funding_8h=fund)
        p = motor.Params(riesgo_frac=0.0075, max_posiciones=3, costos=c)
        r = correr(20, 3.5, 20.0, p).resumen()
        r["fee_bp"] = fee * 10000
        r["slip_bp"] = slip * 10000
        r["fund_8h_bp"] = fund * 10000
        filas.append(r)
    print(pd.DataFrame(filas)[["fee_bp", "slip_bp", "fund_8h_bp", "retorno_%", "max_DD_%",
                               "Sharpe", "trades", "win_rate_%", "expect_R",
                               "profit_factor"]].to_string(index=False))


def cortos():
    print("\n" + "=" * 110)
    print("3) ¿HUBO TENDENCIAS BAJISTAS EN EL PERIODO? (para juzgar los cortos)")
    print("=" * 110)
    for s, df in DATA.items():
        c = df.set_index("datetime")["close"]
        dd = (c / c.cummax() - 1).min()
        rally = (c / c.cummin() - 1).max()
        baj = c.resample("W").last().pct_change()
        print(f"  {s.split('/')[0]:<5} cierre {c.iloc[0]:>10.4f} → {c.iloc[-1]:>10.4f} "
              f"({(c.iloc[-1]/c.iloc[0]-1)*100:+6.1f}%) · mayor caída {dd*100:6.1f}% · "
              f"mayor subida {rally*100:6.1f}% · semanas rojas {(baj < 0).sum()}/{baj.notna().sum()}")


if __name__ == "__main__":
    vecindario()
    costos()
    cortos()
