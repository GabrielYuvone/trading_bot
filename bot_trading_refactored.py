#!/usr/bin/env python3
"""
Bot de Trading OKX — v3

Estrategia: SuperTrend(24, 3.5) + EMA200 + ADX(14) > 20, velas de 1h cerradas.

    LARGO  cuando el SuperTrend gira a alcista, el cierre > EMA200 y ADX > 20
    CORTO  cuando gira a bajista, el cierre < EMA200 y ADX > 20
    STOP   banda contraria del SuperTrend en la vela de la señal (~2-3%)
    SALIDA giro del SuperTrend en contra, o stop. SIN TAKE PROFIT: el
           beneficio se deja correr (el TP fijo de 2R destruía la
           expectativa: ver research/).

Validado con 6 meses de velas horarias de 5 perpetuos (research/):
    ~4,7 señales/semana, 39% de aciertos, expectativa ≈ +0,35R,
    profit factor 1,8, drawdown máximo ≈ -16%, 33 de 36 combinaciones de
    parámetros del vecindario rentables (no es un pico sobreajustado).

Cambios de la v3 respecto de la v2 (la que dejó de operar):
- Parámetros recalibrados (ST 10/3 → 24/3.5, ADX 22 → 20) y TP eliminado.
- Una señal fallida por un error transitorio YA NO se pierde: se reintenta
  hasta N ciclos mientras la vela siga vigente (antes se marcaba la vela
  como procesada ANTES de intentar la entrada: cualquier fallo de red se
  comía la señal para siempre).
- Diagnóstico: contador de motivos por símbolo en /status y
  /por_que_no_opero, más latido por Telegram cada HEARTBEAT_HORAS.
- Pre-flight al arrancar: avisa si con el capital actual el tamaño por
  riesgo redondea a cero en algún símbolo (el bot "andaba" pero jamás
  podía operar).
- Inicialización con reintentos: un fallo de red al arrancar ya no deja el
  health-check verde con el bot muerto.
- Control de portafolio: tope de posiciones simultáneas y por dirección.
- Órdenes de protección: stop reduceOnly sin TP ('conditional').

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
import json
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
# Horas (ARG) en las que se manda el reporte horario. El latido va aparte.
def _horas_reporte() -> tuple:
    raw = os.getenv("REPORTES_HORAS")
    if raw:
        try:
            return tuple(int(x) for x in raw.split(",") if x.strip())
        except ValueError:
            pass
    return (9, 13, 17, 21)


REPORTES_HORAS = _horas_reporte()
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
        "XRP/USDT:USDT", "DOGE/USDT:USDT",
    ])
    timeframe: str = "1h"

    # Riesgo
    leverage: int = 10
    capital_riesgo_usdt: float = 50.0     # tope absoluto de riesgo por trade
    fraccion_equity: float = 0.0075       # riesgo = min(equity * fracción, tope)
    limite_diario_r: float = 3.0          # sin entradas si el día va -3R o peor
    fee_rate: float = 0.0005              # taker OKX aprox. (por lado)
    mmr: float = 0.005                    # margen de mantenimiento aprox.
    rr_objetivo: float = 2.0              # SÓLO si usar_tp=True (no recomendado)
    usar_tp: bool = False                 # el TP fijo destruye la expectativa
    sl_min_pct: float = 0.002
    sl_max_pct: float = 0.10              # no entrar si el SL queda demasiado lejos
    margen_seguridad: float = 1.15        # margen requerido * 1.15 <= libre
    max_posiciones: int = 4               # posiciones simultáneas (5 símbolos)
    max_pos_misma_dir: int = 3            # tope por dirección (3 = sin tope efectivo)

    # Indicadores — SuperTrend(24, 3.5) + EMA200 + ADX(14) > 20 en velas de 1h
    st_periodo: int = 24
    st_multiplier: float = 3.5
    periodo_ema: int = 200
    adx_periodo: int = 14
    adx_threshold: float = 20.0
    permitir_cortos: bool = True

    # Operación
    ciclo_segundos: int = 60
    limite_velas: int = 1000
    velas_minimas: int = 320
    modo_simulacion: bool = False
    sim_equity_inicial: float = 1000.0
    sandbox: bool = True              # OKX demo. Pasar a REAL sólo con OKX_SANDBOX=0
    heartbeat_horas: float = 6.0          # "estoy vivo" por Telegram
    reintentos_senal: int = 8             # ciclos en que se reintenta una señal fallida
    deriva_max_entrada: float = 0.015     # si el precio ya corrió >1,5% desde la señal, no perseguir

    @classmethod
    def desde_env(cls) -> "BotConfig":
        simbolos_env = os.getenv("SIMBOLOS")
        simbolos = ([s.strip() for s in simbolos_env.split(",") if s.strip()]
                    if simbolos_env else None)
        cfg = cls(
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
            sandbox=_env_bool("OKX_SANDBOX", True),
            usar_tp=_env_bool("USAR_TP", False),
            rr_objetivo=_env_float("RR_OBJETIVO", 2.0),
            st_periodo=_env_int("ST_PERIODO", 24),
            st_multiplier=_env_float("ST_MULTIPLIER", 3.5),
            adx_threshold=_env_float("ADX_THRESHOLD", 20.0),
            permitir_cortos=_env_bool("PERMITIR_CORTOS", True),
            max_posiciones=_env_int("MAX_POSICIONES", 4),
            max_pos_misma_dir=_env_int("MAX_POS_MISMA_DIR", 3),
            ciclo_segundos=_env_int("CICLO_SEGUNDOS", 60),
            heartbeat_horas=_env_float("HEARTBEAT_HORAS", 6.0),
        )
        if simbolos:
            cfg.simbolos = simbolos
        return cfg

    def validar(self) -> List[str]:
        errores = []
        if not all([self.api_key, self.api_secret, self.api_password]):
            errores.append("Credenciales OKX no configuradas (OKX_API_KEY/SECRET/PASSWORD)")
        if self.leverage < 1 or self.leverage > 50:
            errores.append(f"Leverage fuera de rango: {self.leverage}")
        if not (0 < self.fraccion_equity <= 0.02):
            errores.append(f"FRACCION_EQUITY debe estar entre 0 y 0.02: {self.fraccion_equity}")
        if self.adx_threshold < 0 or self.adx_threshold > 60:
            errores.append(f"ADX_THRESHOLD fuera de rango: {self.adx_threshold}")
        if self.st_periodo < 2 or self.st_multiplier <= 0:
            errores.append(f"Parámetros de SuperTrend inválidos: {self.st_periodo}/{self.st_multiplier}")
        if self.max_posiciones < 1:
            errores.append(f"MAX_POSICIONES debe ser >= 1: {self.max_posiciones}")
        if self.velas_minimas < self.periodo_ema + self.st_periodo * 3:
            errores.append("VELAS_MINIMAS insuficiente para calcular EMA200 + ADX")
        return errores

    @property
    def dist_liquidacion(self) -> float:
        """Distancia aproximada a liquidación con margen aislado."""
        return max(1.0 / max(self.leverage, 1) - self.mmr, 0.0)

    @property
    def dist_sl_maxima(self) -> float:
        return min(self.dist_liquidacion * 0.8, self.sl_max_pct)


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
# DIAGNÓSTICO (la respuesta a "¿por qué no está entrando?")
# ============================================================================

class Diagnostico:
    """Contadores de cada motivo por el que una señal NO se convirtió en orden.

    Se exponen en /status y en el heartbeat de Telegram. Si el bot deja de
    operar, esto dice exactamente en qué filtro (o en qué error de API) se
    está cayendo, sin necesidad de leer logs a ciegas.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.bloqueos: Dict[str, Dict[str, int]] = {}
        self.globales: Dict[str, int] = {}
        self.ultimo_error: Optional[str] = None
        self.ultimo_error_ts: Optional[datetime] = None
        self.ultima_api_ok: Optional[datetime] = None
        self.fallos_api_consecutivos: int = 0
        self.max_fallos_api_consecutivos: int = 0
        self.inicio: datetime = datetime.now(timezone.utc)
        self.ultimo_ciclo: Optional[datetime] = None
        self.ultimo_heartbeat: Optional[datetime] = None
        self.senales_detectadas: int = 0
        self.entradas_ejecutadas: int = 0
        self.entradas_fallidas: int = 0

    def bloqueo(self, symbol: str, motivo: str):
        with self._lock:
            self.bloqueos.setdefault(symbol, {})
            self.bloqueos[symbol][motivo] = self.bloqueos[symbol].get(motivo, 0) + 1
            self.globales[motivo] = self.globales.get(motivo, 0) + 1

    def api_ok(self):
        with self._lock:
            self.ultima_api_ok = datetime.now(timezone.utc)
            self.fallos_api_consecutivos = 0

    def api_error(self, motivo: str):
        with self._lock:
            self.fallos_api_consecutivos += 1
            self.max_fallos_api_consecutivos = max(self.max_fallos_api_consecutivos,
                                                   self.fallos_api_consecutivos)
            self.ultimo_error = str(motivo)[:400]
            self.ultimo_error_ts = datetime.now(timezone.utc)

    def ciclo(self):
        with self._lock:
            self.ultimo_ciclo = datetime.now(timezone.utc)

    def resumen(self) -> Dict:
        with self._lock:
            ahora = datetime.now(timezone.utc)
            return {
                "desde": self.inicio.isoformat(),
                "horas_activo": round((ahora - self.inicio).total_seconds() / 3600, 2),
                "ultimo_ciclo": self.ultimo_ciclo.isoformat() if self.ultimo_ciclo else None,
                "segundos_desde_ultimo_ciclo": (
                    (ahora - self.ultimo_ciclo).total_seconds() if self.ultimo_ciclo else None),
                "ultima_api_ok": self.ultima_api_ok.isoformat() if self.ultima_api_ok else None,
                "segundos_desde_api_ok": (
                    (ahora - self.ultima_api_ok).total_seconds() if self.ultima_api_ok else None),
                "fallos_api_consecutivos": self.fallos_api_consecutivos,
                "max_fallos_api_consecutivos": self.max_fallos_api_consecutivos,
                "ultimo_error": self.ultimo_error,
                "ultimo_error_ts": self.ultimo_error_ts.isoformat() if self.ultimo_error_ts else None,
                "senales_detectadas": self.senales_detectadas,
                "entradas_ejecutadas": self.entradas_ejecutadas,
                "entradas_fallidas": self.entradas_fallidas,
                "bloqueos_por_simbolo": {k: dict(v) for k, v in self.bloqueos.items()},
                "bloqueos_totales": dict(sorted(self.globales.items(),
                                                key=lambda kv: -kv[1])),
            }


