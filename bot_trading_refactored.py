"""
Bot de Trading OKX Testnet - Versión Refactorizada
Estrategia: SuperTrend + EMA200 + ADX
"""

from datetime import datetime
import os
import threading
import time
import logging
import json
import traceback
from dataclasses import dataclass
from typing import Optional, Dict, Tuple
from enum import Enum

from flask import Flask
import ccxt
import numpy as np
import pandas as pd
import requests

# ============================================================================
# CONFIGURACIÓN DE LOGGING
# ============================================================================

class ColoredFormatter(logging.Formatter):
    """Formatter con colores para mejor visualización en consola"""
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[92m',       # Green
        'WARNING': '\033[93m',    # Yellow
        'ERROR': '\033[91m',      # Red
        'CRITICAL': '\033[95m',   # Magenta
        'RESET': '\033[0m'
    }

    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        record.levelname = f"{log_color}{record.levelname}{self.COLORS['RESET']}"
        return super().format(record)


def setup_logging(name: str, log_file: str = None) -> logging.Logger:
    """Configura logging con salida a consola y archivo opcional"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    # Handler para consola
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_format = ColoredFormatter(
        '%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # Handler para archivo (opcional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)
    
    return logger


logger = setup_logging('TradingBot', 'bot_trading.log')


# ============================================================================
# ENUMS Y DATA CLASSES
# ============================================================================

class TrendDirection(Enum):
    """Dirección de la tendencia"""
    UPTREND = "long"
    DOWNTREND = "short"
    NEUTRAL = None


@dataclass
class IndicatorValues:
    """Valores de indicadores para análisis"""
    precio_actual: float
    ema200: float
    adx: float
    st_direction: bool
    st_direction_anterior: bool
    upper_band: float
    lower_band: float


@dataclass
class TradeSignal:
    """Señal de trading detectada"""
    symbol: str
    direction: TrendDirection
    precio: float
    adx: float
    timestamp: datetime
    
    def __str__(self):
        return (f"[{self.timestamp.strftime('%H:%M:%S')}] "
                f"{self.symbol} - {self.direction.value.upper()} @ {self.precio:.4f} "
                f"(ADX: {self.adx:.2f})")


# ============================================================================
# CONFIGURACIÓN DEL BOT
# ============================================================================

@dataclass
class BotConfig:
    """Configuración centralizada del bot"""
    # Credenciales (desde variables de entorno)
    api_key: str
    api_secret: str
    api_password: str
    telegram_token: str
    telegram_chat_id: str
    
    # Trading
    simbolos: list = None
    timeframe: str = '1h'
    leverage: int = 50
    capital_riesgo_usdt: float = 50.0
    porcentaje_sl: float = 0.015
    
    # Indicadores
    st_periodo: int = 10
    st_multiplier: float = 3.0
    periodo_ema: int = 200
    adx_threshold: float = 22.0
    
    # Sistema
    ciclo_segundos: int = 60
    limite_velas: int = 250
    velas_minimas: int = 220
    
    def __post_init__(self):
        if self.simbolos is None:
            self.simbolos = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
    
    @classmethod
    def desde_env(cls) -> 'BotConfig':
        """Carga configuración desde variables de entorno"""
        return cls(
            api_key=os.getenv("OKX_API_KEY"),
            api_secret=os.getenv("OKX_API_SECRET"),
            api_password=os.getenv("OKX_API_PASSWORD"),
            telegram_token=os.getenv("TELEGRAM_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        )


config = BotConfig.desde_env()

if not all([config.api_key, config.api_secret, config.api_password]):
    logger.error("❌ CREDENCIALES OKX NO CONFIGURADAS EN VARIABLES DE ENTORNO")
    logger.error("   Por favor establecer: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD")
    raise ValueError("Credenciales OKX no configuradas")


# ============================================================================
# SERVICIO DE TELEGRAM
# ============================================================================

class TelegramNotifier:
    """Gestor de notificaciones por Telegram"""
    
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.logger = logging.getLogger('Telegram')
        self.habilitado = bool(token and chat_id)
    
    def enviar(self, mensaje: str, nivel: str = "INFO") -> bool:
        """Envía mensaje a Telegram"""
        if not self.habilitado:
            self.logger.debug(f"Telegram deshabilitado, mensaje no enviado: {mensaje[:50]}...")
            return False
        
        try:
            # Agregar emoji según nivel
            emojis = {
                "INFO": "ℹ️",
                "SUCCESS": "✅",
                "WARNING": "⚠️",
                "ERROR": "❌",
                "TRADE": "🎯"
            }
            
            emoji = emojis.get(nivel, "•")
            mensaje_formateado = f"{emoji} {mensaje}"
            
            url = f'https://api.telegram.org/bot{self.token}/sendMessage'
            payload = {
                'chat_id': self.chat_id,
                'text': mensaje_formateado,
                'parse_mode': 'HTML'
            }
            
            response = requests.post(url, json=payload, timeout=5)
            response.raise_for_status()
            
            self.logger.debug(f"✓ Telegram enviado: {mensaje[:60]}...")
            return True
            
        except requests.exceptions.Timeout:
            self.logger.warning("⚠️ Timeout al enviar a Telegram")
            return False
        except requests.exceptions.RequestException as e:
            self.logger.error(f"❌ Error en Telegram: {e}")
            return False
        except Exception as e:
            self.logger.error(f"❌ Error inesperado en Telegram: {e}")
            return False


notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)


# ============================================================================
# GESTOR DE EXCHANGE
# ============================================================================

class ExchangeManager:
    """Gestor de conexión y operaciones con OKX"""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.logger = logging.getLogger('Exchange')
        self.exchange = None
        self._inicializar()
    
    def _inicializar(self):
        """Inicializa la conexión con OKX"""
        try:
            self.exchange = ccxt.okx({
                'apiKey': self.config.api_key,
                'secret': self.config.api_secret,
                'password': self.config.api_password,
                'enableRateLimit': True,
                'timeout': 15000,
                'options': {'defaultType': 'swap'},
            })
            
            self.exchange.set_sandbox_mode(True)
            
            # Verificar conexión
            markets = self.exchange.load_markets()
            self.logger.info(f"✓ Conexión OKX exitosa ({len(markets)} mercados cargados)")
            
        except ccxt.AuthenticationError as e:
            self.logger.error(f"❌ Error de autenticación OKX: {e}")
            notifier.enviar(f"Error de autenticación OKX", "ERROR")
            raise
        except Exception as e:
            self.logger.error(f"❌ Error al conectar con OKX: {e}")
            raise
    
    def configurar_mercado(self, symbol: str) -> bool:
        """Configura un mercado específico"""
        try:
            self.exchange.set_leverage(self.config.leverage, symbol)
            self.logger.info(f"✓ {symbol}: Apalancamiento configurado a {self.config.leverage}x")
            return True
        except ccxt.BadSymbol:
            self.logger.error(f"❌ Símbolo inválido: {symbol}")
            return False
        except Exception as e:
            self.logger.warning(f"⚠️ Error configurando {symbol}: {e}")
            return False
    
    def obtener_velas(self, symbol: str, limit: int = None) -> Optional[pd.DataFrame]:
        """Obtiene velas OHLCV"""
        limit = limit or self.config.limite_velas
        
        try:
            self.logger.debug(f"Obteniendo {limit} velas para {symbol}...")
            ohlcv = self.exchange.fetch_ohlcv(symbol, self.config.timeframe, limit=limit)
            
            df = pd.DataFrame(
                ohlcv,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            self.logger.debug(f"✓ {symbol}: {len(df)} velas obtenidas")
            return df
            
        except ccxt.BadSymbol:
            self.logger.error(f"❌ Símbolo inválido: {symbol}")
            return None
        except ccxt.NetworkError:
            self.logger.error(f"❌ Error de red al obtener velas de {symbol}")
            return None
        except Exception as e:
            self.logger.error(f"❌ Error obteniendo velas de {symbol}: {e}")
            return None
    
    def obtener_posicion_abierta(self, symbol: str) -> Optional[Dict]:
        """Obtiene posición abierta si existe"""
        try:
            positions = self.exchange.fetch_positions([symbol])
            self.logger.debug(f"{symbol}: Consultando {len(positions)} posición(es)")
            
            for p in positions:
                contracts = float(p.get('contracts', 0))
                if contracts > 0:
                    side = p['side']
                    self.logger.debug(f"  ✓ Posición activa: {side.upper()} {contracts} contratos")
                    return p
            
            self.logger.debug(f"{symbol}: Sin posiciones abiertas")
            return None
            
        except ccxt.NetworkError:
            self.logger.error(f"❌ Error de red consultando posiciones de {symbol}")
            return None
        except Exception as e:
            self.logger.error(f"❌ Error consultando posición en {symbol}: {e}")
            self.logger.debug(traceback.format_exc())
            return None
    
    def obtener_ticker(self, symbol: str) -> Optional[Dict]:
        """Obtiene ticker actual"""
        try:
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            self.logger.error(f"❌ Error obteniendo ticker de {symbol}: {e}")
            return None
    
    def crear_orden_mercado(self, symbol: str, side: str, amount: float) -> bool:
        """Crea orden de mercado"""
        try:
            self.logger.info(f"📤 Creando orden {side.upper()} de {amount} en {symbol}...")
            self.exchange.create_market_order(symbol, side, amount)
            self.logger.info(f"✓ Orden {side.upper()} ejecutada en {symbol}")
            return True
        except ccxt.InsufficientBalance:
            self.logger.error(f"❌ Balance insuficiente para operar en {symbol}")
            notifier.enviar(f"Balance insuficiente en {symbol}", "ERROR")
            return False
        except Exception as e:
            self.logger.error(f"❌ Error creando orden en {symbol}: {e}")
            return False
    
    def crear_stop_loss(self, symbol: str, side: str, amount: float, 
                       sl_precio: float) -> bool:
        """Crea orden de stop loss"""
        try:
            side_opuesto = 'sell' if side == 'buy' else 'buy'
            
            self.logger.debug(
                f"Creando SL {side_opuesto.upper()} de {amount} @ {sl_precio:.4f}..."
            )
            
            # Intenta con parámetros estándar de OKX
            self.exchange.create_order(
                symbol,
                'market',
                side_opuesto,
                amount,
                None,  # precio
                params={
                    'stopPrice': sl_precio,
                    'triggerPrice': sl_precio,
                    'reduceOnly': True,
                }
            )
            
            self.logger.info(f"✓ Stop loss creado en {symbol} @ {sl_precio:.4f}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Error creando SL en {symbol}: {e}")
            # Continúa sin SL pero registra el error
            return False


# ============================================================================
# ANALIZADOR TÉCNICO
# ============================================================================

class TechnicalAnalyzer:
    """Análisis técnico: SuperTrend, ADX, EMA"""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.logger = logging.getLogger('Technical')
    
    def calcular_indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calcula todos los indicadores técnicos"""
        try:
            self.logger.debug("Calculando indicadores...")
            
            high = df['high']
            low = df['low']
            close = df['close']
            hl2 = (high + low) / 2

            # Directional Movement
            df['up_move'] = high - high.shift(1)
            df['down_move'] = low.shift(1) - low
            df['plus_dm'] = np.where(
                (df['up_move'] > df['down_move']) & (df['up_move'] > 0),
                df['up_move'], 0.0
            )
            df['minus_dm'] = np.where(
                (df['down_move'] > df['up_move']) & (df['down_move'] > 0),
                df['down_move'], 0.0
            )

            # True Range
            df['tr0'] = abs(high - low)
            df['tr1'] = abs(high - close.shift(1))
            df['tr2'] = abs(low - close.shift(1))
            df['tr'] = pd.concat([df['tr0'], df['tr1'], df['tr2']], axis=1).max(axis=1)

            # Smoothing
            alpha = 1 / self.config.st_periodo
            df['tr_smoothed'] = df['tr'].ewm(alpha=alpha, adjust=False).mean()
            df['plus_dm_smoothed'] = df['plus_dm'].ewm(alpha=alpha, adjust=False).mean()
            df['minus_dm_smoothed'] = df['minus_dm'].ewm(alpha=alpha, adjust=False).mean()

            # ADX
            df['plus_di'] = 100 * (df['plus_dm_smoothed'] / df['tr_smoothed'])
            df['minus_di'] = 100 * (df['minus_dm_smoothed'] / df['tr_smoothed'])
            di_sum = df['plus_di'] + df['minus_di']
            df['dx'] = 100 * abs(df['plus_di'] - df['minus_di']) / di_sum.replace(0, 1)
            df['adx'] = df['dx'].ewm(alpha=alpha, adjust=False).mean()

            # SuperTrend
            df['upper_basic'] = hl2 + (self.config.st_multiplier * df['tr_smoothed'])
            df['lower_basic'] = hl2 - (self.config.st_multiplier * df['tr_smoothed'])

            upper_basic = df['upper_basic'].values
            lower_basic = df['lower_basic'].values
            close_vals = close.values

            upper_band = np.zeros(len(df))
            lower_band = np.zeros(len(df))
            st = np.ones(len(df), dtype=bool)

            for i in range(self.config.st_periodo, len(df)):
                if (upper_basic[i] < upper_band[i - 1] or 
                    close_vals[i - 1] > upper_band[i - 1]):
                    upper_band[i] = upper_basic[i]
                else:
                    upper_band[i] = upper_band[i - 1]

                if (lower_basic[i] > lower_band[i - 1] or 
                    close_vals[i - 1] < lower_band[i - 1]):
                    lower_band[i] = lower_basic[i]
                else:
                    lower_band[i] = lower_band[i - 1]

                if i == self.config.st_periodo:
                    st[i] = True
                elif st[i - 1]:
                    st[i] = False if close_vals[i] <= lower_band[i] else True
                else:
                    st[i] = True if close_vals[i] >= upper_band[i] else False

            df['upper_band'] = upper_band
            df['lower_band'] = lower_band
            df['st_direction'] = st
            df['ema200'] = close.ewm(span=self.config.periodo_ema, adjust=False).mean()

            self.logger.debug("✓ Indicadores calculados")
            return df
            
        except Exception as e:
            self.logger.error(f"❌ Error calculando indicadores: {e}")
            self.logger.debug(traceback.format_exc())
            raise
    
    def extraer_valores(self, df: pd.DataFrame) -> Tuple[IndicatorValues, IndicatorValues]:
        """Extrae valores de indicadores para análisis"""
        try:
            fila_actual = df.iloc[-2]
            fila_anterior = df.iloc[-3]
            
            valores_actual = IndicatorValues(
                precio_actual=fila_actual['close'],
                ema200=fila_actual['ema200'],
                adx=fila_actual['adx'],
                st_direction=fila_actual['st_direction'],
                st_direction_anterior=fila_anterior['st_direction'],
                upper_band=fila_actual['upper_band'],
                lower_band=fila_actual['lower_band']
            )
            
            valores_anterior = IndicatorValues(
                precio_actual=fila_anterior['close'],
                ema200=fila_anterior['ema200'],
                adx=fila_anterior['adx'],
                st_direction=fila_anterior['st_direction'],
                st_direction_anterior=df.iloc[-4]['st_direction'],
                upper_band=fila_anterior['upper_band'],
                lower_band=fila_anterior['lower_band']
            )
            
            return valores_actual, valores_anterior
            
        except Exception as e:
            self.logger.error(f"❌ Error extrayendo valores: {e}")
            return None, None
    
    def detectar_señal(self, valores: IndicatorValues) -> Optional[TrendDirection]:
        """Detecta señal de trading basada en indicadores"""
        try:
            # Condiciones para LONG
            if (not valores.st_direction_anterior and 
                valores.st_direction and 
                valores.precio_actual > valores.ema200 and
                valores.adx > self.config.adx_threshold):
                return TrendDirection.UPTREND
            
            # Condiciones para SHORT
            if (valores.st_direction_anterior and 
                not valores.st_direction and 
                valores.precio_actual < valores.ema200 and
                valores.adx > self.config.adx_threshold):
                return TrendDirection.DOWNTREND
            
            return TrendDirection.NEUTRAL
            
        except Exception as e:
            self.logger.error(f"❌ Error detectando señal: {e}")
            return TrendDirection.NEUTRAL


