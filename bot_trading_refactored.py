#!/usr/bin/env python3
"""
Bot de Trading OKX Testnet — Opción A (reemplazo)

Estrategia: SuperTrend + EMA200 + ADX.

Este archivo es el reemplazo del bot original. Las ~400 líneas que faltan
NO son el fail-safe, ni el margen aislado, ni el order-algo. Son el ensayo
de la API de OKX, el alias legacy crear_stop_loss, un IndicatorValues que
nadie usaba, banners de log y comentarios que repetían el if de abajo.

Lo que SÍ cambia el comportamiento (a propósito):
- Apalancamiento por defecto 10x (el 50x original liquidaba antes del TP).
  Se puede subir con OKX_LEVERAGE; si el SL cae dentro del 80% de
  1/apalancamiento, el bot no entra.
- SL = banda del SuperTrend, TP = 2R, tamaño = riesgo / distancia.
- Una señal = una vela (llave = timestamp de iloc[-2]).
- Cierres con reduceOnly. SL/TP con el filled real.
- Telegram sin parse_mode HTML.
- ADX 14 separado del SuperTrend 10. 1000 velas para la EMA200.
- SQLite de trades + MODO_SIMULACION=1 para dry-run.

Sandbox sigue en True. Margen aislado sigue. Si el SL no se crea, se cierra
la posición recién abierta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, Optional, Tuple
import html
import logging
import os
import sqlite3
import threading
import time
import traceback

from flask import Flask
import ccxt
import numpy as np
import pandas as pd
import requests

GMT_MINUS_3 = timezone(timedelta(hours=-3))
REPORTES_HORAS = (
    7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22,
    23, 1, 3, 5,
)


# ============================================================================
# LOGGING
# ============================================================================

class ColoredFormatter(logging.Formatter):
    """Colorea el levelname en consola y lo restaura para no ensuciar el archivo."""

    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[95m",
        "RESET": "\033[0m",
    }

    def format(self, record):
        color = self.COLORS.get(record.levelname, "")
        reset = self.COLORS["RESET"] if color else ""
        original = record.levelname
        record.levelname = f"{color}{original}{reset}" if color else original
        try:
            return super().format(record)
        finally:
            record.levelname = original


def setup_logging(log_file: str = "bot_trading.log") -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    consola = logging.StreamHandler()
    consola.setLevel(logging.DEBUG)
    consola.setFormatter(ColoredFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(consola)

    if log_file:
        archivo = logging.FileHandler(log_file, encoding="utf-8")
        archivo.setLevel(logging.DEBUG)
        archivo.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(archivo)

    for noisy in ("flask", "werkzeug", "urllib3", "ccxt", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("TradingBot")


logger = setup_logging("bot_trading.log")


# ============================================================================
# TIPOS
# ============================================================================

class TrendDirection(Enum):
    UPTREND = "long"
    DOWNTREND = "short"
    NEUTRAL = None


@dataclass
class IndicatorValues:
    precio_actual: float
    ema200: float
    adx: float
    st_direction: bool
    st_direction_anterior: bool
    upper_band: float
    lower_band: float
    vela_ts: int


@dataclass
class TradeSignal:
    symbol: str
    direction: TrendDirection
    precio: float
    adx: float
    timestamp: datetime
    vela_ts: int

    def __str__(self):
        return (
            f"[{self.timestamp.strftime('%H:%M:%S')}] "
            f"{self.symbol} - {self.direction.value.upper()} @ {self.precio:.4f} "
            f"(ADX: {self.adx:.2f})"
        )


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
    api_key: str
    api_secret: str
    api_password: str
    telegram_token: str
    telegram_chat_id: str

    simbolos: list = None
    timeframe: str = "1h"
    leverage: int = 10
    capital_riesgo_usdt: float = 50.0
    fraccion_equity: float = 0.0075

    st_periodo: int = 10
    st_multiplier: float = 3.0
    periodo_ema: int = 200
    adx_periodo: int = 14
    adx_threshold: float = 22.0

    ciclo_segundos: int = 60
    limite_velas: int = 1000
    velas_minimas: int = 300
    modo_simulacion: bool = False

    def __post_init__(self):
        if self.simbolos is None:
            self.simbolos = [
                "BTC/USDT:USDT",
                "ETH/USDT:USDT",
                "SOL/USDT:USDT",
                "XRP/USDT:USDT",
                "AVAX/USDT:USDT",
            ]

    @classmethod
    def desde_env(cls) -> "BotConfig":
        return cls(
            api_key=os.getenv("OKX_API_KEY") or "",
            api_secret=os.getenv("OKX_API_SECRET") or "",
            api_password=os.getenv("OKX_API_PASSWORD") or "",
            telegram_token=os.getenv("TELEGRAM_TOKEN") or "",
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or "",
            leverage=_env_int("OKX_LEVERAGE", 10),
            capital_riesgo_usdt=_env_float("CAPITAL_RIESGO_USDT", 50.0),
            modo_simulacion=_env_bool("MODO_SIMULACION", False),
        )


config = BotConfig.desde_env()


# ============================================================================
# TELEGRAM — sin parse_mode HTML (el '<' de "precio < EMA200" tumba el reporte)
# ============================================================================

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.logger = logging.getLogger("Telegram")
        self.habilitado = bool(token and chat_id)

    def enviar(self, mensaje: str, nivel: str = "INFO") -> bool:
        if not self.habilitado:
            self.logger.debug("Telegram deshabilitado: %s", mensaje[:80])
            return False

        emojis = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "TRADE": "🎯",
        }
        texto = f"{emojis.get(nivel, '•')} {mensaje}"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": texto,
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=5)
            r.raise_for_status()
            return True
        except requests.exceptions.Timeout:
            self.logger.warning("Timeout al enviar a Telegram")
            return False
        except Exception as e:
            self.logger.error("Error en Telegram: %s", e)
            return False


notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)


# ============================================================================
# PERSISTENCIA (SQLite) — sin esto no hay forma de medir la estrategia
# ============================================================================

class Persistencia:
    def __init__(self, path: str = "trades.db"):
        self.path = path
        self.logger = logging.getLogger("Persistencia")
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_ts TEXT NOT NULL,
                    entry_px REAL NOT NULL,
                    sl_px REAL,
                    tp_px REAL,
                    size REAL NOT NULL,
                    exit_ts TEXT,
                    exit_px REAL,
                    razon_entrada TEXT,
                    razon_salida TEXT,
                    pnl REAL,
                    virtual INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def abrir(
        self,
        symbol: str,
        side: str,
        entry_px: float,
        sl_px: float,
        tp_px: float,
        size: float,
        razon: str,
        virtual: bool,
    ) -> Optional[int]:
        try:
            with self._conn() as c:
                cur = c.execute(
                    """
                    INSERT INTO trades (
                        symbol, side, entry_ts, entry_px, sl_px, tp_px,
                        size, razon_entrada, virtual
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        side,
                        datetime.now(timezone.utc).isoformat(),
                        entry_px,
                        sl_px,
                        tp_px,
                        size,
                        razon,
                        1 if virtual else 0,
                    ),
                )
                return int(cur.lastrowid)
        except Exception as e:
            self.logger.error("No pude registrar apertura: %s", e)
            return None

    def cerrar(self, trade_id: Optional[int], exit_px: float, razon: str, pnl: Optional[float]):
        if not trade_id:
            return
        try:
            with self._conn() as c:
                c.execute(
                    """
                    UPDATE trades
                    SET exit_ts = ?, exit_px = ?, razon_salida = ?, pnl = ?
                    WHERE id = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        exit_px,
                        razon,
                        pnl,
                        trade_id,
                    ),
                )
        except Exception as e:
            self.logger.error("No pude registrar cierre: %s", e)


# ============================================================================
# EXCHANGE
# ============================================================================

class ExchangeManager:
    def __init__(self, cfg: BotConfig):
        self.config = cfg
        self.logger = logging.getLogger("Exchange")
        self.exchange = None
        self._inicializar()

    def _inicializar(self):
        self.exchange = ccxt.okx({
            "apiKey": self.config.api_key,
            "secret": self.config.api_secret,
            "password": self.config.api_password,
            "enableRateLimit": True,
            "timeout": 15000,
            "options": {"defaultType": "swap"},
        })
        self.exchange.set_sandbox_mode(True)
        markets = self.exchange.load_markets()
        self.logger.info("Conexión OKX sandbox OK (%s mercados)", len(markets))

    def configurar_mercado(self, symbol: str) -> bool:
        try:
            try:
                self.exchange.set_margin_mode("isolated", symbol)
                self.logger.info("%s: margen AISLADO", symbol)
            except Exception as e:
                self.logger.debug("%s: margen ya aislado o no se pudo cambiar: %s", symbol, e)
            try:
                self.exchange.set_leverage(self.config.leverage, symbol)
            except Exception as e:
                msg = str(e).lower()
                if any(p in msg for p in ("already", "same", "not modified", "no change")):
                    self.logger.info("%s: leverage ya era %sx", symbol, self.config.leverage)
                else:
                    self.logger.error("%s: no pude fijar leverage %sx: %s", symbol, self.config.leverage, e)
                    return False
            self.logger.info("%s: apalancamiento %sx", symbol, self.config.leverage)
            return True
        except ccxt.BadSymbol:
            self.logger.error("Símbolo inválido: %s", symbol)
            return False
        except Exception as e:
            self.logger.warning("Error configurando %s: %s", symbol, e)
            return False

    def obtener_velas(self, symbol: str, limit: int = None) -> Optional[pd.DataFrame]:
        """Pagina fetch_ohlcv. OKX entrega de a 300; la EMA200 necesita ~1000."""
        limit = limit or self.config.limite_velas
        try:
            tf_ms = int(self.exchange.parse_timeframe(self.config.timeframe) * 1000)
            since = int(time.time() * 1000) - limit * tf_ms
            rows = []
            seen = set()
            while len(rows) < limit:
                batch = min(300, limit - len(rows))
                chunk = self.exchange.fetch_ohlcv(
                    symbol, self.config.timeframe, since=since, limit=batch,
                )
                if not chunk:
                    break
                nuevos = 0
                for c in chunk:
                    if c[0] in seen:
                        continue
                    seen.add(c[0])
                    rows.append(c)
                    nuevos += 1
                if nuevos == 0:
                    break
                since = chunk[-1][0] + tf_ms
                if len(chunk) < batch:
                    break
            rows.sort(key=lambda r: r[0])
            if len(rows) > limit:
                rows = rows[-limit:]
            df = pd.DataFrame(
                rows, columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
            return df
        except ccxt.BadSymbol:
            self.logger.error("Símbolo inválido: %s", symbol)
            return None
        except ccxt.NetworkError:
            self.logger.error("Error de red al obtener velas de %s", symbol)
            return None
        except Exception as e:
            self.logger.error("Error obteniendo velas de %s: %s", symbol, e)
            return None

    def obtener_posicion_abierta(self, symbol: str) -> Optional[Dict]:
        try:
            positions = self.exchange.fetch_positions([symbol])
            for p in positions:
                if float(p.get("contracts") or 0) > 0:
                    return p
            return None
        except ccxt.NetworkError:
            self.logger.error("Error de red consultando posiciones de %s", symbol)
            return None
        except Exception as e:
            self.logger.error("Error consultando posición en %s: %s", symbol, e)
            return None

    def obtener_ticker(self, symbol: str) -> Optional[Dict]:
        try:
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            self.logger.error("Error obteniendo ticker de %s: %s", symbol, e)
            return None

    def equity_libre_usdt(self) -> Optional[float]:
        try:
            bal = self.exchange.fetch_balance()
            usdt = bal.get("USDT") or {}
            free = usdt.get("free")
            if free is None:
                free = usdt.get("total")
            return float(free or 0)
        except Exception as e:
            self.logger.error("No pude leer balance USDT: %s", e)
            return None

    def crear_orden_mercado(
        self, symbol: str, side: str, amount: float, reduce_only: bool = False,
    ) -> Optional[Dict]:
        try:
            params = {"tdMode": "isolated"}
            if reduce_only:
                params["reduceOnly"] = True
            self.logger.info(
                "Orden %s %s %s reduceOnly=%s",
                side.upper(), amount, symbol, reduce_only,
            )
            return self.exchange.create_market_order(symbol, side, amount, None, params)
        except ccxt.InsufficientBalance:
            self.logger.error("Balance insuficiente en %s", symbol)
            notifier.enviar(f"Balance insuficiente en {symbol}", "ERROR")
            return None
        except Exception as e:
            self.logger.error("Error creando orden en %s: %s", symbol, e)
            return None

    def filled_real(self, order: Optional[Dict], symbol: str) -> float:
        if order:
            filled = float(order.get("filled") or 0)
            if filled > 0:
                return filled
        time.sleep(0.8)
        pos = self.obtener_posicion_abierta(symbol)
        if pos:
            return float(pos.get("contracts") or 0)
        return 0.0

    def crear_tp_sl(
        self, symbol: str, side: str, amount: float, sl_precio: float, tp_precio: float,
    ) -> Tuple[bool, bool]:
        """Órdenes condicionales nativas de OKX (order-algo). slOrdPx='-1' = market."""
        side_opuesto = "sell" if side == "buy" else "buy"
        market = self.exchange.market(symbol)
        inst_id = market["id"]
        base = {
            "instId": inst_id,
            "tdMode": "isolated",
            "side": side_opuesto,
            "posSide": "net",
            "ordType": "conditional",
            "sz": str(amount),
            "tradeSide": "close",
        }

        def _ok(resp: dict, etiqueta: str, px: float) -> bool:
            if resp.get("code") == "0":
                data = resp.get("data") or [{}]
                algo_id = data[0].get("algoId", "?") if data else "?"
                self.logger.info("%s creado @ %.4f (algoId=%s)", etiqueta, px, algo_id)
                return True
            smsg = ""
            if resp.get("data"):
                smsg = resp["data"][0].get("sMsg", "")
            self.logger.error(
                "%s rechazado: code=%s msg=%s sMsg=%s",
                etiqueta, resp.get("code"), resp.get("msg"), smsg,
            )
            return False

        sl_ok = False
        try:
            resp = self.exchange.private_post_trade_order_algo({
                **base, "slTriggerPx": str(sl_precio), "slOrdPx": "-1",
            })
            sl_ok = _ok(resp, "SL", sl_precio)
        except Exception as e:
            self.logger.error("Error creando SL: %s", e)

        tp_ok = False
        try:
            resp = self.exchange.private_post_trade_order_algo({
                **base, "tpTriggerPx": str(tp_precio), "tpOrdPx": "-1",
            })
            tp_ok = _ok(resp, "TP", tp_precio)
        except Exception as e:
            self.logger.error("Error creando TP: %s", e)

        return sl_ok, tp_ok

    def precio_preciso(self, symbol: str, px: float) -> float:
        return float(self.exchange.price_to_precision(symbol, px))

    def cerrar_todo(self, symbol: str, side_cierre: str, amount: float) -> bool:
        """Cierra la posición neta. Orden: endpoint nativo → reduceOnly → market + verificar plano.

        OKX a veces responde 51205 si reduceOnly va por /trade/order. El close-position
        nativo no pide tamaño, así que no puede invertir la posición.
        """
        market = self.exchange.market(symbol)

        try:
            resp = self.exchange.private_post_trade_close_position({
                "instId": market["id"],
                "mgnMode": "isolated",
                "autoCxl": "true",
            })
            if str(resp.get("code")) == "0":
                time.sleep(0.7)
                if self.obtener_posicion_abierta(symbol) is None:
                    self.logger.info("%s: cerrado por close-position", symbol)
                    return True
        except Exception as e:
            self.logger.debug("%s: close-position no disponible: %s", symbol, e)

        if amount > 0:
            self.crear_orden_mercado(symbol, side_cierre, amount, reduce_only=True)
            time.sleep(0.7)
            if self.obtener_posicion_abierta(symbol) is None:
                return True

        pos = self.obtener_posicion_abierta(symbol)
        if not pos:
            return True
        amt = float(pos.get("contracts") or 0)
        side = (pos.get("side") or "").lower()
        cierre = "sell" if side == "long" else "buy"
        self.logger.warning("%s: cierre nativo falló — market de %.6f %s y verifico plano", symbol, amt, cierre)
        self.crear_orden_mercado(symbol, cierre, amt, reduce_only=False)
        time.sleep(0.8)
        resto = self.obtener_posicion_abierta(symbol)
        if not resto:
            return True
        # Si el market sin reduceOnly invirtió, cerrar el lado nuevo.
        amt2 = float(resto.get("contracts") or 0)
        side2 = (resto.get("side") or "").lower()
        cierre2 = "sell" if side2 == "long" else "buy"
        self.logger.error("%s: quedó resto %s %.6f — intento reduceOnly", symbol, side2, amt2)
        self.crear_orden_mercado(symbol, cierre2, amt2, reduce_only=True)
        time.sleep(0.8)
        return self.obtener_posicion_abierta(symbol) is None

    def cancelar_algos(self, symbol: str):
        try:
            market = self.exchange.market(symbol)
            pending = self.exchange.private_get_trade_orders_algo_pending({
                "instType": "SWAP",
                "instId": market["id"],
                "ordType": "conditional",
            })
            ids = [d.get("algoId") for d in (pending.get("data") or []) if d.get("algoId")]
            if not ids:
                return
            self.exchange.private_post_trade_cancel_algos({
                "instId": market["id"],
                "algoIds": ids,
            })
            self.logger.info("%s: cancelados %s algo-orders", symbol, len(ids))
        except Exception as e:
            self.logger.debug("No se cancelaron algos de %s: %s", symbol, e)


# ============================================================================
# INDICADORES
# ============================================================================

class TechnicalAnalyzer:
    def __init__(self, cfg: BotConfig):
        self.config = cfg
        self.logger = logging.getLogger("Technical")

    def calcular_indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        high, low, close = df["high"], df["low"], df["close"]
        hl2 = (high + low) / 2

        df["up_move"] = high - high.shift(1)
        df["down_move"] = low.shift(1) - low
        df["plus_dm"] = np.where(
            (df["up_move"] > df["down_move"]) & (df["up_move"] > 0),
            df["up_move"], 0.0,
        )
        df["minus_dm"] = np.where(
            (df["down_move"] > df["up_move"]) & (df["down_move"] > 0),
            df["down_move"], 0.0,
        )

        df["tr0"] = abs(high - low)
        df["tr1"] = abs(high - close.shift(1))
        df["tr2"] = abs(low - close.shift(1))
        df["tr"] = pd.concat([df["tr0"], df["tr1"], df["tr2"]], axis=1).max(axis=1)

        alpha_st = 1 / self.config.st_periodo
        alpha_adx = 1 / self.config.adx_periodo

        df["tr_st"] = df["tr"].ewm(alpha=alpha_st, adjust=False).mean()
        df["tr_adx"] = df["tr"].ewm(alpha=alpha_adx, adjust=False).mean()
        df["plus_dm_s"] = df["plus_dm"].ewm(alpha=alpha_adx, adjust=False).mean()
        df["minus_dm_s"] = df["minus_dm"].ewm(alpha=alpha_adx, adjust=False).mean()

        df["plus_di"] = 100 * (df["plus_dm_s"] / df["tr_adx"])
        df["minus_di"] = 100 * (df["minus_dm_s"] / df["tr_adx"])
        di_sum = df["plus_di"] + df["minus_di"]
        df["dx"] = 100 * abs(df["plus_di"] - df["minus_di"]) / di_sum.replace(0, 1)
        df["adx"] = df["dx"].ewm(alpha=alpha_adx, adjust=False).mean()

        df["upper_basic"] = hl2 + (self.config.st_multiplier * df["tr_st"])
        df["lower_basic"] = hl2 - (self.config.st_multiplier * df["tr_st"])

        upper_basic = df["upper_basic"].values
        lower_basic = df["lower_basic"].values
        close_vals = close.values
        n = len(df)
        upper_band = np.zeros(n)
        lower_band = np.zeros(n)
        st = np.ones(n, dtype=bool)
        p = self.config.st_periodo

        for i in range(p, n):
            if upper_basic[i] < upper_band[i - 1] or close_vals[i - 1] > upper_band[i - 1]:
                upper_band[i] = upper_basic[i]
            else:
                upper_band[i] = upper_band[i - 1]
            if lower_basic[i] > lower_band[i - 1] or close_vals[i - 1] < lower_band[i - 1]:
                lower_band[i] = lower_basic[i]
            else:
                lower_band[i] = lower_band[i - 1]
            if i == p:
                st[i] = True
            elif st[i - 1]:
                st[i] = False if close_vals[i] <= lower_band[i] else True
            else:
                st[i] = True if close_vals[i] >= upper_band[i] else False

        df["upper_band"] = upper_band
        df["lower_band"] = lower_band
        df["st_direction"] = st
        df["ema200"] = close.ewm(span=self.config.periodo_ema, adjust=False).mean()
        return df

    def extraer_valores(self, df: pd.DataFrame) -> Optional[IndicatorValues]:
        try:
            actual = df.iloc[-2]
            anterior = df.iloc[-3]
            return IndicatorValues(
                precio_actual=float(actual["close"]),
                ema200=float(actual["ema200"]),
                adx=float(actual["adx"]),
                st_direction=bool(actual["st_direction"]),
                st_direction_anterior=bool(anterior["st_direction"]),
                upper_band=float(actual["upper_band"]),
                lower_band=float(actual["lower_band"]),
                vela_ts=int(actual["timestamp"]),
            )
        except Exception as e:
            self.logger.error("Error extrayendo valores: %s", e)
            return None

    def detectar_senal(self, valores: IndicatorValues) -> TrendDirection:
        try:
            if (
                not valores.st_direction_anterior
                and valores.st_direction
                and valores.precio_actual > valores.ema200
                and valores.adx > self.config.adx_threshold
            ):
                return TrendDirection.UPTREND
            if (
                valores.st_direction_anterior
                and not valores.st_direction
                and valores.precio_actual < valores.ema200
                and valores.adx > self.config.adx_threshold
            ):
                return TrendDirection.DOWNTREND
            return TrendDirection.NEUTRAL
        except Exception as e:
            self.logger.error("Error detectando señal: %s", e)
            return TrendDirection.NEUTRAL


# ============================================================================
# EJECUTOR
# ============================================================================

class TradeExecutor:
    def __init__(self, exchange_manager: ExchangeManager, cfg: BotConfig, persistencia: Persistencia):
        self.exchange = exchange_manager
        self.config = cfg
        self.persistencia = persistencia
        self.logger = logging.getLogger("Executor")
        self.trade_abierto_id: Dict[str, int] = {}

    def _dimensionar(
        self, symbol: str, precio: float, direction: TrendDirection, valores: IndicatorValues,
    ) -> Optional[Tuple[float, float, float, str]]:
        """Devuelve (amount, sl, tp, side) o None si el trade no es operable."""
        if direction == TrendDirection.UPTREND:
            sl = float(valores.lower_band)
            if sl >= precio:
                self.logger.warning("%s: banda inferior (%.4f) no sirve de SL long", symbol, sl)
                return None
            dist = (precio - sl) / precio
            side = "buy"
        else:
            sl = float(valores.upper_band)
            if sl <= precio:
                self.logger.warning("%s: banda superior (%.4f) no sirve de SL short", symbol, sl)
                return None
            dist = (sl - precio) / precio
            side = "sell"

        liq = 1.0 / max(self.config.leverage, 1)
        if dist >= liq * 0.8:
            self.logger.warning(
                "%s: SL a %.2f%% cae dentro de liquidación ~%.2f%% — no opero",
                symbol, dist * 100, liq * 100,
            )
            return None
        if dist < 0.002:
            self.logger.warning("%s: SL demasiado justo (%.2f%%) — no opero", symbol, dist * 100)
            return None

        equity = self.exchange.equity_libre_usdt()
        if equity is None:
            return None
        if equity <= 0:
            self.logger.error("Equity USDT libre = 0")
            return None

        riesgo = min(equity * self.config.fraccion_equity, self.config.capital_riesgo_usdt)
        market = self.exchange.exchange.market(symbol)
        contract_size = float(market.get("contractSize") or 1.0)
        raw = riesgo / (dist * precio * contract_size)
        amount = float(self.exchange.exchange.amount_to_precision(symbol, raw))
        if amount <= 0:
            self.logger.warning("%s: amount=0 después de precisión", symbol)
            return None

        if direction == TrendDirection.UPTREND:
            tp = precio + 2 * (precio - sl)
        else:
            tp = precio - 2 * (sl - precio)

        sl = self.exchange.precio_preciso(symbol, sl)
        tp = self.exchange.precio_preciso(symbol, tp)

        self.logger.info(
            "%s: riesgo %.2f USDT (equity %.2f) · dist SL %.2f%% · %s contratos · SL %.4f TP %.4f",
            symbol, riesgo, equity, dist * 100, amount, sl, tp,
        )
        return amount, sl, tp, side

    def _cerrar_desprotegida(self, symbol: str, side_entrada: str) -> bool:
        side_cierre = "sell" if side_entrada == "buy" else "buy"
        for intento in range(3):
            pos = self.exchange.obtener_posicion_abierta(symbol)
            if not pos:
                return True
            amt = float(pos.get("contracts") or 0)
            if amt <= 0:
                return True
            self.logger.error(
                "Rollback %s intento %s: cierro %s %s", symbol, intento + 1, side_cierre, amt,
            )
            if self.exchange.cerrar_todo(symbol, side_cierre, amt):
                return True
            time.sleep(1.2)
        return self.exchange.obtener_posicion_abierta(symbol) is None

    def abrir_posicion(self, signal: TradeSignal, valores: IndicatorValues) -> bool:
        symbol = signal.symbol
        direction = signal.direction
        try:
            ticker = self.exchange.obtener_ticker(symbol)
            if not ticker:
                return False
            precio = float(ticker["last"])

            dim = self._dimensionar(symbol, precio, direction, valores)
            if not dim:
                return False
            amount, sl_precio, tp_precio, side = dim

            razon = f"ST flip {direction.value} ADX {valores.adx:.1f} vela {signal.vela_ts}"

            if self.config.modo_simulacion:
                tid = self.persistencia.abrir(
                    symbol, direction.value, precio, sl_precio, tp_precio,
                    amount, razon, virtual=True,
                )
                if tid:
                    self.trade_abierto_id[symbol] = tid
                msg = (
                    f"SIMULADA {symbol}\n"
                    f"Dirección: {direction.value.upper()}\n"
                    f"Tamaño: {amount}\n"
                    f"Entrada: {precio:.4f}\n"
                    f"SL: {sl_precio:.4f}\n"
                    f"TP: {tp_precio:.4f}\n"
                    f"ADX: {valores.adx:.2f}"
                )
                self.logger.info(msg)
                notifier.enviar(msg, "TRADE")
                return True

            orden = self.exchange.crear_orden_mercado(symbol, side, amount, reduce_only=False)
            if not orden:
                return False

            filled = self.exchange.filled_real(orden, symbol)
            if filled <= 0:
                self.logger.error("%s: entrada sin fill — no creo SL/TP", symbol)
                notifier.enviar(f"Entrada sin fill en {symbol}", "ERROR")
                return False

            fill_px = float(orden.get("average") or orden.get("price") or precio)
            if fill_px <= 0:
                fill_px = precio
            if side == "buy":
                dist_real = (fill_px - sl_precio) / fill_px if fill_px else 0
            else:
                dist_real = (sl_precio - fill_px) / fill_px if fill_px else 0
            liq = 1.0 / max(self.config.leverage, 1)
            if dist_real <= 0 or dist_real >= liq * 0.8:
                self.logger.error(
                    "%s: fill %.4f deja SL a %.2f%% (liq ~%.2f%%) — rollback",
                    symbol, fill_px, dist_real * 100, liq * 100,
                )
                self._cerrar_desprotegida(symbol, side)
                notifier.enviar(
                    f"Rollback {symbol}: el fill dejó el SL dentro de la zona de liquidación",
                    "ERROR",
                )
                return False

            sl_ok, tp_ok = self.exchange.crear_tp_sl(
                symbol, side, filled, sl_precio, tp_precio,
            )

            if sl_ok:
                tid = self.persistencia.abrir(
                    symbol, direction.value, precio, sl_precio, tp_precio,
                    filled, razon, virtual=False,
                )
                if tid:
                    self.trade_abierto_id[symbol] = tid
                tp_status = f"TP: {tp_precio:.4f}" if tp_ok else "TP no creado"
                msg = (
                    f"NUEVA OPERACIÓN ({symbol})\n"
                    f"Dirección: {direction.value.upper()}\n"
                    f"Tamaño: {filled} contratos\n"
                    f"Entrada: {precio:.4f}\n"
                    f"SL: {sl_precio:.4f}\n"
                    f"{tp_status}\n"
                    f"ADX: {valores.adx:.2f}"
                )
                self.logger.info(msg)
                notifier.enviar(msg, "TRADE")
                return True

            self.logger.error(
                "SL NO creado en %s — cerrando posición para no dejarla desprotegida", symbol,
            )
            cierre_ok = self._cerrar_desprotegida(symbol, side)
            if cierre_ok:
                msg = (
                    f"OPERACIÓN CANCELADA ({symbol})\n"
                    f"Se abrió {direction.value.upper()} @ {precio:.4f} "
                    f"pero el SL no se pudo crear. El bot cerró la posición."
                )
            else:
                msg = (
                    f"EMERGENCIA ({symbol})\n"
                    f"Se abrió {direction.value.upper()} @ {precio:.4f}.\n"
                    f"NO se pudo crear el SL ni cerrar la posición.\n"
                    f"CERRÁ MANUALMENTE EN OKX AHORA."
                )
                self.logger.critical("%s: posición desprotegida, intervención humana", symbol)
            notifier.enviar(msg, "ERROR")
            return False

        except Exception as e:
            self.logger.error("Error abriendo posición en %s: %s", symbol, e)
            self.logger.debug(traceback.format_exc())
            notifier.enviar(f"Error abriendo posición en {symbol}: {e}", "ERROR")
            return False

    def cerrar_posicion(self, symbol: str, posicion: Dict, razon: str) -> bool:
        try:
            side = posicion.get("side") or ""
            amount = float(posicion.get("contracts") or 0)
            if amount <= 0:
                return True
            order_side = "sell" if side == "long" else "buy"

            if self.config.modo_simulacion:
                self.logger.info("SIMULADA cierre %s %s (%s)", symbol, side, razon)
                tid = self.trade_abierto_id.pop(symbol, None)
                last = float(posicion.get("markPrice") or posicion.get("entryPrice") or 0)
                self.persistencia.cerrar(tid, last, razon, None)
                notifier.enviar(
                    f"SIMULADA CIERRE ({symbol})\nTipo: {side.upper()}\nRazón: {razon}",
                    "INFO",
                )
                return True

            self.exchange.cancelar_algos(symbol)
            self.logger.info("Cerrando %s %s de %s en %s (%s)", side, amount, symbol, order_side, razon)

            pos = self.exchange.obtener_posicion_abierta(symbol)
            if not pos:
                return True
            amount = float(pos.get("contracts") or 0)
            plano = self.exchange.cerrar_todo(symbol, order_side, amount)
            if not plano:
                notifier.enviar(
                    f"EMERGENCIA ({symbol}): no pude dejar la posición en cero. Revisá OKX.",
                    "ERROR",
                )
                return False

            tid = self.trade_abierto_id.pop(symbol, None)
            exit_px = float(pos.get("markPrice") or pos.get("entryPrice") or 0)
            self.persistencia.cerrar(tid, exit_px, razon, None)

            msg = (
                f"POSICIÓN CERRADA ({symbol})\n"
                f"Tipo: {side.upper()}\n"
                f"Tamaño: {amount} contratos\n"
                f"Razón: {razon}"
            )
            self.logger.info(msg)
            notifier.enviar(msg, "INFO")
            return True
        except Exception as e:
            self.logger.error("Error cerrando posición en %s: %s", symbol, e)
            self.logger.debug(traceback.format_exc())
            return False


# ============================================================================
# BOT
# ============================================================================

class TradingBot:
    def __init__(self, cfg: BotConfig):
        self.config = cfg
        self.logger = logging.getLogger("TradingBot")
        self.bot_logger = logging.getLogger("Bot")
        self.persistencia = Persistencia("trades.db")
        self.exchange = ExchangeManager(cfg)
        self.analyzer = TechnicalAnalyzer(cfg)
        self.executor = TradeExecutor(self.exchange, cfg, self.persistencia)
        self.activo = True
        self.ciclo_contador = 0
        self.ultimo_reporte_ts = None
        self.estado_simbolos: Dict[str, dict] = {}
        self.ultima_senal_vela: Dict[str, int] = {}
        self.simbolos_ok: list = list(cfg.simbolos)
        self.lock = threading.Lock()

    def inicializar(self) -> bool:
        if not all([self.config.api_key, self.config.api_secret, self.config.api_password]):
            self.logger.error(
                "Credenciales OKX no configuradas "
                "(OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD)"
            )
            notifier.enviar("Credenciales OKX no configuradas", "ERROR")
            return False
        try:
            self.logger.info("=" * 80)
            self.logger.info("BOT OKX TESTNET — OPCIÓN A")
            self.logger.info(
                "Símbolos: %s | tf=%s | lev=%sx | riesgo máx=%.1f USDT | simulación=%s",
                ", ".join(self.config.simbolos),
                self.config.timeframe,
                self.config.leverage,
                self.config.capital_riesgo_usdt,
                self.config.modo_simulacion,
            )
            ok = []
            for symbol in self.config.simbolos:
                if self.exchange.configurar_mercado(symbol):
                    ok.append(symbol)
                else:
                    self.logger.error("EXCLUYO %s: no pude fijar margen aislado o leverage", symbol)
            self.simbolos_ok = ok
            if not self.simbolos_ok:
                notifier.enviar("Ningún símbolo pudo configurarse — no arranco", "ERROR")
                return False
            self.logger.info("Símbolos activos: %s", ", ".join(self.simbolos_ok))
            self.logger.info("Listo para operar")
            notifier.enviar(
                "Bot OKX opción A iniciado"
                + (" (SIMULACIÓN)" if self.config.modo_simulacion else " (testnet)"),
                "SUCCESS",
            )
            return True
        except ccxt.AuthenticationError as e:
            self.logger.error("Autenticación OKX: %s", e)
            notifier.enviar("Error de autenticación OKX", "ERROR")
            return False
        except Exception as e:
            self.logger.error("Error inicializando: %s", e)
            self.logger.debug(traceback.format_exc())
            notifier.enviar(f"Error inicializando bot: {e}", "ERROR")
            return False

    def _razon_no_operar(self, valores: IndicatorValues, posicion_abierta: bool) -> str:
        if posicion_abierta:
            return "pos. abierta"
        if valores.adx < self.config.adx_threshold:
            return f"ADX débil ({valores.adx:.0f})"
        if valores.st_direction == valores.st_direction_anterior:
            return "ST sin cambio"
        if valores.st_direction and valores.precio_actual <= valores.ema200:
            return "precio < EMA200"
        if not valores.st_direction and valores.precio_actual >= valores.ema200:
            return "precio > EMA200"
        return "sin señal"

    def analizar_symbol(self, symbol: str) -> bool:
        try:
            df_raw = self.exchange.obtener_velas(symbol)
            if df_raw is None:
                self.logger.error("%s: no se pudieron obtener velas", symbol)
                return False
            if len(df_raw) < self.config.velas_minimas:
                self.logger.warning(
                    "%s: datos insuficientes (%s/%s)",
                    symbol, len(df_raw), self.config.velas_minimas,
                )
                return False

            df = self.analyzer.calcular_indicadores(df_raw)
            valores = self.analyzer.extraer_valores(df)
            if not valores:
                return False

            fila_live = df.iloc[-1]
            precio_live = float(fila_live["close"])
            ema200_live = float(fila_live["ema200"])
            adx_live = float(fila_live["adx"])
            st_live = bool(fila_live["st_direction"])
            tendencia = "ALCISTA" if st_live else "BAJISTA"
            dist_ema = ((precio_live - ema200_live) / ema200_live) * 100
            self.bot_logger.info(
                "%-15s | $%8.2f | EMA200 $%8.2f (%+6.2f%%) | ADX %5.2f | ST %s",
                symbol, precio_live, ema200_live, dist_ema, adx_live, tendencia,
            )

            posicion = self.exchange.obtener_posicion_abierta(symbol)
            tiene_posicion = posicion is not None
            razon = self._razon_no_operar(valores, tiene_posicion)
            self.estado_simbolos[symbol] = {
                "precio": precio_live,
                "st": st_live,
                "adx": valores.adx,
                "posicion": tiene_posicion,
                "side_posicion": posicion["side"] if tiene_posicion else None,
                "razon": razon,
            }

            if posicion:
                self.bot_logger.info(
                    "%s: POSICIÓN ABIERTA (%s) %s contratos",
                    symbol, str(posicion.get("side", "")).upper(),
                    posicion.get("contracts"),
                )
                self._manejar_posicion_abierta(symbol, posicion, valores)
                return True

            senal = self.analyzer.detectar_senal(valores)
            if senal == TrendDirection.NEUTRAL:
                return True

            if self.ultima_senal_vela.get(symbol) == valores.vela_ts:
                self.bot_logger.info(
                    "%s: señal %s ya procesada en esta vela",
                    symbol, senal.value.upper(),
                )
                return True

            signal = TradeSignal(
                symbol=symbol,
                direction=senal,
                precio=valores.precio_actual,
                adx=valores.adx,
                timestamp=datetime.now(),
                vela_ts=valores.vela_ts,
            )
            self.logger.warning("SEÑAL: %s", signal)
            self.ultima_senal_vela[symbol] = valores.vela_ts
            self.executor.abrir_posicion(signal, valores)
            return True
        except Exception as e:
            self.logger.error("Error analizando %s: %s", symbol, e)
            self.logger.debug(traceback.format_exc())
            return False

    def _manejar_posicion_abierta(self, symbol: str, posicion: Dict, valores: IndicatorValues):
        side = posicion.get("side")
        debe_cerrar = False
        razon = ""
        if side == "long" and not valores.st_direction:
            debe_cerrar = True
            razon = "SuperTrend cambió a bajista"
        elif side == "short" and valores.st_direction:
            debe_cerrar = True
            razon = "SuperTrend cambió a alcista"
        if debe_cerrar:
            self.bot_logger.warning("SALIDA %s: %s", symbol, razon)
            self.executor.cerrar_posicion(symbol, posicion, razon)

    def _debe_enviar_reporte(self) -> bool:
        ahora = datetime.now(GMT_MINUS_3)
        if (
            self.ultimo_reporte_ts is not None
            and self.ultimo_reporte_ts.hour == ahora.hour
            and self.ultimo_reporte_ts.date() == ahora.date()
        ):
            return False
        if ahora.hour in REPORTES_HORAS:
            self.ultimo_reporte_ts = ahora
            return True
        return False

    def _generar_reporte_telegram(self) -> str:
        ahora = datetime.now(GMT_MINUS_3)
        lineas = [f"{ahora.strftime('%H:%M')} AR | C#{self.ciclo_contador:04d}", ""]
        razones = []
        hay_pos = []
        for symbol in self.simbolos_ok:
            estado = self.estado_simbolos.get(symbol) or {}
            if not estado:
                continue
            corto = symbol.split("/")[0]
            precio = estado.get("precio") or 0
            st = estado.get("st", False)
            adx = estado.get("adx") or 0
            flecha = "alcista" if st else "bajista"
            if estado.get("posicion"):
                side = (estado.get("side_posicion") or "").upper()
                lineas.append(f"- {corto} {precio:.0f} {flecha} ADX{adx:.0f} POS {side}")
                hay_pos.append(f"{corto} {side}")
            else:
                lineas.append(f"- {corto} {precio:.0f} {flecha} ADX{adx:.0f}")
                razon = estado.get("razon")
                if razon:
                    razones.append(f"{corto}: {razon}")
        if razones:
            lineas.append("")
            prefijo = "No opere" if not hay_pos else "Sin senales nuevas"
            lineas.append(f"{prefijo}: {'; '.join(razones)}")
        return "\n".join(lineas)

    def ciclo_analisis(self):
        self.ciclo_contador += 1
        self.logger.info(
            "[CICLO #%04d] %s | %s símbolos",
            self.ciclo_contador,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            len(self.simbolos_ok),
        )
        exitos = 0
        fallos = []
        for symbol in self.simbolos_ok:
            try:
                if self.analizar_symbol(symbol):
                    exitos += 1
                else:
                    fallos.append(symbol)
            except Exception as e:
                self.logger.error("Excepción en %s: %s", symbol, e)
                fallos.append(symbol)
        if fallos:
            self.logger.warning(
                "Ciclo #%04d %s/%s · fallos: %s",
                self.ciclo_contador, exitos, len(self.simbolos_ok), ", ".join(fallos),
            )
        else:
            self.logger.info(
                "Ciclo #%04d completo · próximo en %ss",
                self.ciclo_contador, self.config.ciclo_segundos,
            )
        if self._debe_enviar_reporte():
            try:
                notifier.enviar(self._generar_reporte_telegram(), "INFO")
            except Exception as e:
                self.logger.warning("Error enviando reporte: %s", e)

    def ejecutar(self):
        if not self.inicializar():
            self.logger.error("Falló la inicialización, no arranco el ciclo")
            return
        try:
            while self.activo:
                try:
                    self.ciclo_analisis()
                except Exception as e:
                    self.logger.error("Error en ciclo: %s", e)
                    self.logger.debug(traceback.format_exc())
                    notifier.enviar(f"Error en ciclo del bot: {e}", "ERROR")
                time.sleep(self.config.ciclo_segundos)
        except KeyboardInterrupt:
            self.logger.info("Bot detenido por usuario")
            notifier.enviar("Bot detenido manualmente", "INFO")
        except Exception as e:
            self.logger.critical("Error crítico: %s", e)
            notifier.enviar(f"Error crítico en bot: {e}", "ERROR")
        finally:
            self.activo = False


# ============================================================================
# FLASK
# ============================================================================

app = Flask(__name__)
bot_lock = threading.Lock()
bot: Optional[TradingBot] = None


@app.route("/")
def home():
    with bot_lock:
        b = bot
    if b is None:
        return (
            "Bot OKX opción A — inicializando. "
            "Reintentá en 10s. Logs: /logs · estado: /status"
        ), 202
    estado = "ACTIVO" if b.activo else "INACTIVO"
    sim = " · SIMULACIÓN" if b.config.modo_simulacion else ""
    return (
        f"Bot OKX Testnet opción A — {estado}{sim}<br>"
        f"Ciclos: {b.ciclo_contador}<br>"
        f"Símbolos: {', '.join(b.simbolos_ok)}<br>"
        f"<a href='/logs'>logs</a> · <a href='/status'>status</a>"
    )


@app.route("/status")
def status():
    with bot_lock:
        b = bot
    if b is None:
        return {
            "estado": "inicializando",
            "ciclos_ejecutados": 0,
            "simbolos": config.simbolos,
            "timestamp": datetime.now().isoformat(),
        }, 202
    return {
        "estado": "activo" if b.activo else "inactivo",
        "ciclos_ejecutados": b.ciclo_contador,
        "simbolos": b.simbolos_ok,
        "leverage": b.config.leverage,
        "simulacion": b.config.modo_simulacion,
        "timestamp": datetime.now().isoformat(),
    }, 200


@app.route("/logs")
def logs():
    try:
        with open("bot_trading.log", "r", encoding="utf-8", errors="replace") as f:
            cuerpo = html.escape("".join(f.readlines()[-250:]))
        return (
            "<pre style='font:12px/1.45 ui-monospace,monospace;"
            f"white-space:pre-wrap'>{cuerpo}</pre>"
        )
    except FileNotFoundError:
        return "sin logs todavía", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Flask en puerto %s", port)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    time.sleep(2)
    try:
        instancia = TradingBot(config)
        with bot_lock:
            bot = instancia
        instancia.ejecutar()
    except Exception as e:
        logger.critical("Error fatal: %s", e)
        logger.debug(traceback.format_exc())
        notifier.enviar(f"Error fatal: {e}", "ERROR")
        while True:
            time.sleep(60)