diagnostico = Diagnostico()


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
        self.exchange.set_sandbox_mode(cfg.sandbox)
        self.logger.warning("Modo %s", "DEMO/TESTNET (OKX_SANDBOX=1)" if cfg.sandbox
                            else "REAL — dinero real (OKX_SANDBOX=0)")
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

    def posiciones_todas(self) -> Optional[Dict[str, Dict]]:
        """{symbol: posición} de todas las posiciones abiertas.

        Devuelve None si la consulta falla (nunca se asume 'estoy plano').
        Una sola llamada por ciclo: menos rate-limit y estado consistente.
        """
        try:
            out: Dict[str, Dict] = {}
            for p in self.exchange.fetch_positions(None, {"instType": "SWAP"}):
                if float(p.get("contracts") or 0) > 0:
                    out[p["symbol"]] = p
            return out
        except Exception as e:
            self.logger.error("No pude leer posiciones: %s", e)
            return None

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
        return self._algo_stop(symbol, side_entrada, amount, sl_precio, tp_precio, "oco")

    def crear_proteccion(self, symbol: str, side_entrada: str, amount: float,
                         sl_precio: float, tp_precio: Optional[float] = None) -> Optional[str]:
        """Stop de protección reduceOnly.

        Sin TP (recomendado): orden 'conditional' sólo con stop-loss. El
        beneficio se deja correr hasta que el SuperTrend gire y el bot cierre.
        Con TP: OCO clásica SL+TP.
        Si 'conditional' es rechazado, se reintenta como OCO (con TP si hay).
        """
        algo = self._algo_stop(symbol, side_entrada, amount, sl_precio, tp_precio, "conditional")
        if algo or tp_precio is None:
            return algo
        self.logger.warning("%s: 'conditional' rechazado, reintento como OCO", symbol)
        return self._algo_stop(symbol, side_entrada, amount, sl_precio, tp_precio, "oco")

    def _algo_stop(self, symbol: str, side_entrada: str, amount: float,
                   sl_precio: float, tp_precio: Optional[float], ord_type: str) -> Optional[str]:
        side_cierre = "sell" if side_entrada == "buy" else "buy"
        body = {
            "instId": self.exchange.market(symbol)["id"],
            "tdMode": "isolated",
            "side": side_cierre,
            "ordType": ord_type,
            "sz": self.exchange.amount_to_precision(symbol, amount),
            "reduceOnly": "true",
            "slTriggerPx": self.exchange.price_to_precision(symbol, sl_precio),
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
        }
        if tp_precio is not None:
            body["tpTriggerPx"] = self.exchange.price_to_precision(symbol, tp_precio)
            body["tpOrdPx"] = "-1"
            body["tpTriggerPxType"] = "last"
        try:
            resp = self.exchange.private_post_trade_order_algo(body)
            data = (resp.get("data") or [{}])[0]
            if str(resp.get("code")) == "0" and str(data.get("sCode", "0")) == "0":
                algo_id = data.get("algoId")
                self.logger.info("%s: protección %s creada SL %.6f%s (algoId=%s)", symbol,
                                 ord_type, sl_precio,
                                 f" / TP {tp_precio:.6f}" if tp_precio else " (sin TP)",
                                 algo_id)
                return algo_id or "?"
            self.logger.error("%s: orden algo '%s' rechazada code=%s msg=%s sMsg=%s", symbol,
                              ord_type, resp.get("code"), resp.get("msg"), data.get("sMsg"))
        except Exception as e:
            self.logger.error("%s: error creando orden algo '%s': %s", symbol, ord_type, e)
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
        """Giro del SuperTrend confirmado con ADX y EMA200 (sólo velas cerradas)."""
        if v.adx <= self.config.adx_threshold:
            return TrendDirection.NEUTRAL
        if not v.st_direction_anterior and v.st_direction and v.precio_cierre > v.ema200:
            return TrendDirection.UPTREND
        if self.config.permitir_cortos and v.st_direction_anterior and \
                not v.st_direction and v.precio_cierre < v.ema200:
            return TrendDirection.DOWNTREND
        return TrendDirection.NEUTRAL

    def razon_no_operar(self, v: IndicatorValues) -> Optional[str]:
        """Motivo de la ÚLTIMA vela cerrada. Se acumula en el Diagnostico."""
        if v.adx <= self.config.adx_threshold:
            return f"ADX bajo ({v.adx:.1f})"
        if v.st_direction == v.st_direction_anterior:
            return "sin giro de SuperTrend"
        if v.st_direction and v.precio_cierre <= v.ema200:
            return "giro alcista pero bajo EMA200"
        if not v.st_direction and v.precio_cierre >= v.ema200:
            return "giro bajista pero sobre EMA200"
        if not v.st_direction and not self.config.permitir_cortos:
            return "cortos deshabilitados"
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
                    v: IndicatorValues) -> Tuple[Optional[Plan], str]:
        """Devuelve (plan, motivo). motivo = "" si hay plan; si no, dice por qué.

        Los motivos se registran en el Diagnostico: son la respuesta a
        "el bot está vivo pero no entra".
        """
        if direction == TrendDirection.UPTREND:
            side, sl = "buy", v.lower_band
            if sl >= precio:
                return None, "banda ST no sirve de SL"
            dist = (precio - sl) / precio
        else:
            side, sl = "sell", v.upper_band
            if sl <= precio:
                return None, "banda ST no sirve de SL"
            dist = (sl - precio) / precio

        if dist >= self.config.dist_sl_maxima:
            self.logger.warning("%s: SL a %.2f%% vs liquidación ~%.2f%% (%sx) — no opero",
                                symbol, dist * 100, self.config.dist_liquidacion * 100,
                                self.config.leverage)
            return None, f"SL demasiado lejos ({dist*100:.1f}%)"
        if dist < self.config.sl_min_pct:
            self.logger.warning("%s: SL demasiado justo (%.2f%%) — no opero", symbol, dist * 100)
            return None, f"SL demasiado justo ({dist*100:.2f}%)"

        bal = self._equity()
        if not bal:
            return None, "no pude leer balance (API)"
        equity, libre = bal
        if equity <= 0 or libre <= 0:
            self.logger.error("Equity/libre USDT <= 0")
            return None, "equity o saldo libre <= 0"

        riesgo = min(equity * self.config.fraccion_equity, self.config.capital_riesgo_usdt)
        cs = self.exchange.contract_size(symbol)
        # Pérdida por contrato si salta el SL: distancia + comisiones de ida y vuelta
        perdida_x_contrato = (dist * precio + 2 * self.config.fee_rate * precio) * cs
        amount = self.exchange.cantidad_precisa(symbol, riesgo / perdida_x_contrato)
        minimo = self.exchange.cantidad_minima(symbol)
        if amount <= 0 or amount < minimo:
            self.logger.warning("%s: tamaño %.6f por debajo del mínimo %.6f "
                                "(equity %.2f, riesgo %.2f) — subí FRACCION_EQUITY o el capital",
                                symbol, amount, minimo, equity, riesgo)
            return None, f"tamaño mínimo ({amount:.4f} < {minimo:.4f})"

        margen = amount * cs * precio / self.config.leverage
        if margen * self.config.margen_seguridad > libre:
            self.logger.warning("%s: margen requerido %.2f > libre %.2f — no opero",
                                symbol, margen, libre)
            return None, f"margen insuficiente ({margen:.2f} > {libre:.2f})"

        riesgo_real = amount * perdida_x_contrato
        self.logger.info("%s: riesgo %.2f USDT (equity %.2f) · SL %.2f%% · %s contratos · margen %.2f",
                         symbol, riesgo_real, equity, dist * 100, amount, margen)
        return Plan(side=side, amount=amount, sl=self.exchange.precio_preciso(symbol, sl),
                    riesgo_usdt=riesgo_real, dist_sl=dist), ""

    # --- apertura --------------------------------------------------------
    def abrir_posicion(self, signal: TradeSignal, v: IndicatorValues) -> Tuple[bool, str, bool]:
        """Devuelve (ok, motivo, transitorio).

        transitorio=True significa "falló por algo que puede arreglarse solo"
        (red, sin fill, API). En ese caso el bot NO consume la señal: la
        reintenta en los próximos ciclos mientras la vela siga vigente.
        """
        symbol = signal.symbol
        try:
            ticker = self.exchange.obtener_ticker(symbol)
            if not ticker or not ticker.get("last"):
                return False, "sin ticker (API)", True
            precio = float(ticker["last"])
            # No perseguir: si el precio ya se escapó de la señal, abortar.
            deriva = abs(precio - signal.precio) / signal.precio
            if deriva > self.config.deriva_max_entrada:
                self.logger.warning("%s: precio corrido %.2f%% desde la señal — no persigo",
                                    symbol, deriva * 100)
                return False, f"precio corrido {deriva*100:.2f}%", False

            plan, motivo = self.dimensionar(symbol, precio, signal.direction, v)
            if not plan:
                # "no pude leer balance (API)" es transitorio; el resto es definitivo
                return False, motivo, motivo.startswith("no pude leer balance")
            razon = f"ST({self.config.st_periodo},{self.config.st_multiplier}) flip " \
                    f"{signal.direction.value} ADX {v.adx:.1f}"
            cs = self.exchange.contract_size(symbol)
            tp = (self.exchange.precio_preciso(symbol, self._tp(plan.side, precio, plan.sl))
                  if self.config.usar_tp else None)

            if self.sim:
                self.db.abrir(symbol, signal.direction.value, True, v.vela_ts, precio, plan.sl,
                              tp or 0.0, plan.amount, cs, plan.riesgo_usdt, razon)
                msg = (f"SIMULADA {symbol} {signal.direction.value.upper()}\n"
                       f"Tamaño: {plan.amount} · Entrada: {precio:.4f}\n"
                       f"SL: {plan.sl:.4f}" + (f" · TP: {tp:.4f}" if tp else " · sin TP (corre el beneficio)") +
                       f" · 1R = {plan.riesgo_usdt:.2f} USDT")
                self.logger.info(msg.replace("\n", " | "))
                notifier.enviar(msg, "TRADE")
                return True, "", False

            orden = self.exchange.crear_orden_mercado(symbol, plan.side, plan.amount, reduce_only=False)
            if not orden:
                return False, "la orden de entrada fue rechazada", True

            filled, avg = self.exchange.detalle_fill(orden, symbol)
            # Verificar contra la posición real (fuente de verdad)
            pos = self.exchange.posicion_o_none(symbol)
            contratos = float(pos.get("contracts") or 0) if pos else 0.0
            if contratos <= 0:
                self.logger.error("%s: entrada sin fill (filled=%s) — no creo protección", symbol, filled)
                return False, "entrada sin fill", True
            if abs(contratos - plan.amount) > 1e-9:
                self.logger.warning("%s: fill parcial/distinto: pedido %s, posición %s",
                                    symbol, plan.amount, contratos)
            fill_px = avg or float(pos.get("entryPrice") or 0) or precio

            dist_real = ((fill_px - plan.sl) / fill_px) if plan.side == "buy" else ((plan.sl - fill_px) / fill_px)
            if dist_real <= 0 or dist_real >= self.config.dist_sl_maxima:
                self.logger.error("%s: fill %.4f deja SL a %.2f%% — rollback", symbol, fill_px, dist_real * 100)
                self._rollback(symbol, signal, fill_px, "el fill dejó el SL fuera de rango")
                return False, "fill fuera de rango (rollback)", False

            # Protección obligatoria: stop en el exchange. Sin TP: el beneficio
            # se deja correr hasta el giro del SuperTrend (ver README/backtests).
            algo_id = self.exchange.crear_proteccion(symbol, plan.side, contratos, plan.sl, tp)
            if not algo_id:
                self._rollback(symbol, signal, fill_px, "no se pudo crear el stop de protección")
                return False, "sin stop de protección (rollback)", True

            riesgo_real = contratos * cs * abs(fill_px - plan.sl) + 2 * self.config.fee_rate * fill_px * contratos * cs
            self.db.abrir(symbol, signal.direction.value, False, v.vela_ts, fill_px, plan.sl,
                          tp or 0.0, contratos, cs, riesgo_real, razon)
            msg = (f"NUEVA OPERACIÓN {symbol} {signal.direction.value.upper()}\n"
                   f"Tamaño: {contratos} contratos · Entrada: {fill_px:.4f}\n"
                   f"SL: {plan.sl:.4f}" + (f" · TP: {tp:.4f}" if tp else "") +
                   f" (algo {algo_id})\n"
                   f"1R = {riesgo_real:.2f} USDT · ADX {v.adx:.1f}")
            self.logger.info(msg.replace("\n", " | "))
            notifier.enviar(msg, "TRADE")
            return True, "", False
        except ccxt.NetworkError as e:
            self.logger.error("Error de red abriendo posición en %s: %s", symbol, e)
            return False, f"error de red: {e}", True
        except ccxt.ExchangeError as e:
            self.logger.error("Exchange rechazó la entrada en %s: %s", symbol, e)
            self.logger.debug(traceback.format_exc())
            return False, f"exchange: {str(e)[:120]}", False
        except Exception as e:
            self.logger.error("Error abriendo posición en %s: %s", symbol, e)
            self.logger.debug(traceback.format_exc())
            notifier.enviar(f"Error abriendo posición en {symbol}: {e}. Revisá OKX.", "ERROR")
            return False, f"excepción: {str(e)[:120]}", True

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
            tp = float(trade["tp_px"] or 0)
            if trade["side"] == "long":
                toca_sl = lo <= trade["sl_px"]
                toca_tp = tp > 0 and hi >= tp
            else:
                toca_sl = hi >= trade["sl_px"]
                toca_tp = tp > 0 and lo <= tp
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
        self.posiciones: Dict[str, Optional[Dict]] = {}
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
        return True   # el aviso de arranque lo manda notificar_arranque()

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
            res_razon = "SL en exchange"
            t = self.exchange.obtener_ticker(symbol)
            if t and t.get("last") and float(trade["tp_px"] or 0) > 0:
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
            tp = float(trade["tp_px"] or 0) or None
            if not self.exchange.crear_proteccion(symbol, side, amt, trade["sl_px"], tp):
                self.executor.cerrar_por_senal(symbol, "sin protección y OCO rechazada", trade["entry_px"])

    # --- señales pendientes (fallo transitorio ≠ señal perdida) ----------
    def _clave_pendiente(self, symbol: str) -> str:
        return f"senal_pendiente:{'sim' if self.sim else 'real'}:{symbol}"

    def _leer_pendiente(self, symbol: str) -> Optional[Dict]:
        raw = self.db.kv_get(self._clave_pendiente(symbol))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _guardar_pendiente(self, symbol: str, datos: Optional[Dict]):
        clave = self._clave_pendiente(symbol)
        if datos is None:
            self.db.kv_set(clave, "")
        else:
            self.db.kv_set(clave, json.dumps(datos))

    def analizar_symbol(self, symbol: str, entradas_habilitadas: bool,
                        posicion: Optional[Dict] = None) -> bool:
        df_raw = self.exchange.obtener_velas_cerradas(symbol)
        if df_raw is None or len(df_raw) < self.config.velas_minimas:
            diagnostico.bloqueo(symbol, f"datos insuficientes ({0 if df_raw is None else len(df_raw)})")
            self.logger.warning("%s: datos insuficientes (%s/%s)", symbol,
                                0 if df_raw is None else len(df_raw), self.config.velas_minimas)
            return False
        df = self.analyzer.calcular_indicadores(df_raw)
        v = self.analyzer.extraer_valores(df)
        if not v:
            diagnostico.bloqueo(symbol, "indicadores no listos")
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
            self._conciliar_real(symbol, posicion)
            side_pos = posicion.get("side") if posicion else None

        razon = self.analyzer.razon_no_operar(v)
        if razon:
            diagnostico.bloqueo(symbol, razon)
        self.set_estado(symbol, {"precio": v.precio_cierre, "st": v.st_direction, "adx": v.adx,
                                 "posicion": side_pos is not None, "side_posicion": side_pos,
                                 "razon": razon, "ema200": v.ema200})

        # --- salida por giro de SuperTrend (con reversión en la misma vela) ---
        if side_pos:
            giro_contra = ((side_pos == "long" and not v.st_direction) or
                           (side_pos == "short" and v.st_direction))
            if not giro_contra:
                return True
            motivo = f"SuperTrend giró {'bajista' if side_pos == 'long' else 'alcista'}"
            self.logger.warning("SALIDA %s: %s", symbol, motivo)
            if self.executor.cerrar_por_senal(symbol, motivo, v.precio_cierre):
                # Cerrada. Si la vela también da señal en el otro sentido, se
                # abre YA: un sistema de tendencia revierte cuando gira.
                (self.posiciones or {}).pop(symbol, None)
                side_pos = None
                self.set_estado(symbol, {"precio": v.precio_cierre, "st": v.st_direction,
                                         "adx": v.adx, "posicion": False, "side_posicion": None,
                                         "razon": razon, "ema200": v.ema200})
            else:
                self.logger.error("%s: no pude cerrar la posición; no abro la contraria", symbol)
                return True

        # --- entrada ---
        senal = self.analyzer.detectar_senal(v)
        if senal == TrendDirection.NEUTRAL:
            return True

        pendiente = self._leer_pendiente(symbol)
        if pendiente and pendiente.get("vela_ts") != v.vela_ts:
            # la vela de la señal ya no es la última: el intento caducó
            self.logger.warning("%s: señal pendiente de la vela %s caducó sin ejecutarse",
                                symbol, pendiente.get("vela_ts"))
            notifier.enviar(f"{symbol}: señal {pendiente.get('direccion','?')} NO ejecutada "
                            f"(se acabaron los reintentos). Motivo: {pendiente.get('motivo','?')}",
                            "WARNING")
            self._guardar_pendiente(symbol, None)
            pendiente = None

        if self.ya_procesada(symbol, v.vela_ts):
            if not pendiente:
                self.logger.debug("%s: señal de la vela %s ya procesada", symbol, v.vela_ts)
                return True
            if pendiente.get("intentos", 0) > self.config.reintentos_senal:
                self.logger.error("%s: señal agotó los %s reintentos — se descarta",
                                  symbol, self.config.reintentos_senal)
                notifier.enviar(f"{symbol}: señal {pendiente.get('direccion','?')} descartada tras "
                                f"{self.config.reintentos_senal} reintentos. "
                                f"Último motivo: {pendiente.get('motivo','?')}", "ERROR")
                self._guardar_pendiente(symbol, None)
                return True

        if not entradas_habilitadas:
            diagnostico.bloqueo(symbol, "límite diario de pérdidas")
            self.logger.warning("%s: señal %s descartada por límite diario", symbol, senal.value)
            return True

        # --- control de portafolio (huecos y dirección) ---
        abiertas = [p for s, p in (self.posiciones or {}).items() if p and s != symbol]
        if len(abiertas) >= self.config.max_posiciones:
            diagnostico.bloqueo(symbol, f"sin hueco (máx {self.config.max_posiciones} posiciones)")
            return True
        dir_objetivo = "long" if senal == TrendDirection.UPTREND else "short"
        misma_dir = sum(1 for p in abiertas if p.get("side") == dir_objetivo)
        if misma_dir >= self.config.max_pos_misma_dir:
            diagnostico.bloqueo(symbol, f"ya hay {misma_dir} posiciones en {dir_objetivo}")
            return True

        signal = TradeSignal(symbol, senal, v.precio_cierre, v.adx, v.vela_ts)
        diagnostico.senales_detectadas += 1
        self.logger.warning("SEÑAL: %s%s", signal,
                            f" (reintento {pendiente['intentos']})" if pendiente else "")
        ok, motivo, transitorio = self.executor.abrir_posicion(signal, v)

        if ok:
            diagnostico.entradas_ejecutadas += 1
            self.marcar_procesada(symbol, v.vela_ts)
            self._guardar_pendiente(symbol, None)
            return True

        diagnostico.entradas_fallidas += 1
        diagnostico.bloqueo(symbol, f"entrada fallida: {motivo}")
        if transitorio:
            # NO se consume la señal: se reintenta en el próximo ciclo mientras
            # la vela siga siendo la última cerrada.
            self._guardar_pendiente(symbol, {
                "vela_ts": v.vela_ts, "direccion": senal.value, "motivo": motivo,
                "intentos": (pendiente or {}).get("intentos", 0) + 1,
            })
            self.logger.warning("%s: entrada diferida (%s). Reintento %s de %s.",
                                symbol, motivo, (pendiente or {}).get("intentos", 0) + 1,
                                self.config.reintentos_senal)
        else:
            self.marcar_procesada(symbol, v.vela_ts)
            self._guardar_pendiente(symbol, None)
        return True

    def ciclo_analisis(self):
        with self._lock:
            self.ciclo_contador += 1
            n = self.ciclo_contador
        diagnostico.ciclo()

        if self.sim:
            self.posiciones = {s: {"side": t["side"]} for s in self.simbolos_ok
                               if (t := self.db.trade_abierto(s, virtual=True))}
        else:
            posiciones = self.exchange.posiciones_todas()
            if posiciones is None:
                # Sin saber qué hay abierto, no se opera: sería abrir a ciegas.
                diagnostico.api_error("fetch_positions")
                self.logger.error("Ciclo #%04d abortado: no pude leer las posiciones. "
                                  "Fallos consecutivos: %s", n, diagnostico.fallos_api_consecutivos)
                if diagnostico.fallos_api_consecutivos in (3, 10, 30):
                    notifier.enviar(f"OKX no responde (fetch_positions) tras "
                                    f"{diagnostico.fallos_api_consecutivos} intentos: "
                                    f"{diagnostico.ultimo_error}. El bot sigue vivo pero NO opera "
                                    f"hasta que la API responda.", "ERROR")
                return
            self.posiciones = posiciones
            diagnostico.api_ok()

        entradas = not self._limite_diario_alcanzado()
        fallos = []
        for symbol in self.simbolos_ok:
            try:
                if not self.analizar_symbol(symbol, entradas,
                                            (self.posiciones or {}).get(symbol)):
                    fallos.append(symbol)
            except Exception as e:
                self.logger.error("Excepción en %s: %s", symbol, e)
                self.logger.debug(traceback.format_exc())
                fallos.append(symbol)
                diagnostico.bloqueo(symbol, f"excepción: {str(e)[:60]}")
        if fallos:
            self.logger.warning("Ciclo #%04d · fallos: %s", n, ", ".join(fallos))
        else:
            self.logger.info("Ciclo #%04d completo · próximo en %ss", n, self.config.ciclo_segundos)
        if self._debe_enviar_reporte():
            notifier.enviar(self._generar_reporte(), "INFO")
        self._heartbeat()

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
        d = diagnostico.resumen()
        if d["fallos_api_consecutivos"]:
            lineas += ["", f"API: {d['fallos_api_consecutivos']} fallos consecutivos · "
                           f"último: {d['ultimo_error']}"]
        if d["senales_detectadas"]:
            lineas += ["", f"Señales: {d['senales_detectadas']} · ejecutadas "
                           f"{d['entradas_ejecutadas']} · fallidas {d['entradas_fallidas']}"]
        return "\n".join(lineas)

    def _heartbeat(self):
        """Latido: si el bot está sano pero no opera, hay que enterarse."""
        ahora = datetime.now(timezone.utc)
        if diagnostico.ultimo_heartbeat and \
                (ahora - diagnostico.ultimo_heartbeat).total_seconds() < \
                self.config.heartbeat_horas * 3600:
            return
        diagnostico.ultimo_heartbeat = ahora
        d = diagnostico.resumen()
        st = self.db.estadisticas(virtual=self.sim)
        abiertas = len([p for p in (self.posiciones or {}).values() if p])
        lineas = [f"LATIDO · {ahora.astimezone(GMT_MINUS_3).strftime('%d/%m %H:%M')} AR",
                  f"Ciclos: {self.ciclo_contador} · posiciones abiertas: {abiertas}/"
                  f"{self.config.max_posiciones}",
                  f"Trades: {st['trades']} · PnL {st['pnl']:+.2f} USDT · "
                  f"hoy {self.db.resultado_hoy_r(virtual=self.sim):+.2f}R"]
        if d["segundos_desde_api_ok"] is not None:
            lineas.append(f"Última API OK hace {d['segundos_desde_api_ok']/60:.0f} min · "
                          f"fallos consecutivos: {d['fallos_api_consecutivos']}")
        lineas.append(f"Señales vistas: {d['senales_detectadas']} · ejecutadas: "
                      f"{d['entradas_ejecutadas']} · fallidas: {d['entradas_fallidas']}")
        if d["bloqueos_totales"]:
            top = list(d["bloqueos_totales"].items())[:4]
            lineas.append("Bloqueos: " + " | ".join(f"{k} ×{v}" for k, v in top))
        if d["ultimo_error"]:
            lineas.append(f"Último error: {d['ultimo_error'][:120]}")
        notifier.enviar("\n".join(lineas), "WARNING" if d["fallos_api_consecutivos"] else "INFO")

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
estado_global: Dict[str, object] = {"error_fatal": None, "intentos": 0}


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
    """Estado operativo completo, incluido el motivo por el que no se entra."""
    salida = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "diagnostico": diagnostico.resumen(),
    }
    b = _get_bot()
    if b is None:
        salida["estado"] = "inicializando" if not estado_global.get("error_fatal") else "error_fatal"
        salida["error_fatal"] = estado_global.get("error_fatal")
        salida["intentos_inicializacion"] = estado_global.get("intentos", 0)
        return salida, (500 if estado_global.get("error_fatal") else 202)
    s = b.snapshot()
    s["hoy_r"] = b.db.resultado_hoy_r(virtual=b.sim)
    s["estadisticas"] = b.db.estadisticas(virtual=b.sim)
    s["estrategia"] = {
        "nombre": f"SuperTrend({b.config.st_periodo},{b.config.st_multiplier}) + EMA200 + ADX>{b.config.adx_threshold:.0f}",
        "timeframe": b.config.timeframe,
        "riesgo_por_trade": b.config.fraccion_equity,
        "max_posiciones": b.config.max_posiciones,
        "take_profit": b.config.usar_tp,
        "cortos": b.config.permitir_cortos,
    }
    salida.update(s)
    salida["posiciones_abiertas"] = {k: {"side": v.get("side"), "contratos": v.get("contracts")}
                                     for k, v in (b.posiciones or {}).items() if v}
    return salida, 200