# ============================================================================
# EJECUTOR DE TRADES
# ============================================================================

class TradeExecutor:
    """Ejecuta operaciones de trading"""
    
    def __init__(self, exchange_manager: ExchangeManager, config: BotConfig):
        self.exchange = exchange_manager
        self.config = config
        self.logger = logging.getLogger('Executor')
    
    def calcular_tamaño_posicion(self, symbol: str, precio_mercado: float) -> Optional[float]:
        """Calcula el tamaño de posición según capital de riesgo"""
        try:
            market = self.exchange.exchange.market(symbol)
            contract_size = market.get('contractSize', 1.0)
            
            notional = self.config.capital_riesgo_usdt * self.config.leverage
            raw_amount = notional / (precio_mercado * contract_size)
            amount = float(self.exchange.exchange.amount_to_precision(symbol, raw_amount))
            
            if amount <= 0:
                self.logger.warning(f"⚠️ Monto calculado inválido: {amount}")
                return None
            
            self.logger.debug(f"{symbol}: Tamaño posición = {amount} contratos")
            return amount
            
        except Exception as e:
            self.logger.error(f"❌ Error calculando tamaño: {e}")
            return None
    
    def abrir_posicion(self, signal: TradeSignal, valores: IndicatorValues) -> bool:
        """Abre una nueva posición con stop loss"""
        symbol = signal.symbol
        direction = signal.direction
        
        try:
            self.logger.info(f"🎯 Procesando señal {direction.value.upper()} en {symbol}...")
            
            # Obtener ticker
            ticker = self.exchange.obtener_ticker(symbol)
            if not ticker:
                return False
            
            precio_mercado = ticker['last']
            
            # Calcular tamaño
            amount = self.calcular_tamaño_posicion(symbol, precio_mercado)
            if not amount:
                return False
            
            # Calcular stop loss
            if direction == TrendDirection.UPTREND:
                sl_precio = precio_mercado * (1 - self.config.porcentaje_sl)
                side = 'buy'
            else:
                sl_precio = precio_mercado * (1 + self.config.porcentaje_sl)
                side = 'sell'
            
            # Crear orden de entrada
            if not self.exchange.crear_orden_mercado(symbol, side, amount):
                return False
            
            # Crear stop loss
            self.exchange.crear_stop_loss(symbol, side, amount, sl_precio)
            
            # Notificar
            msg = (
                f"🟢 NUEVA OPERACIÓN ({symbol})\n"
                f"Dirección: {direction.value.upper()}\n"
                f"Tamaño: {amount} contratos\n"
                f"Entrada: {precio_mercado:.4f}\n"
                f"SL: {sl_precio:.4f}\n"
                f"ADX: {valores.adx:.2f}"
            )
            self.logger.info(msg)
            notifier.enviar(msg, "TRADE")
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Error abriendo posición en {symbol}: {e}")
            self.logger.debug(traceback.format_exc())
            notifier.enviar(f"Error abriendo posición en {symbol}: {e}", "ERROR")
            return False
    
    def cerrar_posicion(self, symbol: str, posicion: Dict, razon: str) -> bool:
        """Cierra una posición abierta"""
        try:
            side = posicion['side']
            amount = float(posicion['contracts'])
            
            # Orden opuesta
            order_side = 'sell' if side == 'long' else 'buy'
            
            self.logger.info(f"📤 Cerrando posición {side.upper()} de {amount} en {symbol}...")
            
            if not self.exchange.crear_orden_mercado(symbol, order_side, amount):
                return False
            
            # Notificar
            msg = (
                f"🔴 POSICIÓN CERRADA ({symbol})\n"
                f"Tipo: {side.upper()}\n"
                f"Tamaño: {amount} contratos\n"
                f"Razón: {razon}"
            )
            self.logger.info(msg)
            notifier.enviar(msg, "INFO")
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Error cerrando posición en {symbol}: {e}")
            self.logger.debug(traceback.format_exc())
            return False


