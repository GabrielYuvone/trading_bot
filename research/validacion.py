"""Validación de la configuración final:
   1) aporte por símbolo, 2) concentración en pocos trades, 3) bootstrap,
   4) leave-one-out, 5) mismo set-up en otros timeframes.

Uso: python3 research/validacion.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos, motor, estrategias  # noqa: E402

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)

KW = dict(st_p=20, st_m=3.5, adx_t=20.0, usar_adx=True, usar_ema=True, cortos=True)
PARAMS = motor.Params(riesgo_frac=0.0075, max_posiciones=3)
TF = "1h"


def senales(tf=TF, simbolos=None):
    data = datos.cargar_todos(tf)
    if simbolos:
        data = {k: v for k, v in data.items() if k in simbolos}
    return {s: estrategias.st_flip(df, **KW) for s, df in data.items()}


def main():
    # ---------- 1) portafolio completo ----------
    r = motor.simular(senales(), PARAMS, nombre="FINAL ST(20,3.5) ADX>20")
    print("\n" + "=" * 110)
    print("CONFIG FINAL · 5 símbolos · 1h · riesgo 0.75% · máx 3 posiciones")
    print("=" * 110)
    print(pd.DataFrame([r.resumen()]).to_string(index=False))

    # ---------- 2) aporte por símbolo ----------
    por = pd.DataFrame([{"símbolo": t.symbol.split("/")[0],
                         "lado": "L" if t.side == 1 else "S",
                         "R": t.r, "pnl": t.pnl} for t in r.trades])
    print("\n--- Aporte por símbolo ---")
    print(por.groupby("símbolo").agg(n=("R", "size"), expect_R=("R", "mean"),
                                     pnl=("pnl", "sum")).round(3).to_string())
    print("\n--- Largo vs corto ---")
    print(por.groupby("lado").agg(n=("R", "size"), expect_R=("R", "mean"),
                                  pnl=("pnl", "sum")).round(3).to_string())

    # ---------- 3) concentración ----------
    rr = np.sort(np.array([t.r for t in r.trades]))[::-1]
    total = rr.sum()
    print(f"\n--- Concentración: {len(rr)} trades, suma R = {total:.1f}")
    for k in (1, 2, 3, 5, 10):
        print(f"  top {k:>2} trades aportan el {rr[:k].sum()/total*100:5.1f}% del total")
    print(f"  peores 5: {rr[-5:].sum():.1f}R · mediana R: {np.median(rr):+.2f}")
    print(f"  distribución por razón de salida:")
    raz = pd.Series([t.razon for t in r.trades]).value_counts()
    print("   " + raz.to_string().replace("\n", "\n   "))

    # ---------- 4) bootstrap ----------
    rng = np.random.default_rng(7)
    n = len(rr)
    medias, totales = [], []
    for _ in range(20000):
        muestra = rng.choice(rr, size=n, replace=True)
        medias.append(muestra.mean())
        # equity compuesta con 0.75% por unidad de R
        totales.append(np.prod(1 + muestra * PARAMS.riesgo_frac) - 1)
    medias, totales = np.array(medias), np.array(totales)
    print(f"\n--- Bootstrap (20.000 remuestreos de la secuencia de {n} trades) ---")
    print(f"  expectativa R: media {medias.mean():+.3f} · IC95% "
          f"[{np.percentile(medias,2.5):+.3f}, {np.percentile(medias,97.5):+.3f}]")
    print(f"  P(expectativa > 0) = {(medias > 0).mean()*100:.1f}%")
    print(f"  retorno 6 meses: mediana {np.median(totales)*100:+.1f}% · IC95% "
          f"[{np.percentile(totales,2.5)*100:+.1f}%, {np.percentile(totales,97.5)*100:+.1f}%]")
    print(f"  P(retorno > 0) = {(totales > 0).mean()*100:.1f}%")

    # ---------- 5) leave-one-out ----------
    print("\n--- Leave-one-out (quito un símbolo; ¿se cae el resultado?) ---")
    filas = []
    for s in datos.SIMBOLOS:
        resto = [x for x in datos.SIMBOLOS if x != s]
        rr2 = motor.simular(senales(simbolos=resto),
                            motor.Params(riesgo_frac=0.0075, max_posiciones=3))
        d = rr2.resumen()
        d["sin"] = s.split("/")[0]
        filas.append(d)
    print(pd.DataFrame(filas)[["sin", "retorno_%", "max_DD_%", "Sharpe", "trades",
                               "win_rate_%", "expect_R", "profit_factor"]].to_string(index=False))

    # ---------- 6) otros timeframes ----------
    print("\n--- Mismo set-up en otros timeframes (robustez) ---")
    filas = []
    for tf in ("15m", "30m", "1h"):
        try:
            rtf = motor.simular(senales(tf), motor.Params(riesgo_frac=0.0075, max_posiciones=3))
            d = rtf.resumen()
            d["tf"] = tf
            filas.append(d)
        except Exception as e:
            print(f"  {tf}: {e}")
    print(pd.DataFrame(filas)[["tf", "retorno_%", "max_DD_%", "Sharpe", "trades",
                               "win_rate_%", "expect_R", "profit_factor"]].to_string(index=False))


if __name__ == "__main__":
    main()
