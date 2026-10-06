"""Motor de backtest event-driven para el portafolio de swaps.

Características (todo conservador / realista):
- Entrada en la APERTURA de la vela siguiente a la señal + slippage.
- Stop evaluado intra-vela (high/low). Si una vela toca SL y TP → gana el SL.
- Comisiones taker por lado + slippage + funding cada 8 h.
- Tamaño por riesgo fijo (% del equity / distancia al stop), redondeado al
  tamaño de contrato de OKX, con mínimo de 1 contrato y tope de apalancamiento.
- Portafolio multi-símbolo con tope de posiciones simultáneas y equity común.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datos  # noqa: E402


@dataclass
class Costos:
    fee: float = 0.0005          # taker OKX VIP0 por lado
    slippage: float = 0.0003     # market order, majors líquidos
    funding_8h: float = 0.0001   # 0.01% cada 8 h sobre el notional (coste)


@dataclass
class Params:
    equity_inicial: float = 1000.0
    riesgo_frac: float = 0.0075
    riesgo_max_usdt: float = 1e9
    max_posiciones: int = 3
    leverage_max: float = 10.0
    max_horas: Optional[int] = None   # cierre forzado por tiempo
    cooldown_barras: int = 0          # velas de espera tras cerrar antes de re-entrar
    be_r: Optional[float] = None      # mover el stop a break-even al llegar a +be_r R
    costos: Costos = field(default_factory=Costos)


@dataclass
class Trade:
    symbol: str
    side: int                 # 1 long, -1 short
    entry_idx: int
    entry_t: pd.Timestamp
    entry_px: float
    size: float               # contratos
    notional: float
    riesgo: float             # 1R en USDT
    sl_px: float = 0.0
    tp_px: float = np.nan
    dist_r: float = 0.0
    be_activado: bool = False
    exit_idx: int = -1
    exit_t: Optional[pd.Timestamp] = None
    exit_px: float = 0.0
    razon: str = ""
    pnl: float = 0.0
    r: float = 0.0
    fees: float = 0.0
    funding: float = 0.0


@dataclass
class Resultado:
    trades: List[Trade]
    equity: pd.Series
    params: Params
    nombre: str = ""

    @property
    def n(self) -> int:
        return len(self.trades)

    def resumen(self) -> dict:
        eq = self.equity
        if len(eq) < 2:
            return {}
        r_mult = np.array([t.r for t in self.trades]) if self.trades else np.array([0.0])
        pnl = np.array([t.pnl for t in self.trades]) if self.trades else np.array([0.0])
        ret_total = eq.iloc[-1] / eq.iloc[0] - 1
        dias = (eq.index[-1] - eq.index[0]).total_seconds() / 86400
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (365.25 / max(dias, 1)) - 1 if dias > 1 else np.nan
        dd = eq / eq.cummax() - 1
        max_dd = float(dd.min())
        ret_h = eq.pct_change().fillna(0)
        sharpe = float(ret_h.mean() / ret_h.std() * np.sqrt(24 * 365)) if ret_h.std() > 0 else np.nan
        gan, per = pnl[pnl > 0], pnl[pnl <= 0]
        pf = float(gan.sum() / abs(per.sum())) if len(per) and per.sum() != 0 else float("inf")
        dur = np.array([(t.exit_idx - t.entry_idx) for t in self.trades]) if self.trades else np.array([0])
        return {
            "estrategia": self.nombre,
            "retorno_%": round(ret_total * 100, 1),
            "CAGR_%": round(cagr * 100, 1) if cagr == cagr else np.nan,
            "max_DD_%": round(max_dd * 100, 1),
            "MAR": round((cagr / abs(max_dd)) if max_dd < 0 else np.nan, 2) if cagr == cagr else np.nan,
            "Sharpe": round(sharpe, 2) if sharpe == sharpe else np.nan,
            "trades": self.n,
            "trades/sem": round(self.n / (dias / 7), 2) if dias > 0 else np.nan,
            "win_rate_%": round(float((pnl > 0).mean() * 100), 1) if self.n else np.nan,
            "expect_R": round(float(r_mult.mean()), 3) if self.n else np.nan,
            "profit_factor": round(pf, 2),
            "horas_media": round(float(dur.mean()), 1) if self.n else np.nan,
            "equity_final": round(float(eq.iloc[-1]), 2),
        }


def simular(senales: Dict[str, pd.DataFrame], params: Params, nombre: str = "",
            salida_por_senal: bool = True) -> Resultado:
    """senales: {symbol: DataFrame} con columnas
        ['datetime','open','high','low','close',
         'senal'  (1/-1/0: entrada en la próxima apertura),
         'sl'     (precio del stop inicial),
         'trail'  (opcional: stop dinámico; se aplica max/min según el lado),
         'salida' (opcional bool: cerrar la posición abierta)]
    """
    simbolos = list(senales.keys())
    ref = senales[simbolos[0]]
    n_bars = len(ref)
    cost = params.costos
    estado = {"eq": params.equity_inicial}
    posiciones: Dict[str, Trade] = {}
    trades: List[Trade] = []
    curva = []
    ultima_salida_idx: Dict[str, int] = {}

    def px_entrada(px: float, side: int) -> float:
        return px * (1 + cost.slippage * side)

    def px_salida(px: float, side: int) -> float:
        return px * (1 - cost.slippage * side)

    for i in range(n_bars - 1):
        t = ref["datetime"].iloc[i]

        # ---------- 1) gestión de posiciones ----------
        for s in list(posiciones.keys()):
            tr = posiciones[s]
            d = senales[s]
            bar = d.iloc[i]
            hi, lo = float(bar["high"]), float(bar["low"])
            op = float(bar["open"])

            if i > tr.entry_idx:                     # el stop rige desde la vela siguiente
                if tr.side == 1 and lo <= tr.sl_px:
                    _cerrar(tr, i, t, px_salida(min(tr.sl_px, op), 1), "stop", trades, posiciones, s, cost, estado, salidas=ultima_salida_idx)
                    continue
                if tr.side == -1 and hi >= tr.sl_px:
                    _cerrar(tr, i, t, px_salida(max(tr.sl_px, op), -1), "stop", trades, posiciones, s, cost, estado, salidas=ultima_salida_idx)
                    continue
                # take profit (si la vela toca SL y TP, gana el SL: ya salimos arriba)
                tp = tr.tp_px
                if tp == tp and i > tr.entry_idx:
                    if (tr.side == 1 and hi >= tp) or (tr.side == -1 and lo <= tp):
                        fill = max(tp, op) if tr.side == 1 else min(tp, op)
                        _cerrar(tr, i, t, px_salida(fill, tr.side), "take_profit", trades, posiciones, s, cost, estado, salidas=ultima_salida_idx)
                        continue

            # break-even: se activa desde la vela siguiente (conservador)
            if params.be_r is not None and not tr.be_activado:
                objetivo = tr.entry_px + tr.side * params.be_r * tr.dist_r
                if (tr.side == 1 and hi >= objetivo) or (tr.side == -1 and lo <= objetivo):
                    tr.be_activado = True
                    tr.sl_px = tr.entry_px      # break-even puro (las fees ya no se recuperan)

            # stop dinámico (trailing)
            if tr.side == 1:
                col = "trail_l" if "trail_l" in d.columns else ("trail" if "trail" in d.columns else None)
                if col:
                    nuevo = d[col].iloc[i]
                    if nuevo == nuevo:
                        tr.sl_px = max(tr.sl_px, float(nuevo))
            else:
                col = "trail_s" if "trail_s" in d.columns else ("trail" if "trail" in d.columns else None)
                if col:
                    nuevo = d[col].iloc[i]
                    if nuevo == nuevo:
                        tr.sl_px = min(tr.sl_px, float(nuevo))

            # salida por señal contraria o flag de salida
            if salida_por_senal:
                salir = False
                if tr.side == 1 and "salida_l" in d.columns:
                    salir = bool(d["salida_l"].iloc[i])
                elif tr.side == -1 and "salida_s" in d.columns:
                    salir = bool(d["salida_s"].iloc[i])
                elif "salida" in d.columns:
                    salir = bool(d["salida"].iloc[i])
                sig = d["senal"].iloc[i]
                if sig == sig and sig != 0 and sig != tr.side:
                    salir = True
                if salir:
                    _cerrar(tr, i + 1, d["datetime"].iloc[i + 1],
                            px_salida(float(d["open"].iloc[i + 1]), tr.side), "señal",
                            trades, posiciones, s, cost, estado, salidas=ultima_salida_idx)
                    continue

            # cierre por tiempo máximo
            if params.max_horas and (i - tr.entry_idx) >= params.max_horas:
                _cerrar(tr, i + 1, d["datetime"].iloc[i + 1],
                        px_salida(float(d["open"].iloc[i + 1]), tr.side), "tiempo",
                        trades, posiciones, s, cost, estado, salidas=ultima_salida_idx)
                continue

            # funding sobre el notional
            horas = (d["datetime"].iloc[i] - d["datetime"].iloc[i - 1]).total_seconds() / 3600 if i else 0
            f = tr.notional * cost.funding_8h * (horas / 8)
            tr.funding -= f
            estado["eq"] -= f

        # ---------- 2) entradas (apertura de la vela i+1) ----------
        libres = params.max_posiciones - len(posiciones)
        if libres > 0:
            candidatos = []
            for s in simbolos:
                if s in posiciones:
                    continue
                if params.cooldown_barras and (i - ultima_salida_idx.get(s, -10**9)) < params.cooldown_barras:
                    continue
                d = senales[s]
                sig = d["senal"].iloc[i]
                if sig != sig or sig == 0:
                    continue
                sl = d["sl"].iloc[i]
                if sl != sl:
                    continue
                fuerza = float(d["fuerza"].iloc[i]) if "fuerza" in d.columns else 1.0
                candidatos.append((fuerza if fuerza == fuerza else -9e9, s, int(sig), float(sl)))
            candidatos.sort(reverse=True)
            for _, s, sig, sl in candidatos[:libres]:
                d = senales[s]
                px = px_entrada(float(d["open"].iloc[i + 1]), sig)
                dist = abs(px - sl) / px
                if dist <= 0 or dist > 0.25:
                    continue
                riesgo = min(estado["eq"] * params.riesgo_frac, params.riesgo_max_usdt)
                cs = datos.CONTRACT_SIZE[s]
                paso = datos.LOT_SIZE.get(s, 1.0)
                contratos = float(np.floor((riesgo / dist) / (px * cs) / paso) * paso)
                if contratos < datos.MIN_SIZE.get(s, 1.0) - 1e-12:
                    continue
                notional = contratos * cs * px
                if notional > estado["eq"] * params.leverage_max:
                    continue
                fee_ent = notional * cost.fee
                tr = Trade(symbol=s, side=sig, entry_idx=i + 1,
                           entry_t=d["datetime"].iloc[i + 1], entry_px=px,
                           size=contratos, notional=notional,
                           riesgo=abs(px - sl) * contratos * cs + fee_ent,
                           sl_px=sl, dist_r=abs(px - sl))
                if "tp" in d.columns:
                    tp_v = d["tp"].iloc[i]
                    if tp_v == tp_v:
                        # 'tp' puede ser un multiplicador de R (ej. 2.0) o un precio
                        tr.tp_px = (px + float(tp_v) * abs(px - sl) * sig) if float(tp_v) <= 20 else float(tp_v)
                tr.fees = fee_ent
                estado["eq"] -= fee_ent
                posiciones[s] = tr

        # ---------- 3) mark to market ----------
        marca = estado["eq"]
        for s, tr in posiciones.items():
            px = float(senales[s]["close"].iloc[i])
            marca += (px - tr.entry_px) * tr.side * tr.size * datos.CONTRACT_SIZE[s]
        curva.append((t, marca))

    for s, tr in list(posiciones.items()):
        tr.exit_px = float(senales[s]["close"].iloc[-1])
        _cerrar(tr, n_bars - 1, senales[s]["datetime"].iloc[-1], tr.exit_px,
                "fin de datos", trades, posiciones, s, cost, estado, aplicar_fee=False)

    eq = pd.Series([v for _, v in curva], index=[t for t, _ in curva], name="equity")
    return Resultado(trades=trades, equity=eq, params=params, nombre=nombre)


def _cerrar(tr: Trade, idx: int, t: pd.Timestamp, px: float, razon: str, trades: list,
            posiciones: dict, s: str, cost: Costos, estado: dict, aplicar_fee: bool = True,
            salidas: Optional[dict] = None):
    cs = datos.CONTRACT_SIZE[s]
    bruto = (px - tr.entry_px) * tr.side * tr.size * cs
    fee_sal = abs(px * tr.size * cs) * cost.fee if aplicar_fee else 0.0
    tr.fees += fee_sal
    tr.exit_px = px
    tr.pnl = bruto - tr.fees + tr.funding
    tr.r = tr.pnl / tr.riesgo if tr.riesgo > 0 else 0.0
    tr.exit_idx = idx
    tr.exit_t = t
    tr.razon = razon
    estado["eq"] += tr.pnl
    if salidas is not None:
        salidas[s] = idx
    trades.append(tr)
    posiciones.pop(s, None)