# ============================================================================
# MOTOR PRINCIPAL DEL BOT
# ============================================================================

class TradingBot:
    """Orquestador principal del bot de trading"""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.logger = logging.getLogger('Bot')
        
        self.exchange = ExchangeManager(config)
        self.analyzer = TechnicalAnalyzer(config)
        self.executor = TradeExecutor(self.exchange, config)
        
        self.activo = True
        self.ciclo_contador = 0
    
    def inicializar(self) -> bool:
        """Inicializa el bot"""
        try:
            self.logger.info("=" * 80)
            self.logger.info("🤖 INICIANDO BOT DE TRADING OKX")
            self.logger.info("=" * 80)
            
            self.logger.info(f"Símbolos: {', '.join(self.config.simbolos)}")
            self.logger.info(f"Timeframe: {self.config.timeframe}")
            self.logger.info(f"Leverage: {self.config.leverage}x")
            self.logger.info(f"Capital de riesgo: {self.config.capital_riesgo_usdt} USDT")
            self.logger.info(f"ADX Threshold: {self.config.adx_threshold}")
            self.logger.info("=" * 80)
            
            # Configurar mercados
            for symbol in self.config.simbolos:
                if not self.exchange.configurar_mercado(symbol):
                    self.logger.warning(f"⚠️ No se pudo configurar {symbol}")
            
            self.logger.info("✓ Bot inicializado correctamente")
            notifier.enviar("🤖 Bot de trading iniciado", "SUCCESS")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Error inicializando bot: {e}")
            self.logger.debug(traceback.format_exc())
            notifier.enviar(f"Error inicializando bot: {e}", "ERROR")
            return False
    
    def analizar_symbol(self, symbol: str) -> bool:
        """Analiza un símbolo individual"""
        try:
            # Obtener velas
            df_raw = self.exchange.obtener_velas(symbol)
            if df_raw is None or len(df_raw) < self.config.velas_minimas:
                self.logger.warning(
                    f"⚠️ {symbol}: Datos insuficientes "
                    f"({len(df_raw) if df_raw is not None else 0}/{self.config.velas_minimas})"
                )
                return False
            
            # Calcular indicadores
            df = self.analyzer.calcular_indicadores(df_raw)
            valores, _ = self.analyzer.extraer_valores(df)
            
            if not valores:
                return False
            
            # Log de estado
            self.logger.info(
                f"{symbol} | Precio: {valores.precio_actual:.4f} | "
                f"EMA200: {valores.ema200:.4f} | "
                f"ADX: {valores.adx:.2f} | "
                f"ST: {'↗️' if valores.st_direction else '↘️'}"
            )
            
            # Verificar posición existente
            posicion = self.exchange.obtener_posicion_abierta(symbol)
            
            if posicion:
                self.logger.info(f"📌 {symbol}: Posición abierta ({posicion['side'].upper()})")
                self._manejar_posicion_abierta(symbol, posicion, valores)
                return True
            
            # Detectar nueva señal
            señal_direccion = self.analyzer.detectar_señal(valores)
            
            if señal_direccion != TrendDirection.NEUTRAL:
                signal = TradeSignal(
                    symbol=symbol,
                    direction=señal_direccion,
                    precio=valores.precio_actual,
                    adx=valores.adx,
                    timestamp=datetime.now()
                )
                self.logger.warning(f"🎯 SEÑAL DETECTADA: {signal}")
                self.executor.abrir_posicion(signal, valores)
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Error analizando {symbol}: {e}")
            self.logger.debug(traceback.format_exc())
            return False
    
    def _manejar_posicion_abierta(self, symbol: str, posicion: Dict, 
                                   valores: IndicatorValues):
        """Maneja una posición abierta"""
        side = posicion['side']
        debe_cerrar = False
        razon = ""
        
        if side == 'long' and not valores.st_direction:
            debe_cerrar = True
            razon = "SuperTrend cambió a bajista"
        elif side == 'short' and valores.st_direction:
            debe_cerrar = True
            razon = "SuperTrend cambió a alcista"
        
        if debe_cerrar:
            self.logger.warning(f"🚨 Señal de SALIDA en {symbol}: {razon}")
            self.executor.cerrar_posicion(symbol, posicion, razon)
    
    def ciclo_analisis(self):
        """Ciclo principal de análisis"""
        self.ciclo_contador += 1
        
        self.logger.info(
            f"\n{'=' * 80}\n"
            f"[CICLO {self.ciclo_contador}] "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'=' * 80}"
        )
        
        exitos = 0
        for symbol in self.config.simbolos:
            if self.analizar_symbol(symbol):
                exitos += 1
        
        self.logger.info(
            f"✓ Ciclo completado: {exitos}/{len(self.config.simbolos)} análisis exitosos"
        )
    
    def ejecutar(self):
        """Bucle principal del bot"""
        if not self.inicializar():
            self.logger.error("❌ Falló la inicialización, deteniendo bot")
            return
        
        self.logger.info(f"⏱️  Ciclo cada {self.config.ciclo_segundos} segundos...")
        
        try:
            while self.activo:
                try:
                    self.ciclo_analisis()
                except Exception as e:
                    self.logger.error(f"❌ Error en ciclo: {e}")
                    self.logger.debug(traceback.format_exc())
                    notifier.enviar(f"Error en ciclo del bot: {e}", "ERROR")
                
                time.sleep(self.config.ciclo_segundos)
        
        except KeyboardInterrupt:
            self.logger.info("\n🛑 Bot detenido por usuario")
            notifier.enviar("Bot detenido manualmente", "INFO")
        except Exception as e:
            self.logger.critical(f"❌ Error crítico: {e}")
            self.logger.debug(traceback.format_exc())
            notifier.enviar(f"Error crítico en bot: {e}", "ERROR")
        finally:
            self.activo = False


