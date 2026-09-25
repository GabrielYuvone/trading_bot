#!/usr/bin/env python3
"""
BOT DE TRADING OKX v2 - COMPLETO CON 4 MEJORAS
================================================

Características:
1. SuperTrend + EMA200 + ADX como estrategia
2. Cálculo de PnL REAL (con comisiones)
3. SQLite para persistencia
4. Telegram para alertas
5. Flask para monitoreo remoto
6. Modo simulación para testing
7. Manejo robusto de errores
8. Verificación de TP creado

Requisitos:
    pip install ccxt pandas numpy requests python-telegram-bot flask

Configuración:
    .env debe contener:
        OKX_API_KEY=...
        OKX_API_SECRET=...
        OKX_API_PASSWORD=...
        TELEGRAM_TOKEN=...
        TELEGRAM_CHAT_ID=...
        MODO_SIMULACION=1  # 0=real, 1=simulación
        OKX_LEVERAGE=10

Uso:
    python bot_completo.py
"""

import os
import ccxt
import time
import sqlite3
import json
import threading
import logging
import traceback
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List
from enum import Enum
from pathlib import Path

import pandas as pd
import numpy as np
import requests
from flask import Flask, jsonify
from dotenv import load_dotenv

# ============================================================================
# CONFIGURACIÓN INICIAL
# ============================================================================

load_dotenv()

# Variables de entorno
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MODO_SIMULACION = int(os.getenv("MODO_SIMULACION", "1"))
OKX_LEVERAGE = int(os.getenv("OKX_LEVERAGE", "10"))
DB_PATH = "trades.db"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot_trading.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# ENUMS Y DATACLASSES
# ============================================================================

class Direction(Enum):
    """Dirección del trade"""
    LONG = "long"
    SHORT = "short"

@dataclass
class Config:
    """Configuración del bot"""
    modo_simulacion: bool
    leverage: int
    capital_riesgo_usdt: float
    capital_inicial_usdt: float
    porcentaje_sl: float  # 0.008 = 0.8%
    porcentaje_tp: float  # 0.012 = 1.2%
    fraccion_equity: float  # Fracción de equity a arriesgar
    simbolos: List[str]
    
    @classmethod
    def from_env(cls):
        return cls(
            modo_simulacion=(MODO_SIMULACION == 1),
            leverage=OKX_LEVERAGE,
            capital_riesgo_usdt=float(os.getenv("CAPITAL_RIESGO_USDT", "5.0")),
            capital_inicial_usdt=float(os.getenv("CAPITAL_INICIAL_USDT", "1000.0")),
            porcentaje_sl=float(os.getenv("PORCENTAJE_SL", "0.008")),
            porcentaje_tp=float(os.getenv("PORCENTAJE_TP", "0.012")),
            fraccion_equity=float(os.getenv("FRACCION_EQUITY", "0.0075")),
            simbolos=[s.strip() for s in os.getenv("SIMBOLOS", "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT").split(",")],
        )

# ============================================================================
# NOTIFICADOR (TELEGRAM)
# ============================================================================

class TelegramNotifier:
    """Envía notificaciones por Telegram"""
    
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
    
    def enviar(self, mensaje: str, tipo: str = "INFO"):
        """
        Envía mensaje a Telegram.
        
        tipo: INFO, TRADE, WARNING, ERROR
        """
        if not self.token or not self.chat_id:
            return False
        
        # Emoji por tipo
        emojis = {
            "INFO": "ℹ️",
            "TRADE": "📊",
            "WARNING": "⚠️",
            "ERROR": "🔴",
            "SUCCESS": "✅",
        }
        
        emoji = emojis.get(tipo, "")
        texto = f"{emoji} {mensaje}"
        
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": texto},
                timeout=5
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Error enviando Telegram: {e}")
            return False

# ============================================================================
# PERSISTENCIA (SQLite)
# ============================================================================