@app.route("/por_que_no_opero")
def por_que_no_opero():
    """Resumen legible de los motivos por los que el bot no está entrando."""
    d = diagnostico.resumen()
    lineas = [f"Diagnóstico desde {d['desde']} ({d['horas_activo']} h)",
              f"Ciclo: hace {d['segundos_desde_ultimo_ciclo']} s" if d["segundos_desde_ultimo_ciclo"] is not None else "Sin ciclos aún",
              f"Última API OK: hace {d['segundos_desde_api_ok']} s" if d["segundos_desde_api_ok"] is not None else "API nunca respondió",
              f"Fallos API consecutivos: {d['fallos_api_consecutivos']} (máx {d['max_fallos_api_consecutivos']})",
              f"Señales: {d['senales_detectadas']} · ejecutadas {d['entradas_ejecutadas']} · fallidas {d['entradas_fallidas']}",
              ""]
    if d["ultimo_error"]:
        lineas.append(f"Último error: {d['ultimo_error']}")
        lineas.append("")
    lineas.append("Motivos acumulados:")
    if d["bloqueos_totales"]:
        for motivo, n in d["bloqueos_totales"].items():
            lineas.append(f"  ×{n:<5} {motivo}")
    else:
        lineas.append("  (ninguno registrado)")
    return "<pre style='font:13px/1.5 ui-monospace,monospace'>" + html.escape("\n".join(lineas)) + "</pre>"


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

