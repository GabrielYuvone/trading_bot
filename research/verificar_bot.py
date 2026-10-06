"""Verificación: ¿el bot en producción hace lo mismo que el backtest?

Toma las clases REALES del bot (TechnicalAnalyzer con la config nueva), las
corre sobre los mismos CSV con una ventana rodante de 1000 velas (igual que
hace el bot en vivo, que sólo descarga las últimas 1000) y pasa las señales
por el mismo motor de backtest.

Si los números se parecen a los de research/comparar.py, el backtest es
confiable. Si no, hay una divergencia entre investigación y producción.

Uso: python3 research/verificar_bot.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("OKX_API_KEY", "test")
os.environ.setdefault("OKX_API_SECRET", "test")
os.environ.setdefault("OKX_API_PASSWORD", "test")

import datos, motor, estrategias  # noqa: E402

# --- importar el módulo del bot sin ejecutarlo ---------------------------
spec = importlib.util.spec_from_file_location("botmod", ROOT / "bot_trading_refactored.py")
botmod = importlib.util.module_from_spec(spec)
sys.modules["botmod"] = botmod          # los @dataclass necesitan el módulo registrado
spec.loader.exec_module(botmod)

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)

VENTANA = 1000          # igual que config.limite_velas


def senales_del_bot(symbol: str, df: pd.DataFrame, cfg, ventana: int = VENTANA):
    """Señales que generaría el bot en vivo, vela a vela."""
    ta = botmod.TechnicalAnalyzer(cfg)
    n = len(df)
    senal = np.zeros(n)
    sl = np.full(n, np.nan)
    salida_l = np.zeros(n, dtype=bool)
    salida_s = np.zeros(n, dtype=bool)
    adx = np.full(n, np.nan)
    for i in range(cfg.velas_minimas, n):
        ini = max(0, i - ventana + 1)
        d = ta.calcular_indicadores(df.iloc[ini:i + 1].reset_index(drop=True))
        v = ta.extraer_valores(d)
        if v is None:
            continue
        adx[i] = v.adx
        s = ta.detectar_senal(v)
        if s == botmod.TrendDirection.UPTREND:
            senal[i], sl[i] = 1, v.lower_band
        elif s == botmod.TrendDirection.DOWNTREND:
            senal[i], sl[i] = -1, v.upper_band
        # salida: giro del SuperTrend contra la posición
        if v.st_direction_anterior and not v.st_direction:
            salida_l[i] = True
        if not v.st_direction_anterior and v.st_direction:
            salida_s[i] = True
    out = df[["datetime", "open", "high", "low", "close"]].copy()
    out["senal"] = senal
    out["sl"] = sl
    out["salida_l"] = salida_l
    out["salida_s"] = salida_s
    out["fuerza"] = np.nan_to_num(adx)
    return out


def main():
    cfg = botmod.BotConfig(
        st_periodo=24, st_multiplier=3.5, adx_threshold=20.0, periodo_ema=200,
        adx_periodo=14, permitir_cortos=True, usar_tp=False,
        fraccion_equity=0.0075, max_posiciones=3, velas_minimas=320,
    )
    print(f"Config del bot: ST({cfg.st_periodo},{cfg.st_multiplier}) · "
          f"ADX>{cfg.adx_threshold} · EMA{cfg.periodo_ema} · ventana {VENTANA} velas")

    data = datos.cargar_todos("1h")

    # 1) señales del backtest (historia completa)
    sen_bt = {s: estrategias.st_flip(df, st_p=24, st_m=3.5, adx_t=20.0) for s, df in data.items()}
    # 2) señales del bot en vivo (ventana rodante)
    sen_bot = {s: senales_del_bot(s, df, cfg) for s, df in data.items()}

    print("\n--- Coincidencia de señales por símbolo ---")
    for s in data:
        a, b = sen_bt[s]["senal"].values, sen_bot[s]["senal"].values
        mask = (a != 0) | (b != 0)
        iguales = int(((a == b) & mask).sum())
        print(f"  {s.split('/')[0]:<5} señales backtest {int((a != 0).sum()):>4} · "
              f"señales bot {int((b != 0).sum()):>4} · coinciden {iguales:>4} · "
              f"difieren {int(((a != b) & mask).sum()):>3}")

    params = motor.Params(riesgo_frac=0.0075, max_posiciones=3)
    r_bt = motor.simular(sen_bt, params, nombre="backtest (historia completa)")
    r_bot = motor.simular(sen_bot, params, nombre="bot en vivo (ventana 1000)")
    t = pd.DataFrame([r_bt.resumen(), r_bot.resumen()])
    print("\n--- Resultado con el mismo motor ---")
    print(t.to_string(index=False))


if __name__ == "__main__":
    main()
