import ccxt
import time

# Configuración de la conexión a OKX Demo Trading
exchange = ccxt.okx({
    'apiKey': '91a74a90-c744-402d-ae04-86f403bb059f',
    'secret': '923FE090636B096EE97A8A621DD27F61',
    'password': 'Gab@8350',
    'enableRateLimit': True,
})


# Forzar el modo Sandbox / Demo
exchange.set_sandbox_mode(True)

# Parámetros generales del bot
SIMBOLOS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT']
LEVERAGE = 5
MARGIN_MODE = 'isolated'
PORCENTAJE_SL = 0.008  # 0.8% de Stop Loss

print("Configurando apalancamiento y modo de margen...")
for symbol in SIMBOLOS:
    try:
        exchange.set_leverage(LEVERAGE, symbol, {'marginMode': MARGIN_MODE})
        print(f"OKX configurado: {symbol} a x5 ({MARGIN_MODE}).")
    except Exception as e:
        print(f"Nota configurando {symbol}: {e}")

print(f"\nBot multicripto encendido en OKX DEMO TRADING (Gestión interna de SL/TP). Monitoreando: {SIMBOLOS} (15m)...")

def ejecutar_estrategia():
    for symbol in SIMBOLOS:
        try:
            # 1. OBTENER DATOS DE VELAS Y PRECIO ACTUAL
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
            if not ohlcv or len(ohlcv) < 20:
                continue
            
            precio_actual = ohlcv[-1][4]

            # 2. VERIFICAR SI YA TENEMOS POSICIÓN ABIERTA
            posiciones = exchange.fetch_positions([symbol])
            posicion_activa = None
            for p in posiciones:
                if p['symbol'] == symbol and float(p.get('contracts', 0)) > 0:
                    posicion_activa = p
                    break

            # --- SI YA HAY POSICIÓN, MONITOREAMOS SL Y TP POR CÓDIGO ---
            if posicion_activa:
                side = posicion_activa['side']  # 'long' o 'short'
                precio_entrada = float(posicion_activa['entryPrice'])
                contracts = float(posicion_activa['contracts'])

                # Calcular los precios límites de salida
                if side == 'long':
                    sl_price = precio_entrada * (1 - PORCENTAJE_SL)
                    tp_price = precio_entrada * (1 + (PORCENTAJE_SL * 1.5)) # Ratio 1:1.5
                    # Condición de cierre: si toca el SL o el TP
                    if precio_actual <= sl_price or precio_actual >= tp_price:
                        print(f"[{symbol}] Precio ({precio_actual}) alcanzó límite (SL: {sl_price:.2f} / TP: {tp_price:.2f}). Cerrando LONG...")
                        exchange.create_order(symbol, 'market', 'sell', contracts, None, {'marginMode': MARGIN_MODE, 'reduceOnly': True})
                elif side == 'short':
                    sl_price = precio_entrada * (1 + PORCENTAJE_SL)
                    tp_price = precio_entrada * (1 - (PORCENTAJE_SL * 1.5))
                    # Condición de cierre: si toca el SL o el TP
                    if precio_actual >= sl_price or precio_actual <= tp_price:
                        print(f"[{symbol}] Precio ({precio_actual}) alcanzó límite (SL: {sl_price:.2f} / TP: {tp_price:.2f}). Cerrando SHORT...")
                        exchange.create_order(symbol, 'market', 'buy', contracts, None, {'marginMode': MARGIN_MODE, 'reduceOnly': True})
                
                continue  # Si ya tiene posición, pasa al siguiente símbolo

            # --- SI NO HAY POSICIÓN, BUSCAMOS SEÑAL DE ENTRADA ---
            rsi_simulado = 30  # Reemplazá esto por tu cálculo real de RSI / Bollinger
            
            if rsi_simulado < 35:
                side = 'buy'
                print(f"\n[SEÑAL COMPRA] Condición cumplida en {symbol} | Precio: {precio_actual}")
            elif rsi_simulado > 65:
                side = 'sell'
                print(f"\n[SEÑAL VENTA] Condición cumplida en {symbol} | Precio: {precio_actual}")
            else:
                continue

            # 3. CÁLCULO DE TAMAÑO EXACTO POR CONTRATO
            market = exchange.market(symbol)
            contract_size = market.get('contractSize', 1.0)

            capital_margen = 5.0  # USDT arriesgados por trade
            notional_target = capital_margen * LEVERAGE  # $25 USDT con x5
            raw_contracts = notional_target / (precio_actual * contract_size)

            # Ajuste de mínimos según la moneda
            if 'BTC' in symbol:
                amount = max(round(raw_contracts, 2), 0.01)
            elif 'ETH' in symbol:
                amount = max(round(raw_contracts, 2), 0.01)
            elif 'SOL' in symbol:
                amount = max(round(raw_contracts, 2), 0.01)
            elif 'XRP' in symbol:
                amount = max(round(raw_contracts, 1), 0.1)
            else: # DOGE
                amount = max(round(raw_contracts, 1), 1.0)

            # 4. EJECUTAR ORDEN PRINCIPAL A MERCADO (Limpias, sin órdenes condicionales de la API)
            params = {'marginMode': MARGIN_MODE}
            order = exchange.create_order(symbol, 'market', side, amount, None, params)
            print(f"Orden ejecutada con éxito ID: {order['id']} | Tamaño: {amount}")

        except Exception as e:
            print(f"Error operando en {symbol}: {e}")

# Bucle principal del bot
while True:
    try:
        ejecutar_estrategia()
        time.sleep(15)  # Revisa precios y condiciones cada 15 segundos
    except KeyboardInterrupt:
        print("\nBot detenido manualmente por el usuario.")
        break
    except Exception as err:
        print(f"Error en el ciclo principal: {err}")
        time.sleep(10)