# ============================================================================
# FLASK APP Y THREADING
# ============================================================================

app = Flask(__name__)
bot = None


@app.route('/')
def home():
    ciclo = getattr(bot, 'ciclo_contador', 0) if 'bot' in globals() and bot is not None else 0
    return (f'🤖 Bot de Trading OKX Testnet en ejecución (Ciclo {ciclo})<br>'
        f'Símbolos: {", ".join(config.simbolos)}<br>'
        f'Logs: ver bot_trading.log'
    )


@app.route('/status')
def status():
    """Endpoint para verificar estado del bot"""
    if not bot:
        return {'estado': 'no_inicializado'}, 503
    
    return {
        'estado': 'activo' if bot.activo else 'inactivo',
        'ciclos_ejecutados': bot.ciclo_contador,
        'simbolos': config.simbolos,
        'timestamp': datetime.now().isoformat()
    }


def iniciar_bot_background():
    """Inicia el bot en un hilo de fondo"""
    global bot
    
    time.sleep(5)  # Espera a que Flask se levante
    
    try:
        bot = TradingBot(config)
        bot.ejecutar()
    except Exception as e:
        logger.critical(f"❌ Error fatal en bot: {e}")
        logger.debug(traceback.format_exc())
        notifier.enviar(f"Error fatal en bot: {e}", "ERROR")


if __name__ == '__main__':
    # Iniciar bot en hilo de fondo
    hilo = threading.Thread(target=iniciar_bot_background, daemon=True)
    hilo.start()
    
    # Iniciar Flask
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🌐 Iniciando Flask en puerto {port}...")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
