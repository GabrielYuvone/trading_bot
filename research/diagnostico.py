"""Diagnóstico: replica EXACTA de la lógica del bot en producción y cuenta
en qué filtro muere cada posible entrada.

Objetivo: explicar por qué hace semanas que no entra una sola operación.
Uso:  python3 research/diagnostico.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos  # noqa: E402

# --- parámetros del bot en producción (BotConfig) -------------------------
ST_P, ST_M = 10, 3.0
EMA_P = 200
ADX_P, ADX_T = 14, 22.0
LEVERAGE = 10
FRACCION_EQUITY = 0.0075
CAPITAL_RIESGO = 50.0
FEE_RATE = 0.0005
SL_MIN_PCT = 0.002
DIST_LIQ = 1 / LEVERAGE - 0.005
DIST_SL_MAX = DIST_LIQ * 0.8
EQUITY = 1000.0          # equity típica de la cuenta demo


def analizar(symbol: str, df: pd.DataFrame, desde: pd.Timestamp | None = None) -> dict:
    d = datos.indicadores_base(df, ST_P, ST_M, ADX_P, EMA_P)
    if desde is not None:
        d = d[d["datetime"] >= desde]
    d = d.reset_index(drop=True)

    c = Counter()
    adx_en_giro, dist_en_giro = [], []

    for i in range(1, len(d)):
        a, b = d.iloc[i], d.iloc[i - 1]           # a = vela cerrada, b = anterior
        if np.isnan(a["adx"]) or np.isnan(a["st_lower"]) or np.isnan(a["st_upper"]):
            continue
        giro_alc = (not b["st_dir"]) and a["st_dir"]
        giro_baj = b["st_dir"] and (not a["st_dir"])
        if not (giro_alc or giro_baj):
            continue

        c["1_giros_st"] += 1
        adx_en_giro.append(a["adx"])

        if a["adx"] <= ADX_T:
            c["2_rech_ADX"] += 1
            continue
        c["3_pasa_ADX"] += 1

        if giro_alc:
            sl, dist = a["st_lower"], (a["close"] - a["st_lower"]) / a["close"]
            ema_ok = a["close"] > a["ema200"]
        else:
            sl, dist = a["st_upper"], (a["st_upper"] - a["close"]) / a["close"]
            ema_ok = a["close"] < a["ema200"]

        if not ema_ok:
            c["4_rech_EMA200"] += 1
            continue
        c["5_pasa_EMA200"] += 1

        dist_en_giro.append(dist * 100)
        if dist >= DIST_SL_MAX:
            c["6_rech_SL_lejos"] += 1
            continue
        if dist < SL_MIN_PCT:
            c["7_rech_SL_corto"] += 1
            continue
        c["8_pasa_distancia_SL"] += 1

        # dimensionado (mismo cálculo que TradeExecutor.dimensionar)
        riesgo = min(EQUITY * FRACCION_EQUITY, CAPITAL_RIESGO)
        cs = datos.CONTRACT_SIZE[symbol]
        perdida_x_contrato = (dist * a["close"] + 2 * FEE_RATE * a["close"]) * cs
        amount = riesgo / perdida_x_contrato
        minimo = cs                      # el mínimo operable en OKX es 1 contrato
        if amount < minimo:
            c["9_rech_tamano_minimo"] += 1
            continue
        margen = amount * cs * a["close"] / LEVERAGE
        if margen * 1.15 > EQUITY:
            c["10_rech_margen"] += 1
            continue
        c["11_ENTRARIA"] += 1

    return {
        "symbol": symbol,
        "velas": len(d),
        "dias": len(d) / 24,
        "conteos": c,
        "adx_medio_en_giro": float(np.mean(adx_en_giro)) if adx_en_giro else float("nan"),
        "adx_p25": float(np.percentile(adx_en_giro, 25)) if adx_en_giro else float("nan"),
        "dist_sl_mediana_pct": float(np.median(dist_en_giro)) if dist_en_giro else float("nan"),
    }


def main():
    data = datos.cargar_todos("1h")
    fin = max(df["datetime"].max() for df in data.values())
    desde_8s = fin - pd.Timedelta(weeks=8)

    for titulo, desde in (("HISTÓRICO COMPLETO (180 días)", None),
                          ("ÚLTIMAS 8 SEMANAS", desde_8s)):
        print("\n" + "=" * 96)
        print(titulo)
        print("=" * 96)
        filas = []
        tot = Counter()
        for s, df in data.items():
            r = analizar(s, df, desde)
            for k, v in r["conteos"].items():
                tot[k] += v
            filas.append({
                "símbolo": s.split("/")[0],
                "días": round(r["dias"]),
                "giros ST": r["conteos"]["1_giros_st"],
                "ADX<=22": r["conteos"]["2_rech_ADX"],
                "EMA200": r["conteos"]["4_rech_EMA200"],
                "SL fuera rango": r["conteos"]["6_rech_SL_lejos"] + r["conteos"]["7_rech_SL_corto"],
                "tamaño/margen": r["conteos"]["9_rech_tamano_minimo"] + r["conteos"]["10_rech_margen"],
                "ENTRADAS": r["conteos"]["11_ENTRARIA"],
                "ADX medio giro": round(r["adx_medio_en_giro"], 1),
                "dist SL med %": round(r["dist_sl_mediana_pct"], 2) if r["dist_sl_mediana_pct"] == r["dist_sl_mediana_pct"] else None,
            })
        t = pd.DataFrame(filas)
        print(t.to_string(index=False))
        print("\nTOTALES:", dict(sorted(tot.items())))
        n = tot["1_giros_st"]
        if n:
            print(f"\nDe {n} giros de SuperTrend: "
                  f"{(tot['2_rech_ADX']/n)*100:.0f}% muere por ADX, "
                  f"{(tot['4_rech_EMA200']/n)*100:.0f}% por EMA200, "
                  f"{((tot['6_rech_SL_lejos']+tot['7_rech_SL_corto'])/n)*100:.0f}% por distancia de SL, "
                  f"→ {tot['11_ENTRARIA']} entradas en {round(filas[0]['días'])} días "
                  f"= {tot['11_ENTRARIA']/(filas[0]['días']/7):.2f} por semana en todo el portafolio.")


if __name__ == "__main__":
    main()
