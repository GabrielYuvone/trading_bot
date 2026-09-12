import time
import pandas as pd
import numpy as np
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
import eth_account

# ==========================================
# GENERACIÓN AUTOMÁTICA DE WALLET DE PRUEBA
# ==========================================
# Esto crea una billetera completamente nueva y limpia desde cero para evitar MetaMask y errores de red.
account = eth_account.Account.create()
private_key = account.key.hex()
address = account.address

print(f"\n==================================================")
print(f" BILLETERA DE PRUEBA GENERADA AUTOMÁTICAMENTE")
print(f"==================================================")
print(f" Dirección pública : {address}")
print(f" Clave privada     : {private_key}")
print(f"--------------------------------------------------")
print(f" ACCIÓN REQUERIDA:")
print(f" 1. Entra a: https://app.hyperliquid-testnet.xyz/faucet")
print(f" 2. Pega tu dirección y reclama USDC de prueba gratis.")
print(f"==================================================\n")

input("Presiona [ENTER] en la terminal una vez que hayas reclamado los USDC de prueba para arrancar el bot...")

# Conexión oficial a Hyperliquid Testnet
info = Info(constants.TESTNET_API_URL, skip_ws=True)
exchange = Exchange(account, constants.TESTNET_API_URL, account_address=address)

SYMBOL = 'BTC'
TIMEFRAME = '15m'
LEVERAGE = 5
MARGIN_ALLOCATED = 5      # 5 USDC de margen virtual
PORCENTAJE_SL = 0.008     # Stop Loss al 0.8%

def preparar_cuenta(symbol):
    try:
        exchange.update_leverage(LEVERAGE, symbol, is_isolated=True)
        print(f"Hyperliquid configurado: Apalancamiento x{LEVERAGE} en Margen Aislado para {symbol}.")
    except Exception as e:
        print(f"Nota configurando apalancamiento: {e}")

def calcular_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def obtener_y_analizar_mercado(symbol):
    try:
        # Obtener velas de 15 minutos
        raw_candles = info.candles_snapshot(symbol, TIMEFRAME, int(time.time() * 1000) - 8640000, int(time.time() * 1000))
        df = pd.DataFrame(raw_candles)
        
        df['close'] = df['c'].astype(float)
        df['high'] = df['h'].astype(float)
        df['low'] = df['l'].astype(float)
        
        window = 20
        df['sma'] = df['close'].rolling(window=window).mean()
        df['std'] = df['close'].rolling(window=window).std()
        df['bb_upper'] = df['sma'] + (df['std'] * 2)
        df['bb_lower'] = df['sma'] - (df['std'] * 2)
        df['rsi'] = calcular_rsi(df['close'], period=14)
        
        actual = df.iloc[-2]  
        precio_actual = actual['close']
        rsi_actual = actual['rsi']
        bb_lower = actual['bb_lower']
        bb_upper = actual['bb_upper']
        
        if actual['low'] <= bb_lower and rsi_actual > 35:
            return 'buy', precio_actual  
        elif actual['high'] >= bb_upper and rsi_actual < 65:
            return 'sell', precio_actual 
            
        return None, precio_actual

    except Exception as e:
        print(f"Error analizando mercado en Hyperliquid: {e}")
        return None, None

def ejecutar_jugada(symbol, side, capital_usdt):
    try:
        all_mids = info.all_mids()
        precio_entrada = float(all_mids[symbol])
        
        notional_size = capital_usdt * LEVERAGE
        sz = round(notional_size / precio_entrada, 4)
        
        is_buy = True if side == 'buy' else False
        print(f"\n[SEÑAL EN HYPERLIQUID] Abriendo [{side.upper()}] tamaño {sz} BTC a ~{precio_entrada}")
        
        # 1. Orden de entrada a mercado
        order_result = exchange.market_open(symbol, is_buy, sz, precio_entrada)
        print("Orden ejecutada:", order_result)

        # 2. Cálculo de Stop Loss quirúrgico
        if is_buy:
            precio_sl = precio_entrada * (1 - PORCENTAJE_SL)
            sl_is_buy = False 
        else:
            precio_sl = precio_entrada * (1 + PORCENTAJE_SL)
            sl_is_buy = True  

        precio_sl = round(precio_sl, 2)

        # 3. Orden Stop Loss (Trigger) con reduce_only
        order_type = {"trigger": {"triggerPx": precio_sl, "isMarket": True, "tpsl": "sl"}}
        
        sl_result = exchange.order(
            symbol, 
            sl_is_buy, 
            sz, 
            precio_sl, 
            order_type, 
            reduce_only=True
        )
        print(f"-> Stop Loss quirúrgico configurado en Hyperliquid a: {precio_sl}")
        return True

    except Exception as e:
        print(f"Error ejecutando orden en Hyperliquid: {e}")
        return False

def iniciar_bot():
    preparar_cuenta(SYMBOL)
    print(f"\nBot encendido en HYPERLIQUID TESTNET. Monitoreando {SYMBOL} ({TIMEFRAME})...")
    
    operando_activo = False

    while True:
        side, precio = obtener_y_analizar_mercado(SYMBOL)
        
        if side and not operando_activo:
            print(f"¡Condición cumplida! Precio: {precio}")
            exito = ejecutar_jugada(SYMBOL, side, MARGIN_ALLOCATED)
            if exito:
                operando_activo = True
                time.sleep(300) 
        else:
            print(f"[{time.strftime('%H:%M:%S')}] Monitoreando Hyperliquid Testnet... Precio: {precio}")

        time.sleep(60)

if __name__ == '__main__':
    iniciar_bot()