def preflight(cfg: BotConfig, instancia: "TradingBot") -> str:
    """Chequeo de arranque: ¿puede el bot operar cada símbolo con este capital?

    El error silencioso más común: con poco capital, el tamaño por riesgo
    redondea a 0 contratos y el símbolo jamás opera. Acá se ve de entrada.
    """
    lineas = []
    bal = instancia.exchange.balance_usdt() if not cfg.modo_simulacion else None
    equity = bal[0] if bal else (cfg.sim_equity_inicial +
                                 instancia.db.pnl_total(virtual=True))
    riesgo = min(equity * cfg.fraccion_equity, cfg.capital_riesgo_usdt)
    for symbol in instancia.simbolos_ok:
        try:
            df = instancia.exchange.obtener_velas_cerradas(symbol)
            if df is None:
                lineas.append(f"- {symbol}: sin datos")
                continue
            v = instancia.analyzer.extraer_valores(
                instancia.analyzer.calcular_indicadores(df))
            cs = instancia.exchange.contract_size(symbol)
            minimo = instancia.exchange.cantidad_minima(symbol)
            px = v.precio_cierre
            dist = (abs(px - (v.lower_band if v.st_direction else v.upper_band)) / px
                    if v else 0.02)
            dist = max(min(dist, cfg.dist_sl_maxima), cfg.sl_min_pct)
            contratos = riesgo / (dist * px * cs)
            redondeado = instancia.exchange.cantidad_precisa(symbol, contratos)
            ok = redondeado >= minimo
            lineas.append(f"- {symbol.split('/')[0]:<5} px {px:<12.4g} stop típico {dist*100:4.1f}% · "
                          f"1 contrato ≈ {cs * px:,.0f} USDT · tamaño sugerido {redondeado:g} "
                          f"(mín {minimo:g}) {'OK' if ok else '⚠️ REDONDEA A CERO'}")
        except Exception as e:
            lineas.append(f"- {symbol}: no pude evaluar ({e})")
    encabezado = (f"Equity {equity:,.2f} USDT · riesgo por trade {riesgo:,.2f} USDT "
                  f"({cfg.fraccion_equity * 100:.2f}%)")
    return encabezado + "\n" + "\n".join(lineas)


