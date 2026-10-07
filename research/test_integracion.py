"""Prueba de integración: el bot REAL (bot_trading_refactored.py) contra un
exchange simulado que reproduce las velas históricas.

No toca la red. Ejercita el camino completo: detección de señal → orden →
stop en el exchange → conciliación → cierre por giro del SuperTrend → PnL.

Sirve para responder dos preguntas:
  1) ¿el bot de producción ejecuta lo que dice el backtest?
  2) ¿qué pasa cuando la API falla? (señal diferida, no perdida)

Uso: python3 research/test_integracion.py [--fallos-api N]
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

import datos  # noqa: E402

spec = importlib.util.spec_from_file_location("botmod", ROOT / "bot_trading_refactored.py")
botmod = importlib.util.module_from_spec(spec)
sys.modules["botmod"] = botmod
spec.loader.exec_module(botmod)

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)

FEE = 0.0005
SLIP = 0.0003


# ===========================================================================
# Reloj virtual
# ===========================================================================

class Reloj:
    def __init__(self, ts_ms: int):
        self.ms = ts_ms

    def ahora(self, tz=None):
        import datetime as dt
        return dt.datetime.fromtimestamp(self.ms / 1000, tz=dt.timezone.utc).astimezone(tz) \
            if tz else dt.datetime.utcfromtimestamp(self.ms / 1000)


# ===========================================================================
# Exchange simulado
# ===========================================================================

class ExchangeFalso:
    def __init__(self, cfg, data: dict, reloj: Reloj):
        self.config = cfg
        self.data = {s: df.reset_index(drop=True) for s, df in data.items()}
        self.reloj = reloj
        self.logger = botmod.logging.getLogger("ExchangeFalso")
        self.posiciones: dict = {}
        self.algos: dict = {}
        self.equity = 1000.0
        self.fills: list = []
        self.ordenes_rechazadas = 0
        self.vela_actual: dict = {}
        self.indice: dict = {s: 0 for s in self.data}

    # --- reloj / datos ---------------------------------------------------
    def _indice_actual(self, symbol: str) -> int:
        """Última vela CERRADA para el reloj actual.

        El reloj vale exactamente el instante en que cierra una vela
        (= apertura de la siguiente). Con side="left" la vela que recién
        abre NO cuenta como cerrada: es lo que ve el bot en vivo.
        """
        ts = self.data[symbol]["timestamp"].values
        return int(np.searchsorted(ts, self.reloj.ms, side="left") - 1)

    def obtener_velas_cerradas(self, symbol: str):
        i = self._indice_actual(symbol)
        if i < 10:
            return None
        ini = max(0, i - self.config.limite_velas + 1)
        return self.data[symbol].iloc[ini:i + 1].reset_index(drop=True)

    def obtener_ticker(self, symbol: str):
        i = self._indice_actual(symbol)
        return {"last": float(self.data[symbol]["close"].iloc[i])}

    # --- metadatos -------------------------------------------------------
    def contract_size(self, symbol):
        return datos.CONTRACT_SIZE[symbol]

    def precio_preciso(self, symbol, px):
        return float(px)

    def cantidad_precisa(self, symbol, amount):
        paso = datos.LOT_SIZE[symbol]
        return float(np.floor(amount / paso) * paso)

    def cantidad_minima(self, symbol):
        return datos.MIN_SIZE[symbol]

    def balance_usdt(self):
        return self.equity, self.equity

    # --- posiciones / órdenes --------------------------------------------
    def posiciones_todas(self):
        if getattr(self, "fallar_proximo", 0) > 0:
            self.fallar_proximo -= 1
            raise RuntimeError("fallo simulado de API")
        return {s: dict(p) for s, p in self.posiciones.items()}

    def posicion_o_none(self, symbol):
        return self.posiciones.get(symbol)

    def crear_orden_mercado(self, symbol, side, amount, reduce_only):
        i = self._indice_actual(symbol) + 1          # se ejecuta en la vela siguiente
        px = float(self.data[symbol]["open"].iloc[i]) * (1 + SLIP * (1 if side == "buy" else -1))
        cs = self.contract_size(symbol)
        notional = amount * cs * px
        fee = notional * FEE
        self.equity -= fee
        if reduce_only:
            pos = self.posiciones.pop(symbol, None)
            if pos:
                pnl = (px - pos["entryPrice"]) * pos["side_sign"] * amount * cs - fee
                self.equity += pnl
                self.fills.append((symbol, "cierre", px, pnl))
        else:
            self.posiciones[symbol] = {
                "symbol": symbol, "side": "long" if side == "buy" else "short",
                "side_sign": 1 if side == "buy" else -1,
                "contracts": amount, "entryPrice": px, "entry_ms": self.reloj.ms,
            }
            self.fills.append((symbol, "apertura", px, -fee))
        return {"id": f"o{len(self.fills)}", "filled": amount, "average": px}

    def detalle_fill(self, order, symbol):
        return float(order["filled"]), float(order["average"])

    def crear_proteccion(self, symbol, side_entrada, amount, sl, tp=None):
        self.algos[symbol] = {"algoId": f"a{len(self.algos)}", "sl": sl, "tp": tp,
                              "sz": amount}
        return self.algos[symbol]["algoId"]

    def algos_pendientes(self, symbol):
        return [self.algos[symbol]] if symbol in self.algos else []

    def cancelar_algos(self, symbol):
        self.algos.pop(symbol, None)
        return True

    def cerrar_todo(self, symbol):
        pos = self.posiciones.get(symbol)
        if not pos:
            return True
        self.crear_orden_mercado(symbol, "sell" if pos["side"] == "long" else "buy",
                                 pos["contracts"], reduce_only=True)
        self.algos.pop(symbol, None)
        return True

    def resultado_real(self, symbol, side, since_ms):
        px = None
        fees = 0.0
        for s, tipo, p, pnl in reversed(self.fills):
            if s == symbol and tipo == "cierre":
                px = p
                break
        return {"exit_px": px, "fees": fees, "funding": 0.0, "qty": 1}

    # --- avance del tiempo -----------------------------------------------
    def cerrar_vela(self, symbol):
        """El exchange ejecuta stops contra la vela que se acaba de cerrar."""
        i = self._indice_actual(symbol)
        if i < 1:
            return
        bar = self.data[symbol].iloc[i]
        pos = self.posiciones.get(symbol)
        algo = self.algos.get(symbol)
        if not pos or not algo:
            return
        hi, lo, op = float(bar["high"]), float(bar["low"]), float(bar["open"])
        sl = algo["sl"]
        if pos["side"] == "long" and lo <= sl:
            fill = min(sl, op)
        elif pos["side"] == "short" and hi >= sl:
            fill = max(sl, op)
        else:
            return
        self.algos.pop(symbol, None)
        self.crear_orden_mercado(symbol, "sell" if pos["side"] == "long" else "buy",
                                 pos["contracts"], reduce_only=True)
        # precio de salida real = fill con slippage
        self.fills[-1] = (symbol, "cierre", fill, self.fills[-1][3])


# ===========================================================================
# Test
# ===========================================================================

def correr(fallos_api: int = 0, verbose: bool = False):
    data = datos.cargar_todos("1h")
    simbolos = list(data.keys())
    inicio = max(int(df["timestamp"].iloc[400]) for df in data.values())
    reloj = Reloj(inicio)
    botmod.datetime = type("FakeDatetime", (), {})      # placeholder
    import datetime as dt

    class FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.fromtimestamp(reloj.ms / 1000, tz=dt.timezone.utc).astimezone(tz) \
                if tz else dt.datetime.fromtimestamp(reloj.ms / 1000, tz=dt.timezone.utc)

        @classmethod
        def utcnow(cls):
            return dt.datetime.fromtimestamp(reloj.ms / 1000, tz=dt.timezone.utc)

    botmod.datetime = FakeDT

    cfg = botmod.BotConfig(
        simbolos=simbolos, st_periodo=24, st_multiplier=3.5, adx_threshold=20.0,
        periodo_ema=200, adx_periodo=14, permitir_cortos=True, usar_tp=False,
        fraccion_equity=0.0075, max_posiciones=4, velas_minimas=320,
        ciclo_segundos=60, heartbeat_horas=6.0,
    )
    import tempfile
    tmpdir = tempfile.mkdtemp()
    db = botmod.Persistencia(os.path.join(tmpdir, "trades_test.db"))
    ex = ExchangeFalso(cfg, data, reloj)
    bot = botmod.TradingBot.__new__(botmod.TradingBot)
    bot.config = cfg
    bot.db = db
    bot.exchange = ex
    bot.analyzer = botmod.TechnicalAnalyzer(cfg)
    bot.executor = botmod.TradeExecutor(ex, cfg, db)
    bot.sim = False
    bot.logger = botmod.logging.getLogger("TradingBotTest")
    bot.activo = True
    bot.ciclo_contador = 0
    bot.ultimo_reporte_ts = None
    bot.simbolos_ok = list(simbolos)
    bot.posiciones = {}
    bot._lock = botmod.threading.Lock()
    bot._estado = {}
    bot._huerfanas_avisadas = set()
    bot._limite_avisado_dia = None

    tf_ms = 3600 * 1000
    n_ciclos = 0
    while reloj.ms < min(int(df["timestamp"].iloc[-2]) for df in data.values()):
        if fallos_api and n_ciclos % 40 == 39:
            ex.fallar_proximo = fallos_api
        bot.ciclo_analisis()
        n_ciclos += 1
        # avanzar una hora: el exchange ejecuta los stops de la vela cerrada
        for s in simbolos:
            reloj.ms += 0
        reloj.ms += tf_ms
        for s in simbolos:
            ex.cerrar_vela(s)

    import tempfile, os as _os
    with db._conn() as c:
        filas = c.execute("SELECT symbol, side, entry_ts, entry_px, exit_px, razon_salida, pnl, r_mult "
                          "FROM trades ORDER BY id").fetchall()
    pd.DataFrame([dict(f) for f in filas]).to_csv("/tmp/trades_sim.csv", index=False)
    print("(lista de trades guardada en /tmp/trades_sim.csv)")
    trades = db.estadisticas(virtual=False)
    r = np.array([t for t in [db.trade_abierto(s, virtual=False) for s in simbolos] if t])
    print(f"\n=== RESULTADO (ciclos simulados: {n_ciclos}) ===")
    print(f"Trades cerrados: {trades['trades']} · WR {trades['win_rate']} · "
          f"expectativa {trades['expectativa_r']} · PnL {trades['pnl']:.2f} USDT")
    print(f"Equity final del exchange simulado: {ex.equity:.2f} USDT")
    d = botmod.diagnostico.resumen()
    print(f"Señales detectadas: {d['senales_detectadas']} · ejecutadas: {d['entradas_ejecutadas']} "
          f"· fallidas: {d['entradas_fallidas']}")
    print("Bloqueos (todos):")
    for k, v in d["bloqueos_totales"].items():
        print(f"   ×{v:<6} {k}")
    if verbose:
        with db._conn() as c:
            filas = c.execute("SELECT symbol, side, entry_px, exit_px, razon_salida, pnl, r_mult "
                              "FROM trades WHERE exit_ts IS NOT NULL ORDER BY id").fetchall()
        print(pd.DataFrame([dict(f) for f in filas]).to_string(index=False))
    return trades, ex.equity


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fallos-api", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    correr(a.fallos_api, a.verbose)
