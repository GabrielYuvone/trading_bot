"""Ajuste fino: reglas de salida y dimensionamiento sobre las mejores configs.

Uso: python3 research/ajuste_fino.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos, motor, estrategias  # noqa: E402

pd.set_option("display.width", 260)
pd.set_option("display.max_columns", 50)
pd.set_option("display.max_rows", 400)

TF = "1h"
DATA = datos.cargar_todos(TF)
FECHAS = DATA["BTC/USDT:USDT"]["datetime"]
T0, T1 = FECHAS.iloc[0], FECHAS.iloc[-1]
CORTE = T0 + (T1 - T0) * 0.6

CONFIGS = {
    "ST(10,3) ADX22 (actual)": dict(st_p=10, st_m=3.0, adx_t=22.0),
    "ST(14,3.5) ADX20": dict(st_p=14, st_m=3.5, adx_t=20.0),
    "ST(20,3.5) ADX20": dict(st_p=20, st_m=3.5, adx_t=20.0),
}
_cache = {}


def sigs(kw):
    clave = tuple(sorted(kw.items()))
    if clave not in _cache:
        _cache[clave] = {s: estrategias.st_flip(df, **kw) for s, df in DATA.items()}
    return _cache[clave]


def correr(kw, params, desde=None, hasta=None, tp_r=None):
    out = {}
    for s, sig in sigs(kw).items():
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
    return motor.simular(out, params, nombre="")


def mostrar(filas, titulo):
    print("\n" + "=" * 110)
    print(titulo)
    print("=" * 110)
    print(pd.DataFrame(filas).to_string(index=False))


def main():
    # ---------- 1) break-even y tiempo máximo ----------
    filas = []
    for nombre, kw in CONFIGS.items():
        for be in (None, 1.0, 1.5, 2.0, 3.0):
            for horas in (None, 72, 168):
                p = motor.Params(riesgo_frac=0.0075, max_posiciones=3, be_r=be, max_horas=horas)
                f = correr(kw, p).resumen()
                i = correr(kw, p, hasta=CORTE).resumen()
                o = correr(kw, p, desde=CORTE).resumen()
                filas.append({"config": nombre, "BE": be or 0, "max_h": horas or 0,
                              "ret_%": f.get("retorno_%"), "DD_%": f.get("max_DD_%"),
                              "Sharpe": f.get("Sharpe"), "exp_R": f.get("expect_R"),
                              "PF": f.get("profit_factor"), "trades": f.get("trades"),
                              "ret_IS": i.get("retorno_%"), "ret_OOS": o.get("retorno_%")})
    t = pd.DataFrame(filas)
    t["peor"] = t[["ret_%", "ret_IS", "ret_OOS"]].min(axis=1)
    mostrar(t.sort_values("peor", ascending=False).head(30),
            "1) BREAK-EVEN Y TIEMPO MÁXIMO (ordenado por peor retorno full/IS/OOS)")

    # ---------- 2) riesgo por trade y nº de posiciones ----------
    filas = []
    for nombre, kw in CONFIGS.items():
        for riesgo in (0.005, 0.0075, 0.01, 0.015, 0.02):
            for maxpos in (2, 3, 4, 5):
                p = motor.Params(riesgo_frac=riesgo, max_posiciones=maxpos)
                f = correr(kw, p).resumen()
                i = correr(kw, p, hasta=CORTE).resumen()
                o = correr(kw, p, desde=CORTE).resumen()
                filas.append({"config": nombre, "riesgo_%": riesgo * 100, "max_pos": maxpos,
                              "ret_%": f.get("retorno_%"), "DD_%": f.get("max_DD_%"),
                              "Sharpe": f.get("Sharpe"), "exp_R": f.get("expect_R"),
                              "trades": f.get("trades"),
                              "ret_IS": i.get("retorno_%"), "ret_OOS": o.get("retorno_%")})
    t2 = pd.DataFrame(filas)
    t2["peor"] = t2[["ret_%", "ret_IS", "ret_OOS"]].min(axis=1)
    mostrar(t2.sort_values("peor", ascending=False).head(30),
            "2) RIESGO POR TRADE Y Nº DE POSICIONES")

    # ---------- 3) retorno mensual de la config elegida ----------
    for nombre, kw in CONFIGS.items():
        p = motor.Params(riesgo_frac=0.0075, max_posiciones=3)
        r = correr(kw, p)
        eq = r.equity
        mensual = eq.resample("ME").last().pct_change().dropna() * 100
        print(f"\n{nombre} · retorno mensual: " +
              " | ".join(f"{d.strftime('%Y-%m')}: {v:+.1f}%" for d, v in mensual.items()))


if __name__ == "__main__":
    main()