class Persistencia:
    """Gestiona almacenamiento de trades en SQLite"""
    
    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self._crear_tabla()
    
    def _conn(self):
        """Obtiene conexión a BD"""
        return sqlite3.connect(self.db_path)
    
    def _crear_tabla(self):
        """Crea tabla si no existe"""
        con = self._conn()
        cursor = con.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_ts TEXT NOT NULL,
                entry_px REAL NOT NULL,
                exit_ts TEXT,
                exit_px REAL,
                sl_px REAL NOT NULL,
                tp_px REAL NOT NULL,
                size REAL NOT NULL,
                razon_salida TEXT,
                pnl REAL,
                virtual INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.commit()
        con.close()
    
    def abrir(
        self,
        symbol: str,
        side: str,
        entry_px: float,
        sl_px: float,
        tp_px: float,
        size: float,
        razon: str,
        virtual: int = 0,
    ) -> Optional[int]:
        """Abre un nuevo trade"""
        try:
            con = self._conn()
            cursor = con.cursor()
            cursor.execute("""
                INSERT INTO trades (symbol, side, entry_ts, entry_px, sl_px, tp_px, size, razon_salida, virtual)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol,
                side,
                datetime.now(timezone.utc).isoformat(),
                entry_px,
                sl_px,
                tp_px,
                size,
                razon,
                virtual,
            ))
            con.commit()
            tid = cursor.lastrowid
            con.close()
            return tid
        except Exception as e:
            logger.error(f"Error abriendo trade en BD: {e}")
            return None
    
    def cerrar(
        self,
        trade_id: Optional[int],
        exit_px: float,
        razon: str,
        pnl: Optional[float] = None,
    ):
        """Cierra un trade y calcula PnL si es necesario"""
        if not trade_id:
            return
        
        try:
            con = self._conn()
            
            # Si no me dan PnL, lo calculo
            if pnl is None:
                cursor = con.cursor()
                cursor.execute(
                    "SELECT entry_px, side, size FROM trades WHERE id = ?",
                    (trade_id,)
                )
                fila = cursor.fetchone()
                if fila:
                    entry_px, side, size = fila
                    
                    # MEJORA 1: Calcular PnL bruto
                    if side == "long":
                        pnl_bruto = (exit_px - entry_px) * size
                    else:  # short
                        pnl_bruto = (entry_px - exit_px) * size
                    
                    # MEJORA 2: Restar comisiones (0.05% entrada + 0.05% salida = 0.1% total)
                    comisiones = (entry_px * size * 0.0005) + (exit_px * size * 0.0005)
                    pnl = pnl_bruto - comisiones
            
            # Registrar cierre
            con.execute(
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
            con.commit()
            con.close()
        except Exception as e:
            logger.error(f"Error cerrando trade en BD: {e}")
    
    def obtener_abierto(self, symbol: str) -> Optional[Dict]:
        """Obtiene trade abierto de un símbolo"""
        try:
            con = self._conn()
            cursor = con.cursor()
            cursor.execute(
                "SELECT id, side, entry_px FROM trades WHERE symbol = ? AND exit_px IS NULL ORDER BY entry_ts DESC LIMIT 1",
                (symbol,)
            )
            fila = cursor.fetchone()
            con.close()
            
            if fila:
                return {
                    "id": fila[0],
                    "side": fila[1],
                    "entry_px": fila[2],
                }
            return None
        except Exception as e:
            logger.error(f"Error obteniendo trade abierto: {e}")
            return None

# ============================================================================
# EXCHANGE MANAGER (CCXT)
# ============================================================================

class ExchangeManager:
    """Gestiona conexión con OKX"""
    
    def __init__(self, api_key: str, api_secret: str, api_password: str, modo_simulacion: bool = False):
        self.exchange = ccxt.okx({
            "apiKey": api_key,
            "secret": api_secret,
            "password": api_password,
            "enableRateLimit": True,
            "timeout": 10000,
        })
        
        if modo_simulacion:
            self.exchange.set_sandbox_mode(True)
            logger.info("Modo SANDBOX activado")
        
        self.logger = logger
    
    def obtener_posicion_abierta(self, symbol: str) -> Optional[Dict]:
        """Obtiene posición abierta de un símbolo"""
        try:
            posiciones = self.exchange.fetch_positions([symbol])
            pos = next(
                (p for p in posiciones if p["symbol"] == symbol and float(p.get("contracts", 0)) > 0),
                None
            )
            return pos
        except Exception as e:
            self.logger.error(f"Error obteniendo posición de {symbol}: {e}")
            return None
    
    def crear_orden_mercado(self, symbol: str, side: str, amount: float) -> Optional[Dict]:
        """Crea orden de mercado"""
        try:
            order = self.exchange.create_order(
                symbol,
                "market",
                side,
                amount,
                None,
                {"marginMode": "isolated"}
            )
            return order
        except ccxt.InsufficientBalance:
            self.logger.error(f"Fondos insuficientes para {symbol}")
            return None
        except Exception as e:
            self.logger.error(f"Error creando orden en {symbol}: {e}")
            return None
    
    def cerrar_posicion(self, symbol: str, side: str, amount: float) -> Optional[Dict]:
        """Cierra una posición"""
        try:
            order_side = "sell" if side == "long" else "buy"
            order = self.exchange.create_order(
                symbol,
                "market",
                order_side,
                amount,
                None,
                {"marginMode": "isolated", "reduceOnly": True}
            )
            return order
        except Exception as e:
            self.logger.error(f"Error cerrando posición en {symbol}: {e}")
            return None
    
    def crear_tp_sl(
        self,
        symbol: str,
        side: str,
        amount: float,
        sl_px: float,
        tp_px: float,
    ) -> Tuple[bool, bool]:
        """Crea Stop Loss y Take Profit en OKX"""
        try:
            inst_id = symbol.replace("/", "").replace(":", "-")
            
            # SL
            sl_result = self.exchange.private_post_trade_order_algo({
                "instId": inst_id,
                "slTriggerPx": str(sl_px),
                "slOrdPx": "-1",  # market
            })
            sl_ok = sl_result is not None
            
            # TP
            tp_result = self.exchange.private_post_trade_order_algo({
                "instId": inst_id,
                "tpTriggerPx": str(tp_px),
                "tpOrdPx": "-1",  # market
            })
            tp_ok = tp_result is not None
            
            return sl_ok, tp_ok
        except Exception as e:
            self.logger.error(f"Error creando SL/TP en {symbol}: {e}")
            return False, False
    
    def obtener_balance(self) -> float:
        """Obtiene balance en USDT"""
        try:
            balance = self.exchange.fetch_balance()
            return float(balance["free"].get("USDT", 0))
        except Exception as e:
            self.logger.error(f"Error obteniendo balance: {e}")
            return 0.0

# ============================================================================
# ANALIZADOR TÉCNICO
# ============================================================================

class TechnicalAnalyzer:
    """Calcula indicadores técnicos"""
    
    @staticmethod
    def calcular_indicadores(exchange: ccxt.Exchange, symbol: str, timeframe: str = "15m") -> Optional[Dict]:
        """Calcula SuperTrend, EMA200 y ADX"""
        try:
            # Obtener OHLCV
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
            if not ohlcv or len(ohlcv) < 50:
                return None
            
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            
            # Precio actual (último cierre)
            precio_actual = float(df["close"].iloc[-1])
            
            # EMA200
            ema200 = df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
            
            # SuperTrend (10, 3)
            st = TechnicalAnalyzer._supertrend(df["high"], df["low"], df["close"], period=10, multiplier=3)
            supertrend_actual = float(st.iloc[-1])
            
            # ADX (14)
            adx = TechnicalAnalyzer._adx(df["high"], df["low"], df["close"], period=14)
            adx_actual = float(adx.iloc[-1])
            
            # Tendencia SuperTrend
            if precio_actual > supertrend_actual:
                tendencia = "UPTREND"
            else:
                tendencia = "DOWNTREND"
            
            return {
                "precio": precio_actual,
                "ema200": float(ema200),
                "supertrend": supertrend_actual,
                "adx": adx_actual,
                "tendencia": tendencia,
            }
        except Exception as e:
            logger.error(f"Error calculando indicadores para {symbol}: {e}")
            return None
    
    @staticmethod
    def _supertrend(high, low, close, period=10, multiplier=3):
        """Calcula SuperTrend"""
        hl_avg = (high + low) / 2
        matr = hl_avg.rolling(window=period).mean()
        atr = TechnicalAnalyzer._atr(high, low, close, period) * multiplier
        
        upper = matr + atr
        lower = matr - atr
        
        supertrend = pd.Series(index=close.index, dtype='float64')
        
        for i in range(period, len(close)):
            if i == period:
                supertrend.iloc[i] = lower.iloc[i]
            else:
                if close.iloc[i] <= upper.iloc[i-1]:
                    supertrend.iloc[i] = upper.iloc[i]
                else:
                    supertrend.iloc[i] = lower.iloc[i]
        
        return supertrend
    
    @staticmethod
    def _atr(high, low, close, period=14):
        """Calcula ATR"""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        return atr
    
    @staticmethod
    def _adx(high, low, close, period=14):
        """Calcula ADX"""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        
        up = high - high.shift()
        down = low.shift() - low
        
        pos_dm = up.where((up > down) & (up > 0), 0)
        neg_dm = down.where((down > up) & (down > 0), 0)
        
        pos_di = 100 * pos_dm.rolling(window=period).mean() / atr
        neg_di = 100 * neg_dm.rolling(window=period).mean() / atr
        
        di_diff = abs(pos_di - neg_di)
        di_sum = pos_di + neg_di
        
        dx = 100 * di_diff / di_sum
        adx = dx.rolling(window=period).mean()
        
        return adx

# ============================================================================
# EJECUTOR DE TRADES
# ============================================================================

class TradeExecutor:
    """Ejecuta trades (entrada y salida)"""
    
    def __init__(self, exchange: ExchangeManager, persistencia: Persistencia, notifier: TelegramNotifier, config: Config):
        self.exchange = exchange
        self.persistencia = persistencia
        self.notifier = notifier
        self.config = config
        self.trade_abierto_id = {}  # {symbol: trade_id}
        self.trade_info_abierto = {}  # {symbol: {"entry_px": ..., "side": ..., "size": ...}}
        self.equity_actual = config.capital_inicial_usdt
    
    def buscar_entrada(self, symbol: str, valores: Dict) -> bool:
        """Busca oportunidad de entrada"""
        try:
            # Validar que no hay posición abierta
            if symbol in self.trade_abierto_id:
                return False
            
            precio = valores["precio"]
            ema200 = valores["ema200"]
            supertrend = valores["supertrend"]
            adx = valores["adx"]
            tendencia = valores["tendencia"]
            
            # Lógica de entrada: SuperTrend + EMA + ADX
            signal = None
            
            if tendencia == "UPTREND" and precio > ema200 and adx > 22:
                signal = Direction.LONG
            elif tendencia == "DOWNTREND" and precio < ema200 and adx > 22:
                signal = Direction.SHORT
            
            if not signal:
                return False
            
            # Calcular tamaño basado en equity
            riesgo = min(
                self.equity_actual * self.config.fraccion_equity,
                self.config.capital_riesgo_usdt,
            )
            
            # Calcular precios de SL y TP
            if signal == Direction.LONG:
                sl_precio = precio * (1 - self.config.porcentaje_sl)
                tp_precio = precio * (1 + self.config.porcentaje_tp)
            else:
                sl_precio = precio * (1 + self.config.porcentaje_sl)
                tp_precio = precio * (1 - self.config.porcentaje_tp)
            
            # Validar que SL no cae en liquidación
            liq = 1.0 / max(self.config.leverage, 1)
            dist_sl = abs(precio - sl_precio) / precio
            if dist_sl >= liq * 0.8:
                logger.warning(f"SL cae en zona de liquidación ({dist_sl:.2%} >= {liq*0.8:.2%}) — no opero")
                return False
            
            # Calcular tamaño de contrato
            amount = riesgo / (precio * self.config.porcentaje_sl)
            
            # Normalizar cantidad
            try:
                market = self.exchange.exchange.market(symbol)
                min_amount = market["limits"]["amount"]["min"]
                if amount < min_amount:
                    amount = min_amount
            except:
                pass
            
            # Abrir orden
            logger.info(f"Abriendo {signal.value.upper()} en {symbol} @ ${precio:.4f} | SL: ${sl_precio:.4f} | TP: ${tp_precio:.4f}")
            
            if self.config.modo_simulacion:
                # Simulación
                tid = self.persistencia.abrir(
                    symbol, signal.value, precio, sl_precio, tp_precio, amount, "signal", virtual=1
                )
                if tid:
                    self.trade_abierto_id[symbol] = tid
                    self.trade_info_abierto[symbol] = {
                        "entry_px": precio,
                        "side": signal.value,
                        "size": amount,
                    }
                    self.notifier.enviar(
                        f"SIMULADA ENTRADA ({symbol})\nDirección: {signal.value.upper()}\nEntrada: ${precio:.4f}\nSL: ${sl_precio:.4f}\nTP: ${tp_precio:.4f}\nADX: {adx:.2f}",
                        "TRADE"
                    )
                    return True
            else:
                # Real
                order = self.exchange.crear_orden_mercado(symbol, signal.value, amount)
                if not order:
                    return False
                
                # Crear SL y TP
                sl_ok, tp_ok = self.exchange.crear_tp_sl(symbol, signal.value, amount, sl_precio, tp_precio)
                
                if sl_ok:
                    tid = self.persistencia.abrir(
                        symbol, signal.value, precio, sl_precio, tp_precio, amount, "signal", virtual=0
                    )
                    if tid:
                        self.trade_abierto_id[symbol] = tid
                        self.trade_info_abierto[symbol] = {
                            "entry_px": precio,
                            "side": signal.value,
                            "size": amount,
                        }
                        
                        # MEJORA 4: Verificar que TP se creó
                        if not tp_ok:
                            self.notifier.enviar(
                                f"⚠️ ALERTA TP NO CREADO ({symbol})\nDirección: {signal.value.upper()}\nEntrada: ${precio:.4f}\nSL: ${sl_precio:.4f}\nTP FALTANTE: ${tp_precio:.4f}\nEl SL está activo pero el trade no tiene techo. Revisa OKX manualmente.",
                                "WARNING"
                            )
                            logger.error(f"TP no creado en {symbol} — monitorea manualmente")
                        
                        self.notifier.enviar(
                            f"ENTRADA ({symbol})\nDirección: {signal.value.upper()}\nEntrada: ${precio:.4f}\nSL: ${sl_precio:.4f}\nTP: ${tp_precio:.4f}\nADX: {adx:.2f}",
                            "TRADE"
                        )
                        return True
                else:
                    logger.error(f"SL no creado en {symbol} — cancelando entrada")
                    return False
        
        except Exception as e:
            logger.error(f"Error en entrada de {symbol}: {e}\n{traceback.format_exc()}")
            return False
    
    def buscar_salida(self, symbol: str, valores: Dict) -> bool:
        """Busca oportunidad de salida (SL/TP manual)"""
        try:
            tid = self.trade_abierto_id.get(symbol)
            if not tid:
                return False
            
            info = self.trade_info_abierto.get(symbol)
            if not info:
                return False
            
            precio_actual = valores["precio"]
            entry_px = info["entry_px"]
            side = info["side"]
            size = info["size"]
            
            # Calcular SL y TP
            if side == "long":
                sl = entry_px * (1 - self.config.porcentaje_sl)
                tp = entry_px * (1 + self.config.porcentaje_tp)
                debe_cerrar = precio_actual <= sl or precio_actual >= tp
                razon = "SL" if precio_actual <= sl else "TP"
            else:
                sl = entry_px * (1 + self.config.porcentaje_sl)
                tp = entry_px * (1 - self.config.porcentaje_tp)
                debe_cerrar = precio_actual >= sl or precio_actual <= tp
                razon = "SL" if precio_actual >= sl else "TP"
            
            if debe_cerrar:
                logger.info(f"Salida {side.upper()} en {symbol} ({razon}) @ ${precio_actual:.4f}")
                
                if self.config.modo_simulacion:
                    # Simulación
                    self.persistencia.cerrar(tid, precio_actual, razon)
                    self.trade_abierto_id.pop(symbol, None)
                    self.trade_info_abierto.pop(symbol, None)
                    self.notifier.enviar(
                        f"SIMULADA SALIDA ({symbol})\nDirección: {side.upper()}\nRazón: {razon}\nSalida: ${precio_actual:.4f}",
                        "TRADE"
                    )
                    return True
                else:
                    # Real
                    order_side = "sell" if side == "long" else "buy"
                    order = self.exchange.cerrar_posicion(symbol, side, size)
                    
                    if order:
                        # MEJORA 1 + 2: El PnL se calcula automáticamente en persistencia.cerrar()
                        self.persistencia.cerrar(tid, precio_actual, razon)
                        self.trade_abierto_id.pop(symbol, None)
                        self.trade_info_abierto.pop(symbol, None)
                        
                        self.notifier.enviar(
                            f"SALIDA ({symbol})\nDirección: {side.upper()}\nRazón: {razon}\nSalida: ${precio_actual:.4f}",
                            "TRADE"
                        )
                        return True
            
            return False
        
        except Exception as e:
            logger.error(f"Error en salida de {symbol}: {e}\n{traceback.format_exc()}")
            return False

# ============================================================================
# BOT PRINCIPAL
# ============================================================================

class TradingBot:
    """Bot de trading principal"""
    
    def __init__(self, config: Config):
        self.config = config
        self.notifier = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
        self.exchange = ExchangeManager(OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD, config.modo_simulacion)
        self.persistencia = Persistencia(DB_PATH)
        self.executor = TradeExecutor(self.exchange, self.persistencia, self.notifier, config)
        self.analyzer = TechnicalAnalyzer()
        self.running = True
        self.ciclo_contador = 0
    
    def inicializar(self):
        """Inicializa el bot"""
        try:
            logger.info("Inicializando bot...")
            balance = self.exchange.obtener_balance()
            logger.info(f"Balance USDT: ${balance:.2f}")
            
            if balance < 100:
                logger.warning(f"Balance bajo: ${balance:.2f}")
            
            # Configurar leverage
            for symbol in self.config.simbolos:
                try:
                    self.exchange.exchange.set_leverage(self.config.leverage, symbol, {"marginMode": "isolated"})
                    logger.info(f"Leverage x{self.config.leverage} en {symbol}")
                except:
                    pass
            
            logger.info("Bot inicializado correctamente")
            self.notifier.enviar(
                f"Bot iniciado\nModo: {'SIMULACIÓN' if self.config.modo_simulacion else 'REAL'}\nLeverage: x{self.config.leverage}\nBalance: ${balance:.2f}",
                "INFO"
            )
            return True
        except Exception as e:
            logger.error(f"Error inicializando bot: {e}")
            return False
    
    def ejecutar_ciclo(self):
        """Ejecuta un ciclo de trading"""
        try:
            self.ciclo_contador += 1
            
            for symbol in self.config.simbolos:
                try:
                    # Obtener indicadores
                    valores = self.analyzer.calcular_indicadores(self.exchange.exchange, symbol)
                    if not valores:
                        continue
                    
                    # Buscar salida (SL/TP)
                    self.executor.buscar_salida(symbol, valores)
                    
                    # Buscar entrada
                    self.executor.buscar_entrada(symbol, valores)
                    
                except Exception as e:
                    logger.error(f"Error procesando {symbol}: {e}")
            
            time.sleep(5)  # Esperar 5 segundos
        
        except Exception as e:
            logger.error(f"Error en ciclo: {e}")
    
    def run(self):
        """Ejecuta el bot indefinidamente"""
        if not self.inicializar():
            return
        
        try:
            logger.info("Comenzando loop principal...")
            while self.running:
                self.ejecutar_ciclo()
        except KeyboardInterrupt:
            logger.info("Bot detenido por usuario")
        except Exception as e:
            logger.error(f"Error fatal: {e}\n{traceback.format_exc()}")
        finally:
            self.notifier.enviar("Bot detenido", "INFO")

# ============================================================================
# FLASK API PARA MONITOREO
# ============================================================================

app = Flask(__name__)
bot_instance = None

@app.route("/status")
def status():
    """Retorna estado del bot"""
    if bot_instance:
        return jsonify({
            "running": bot_instance.running,
            "ciclos": bot_instance.ciclo_contador,
            "modo": "simulacion" if bot_instance.config.modo_simulacion else "real",
            "balance": bot_instance.exchange.obtener_balance(),
        })
    return jsonify({"error": "Bot no inicializado"}), 500

@app.route("/trades")
def trades():
    """Retorna últimos trades"""
    try:
        con = sqlite3.connect(DB_PATH)
        cursor = con.cursor()
        cursor.execute("SELECT id, symbol, side, entry_px, exit_px, pnl FROM trades ORDER BY id DESC LIMIT 10")
        trades_list = [
            {
                "id": row[0],
                "symbol": row[1],
                "side": row[2],
                "entry": row[3],
                "exit": row[4],
                "pnl": row[5],
            }
            for row in cursor.fetchall()
        ]
        con.close()
        return jsonify(trades_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/logs")
def logs():
    """Retorna últimas líneas del log"""
    try:
        with open("bot_trading.log", "r") as f:
            lines = f.readlines()[-50:]  # Últimas 50 líneas
        return jsonify({"logs": lines})
    except:
        return jsonify({"error": "No log file"}), 500

# ============================================================================
# MAIN
# ============================================================================

def main():
    global bot_instance
    
    # Crear bot
    config = Config.from_env()
    bot_instance = TradingBot(config)
    
    # Iniciar Flask en thread separado
    def run_flask():
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info(f"Flask iniciado en http://localhost:5000")
    
    # Ejecutar bot
    bot_instance.run()

if __name__ == "__main__":
    main()