def notificar_arranque(cfg: BotConfig, instancia: "TradingBot"):
    modo = "SIMULACIÓN" if cfg.modo_simulacion else ("DEMO/TESTNET" if cfg.sandbox else "REAL")
    msg = (f"Bot iniciado ({modo})\n"
           f"Estrategia: SuperTrend({cfg.st_periodo},{cfg.st_multiplier}) + EMA{cfg.periodo_ema} "
           f"+ ADX>{cfg.adx_threshold:.0f} · velas {cfg.timeframe}\n"
           f"Riesgo {cfg.fraccion_equity * 100:.2f}% · máx {cfg.max_posiciones} posiciones · "
           f"TP {'sí' if cfg.usar_tp else 'no (se deja correr)'} · "
           f"cortos {'sí' if cfg.permitir_cortos else 'no'}\n"
           f"Límite diario {cfg.limite_diario_r:.0f}R\n"
           f"{', '.join(s.split('/')[0] for s in instancia.simbolos_ok)}")
    try:
        msg += "\n" + preflight(cfg, instancia)
    except Exception as e:
        logger.warning("Preflight incompleto: %s", e)
    logger.info("Arranque:\n%s", msg)
    notifier.enviar(msg, "SUCCESS")


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

    # Reintentos de inicialización: un fallo de red al arrancar ya no deja al
    # bot "vivo pero muerto" (el bug que hacía parecer que andaba y no operaba).
    while True:
        estado_global["intentos"] = int(estado_global.get("intentos") or 0) + 1
        try:
            instancia = TradingBot(config)
            estado_global["error_fatal"] = None
            with _bot_lock:
                bot = instancia
            notificar_arranque(config, instancia)
            instancia.ejecutar()          # vuelve sólo si el bucle se detiene
            logger.error("El bucle principal terminó; reintentando en 60 s")
        except Exception as e:
            estado_global["error_fatal"] = f"{type(e).__name__}: {e}"
            logger.critical("Error fatal (intento %s): %s", estado_global["intentos"], e)
            logger.debug(traceback.format_exc())
            notifier.enviar(f"Error fatal al iniciar (intento {estado_global['intentos']}): "
                            f"{type(e).__name__}: {e}. Reintento en 5 min.", "ERROR")
        time.sleep(300)


if __name__ == "__main__":
    main()
