"""Comparación de estrategias candidatas sobre los 5 símbolos.

Uso:
    python3 research/comparar.py                 # tabla comparativa
    python3 research/comparar.py --tf 30m        # otro timeframe
    python3 research/comparar.py --detalle A_actual_ST_ADX_EMA
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos, motor, estrategias  # noqa: E402

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)

# nombre: (función, kwargs, tp_en_R)
CANDIDATAS = {
    "A_actual_ST_ADX_EMA": (estrategias.st_flip, {}, 2.0),
    "A2_actual_sin_TP": (estrategias.st_flip, {}, None),
    "B_solo_ST": (estrategias.st_flip, {"usar_adx": False, "usar_ema": False}, 2.0),
    "C_ST_ADX": (estrategias.st_flip, {"usar_ema": False}, 2.0),
    "D_ST_pullback": (estrategias.st_pullback, {}, 2.0),
    "E_Donchian48": (estrategias.donchian_breakout, {}, None),
    "F_EMA_cruce": (estrategias.ema_cruce, {}, None),
    "G_Momentum14d": (estrategias.momentum, {}, None),
    "H_MeanReversion": (estrategias.mean_reversion, {}, 1.5),
}


def construir(fn, kwargs, tf="1h", solo_long=False, tp_r=None,
              desde=None, hasta=None):
    data = datos.cargar_todos(tf)
    senales = {}
    for s, df in data.items():
        d = df if desde is None else df[df["datetime"] >= desde]
        d = d if hasta is None else d[d["datetime"] <= hasta]
        sig = fn(d.reset_index(drop=True), **kwargs)
        if solo_long:
            sig.loc[sig["senal"] == -1, "senal"] = 0
        if tp_r is not None:
            sig["tp"] = float(tp_r)
        senales[s] = sig
    return senales


def correr(nombre, fn, kwargs, tf="1h", params=None, solo_long=False, tp_r=None,
           desde=None, hasta=None):
    senales = construir(fn, kwargs, tf, solo_long, tp_r, desde, hasta)
    return motor.simular(senales, params or motor.Params(), nombre=nombre)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--riesgo", type=float, default=0.0075)
    ap.add_argument("--maxpos", type=int, default=3)
    ap.add_argument("--solo-long", action="store_true")
    ap.add_argument("--detalle", default=None)
    args = ap.parse_args()

    params = motor.Params(riesgo_frac=args.riesgo, max_posiciones=args.maxpos)

    if args.detalle:
        fn, kw, tp = CANDIDATAS[args.detalle]
        res = correr(args.detalle, fn, kw, args.tf, params, args.solo_long, tp)
        print(res.resumen())
        filas = [{
            "símbolo": t.symbol.split("/")[0], "lado": "L" if t.side == 1 else "S",
            "entrada": t.entry_t, "salida": t.exit_t, "entrada_px": round(t.entry_px, 4),
            "salida_px": round(t.exit_px, 4), "horas": t.exit_idx - t.entry_idx,
            "razón": t.razon, "pnl": round(t.pnl, 2), "R": round(t.r, 2),
        } for t in res.trades]
        print(pd.DataFrame(filas).to_string(index=False))
        # por símbolo
        por = pd.DataFrame([{
            "símbolo": t.symbol.split("/")[0], "lado": "L" if t.side == 1 else "S",
            "R": t.r, "pnl": t.pnl} for t in res.trades])
        if len(por):
            print("\nPor símbolo:")
            print(por.groupby("símbolo").agg(n=("R", "size"), expect_R=("R", "mean"),
                                             pnl=("pnl", "sum")).round(3).to_string())
        return

    filas = []
    for nombre, (fn, kw, tp) in CANDIDATAS.items():
        res = correr(nombre, fn, kw, args.tf, params, args.solo_long, tp)
        filas.append(res.resumen())
    t = pd.DataFrame(filas)
    print(f"\n=== COMPARATIVA tf={args.tf} · riesgo {args.riesgo*100:.2f}% · "
          f"máx {args.maxpos} posiciones · costos reales (fee 5bp + slip 3bp + funding) ===")
    print(t.to_string(index=False))


if __name__ == "__main__":
    main()
