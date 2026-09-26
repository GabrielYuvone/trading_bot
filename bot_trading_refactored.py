#!/usr/bin/env python3
"""
Bot de Trading OKX Testnet — v2

Estrategia: SuperTrend(10, 3) + EMA200 + ADX(14), velas de 1h, solo velas cerradas.

Cambios respecto de la versión "opción A" (revisión técnica, expediente 001):

CRÍTICOS
- SL y TP en UNA sola orden OCO con reduceOnly: cuando una se ejecuta, la otra
  se cancela y ninguna puede abrir una posición nueva.
- Ningún cierre usa órdenes sin reduceOnly. Si falla close-position y fallan
  los reintentos reduceOnly, se emite EMERGENCIA; nunca se "fuerza" con market.
- cerrar_posicion cierra PRIMERO y cancela las algo-orders DESPUÉS. Si el cierre
  falla, la protección queda en pie.

IMPORTANTES
- Modo simulación real: posiciones virtuales en SQLite, SL/TP evaluados contra
  high/low de cada vela cerrada (si tocan ambos, se asume SL), salida por giro
  del SuperTrend, comisiones simuladas y equity virtual.
- Conciliación: si OKX ejecutó el SL/TP, el bot lo detecta, lee los fills
  (precio, comisiones, funding), calcula PnL y R, y cierra el registro.
- Límite diario de pérdidas en R y control de margen disponible antes de entrar.
- Comisiones incluidas en el dimensionado.

MENORES
- La llave "una señal = una vela" se persiste en SQLite (sobrevive reinicios).
- TP recalculado desde el precio real de fill; entry_px = fill.
- La vela cerrada se determina por timestamp, no asumiendo iloc[-1].
- /logs protegido con LOGS_TOKEN. Estado compartido con lock + snapshot.
- Logging y validación de credenciales fuera de la importación del módulo.

Variables de entorno:
  OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD   (obligatorias)
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID                 (opcionales)
  OKX_LEVERAGE=10  CAPITAL_RIESGO_USDT=50  FRACCION_EQUITY=0.0075
  LIMITE_DIARIO_R=3  FEE_RATE=0.0005  MODO_SIMULACION=1
  SIM_EQUITY_INICIAL=1000  LOGS_TOKEN=...  PORT=5000
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple
import html
import logging
import os
import sqlite3
import threading
import time
import traceback

from flask import Flask, request
import ccxt
import numpy as np
import pandas as pd
import requests

GMT_MINUS_3 = timezone(timedelta(hours=-3))
REPORTES_HORAS = (7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 1, 3, 5)
LOG_FILE = "bot_trading.log"
DB_FILE = "trades.db"

logger = logging.getLogger("TradingBot")


# ============================================================================
# LOGGING
# ============================================================================

class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m", "INFO": "\033[92m", "WARNING": "\033[93m",
        "ERROR": "\033[91m", "CRITICAL": "\033[95m",
    }
    RESET = "\033[0m"

    def format(self, record):
        original = record.levelname
        color = self.COLORS.get(original)
        if color:
            record.levelname = f"{color}{original}{self.RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original


def setup_logging(log_file: str = LOG_FILE) -> None:
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-13s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    consola = logging.StreamHandler()
    consola.setFormatter(ColoredFormatter(fmt, datefmt=datefmt))
    root.addHandler(consola)

    if log_file:
        archivo = logging.FileHandler(log_file, encoding="utf-8")
        archivo.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        root.addHandler(archivo)

    for noisy in ("flask", "werkzeug", "urllib3", "ccxt", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ============================================================================
# TIPOS
# ============================================================================

class TrendDirection(Enum):
    UPTREND = "long"
    DOWNTREND = "short"
    NEUTRAL = None


@dataclass
class IndicatorValues:
    precio_cierre: float
    ema200: float
    adx: float
    st_direction: bool
    st_direction_anterior: bool
    upper_band: float
    lower_band: float
    vela_ts: int          # timestamp (ms) de apertura de la última vela CERRADA


@dataclass
class TradeSignal:
    symbol: str
    direction: TrendDirection
    precio: float
    adx: float
    vela_ts: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self):
        return (f"{self.symbol} {self.direction.value.upper()} @ {self.precio:.4f} "
                f"(ADX {self.adx:.2f}, vela {self.vela_ts})")


# ============================================================================
# CONFIG
# ============================================================================

def _env_bool(nombre: str, default: bool = False) -> bool:
    v = os.getenv(nombre)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "si", "sí")


def _env_int(nombre: str, default: int) -> int:
    try:
        return int(os.getenv(nombre, str(default)))
    except ValueError:
        return default


def _env_float(nombre: str, default: float) -> float:
    try:
        return float(os.getenv(nombre, str(default)))
    except ValueError:
        return default


@dataclass
class BotConfig:
    api_key: str = ""
    api_secret: str = ""
    api_password: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    logs_token: str = ""

    simbolos: List[str] = field(default_factory=lambda: [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
        "XRP/USDT:USDT", "AVAX/USDT:USDT",
    ])
    timeframe: str = "1h"

    # Riesgo
    leverage: int = 10
    capital_riesgo_usdt: float = 50.0     # tope absoluto de riesgo por trade
    fraccion_equity: float = 0.0075       # riesgo = min(equity * fracción, tope)
    limite_diario_r: float = 3.0          # sin entradas si el día va -3R o peor
    fee_rate: float = 0.0005              # taker OKX aprox. (por lado)
    mmr: float = 0.005                    # margen de mantenimiento aprox.
    rr_objetivo: float = 2.0
    sl_min_pct: float = 0.002
    margen_seguridad: float = 1.15        # margen requerido * 1.15 <= libre

    # Indicadores
    st_periodo: int = 10
    st_multiplier: float = 3.0
    periodo_ema: int = 200
    adx_periodo: int = 14
    adx_threshold: float = 22.0

    # Operación
    ciclo_segundos: int = 60
    limite_velas: int = 1000
    velas_minimas: int = 300
    modo_simulacion: bool = False
    sim_equity_inicial: float = 1000.0

    @classmethod
    def desde_env(cls) -> "BotConfig":
        return cls(
            api_key=os.getenv("OKX_API_KEY") or "",
            api_secret=os.getenv("OKX_API_SECRET") or "",
            api_password=os.getenv("OKX_API_PASSWORD") or "",
            telegram_token=os.getenv("TELEGRAM_TOKEN") or "",
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or "",
            logs_token=os.getenv("LOGS_TOKEN") or "",
            leverage=_env_int("OKX_LEVERAGE", 10),
            capital_riesgo_usdt=_env_float("CAPITAL_RIESGO_USDT", 50.0),
            fraccion_equity=_env_float("FRACCION_EQUITY", 0.0075),
            limite_diario_r=_env_float("LIMITE_DIARIO_R", 3.0),
            fee_rate=_env_float("FEE_RATE", 0.0005),
            modo_simulacion=_env_bool("MODO_SIMULACION", False),
            sim_equity_inicial=_env_float("SIM_EQUITY_INICIAL", 1000.0),
        )

    def validar(self) -> List[str]:
        errores = []
        if not all([self.api_key, self.api_secret, self.api_password]):
            errores.append("Credenciales OKX no configuradas (OKX_API_KEY/SECRET/PASSWORD)")
        if self.leverage < 1 or self.leverage > 50:
            errores.append(f"Leverage fuera de rango: {self.leverage}")
        if not (0 < self.fraccion_equity <= 0.02):
            errores.append(f"FRACCION_EQUITY debe estar entre 0 y 0.02: {self.fraccion_equity}")
        return errores

    @property
    def dist_liquidacion(self) -> float:
        """Distancia aproximada a liquidación con margen aislado."""
        return max(1.0 / max(self.leverage, 1) - self.mmr, 0.0)

    @property
    def dist_sl_maxima(self) -> float:
        return self.dist_liquidacion * 0.8


# ============================================================================
# TELEGRAM (texto plano, sin parse_mode)
# ============================================================================

class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token
        self.chat_id = chat_id
        self.logger = logging.getLogger("Telegram")
        self.habilitado = bool(token and chat_id)

    def enviar(self, mensaje: str, nivel: str = "INFO") -> bool:
        if not self.habilitado:
            self.logger.debug("Telegram deshabilitado: %s", mensaje[:80])
            return False
        emojis = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌", "TRADE": "🎯"}
        payload = {
            "chat_id": self.chat_id,
            "text": f"{emojis.get(nivel, '•')} {mensaje}"[:4000],
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json=payload, timeout=5)
            r.raise_for_status()
            return True
        except Exception as e:
            self.logger.error("Error en Telegram: %s", e)
            return False


notifier = TelegramNotifier()   # se reemplaza en main()


# ============================================================================
# PERSISTENCIA (SQLite)
# ============================================================================

class Persistencia:
    def __init__(self, path: str = DB_FILE):
        self.path = path
        self.logger = logging.getLogger("Persistencia")
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,              -- long / short
                    virtual INTEGER NOT NULL DEFAULT 0,
                    entry_ts TEXT NOT NULL,
                    entry_ms INTEGER NOT NULL,
                    entry_vela_ts INTEGER NOT NULL,
                    entry_px REAL NOT NULL,
                    sl_px REAL NOT NULL,
                    tp_px REAL NOT NULL,
                    size REAL NOT NULL,              -- contratos
                    contract_size REAL NOT NULL,
                    riesgo_usdt REAL NOT NULL,       -- 1R en USDT
                    razon_entrada TEXT,
                    exit_ts TEXT,
                    exit_px REAL,
                    razon_salida TEXT,
                    fees REAL,
                    funding REAL,
                    pnl REAL,                        -- neto de fees y funding
                    r_mult REAL
                )
            """)
            c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # --- key/value -------------------------------------------------------
    def kv_get(self, k: str) -> Optional[str]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
            return row["v"] if row else None

    def kv_set(self, k: str, v: str):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO kv (k, v) VALUES (?, ?) "
                      "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))

    # --- trades ----------------------------------------------------------
    def abrir(self, symbol: str, side: str, virtual: bool, entry_vela_ts: int,
              entry_px: float, sl_px: float, tp_px: float, size: float,
              contract_size: float, riesgo_usdt: float, razon: str) -> Optional[int]:
        ahora = datetime.now(timezone.utc)
        try:
            with self._lock, self._conn() as c:
                cur = c.execute("""
                    INSERT INTO trades (symbol, side, virtual, entry_ts, entry_ms, entry_vela_ts,
                        entry_px, sl_px, tp_px, size, contract_size, riesgo_usdt, razon_entrada)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (symbol, side, 1 if virtual else 0, ahora.isoformat(),
                      int(ahora.timestamp() * 1000), entry_vela_ts, entry_px, sl_px,
                      tp_px, size, contract_size, riesgo_usdt, razon))
                return int(cur.lastrowid)
        except Exception as e:
            self.logger.error("No pude registrar apertura: %s", e)
            return None

    def cerrar(self, trade_id: int, exit_px: float, razon: str, fees: float,
               funding: float, pnl: float, r_mult: float):
        try:
            with self._lock, self._conn() as c:
                c.execute("""
                    UPDATE trades SET exit_ts = ?, exit_px = ?, razon_salida = ?,
                        fees = ?, funding = ?, pnl = ?, r_mult = ?
                    WHERE id = ? AND exit_ts IS NULL
                """, (datetime.now(timezone.utc).isoformat(), exit_px, razon,
                      fees, funding, pnl, r_mult, trade_id))
        except Exception as e:
            self.logger.error("No pude registrar cierre: %s", e)

    def trade_abierto(self, symbol: str, virtual: bool) -> Optional[sqlite3.Row]:
        with self._lock, self._conn() as c:
            return c.execute("""
                SELECT * FROM trades WHERE symbol = ? AND virtual = ? AND exit_ts IS NULL
                ORDER BY id DESC LIMIT 1
            """, (symbol, 1 if virtual else 0)).fetchone()

    def resultado_hoy_r(self, virtual: bool) -> float:
        """Suma de R de trades cerrados desde las 00:00 UTC."""
        inicio = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock, self._conn() as c:
            row = c.execute("""
                SELECT COALESCE(SUM(r_mult), 0) AS r FROM trades
                WHERE virtual = ? AND exit_ts IS NOT NULL AND exit_ts >= ?
            """, (1 if virtual else 0, inicio.isoformat())).fetchone()
            return float(row["r"] or 0)

    def pnl_total(self, virtual: bool) -> float:
        with self._lock, self._conn() as c:
            row = c.execute("""
                SELECT COALESCE(SUM(pnl), 0) AS p FROM trades
                WHERE virtual = ? AND exit_ts IS NOT NULL
            """, (1 if virtual else 0,)).fetchone()
            return float(row["p"] or 0)

    def estadisticas(self, virtual: bool) -> Dict:
        with self._lock, self._conn() as c:
            row = c.execute("""
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN r_mult > 0 THEN 1 ELSE 0 END) AS ganadores,
                       AVG(r_mult) AS expectativa_r,
                       SUM(pnl) AS pnl, SUM(fees) AS fees
                FROM trades WHERE virtual = ? AND exit_ts IS NOT NULL
            """, (1 if virtual else 0,)).fetchone()
            n = row["n"] or 0
            return {
                "trades": n,
                "win_rate": (row["ganadores"] or 0) / n if n else None,
                "expectativa_r": row["expectativa_r"],
                "pnl": row["pnl"] or 0.0,
                "fees": row["fees"] or 0.0,
            }


def calcular_resultado(side: str, entry_px: float, exit_px: float, size: float,
                       contract_size: float, fees: float, funding: float,
                       riesgo_usdt: float) -> Tuple[float, float]:
    """Devuelve (pnl_neto, r_mult). fees >= 0 es costo; funding con signo (+ cobra)."""
    direccion = 1 if side == "long" else -1
    bruto = (exit_px - entry_px) * size * contract_size * direccion
    neto = bruto - fees + funding
    r = neto / riesgo_usdt if riesgo_usdt > 0 else 0.0
    return neto, r


# ============================================================================
# EXCHANGE
# ============================================================================

class ExchangeManager:
    def __init__(self, cfg: BotConfig):
        self.config = cfg
        self.logger = logging.getLogger("Exchange")
        self.exchange = ccxt.okx({
            "apiKey": cfg.api_key,
            "secret": cfg.api_secret,
            "password": cfg.api_password,
            "enableRateLimit": True,
            "timeout": 15000,
            "options": {"defaultType": "swap"},
        })
        self.exchange.set_sandbox_mode(True)
        markets = self.exchange.load_markets()
        self.logger.info("Conexión OKX sandbox OK (%s mercados)", len(markets))

    # --- mercado ---------------------------------------------------------
    def configurar_mercado(self, symbol: str) -> bool:
        try:
            try:
                self.exchange.set_margin_mode("isolated", symbol,
                                              {"lever": str(self.config.leverage)})
            except Exception as e:
                self.logger.debug("%s: margin mode: %s", symbol, e)
            try:
                self.exchange.set_leverage(self.config.leverage, symbol, {"mgnMode": "isolated"})
            except Exception as e:
                msg = str(e).lower()
                if not any(p in msg for p in ("already", "same", "not modified", "no change")):
                    self.logger.error("%s: no pude fijar leverage %sx: %s",
                                      symbol, self.config.leverage, e)
                    return False
            self.logger.info("%s: aislado %sx", symbol, self.config.leverage)
            return True
        except ccxt.BadSymbol:
            self.logger.error("Símbolo inválido: %s", symbol)
            return False
        except Exception as e:
            self.logger.warning("Error configurando %s: %s", symbol, e)
            return False

    def contract_size(self, symbol: str) -> float:
        return float(self.exchange.market(symbol).get("contractSize") or 1.0)

    def precio_preciso(self, symbol: str, px: float) -> float:
        return float(self.exchange.price_to_precision(symbol, px))

    def cantidad_precisa(self, symbol: str, amount: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(symbol, amount))
        except Exception:
            return 0.0

    def cantidad_minima(self, symbol: str) -> float:
        lim = (self.exchange.market(symbol).get("limits") or {}).get("amount") or {}
        return float(lim.get("min") or 0)

    # --- datos -----------------------------------------------------------
    def obtener_velas_cerradas(self, symbol: str) -> Optional[pd.DataFrame]:
        """Pagina fetch_ohlcv y devuelve SOLO velas cerradas (por timestamp)."""
        limit = self.config.limite_velas
        try:
            tf_ms = int(self.exchange.parse_timeframe(self.config.timeframe) * 1000)
            ahora_ms = int(time.time() * 1000)
            inicio_vela_actual = (ahora_ms // tf_ms) * tf_ms
            since = inicio_vela_actual - (limit + 2) * tf_ms
            rows, seen = [], set()
            for _ in range(20):
                chunk = self.exchange.fetch_ohlcv(symbol, self.config.timeframe,
                                                  since=since, limit=300)
                if not chunk:
                    break
                nuevos = 0
                for c in chunk:
                    if c[0] not in seen:
                        seen.add(c[0])
                        rows.append(c)
                        nuevos += 1
                ultimo = max(c[0] for c in chunk)
                if nuevos == 0 or ultimo >= inicio_vela_actual - tf_ms:
                    break
                since = ultimo + tf_ms
            rows = sorted(r for r in rows if r[0] < inicio_vela_actual)[-limit:]
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            if int(df["timestamp"].iloc[-1]) != inicio_vela_actual - tf_ms:
                self.logger.warning("%s: la última vela cerrada todavía no está disponible", symbol)
            return df.reset_index(drop=True)
        except ccxt.NetworkError as e:
            self.logger.error("Error de red al obtener velas de %s: %s", symbol, e)
            return None
        except Exception as e:
            self.logger.error("Error obteniendo velas de %s: %s", symbol, e)
            return None

    def obtener_posicion_abierta(self, symbol: str) -> Tuple[bool, Optional[Dict]]:
        """(consulta_ok, posición). consulta_ok=False si falló la API: NO asumir plano."""
        try:
            for p in self.exchange.fetch_positions([symbol]):
                if float(p.get("contracts") or 0) > 0:
                    return True, p
            return True, None
        except Exception as e:
            self.logger.error("Error consultando posición en %s: %s", symbol, e)
            return False, None

    def posicion_o_none(self, symbol: str) -> Optional[Dict]:
        ok, pos = self.obtener_posicion_abierta(symbol)
        if not ok:
            raise RuntimeError(f"No se pudo leer la posición de {symbol}")
        return pos

    def obtener_ticker(self, symbol: str) -> Optional[Dict]:
        try:
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            self.logger.error("Error obteniendo ticker de %s: %s", symbol, e)
            return None

    def balance_usdt(self) -> Optional[Tuple[float, float]]:
        """(equity_total, libre)."""
        try:
            usdt = self.exchange.fetch_balance().get("USDT") or {}
            total = float(usdt.get("total") or 0)
            libre = float(usdt.get("free") if usdt.get("free") is not None else total)
            return total, libre
        except Exception as e:
            self.logger.error("No pude leer balance USDT: %s", e)
            return None

    # --- órdenes ---------------------------------------------------------
    def crear_orden_mercado(self, symbol: str, side: str, amount: float,
                            reduce_only: bool) -> Optional[Dict]:
        params = {"tdMode": "isolated"}
        if reduce_only:
            params["reduceOnly"] = True
        try:
            self.logger.info("Orden %s %s %s reduceOnly=%s", side.upper(), amount, symbol, reduce_only)
            return self.exchange.create_market_order(symbol, side, amount, None, params)
        except ccxt.InsufficientFunds:
            self.logger.error("Balance insuficiente en %s", symbol)
            notifier.enviar(f"Balance insuficiente en {symbol}", "ERROR")
            return None
        except Exception as e:
            self.logger.error("Error creando orden en %s: %s", symbol, e)
            return None

    def detalle_fill(self, order: Dict, symbol: str) -> Tuple[float, Optional[float]]:
        """(filled, average). Relee la orden: create_order de OKX solo devuelve el id."""
        filled = float(order.get("filled") or 0)
        avg = order.get("average")
        oid = order.get("id")
        for _ in range(5):
            if filled > 0 and avg:
                break
            time.sleep(0.6)
            try:
                o = self.exchange.fetch_order(oid, symbol)
                filled = float(o.get("filled") or 0)
                avg = o.get("average")
                if o.get("status") in ("closed", "canceled") and filled > 0:
                    break
            except Exception as e:
                self.logger.debug("fetch_order %s: %s", oid, e)
        return filled, (float(avg) if avg else None)

    def crear_proteccion_oco(self, symbol: str, side_entrada: str, amount: float,
                             sl_precio: float, tp_precio: float) -> Optional[str]:
        """SL + TP en UNA orden OCO reduceOnly. Devuelve algoId o None."""
        side_cierre = "sell" if side_entrada == "buy" else "buy"
        try:
            resp = self.exchange.private_post_trade_order_algo({
                "instId": self.exchange.market(symbol)["id"],
                "tdMode": "isolated",
                "side": side_cierre,
                "ordType": "oco",
                "sz": self.exchange.amount_to_precision(symbol, amount),
                "reduceOnly": "true",
                "slTriggerPx": self.exchange.price_to_precision(symbol, sl_precio),
                "slOrdPx": "-1",
                "slTriggerPxType": "last",
                "tpTriggerPx": self.exchange.price_to_precision(symbol, tp_precio),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "last",
            })
            data = (resp.get("data") or [{}])[0]
            if str(resp.get("code")) == "0" and str(data.get("sCode", "0")) == "0":
                algo_id = data.get("algoId")
                self.logger.info("%s: OCO creada SL %.6f / TP %.6f (algoId=%s)",
                                 symbol, sl_precio, tp_precio, algo_id)
                return algo_id or "?"
            self.logger.error("%s: OCO rechazada code=%s msg=%s sMsg=%s", symbol,
                              resp.get("code"), resp.get("msg"), data.get("sMsg"))
        except Exception as e:
            self.logger.error("%s: error creando OCO: %s", symbol, e)
        return None

    def algos_pendientes(self, symbol: str) -> List[Dict]:
        inst_id = self.exchange.market(symbol)["id"]
        pend = []
        for tipo in ("oco", "conditional"):
            try:
                r = self.exchange.private_get_trade_orders_algo_pending(
                    {"instType": "SWAP", "instId": inst_id, "ordType": tipo})
                pend.extend(r.get("data") or [])
            except Exception as e:
                self.logger.debug("%s: algos pendientes %s: %s", symbol, tipo, e)
        return pend

    def cancelar_algos(self, symbol: str) -> bool:
        inst_id = self.exchange.market(symbol)["id"]
        ids = [a.get("algoId") for a in self.algos_pendientes(symbol) if a.get("algoId")]
        if not ids:
            return True
        try:
            r = self.exchange.private_post_trade_cancel_algos(
                [{"algoId": i, "instId": inst_id} for i in ids])
            ok = str(r.get("code")) == "0"
            self.logger.info("%s: cancelación de %s algo-orders ok=%s", symbol, len(ids), ok)
            return ok
        except Exception as e:
            self.logger.error("%s: no pude cancelar algos: %s", symbol, e)
            return False

    def cerrar_todo(self, symbol: str) -> bool:
        """Deja la posición en cero SIN posibilidad de invertirla.

        1) close-position nativo (no recibe tamaño).
        2) Hasta 3 órdenes market reduceOnly con el tamaño releído.
        Nunca se envía una orden de cierre sin reduceOnly.
        """
        inst_id = self.exchange.market(symbol)["id"]
        try:
            resp = self.exchange.private_post_trade_close_position(
                {"instId": inst_id, "mgnMode": "isolated", "autoCxl": "false"})
            if str(resp.get("code")) == "0":
                time.sleep(0.8)
                if self.posicion_o_none(symbol) is None:
                    self.logger.info("%s: cerrado por close-position", symbol)
                    return True
            else:
                self.logger.warning("%s: close-position code=%s msg=%s",
                                    symbol, resp.get("code"), resp.get("msg"))
        except Exception as e:
            self.logger.warning("%s: close-position falló: %s", symbol, e)

        for intento in range(3):
            try:
                pos = self.posicion_o_none(symbol)
            except RuntimeError as e:
                self.logger.error(str(e))
                time.sleep(1.5)
                continue
            if pos is None:
                return True
            amt = float(pos.get("contracts") or 0)
            side = (pos.get("side") or "").lower()
            cierre = "sell" if side == "long" else "buy"
            self.logger.warning("%s: cierre reduceOnly intento %s: %s %s",
                                symbol, intento + 1, cierre, amt)
            self.crear_orden_mercado(symbol, cierre, amt, reduce_only=True)
            time.sleep(1.0)

        try:
            return self.posicion_o_none(symbol) is None
        except RuntimeError:
            return False

    # --- conciliación ----------------------------------------------------
    def resultado_real(self, symbol: str, side: str, since_ms: int) -> Dict:
        """Lee fills desde la entrada: precio medio de salida, comisiones y funding."""
        salida_side = "sell" if side == "long" else "buy"
        fees = 0.0
        qty_salida = 0.0
        notional_salida = 0.0
        try:
            trades = self.exchange.fetch_my_trades(symbol, since=since_ms - 5000, limit=100)
            for t in trades:
                fee = t.get("fee") or {}
                if fee.get("currency") in (None, "USDT"):
                    fees += abs(float(fee.get("cost") or 0))
                if t.get("side") == salida_side:
                    q = float(t.get("amount") or 0)
                    qty_salida += q
                    notional_salida += q * float(t.get("price") or 0)
        except Exception as e:
            self.logger.warning("%s: no pude leer fills: %s", symbol, e)

        funding = 0.0
        try:
            for f in self.exchange.fetch_funding_history(symbol, since=since_ms):
                funding += float(f.get("amount") or 0)
        except Exception as e:
            self.logger.debug("%s: funding no disponible: %s", symbol, e)

        exit_px = notional_salida / qty_salida if qty_salida > 0 else None
        return {"exit_px": exit_px, "fees": fees, "funding": funding, "qty": qty_salida}


# ============================================================================
# INDICADORES
# ============================================================================

class TechnicalAnalyzer:
    def __init__(self, cfg: BotConfig):
        self.config = cfg
        self.logger = logging.getLogger("Technical")

    def calcular_indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        high, low, close = df["high"], df["low"], df["close"]
        hl2 = (high + low) / 2

        up = high.diff()
        down = -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)

        tr = pd.concat([high - low, (high - close.shift()).abs(),
                        (low - close.shift()).abs()], axis=1).max(axis=1)

        a_st = 1 / self.config.st_periodo
        a_adx = 1 / self.config.adx_periodo
        atr_st = tr.ewm(alpha=a_st, adjust=False).mean()
        atr_adx = tr.ewm(alpha=a_adx, adjust=False).mean()
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=a_adx, adjust=False).mean() / atr_adx
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=a_adx, adjust=False).mean() / atr_adx
        di_sum = (plus_di + minus_di).replace(0, np.nan)
        dx = (100 * (plus_di - minus_di).abs() / di_sum).fillna(0)
        df["adx"] = dx.ewm(alpha=a_adx, adjust=False).mean()
        # Descartar el período de estabilización del ADX (~3 períodos x2 etapas)
        df.loc[df.index[: self.config.adx_periodo * 3], "adx"] = np.nan

        upper_basic = (hl2 + self.config.st_multiplier * atr_st).values
        lower_basic = (hl2 - self.config.st_multiplier * atr_st).values
        c = close.values
        n = len(df)
        p = self.config.st_periodo
        upper = np.full(n, np.nan)
        lower = np.full(n, np.nan)
        st = np.ones(n, dtype=bool)
        if n > p:
            upper[p] = upper_basic[p]
            lower[p] = lower_basic[p]
            for i in range(p + 1, n):
                upper[i] = upper_basic[i] if (upper_basic[i] < upper[i - 1] or c[i - 1] > upper[i - 1]) else upper[i - 1]
                lower[i] = lower_basic[i] if (lower_basic[i] > lower[i - 1] or c[i - 1] < lower[i - 1]) else lower[i - 1]
                if st[i - 1]:
                    st[i] = not (c[i] < lower[i])
                else:
                    st[i] = c[i] > upper[i]

        df["upper_band"] = upper
        df["lower_band"] = lower
        df["st_direction"] = st
        df["ema200"] = close.ewm(span=self.config.periodo_ema, adjust=False).mean()
        return df

    def extraer_valores(self, df: pd.DataFrame) -> Optional[IndicatorValues]:
        """df contiene solo velas cerradas: iloc[-1] es la última cerrada."""
        try:
            actual, anterior = df.iloc[-1], df.iloc[-2]
            valores = IndicatorValues(
                precio_cierre=float(actual["close"]),
                ema200=float(actual["ema200"]),
                adx=float(actual["adx"]),
                st_direction=bool(actual["st_direction"]),
                st_direction_anterior=bool(anterior["st_direction"]),
                upper_band=float(actual["upper_band"]),
                lower_band=float(actual["lower_band"]),
                vela_ts=int(actual["timestamp"]),
            )
            if any(np.isnan(x) for x in (valores.adx, valores.upper_band, valores.lower_band)):
                return None
            return valores
        except Exception as e:
            self.logger.error("Error extrayendo valores: %s", e)
            return None

    def detectar_senal(self, v: IndicatorValues) -> TrendDirection:
        if v.adx <= self.config.adx_threshold:
            return TrendDirection.NEUTRAL
        if not v.st_direction_anterior and v.st_direction and v.precio_cierre > v.ema200:
            return TrendDirection.UPTREND
        if v.st_direction_anterior and not v.st_direction and v.precio_cierre < v.ema200:
            return TrendDirection.DOWNTREND
        return TrendDirection.NEUTRAL

    def razon_no_operar(self, v: IndicatorValues) -> Optional[str]:
        if v.adx <= self.config.adx_threshold:
            return f"ADX {v.adx:.1f} <= {self.config.adx_threshold:.0f}"
        if v.st_direction == v.st_direction_anterior:
            return "sin giro de SuperTrend"
        if v.st_direction and v.precio_cierre <= v.ema200:
            return "giro alcista bajo EMA200"
        if not v.st_direction and v.precio_cierre >= v.ema200:
            return "giro bajista sobre EMA200"
        return None


# ============================================================================
# EJECUTOR
# ============================================================================

@dataclass
class Plan:
    side: str             # buy / sell
    amount: float
    sl: float
    riesgo_usdt: float
    dist_sl: float


class TradeExecutor:
    def __init__(self, ex: ExchangeManager, cfg: BotConfig, db: Persistencia):
        self.exchange = ex
        self.config = cfg
        self.db = db
        self.logger = logging.getLogger("Executor")
        self.sim = cfg.modo_simulacion

    # --- dimensionado ----------------------------------------------------
    def _equity(self) -> Optional[Tuple[float, float]]:
        if self.sim:
            eq = self.config.sim_equity_inicial + self.db.pnl_total(virtual=True)
            return eq, eq
        return self.exchange.balance_usdt()

    def _tp(self, side: str, entrada: float, sl: float) -> float:
        rr = self.config.rr_objetivo
        return entrada + rr * (entrada - sl) if side == "buy" else entrada - rr * (sl - entrada)

    def dimensionar(self, symbol: str, precio: float, direction: TrendDirection,
                    v: IndicatorValues) -> Optional[Plan]:
        if direction == TrendDirection.UPTREND:
            side, sl = "buy", v.lower_band
            if sl >= precio:
                self.logger.warning("%s: banda inferior %.4f >= precio, no sirve de SL", symbol, sl)
                return None
            dist = (precio - sl) / precio
        else:
            side, sl = "sell", v.upper_band
            if sl <= precio:
                self.logger.warning("%s: banda superior %.4f <= precio, no sirve de SL", symbol, sl)
                return None
            dist = (sl - precio) / precio

        if dist >= self.config.dist_sl_maxima:
            self.logger.warning("%s: SL a %.2f%% vs liquidación ~%.2f%% (%sx) — no opero",
                                symbol, dist * 100, self.config.dist_liquidacion * 100,
                                self.config.leverage)
            return None
        if dist < self.config.sl_min_pct:
            self.logger.warning("%s: SL demasiado justo (%.2f%%) — no opero", symbol, dist * 100)
            return None

        bal = self._equity()
        if not bal:
            return None
        equity, libre = bal
        if equity <= 0 or libre <= 0:
            self.logger.error("Equity/libre USDT <= 0")
            return None

        riesgo = min(equity * self.config.fraccion_equity, self.config.capital_riesgo_usdt)
        cs = self.exchange.contract_size(symbol)
        # Pérdida por contrato si salta el SL: distancia + comisiones de ida y vuelta
        perdida_x_contrato = (dist * precio + 2 * self.config.fee_rate * precio) * cs
        amount = self.exchange.cantidad_precisa(symbol, riesgo / perdida_x_contrato)
        minimo = self.exchange.cantidad_minima(symbol)
        if amount <= 0 or amount < minimo:
            self.logger.warning("%s: tamaño %.6f por debajo del mínimo %.6f", symbol, amount, minimo)
            return None

        margen = amount * cs * precio / self.config.leverage
        if margen * self.config.margen_seguridad > libre:
            self.logger.warning("%s: margen requerido %.2f > libre %.2f — no opero",
                                symbol, margen, libre)
            return None

        riesgo_real = amount * perdida_x_contrato
        self.logger.info("%s: riesgo %.2f USDT (equity %.2f) · SL %.2f%% · %s contratos · margen %.2f",
                         symbol, riesgo_real, equity, dist * 100, amount, margen)
        return Plan(side=side, amount=amount, sl=self.exchange.precio_preciso(symbol, sl),
                    riesgo_usdt=riesgo_real, dist_sl=dist)

    # --- apertura --------------------------------------------------------
    def abrir_posicion(self, signal: TradeSignal, v: IndicatorValues) -> bool:
        symbol = signal.symbol
        try:
            ticker = self.exchange.obtener_ticker(symbol)
            if not ticker or not ticker.get("last"):
                return False
            precio = float(ticker["last"])
            plan = self.dimensionar(symbol, precio, signal.direction, v)
            if not plan:
                return False
            razon = f"ST flip {signal.direction.value} ADX {v.adx:.1f}"
            cs = self.exchange.contract_size(symbol)

            if self.sim:
                tp = self.exchange.precio_preciso(symbol, self._tp(plan.side, precio, plan.sl))
                self.db.abrir(symbol, signal.direction.value, True, v.vela_ts, precio, plan.sl,
                              tp, plan.amount, cs, plan.riesgo_usdt, razon)
                msg = (f"SIMULADA {symbol} {signal.direction.value.upper()}\n"
                       f"Tamaño: {plan.amount} · Entrada: {precio:.4f}\n"
                       f"SL: {plan.sl:.4f} · TP: {tp:.4f} · 1R = {plan.riesgo_usdt:.2f} USDT")
                self.logger.info(msg.replace("\n", " | "))
                notifier.enviar(msg, "TRADE")
                return True

            orden = self.exchange.crear_orden_mercado(symbol, plan.side, plan.amount, reduce_only=False)
            if not orden:
                return False

            filled, avg = self.exchange.detalle_fill(orden, symbol)
            # Verificar contra la posición real (fuente de verdad)
            pos = self.exchange.posicion_o_none(symbol)
            contratos = float(pos.get("contracts") or 0) if pos else 0.0
            if contratos <= 0:
                self.logger.error("%s: entrada sin fill (filled=%s) — no creo protección", symbol, filled)
                notifier.enviar(f"Entrada sin fill en {symbol}", "ERROR")
                return False
            if abs(contratos - plan.amount) > 1e-9:
                self.logger.warning("%s: fill parcial/distinto: pedido %s, posición %s",
                                    symbol, plan.amount, contratos)
            fill_px = avg or float(pos.get("entryPrice") or 0) or precio

            dist_real = ((fill_px - plan.sl) / fill_px) if plan.side == "buy" else ((plan.sl - fill_px) / fill_px)
            if dist_real <= 0 or dist_real >= self.config.dist_sl_maxima:
                self.logger.error("%s: fill %.4f deja SL a %.2f%% — rollback", symbol, fill_px, dist_real * 100)
                self._rollback(symbol, signal, fill_px, "el fill dejó el SL fuera de rango")
                return False

            tp = self.exchange.precio_preciso(symbol, self._tp(plan.side, fill_px, plan.sl))
            algo_id = self.exchange.crear_proteccion_oco(symbol, plan.side, contratos, plan.sl, tp)
            if not algo_id:
                self._rollback(symbol, signal, fill_px, "no se pudo crear la OCO SL/TP")
                return False

            riesgo_real = contratos * cs * abs(fill_px - plan.sl) + 2 * self.config.fee_rate * fill_px * contratos * cs
            self.db.abrir(symbol, signal.direction.value, False, v.vela_ts, fill_px, plan.sl, tp,
                          contratos, cs, riesgo_real, razon)
            msg = (f"NUEVA OPERACIÓN {symbol} {signal.direction.value.upper()}\n"
                   f"Tamaño: {contratos} contratos · Entrada: {fill_px:.4f}\n"
                   f"SL: {plan.sl:.4f} · TP: {tp:.4f} (OCO {algo_id})\n"
                   f"1R = {riesgo_real:.2f} USDT · ADX {v.adx:.1f}")
            self.logger.info(msg.replace("\n", " | "))
            notifier.enviar(msg, "TRADE")
            return True
        except Exception as e:
            self.logger.error("Error abriendo posición en %s: %s", symbol, e)
            self.logger.debug(traceback.format_exc())
            notifier.enviar(f"Error abriendo posición en {symbol}: {e}. Revisá OKX.", "ERROR")
            return False

    def _rollback(self, symbol: str, signal: TradeSignal, px: float, motivo: str):
        cerrado = self.exchange.cerrar_todo(symbol)
        if cerrado:
            self.exchange.cancelar_algos(symbol)
            notifier.enviar(f"OPERACIÓN CANCELADA {symbol}: {motivo}. "
                            f"El bot cerró la posición ({signal.direction.value} @ {px:.4f}).", "ERROR")
        else:
            self.logger.critical("%s: posición SIN protección, intervención humana", symbol)
            notifier.enviar(f"EMERGENCIA {symbol}: {motivo} y NO se pudo cerrar la posición. "
                            f"CERRÁ MANUALMENTE EN OKX AHORA.", "ERROR")

    # --- cierre por señal ------------------------------------------------
    def cerrar_por_senal(self, symbol: str, razon: str, precio_ref: float) -> bool:
        trade = self.db.trade_abierto(symbol, virtual=self.sim)
        if self.sim:
            if trade:
                self._cerrar_virtual(trade, precio_ref, razon)
            return True

        # 1) cerrar; 2) recién después cancelar la OCO. Si el cierre falla, la OCO queda.
        if not self.exchange.cerrar_todo(symbol):
            notifier.enviar(f"EMERGENCIA {symbol}: no pude dejar la posición en cero. "
                            f"La OCO SL/TP sigue activa. Revisá OKX.", "ERROR")
            return False
        self.exchange.cancelar_algos(symbol)
        if trade:
            self.registrar_cierre_real(trade, razon)
        else:
            notifier.enviar(f"POSICIÓN CERRADA {symbol} ({razon}) — sin registro previo en DB", "INFO")
        return True

    def registrar_cierre_real(self, trade: sqlite3.Row, razon: str):
        res = self.exchange.resultado_real(trade["symbol"], trade["side"], trade["entry_ms"])
        exit_px = res["exit_px"]
        if exit_px is None:
            t = self.exchange.obtener_ticker(trade["symbol"])
            exit_px = float(t["last"]) if t else trade["entry_px"]
            self.logger.warning("%s: sin fills de salida, uso ticker %.4f", trade["symbol"], exit_px)
        fees = res["fees"] or 2 * self.config.fee_rate * trade["entry_px"] * trade["size"] * trade["contract_size"]
        pnl, r = calcular_resultado(trade["side"], trade["entry_px"], exit_px, trade["size"],
                                    trade["contract_size"], fees, res["funding"], trade["riesgo_usdt"])
        self.db.cerrar(trade["id"], exit_px, razon, fees, res["funding"], pnl, r)
        msg = (f"POSICIÓN CERRADA {trade['symbol']} {trade['side'].upper()}\n"
               f"Razón: {razon}\nEntrada {trade['entry_px']:.4f} → Salida {exit_px:.4f}\n"
               f"PnL neto: {pnl:+.2f} USDT ({r:+.2f}R) · fees {fees:.2f} · funding {res['funding']:+.2f}")
        self.logger.info(msg.replace("\n", " | "))
        notifier.enviar(msg, "SUCCESS" if pnl > 0 else "INFO")

    # --- simulación ------------------------------------------------------
    def _cerrar_virtual(self, trade: sqlite3.Row, exit_px: float, razon: str):
        notional_in = trade["entry_px"] * trade["size"] * trade["contract_size"]
        notional_out = exit_px * trade["size"] * trade["contract_size"]
        fees = self.config.fee_rate * (notional_in + notional_out)
        pnl, r = calcular_resultado(trade["side"], trade["entry_px"], exit_px, trade["size"],
                                    trade["contract_size"], fees, 0.0, trade["riesgo_usdt"])
        self.db.cerrar(trade["id"], exit_px, razon, fees, 0.0, pnl, r)
        msg = (f"SIMULADA CIERRE {trade['symbol']} {trade['side'].upper()}\n"
               f"Razón: {razon}\nEntrada {trade['entry_px']:.4f} → Salida {exit_px:.4f}\n"
               f"PnL: {pnl:+.2f} USDT ({r:+.2f}R)")
        self.logger.info(msg.replace("\n", " | "))
        notifier.enviar(msg, "INFO")

    def gestionar_virtual(self, symbol: str, df: pd.DataFrame) -> bool:
        """Evalúa SL/TP contra cada vela cerrada posterior a la entrada.
        Si una vela toca ambos niveles se asume SL (conservador). Devuelve True si cerró."""
        trade = self.db.trade_abierto(symbol, virtual=True)
        if not trade:
            return False
        velas = df[df["timestamp"] > trade["entry_vela_ts"]]
        for _, vela in velas.iterrows():
            # la vela de entrada (la siguiente a la señal) empezó antes de la entrada real:
            # igual se evalúa completa; es una aproximación conservadora.
            hi, lo = float(vela["high"]), float(vela["low"])
            if trade["side"] == "long":
                toca_sl, toca_tp = lo <= trade["sl_px"], hi >= trade["tp_px"]
            else:
                toca_sl, toca_tp = hi >= trade["sl_px"], lo <= trade["tp_px"]
            if toca_sl:
                self._cerrar_virtual(trade, trade["sl_px"], "SL (simulado)")
                return True
            if toca_tp:
                self._cerrar_virtual(trade, trade["tp_px"], "TP (simulado)")
                return True
        return False


# ============================================================================
# BOT
# ============================================================================

class TradingBot:
    def __init__(self, cfg: BotConfig):
        self.config = cfg
        self.logger = logging.getLogger("TradingBot")
        self.db = Persistencia(DB_FILE)
        self.exchange = ExchangeManager(cfg)
        self.analyzer = TechnicalAnalyzer(cfg)
        self.executor = TradeExecutor(self.exchange, cfg, self.db)
        self.sim = cfg.modo_simulacion
        self.activo = True
        self.ciclo_contador = 0
        self.ultimo_reporte_ts: Optional[datetime] = None
        self.simbolos_ok: List[str] = list(cfg.simbolos)
        self._lock = threading.Lock()
        self._estado: Dict[str, dict] = {}
        self._huerfanas_avisadas: set = set()
        self._limite_avisado_dia: Optional[str] = None

    # --- estado compartido ----------------------------------------------
    def set_estado(self, symbol: str, estado: dict):
        with self._lock:
            self._estado[symbol] = dict(estado)

    def snapshot(self) -> Dict:
        with self._lock:
            return {
                "estado": "activo" if self.activo else "inactivo",
                "ciclos": self.ciclo_contador,
                "simbolos": list(self.simbolos_ok),
                "simbolos_estado": {k: dict(v) for k, v in self._estado.items()},
                "leverage": self.config.leverage,
                "simulacion": self.sim,
            }

    # --- deduplicación persistente --------------------------------------
    def _clave_vela(self, symbol: str) -> str:
        return f"ultima_senal_vela:{'sim' if self.sim else 'real'}:{symbol}"

    def ya_procesada(self, symbol: str, vela_ts: int) -> bool:
        return self.db.kv_get(self._clave_vela(symbol)) == str(vela_ts)

    def marcar_procesada(self, symbol: str, vela_ts: int):
        self.db.kv_set(self._clave_vela(symbol), str(vela_ts))

    # --- ciclo -----------------------------------------------------------
    def inicializar(self) -> bool:
        self.logger.info("=" * 70)
        self.logger.info("BOT OKX TESTNET v2 | %s | tf=%s | %sx | riesgo máx %.1f USDT (%.2f%% equity) "
                         "| límite diario %.1fR | simulación=%s",
                         ", ".join(self.config.simbolos), self.config.timeframe, self.config.leverage,
                         self.config.capital_riesgo_usdt, self.config.fraccion_equity * 100,
                         self.config.limite_diario_r, self.sim)
        self.simbolos_ok = [s for s in self.config.simbolos if self.exchange.configurar_mercado(s)]
        if not self.simbolos_ok:
            self.logger.error("Ningún símbolo quedó configurado")
            return False
        notifier.enviar(f"Bot v2 iniciado ({'SIMULACIÓN' if self.sim else 'TESTNET'}) · "
                        f"{self.config.leverage}x · {', '.join(s.split('/')[0] for s in self.simbolos_ok)}",
                        "SUCCESS")
        return True

    def _limite_diario_alcanzado(self) -> bool:
        r_hoy = self.db.resultado_hoy_r(virtual=self.sim)
        if r_hoy <= -self.config.limite_diario_r:
            hoy = datetime.now(timezone.utc).date().isoformat()
            if self._limite_avisado_dia != hoy:
                self._limite_avisado_dia = hoy
                notifier.enviar(f"Límite diario alcanzado ({r_hoy:.2f}R). Sin entradas hasta 00:00 UTC.",
                                "WARNING")
            return True
        return False

    def _conciliar_real(self, symbol: str, posicion: Optional[Dict]):
        trade = self.db.trade_abierto(symbol, virtual=False)
        if posicion is None and trade is not None:
            # OKX ejecutó SL o TP (o se cerró a mano): limpiar y registrar
            self.exchange.cancelar_algos(symbol)
            res_razon = "SL/TP en exchange"
            t = self.exchange.obtener_ticker(symbol)
            if t and t.get("last"):
                last = float(t["last"])
                res_razon = ("TP en exchange" if abs(last - trade["tp_px"]) < abs(last - trade["sl_px"])
                             else "SL en exchange")
            self.executor.registrar_cierre_real(trade, res_razon)
        elif posicion is not None and trade is None and symbol not in self._huerfanas_avisadas:
            self._huerfanas_avisadas.add(symbol)
            protegida = bool(self.exchange.algos_pendientes(symbol))
            nivel = "WARNING" if protegida else "ERROR"
            notifier.enviar(f"{symbol}: posición abierta sin registro en DB "
                            f"({'con' if protegida else 'SIN'} SL/TP). Revisar manualmente.", nivel)
        elif posicion is not None and trade is not None and not self.exchange.algos_pendientes(symbol):
            # Posición registrada pero sin protección: recrearla o cerrar
            side = "buy" if trade["side"] == "long" else "sell"
            amt = float(posicion.get("contracts") or 0)
            self.logger.error("%s: posición sin OCO — intento recrearla", symbol)
            if not self.exchange.crear_proteccion_oco(symbol, side, amt, trade["sl_px"], trade["tp_px"]):
                self.executor.cerrar_por_senal(symbol, "sin protección y OCO rechazada", trade["entry_px"])

    def analizar_symbol(self, symbol: str, entradas_habilitadas: bool) -> bool:
        df_raw = self.exchange.obtener_velas_cerradas(symbol)
        if df_raw is None or len(df_raw) < self.config.velas_minimas:
            self.logger.warning("%s: datos insuficientes (%s/%s)", symbol,
                                0 if df_raw is None else len(df_raw), self.config.velas_minimas)
            return False
        df = self.analyzer.calcular_indicadores(df_raw)
        v = self.analyzer.extraer_valores(df)
        if not v:
            return False

        dist_ema = (v.precio_cierre - v.ema200) / v.ema200 * 100
        self.logger.info("%-14s | cierre %10.4f | EMA200 %+6.2f%% | ADX %5.2f | ST %s",
                         symbol, v.precio_cierre, dist_ema, v.adx,
                         "ALCISTA" if v.st_direction else "BAJISTA")

        # --- posición actual (real o virtual) ---
        if self.sim:
            self.executor.gestionar_virtual(symbol, df)
            trade = self.db.trade_abierto(symbol, virtual=True)
            side_pos = trade["side"] if trade else None
        else:
            ok, posicion = self.exchange.obtener_posicion_abierta(symbol)
            if not ok:
                return False          # sin saber la posición, no se hace nada
            self._conciliar_real(symbol, posicion)
            side_pos = posicion.get("side") if posicion else None

        razon = self.analyzer.razon_no_operar(v)
        self.set_estado(symbol, {"precio": v.precio_cierre, "st": v.st_direction, "adx": v.adx,
                                 "posicion": side_pos is not None, "side_posicion": side_pos,
                                 "razon": razon})

        # --- salida por giro de SuperTrend ---
        if side_pos:
            if (side_pos == "long" and not v.st_direction) or (side_pos == "short" and v.st_direction):
                motivo = f"SuperTrend giró {'bajista' if side_pos == 'long' else 'alcista'}"
                self.logger.warning("SALIDA %s: %s", symbol, motivo)
                self.executor.cerrar_por_senal(symbol, motivo, v.precio_cierre)
            return True

        # --- entrada ---
        senal = self.analyzer.detectar_senal(v)
        if senal == TrendDirection.NEUTRAL:
            return True
        if self.ya_procesada(symbol, v.vela_ts):
            self.logger.debug("%s: señal de la vela %s ya procesada", symbol, v.vela_ts)
            return True
        # Se marca ANTES de ejecutar: pase lo que pase, una vela = un intento.
        self.marcar_procesada(symbol, v.vela_ts)
        if not entradas_habilitadas:
            self.logger.warning("%s: señal %s descartada por límite diario", symbol, senal.value)
            return True
        signal = TradeSignal(symbol, senal, v.precio_cierre, v.adx, v.vela_ts)
        self.logger.warning("SEÑAL: %s", signal)
        self.executor.abrir_posicion(signal, v)
        return True

    def ciclo_analisis(self):
        with self._lock:
            self.ciclo_contador += 1
            n = self.ciclo_contador
        entradas = not self._limite_diario_alcanzado()
        fallos = []
        for symbol in self.simbolos_ok:
            try:
                if not self.analizar_symbol(symbol, entradas):
                    fallos.append(symbol)
            except Exception as e:
                self.logger.error("Excepción en %s: %s", symbol, e)
                self.logger.debug(traceback.format_exc())
                fallos.append(symbol)
        if fallos:
            self.logger.warning("Ciclo #%04d · fallos: %s", n, ", ".join(fallos))
        else:
            self.logger.info("Ciclo #%04d completo · próximo en %ss", n, self.config.ciclo_segundos)
        if self._debe_enviar_reporte():
            notifier.enviar(self._generar_reporte(), "INFO")

    def _debe_enviar_reporte(self) -> bool:
        ahora = datetime.now(GMT_MINUS_3)
        if self.ultimo_reporte_ts and self.ultimo_reporte_ts.strftime("%Y%m%d%H") == ahora.strftime("%Y%m%d%H"):
            return False
        if ahora.hour in REPORTES_HORAS:
            self.ultimo_reporte_ts = ahora
            return True
        return False

    def _generar_reporte(self) -> str:
        snap = self.snapshot()
        ahora = datetime.now(GMT_MINUS_3)
        lineas = [f"{ahora.strftime('%H:%M')} AR | C#{snap['ciclos']:04d}"
                  f"{' | SIM' if self.sim else ''}", ""]
        razones = []
        for symbol in snap["simbolos"]:
            e = snap["simbolos_estado"].get(symbol)
            if not e:
                continue
            corto = symbol.split("/")[0]
            base = f"- {corto} {e['precio']:.4g} {'alcista' if e['st'] else 'bajista'} ADX {e['adx']:.0f}"
            if e["posicion"]:
                lineas.append(f"{base} POS {str(e['side_posicion']).upper()}")
            else:
                lineas.append(base)
                if e.get("razon"):
                    razones.append(f"{corto}: {e['razon']}")
        if razones:
            lineas += ["", "Sin entrada: " + "; ".join(razones)]
        st = self.db.estadisticas(virtual=self.sim)
        r_hoy = self.db.resultado_hoy_r(virtual=self.sim)
        exp = f"{st['expectativa_r']:+.2f}R" if st["expectativa_r"] is not None else "-"
        wr = f"{st['win_rate'] * 100:.0f}%" if st["win_rate"] is not None else "-"
        lineas += ["", f"Hoy: {r_hoy:+.2f}R · Trades: {st['trades']} · WR {wr} · "
                       f"Expectativa {exp} · PnL {st['pnl']:+.2f} USDT"]
        return "\n".join(lineas)

    def ejecutar(self):
        if not self.inicializar():
            self.logger.error("Falló la inicialización")
            return
        try:
            while self.activo:
                inicio = time.time()
                try:
                    self.ciclo_analisis()
                except Exception as e:
                    self.logger.error("Error en ciclo: %s", e)
                    self.logger.debug(traceback.format_exc())
                    notifier.enviar(f"Error en ciclo del bot: {e}", "ERROR")
                time.sleep(max(1.0, self.config.ciclo_segundos - (time.time() - inicio)))
        except KeyboardInterrupt:
            self.logger.info("Bot detenido por usuario")
            notifier.enviar("Bot detenido manualmente", "INFO")
        finally:
            self.activo = False


# ============================================================================
# FLASK
# ============================================================================

app = Flask(__name__)
_bot_lock = threading.Lock()
bot: Optional[TradingBot] = None
config: Optional[BotConfig] = None


def _get_bot() -> Optional[TradingBot]:
    with _bot_lock:
        return bot


@app.route("/")
def home():
    b = _get_bot()
    if b is None:
        return "Bot OKX v2 — inicializando. Estado: /status", 202
    s = b.snapshot()
    return (f"Bot OKX Testnet v2 — {s['estado'].upper()}{' · SIMULACIÓN' if s['simulacion'] else ''}<br>"
            f"Ciclos: {s['ciclos']}<br>Símbolos: {html.escape(', '.join(s['simbolos']))}<br>"
            f"<a href='/status'>status</a> · logs: /logs?token=…")


@app.route("/status")
def status():
    b = _get_bot()
    if b is None:
        return {"estado": "inicializando", "timestamp": datetime.now(timezone.utc).isoformat()}, 202
    s = b.snapshot()
    s["hoy_r"] = b.db.resultado_hoy_r(virtual=b.sim)
    s["estadisticas"] = b.db.estadisticas(virtual=b.sim)
    s["timestamp"] = datetime.now(timezone.utc).isoformat()
    return s, 200


@app.route("/logs")
def logs():
    token = config.logs_token if config else ""
    if not token:
        return "Logs deshabilitados: configurá LOGS_TOKEN", 403
    if request.args.get("token") != token:
        return "No autorizado", 401
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            cuerpo = html.escape("".join(f.readlines()[-250:]))
        return f"<pre style='font:12px/1.45 ui-monospace,monospace;white-space:pre-wrap'>{cuerpo}</pre>"
    except FileNotFoundError:
        return "sin logs todavía", 404


# ============================================================================
# MAIN
# ============================================================================

def main():
    global bot, config, notifier
    setup_logging(LOG_FILE)
    config = BotConfig.desde_env()
    notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)

    errores = config.validar()
    if errores:
        for e in errores:
            logger.error(e)
        notifier.enviar("No arranco: " + "; ".join(errores), "ERROR")
        return

    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
                     daemon=True).start()
    logger.info("Flask en puerto %s", port)

    try:
        instancia = TradingBot(config)
        with _bot_lock:
            bot = instancia
        instancia.ejecutar()
    except Exception as e:
        logger.critical("Error fatal: %s", e)
        logger.debug(traceback.format_exc())
        notifier.enviar(f"Error fatal: {e}", "ERROR")
        while True:          # mantener vivo el health-check para ver el error
            time.sleep(60)


if __name__ == "__main__":
    main()
