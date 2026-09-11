from datetime import datetime
import os
import threading
import time
from flask import Flask
import ccxt
import numpy as np
import pandas as pd

# Configuración del servidor web Flask para Render / UptimeRobot
app = Flask(__name__)


@app.route('/')
def home():
  return 'Bot de Trading OKX Testnet en ejecución 24/7 🚀'


# Configuración de la API de OKX
exchange = ccxt.okx({
    'apiKey': '91a74a90-c744-402d-ae04-86f403bb059f',
    'secret': '923FE090636B096EE97A8A621DD27F61',
    'password': 'Gab@8350',
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {
        'defaultType': 'swap'  # Modo futuros perpetuos (USDT-margined)
    },
})

# Sandbox / Testnet activo
exchange.set_sandbox_mode(True)

SIMBOLOS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
TIMEFRAME = '1h'
LEVERAGE = 50
CAPITAL_RIESGO_USDT = 50.0  # Margen base por operación
PORCENTAJE_SL = 0.015  # 1.5% Stop Loss dinámico
COMISION_TASA = 0.0005

ST_PERIODO = 10
ST_MULTIPLIER = 3.0
PERIODO_EMA = 200
ADX_THRESHOLD = 22.0


def configurar_mercado(symbol):
  try:
    exchange.load_markets()
    exchange.set_leverage(LEVERAGE, symbol)
    print(f'✓ Configurado {symbol}: Apalancamiento {LEVERAGE}x')
  except Exception as e:
    print(f'⚠️ Aviso en configuración de {symbol}: {e}')


def obtener_velas(symbol, limit=250):
  try:
    ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=limit)
    df = pd.DataFrame(
        ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
    )
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df
  except Exception as e:
    print(f'❌ Error al descargar velas para {symbol}: {e}')
    return None


def calcular_indicadores(df):
  high = df['high']
  low = df['low']
  close = df['close']
  hl2 = (high + low) / 2

  # ADX
  df['up_move'] = high - high.shift(1)
  df['down_move'] = low.shift(1) - low
  df['plus_dm'] = np.where(
      (df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0.0
  )
  df['minus_dm'] = np.where(
      (df['down_move'] > df['up_move']) & (df['down_move'] > 0),
      df['down_move'],
      0.0,
  )

  df['tr0'] = abs(high - low)
  df['tr1'] = abs(high - close.shift(1))
  df['tr2'] = abs(low - close.shift(1))
  df['tr'] = pd.concat([df['tr0'], df['tr1'], df['tr2']], axis=1).max(axis=1)

  alpha = 1 / ST_PERIODO
  df['tr_smoothed'] = df['tr'].ewm(alpha=alpha, adjust=False).mean()
  df['plus_dm_smoothed'] = df['plus_dm'].ewm(alpha=alpha, adjust=False).mean()
  df['minus_dm_smoothed'] = df['minus_dm'].ewm(alpha=alpha, adjust=False).mean()

  df['plus_di'] = 100 * (df['plus_dm_smoothed'] / df['tr_smoothed'])
  df['minus_di'] = 100 * (df['minus_dm_smoothed'] / df['tr_smoothed'])
  di_sum = df['plus_di'] + df['minus_di']
  df['dx'] = 100 * abs(df['plus_di'] - df['minus_di']) / di_sum.replace(0, 1)
  df['adx'] = df['dx'].ewm(alpha=alpha, adjust=False).mean()

  # SuperTrend
  df['upper_basic'] = hl2 + (ST_MULTIPLIER * df['tr_smoothed'])
  df['lower_basic'] = hl2 - (ST_MULTIPLIER * df['tr_smoothed'])

  upper_basic = df['upper_basic'].values
  lower_basic = df['lower_basic'].values
  close_vals = close.values

  upper_band = np.zeros(len(df))
  lower_band = np.zeros(len(df))
  st = np.ones(len(df), dtype=bool)

  for i in range(ST_PERIODO, len(df)):
    if (
        upper_basic[i] < upper_band[i - 1]
        or close_vals[i - 1] > upper_band[i - 1]
    ):
      upper_band[i] = upper_basic[i]
    else:
      upper_band[i] = upper_band[i - 1]

    if (
        lower_basic[i] > lower_band[i - 1]
        or close_vals[i - 1] < lower_band[i - 1]
    ):
      lower_band[i] = lower_basic[i]
    else:
      lower_band[i] = lower_band[i - 1]

    if i == ST_PERIODO:
      st[i] = True
    elif st[i - 1]:
      if close_vals[i] <= lower_band[i]:
        st[i] = False
      else:
        st[i] = True
    else:
      if close_vals[i] >= upper_band[i]:
        st[i] = True
      else:
        st[i] = False

  df['upper_band'] = upper_band
  df['lower_band'] = lower_band
  df['st_direction'] = st
  df['ema200'] = close.ewm(span=PERIODO_EMA, adjust=False).mean()

  return df


def obtener_posicion_abierta(symbol):
  try:
    positions = exchange.fetch_positions([symbol])
    for p in positions:
      if p['symbol'] == symbol and float(p['contracts']) > 0:
        return p
  except Exception as e:
    print(f'⚠️ Error al consultar posición en {symbol}: {e}')
  return None


def ejecutar_ciclo_bot():
  print(
      f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 Ejecutando ciclo"
      ' de análisis...'
  )

  for symbol in SIMBOLOS:
    df_raw = obtener_velas(symbol)
    if df_raw is None or len(df_raw) < 220:
      continue

    df = calcular_indicadores(df_raw)

    fila_actual = df.iloc[-2]  # Vela cerrada anterior
    fila_anterior = df.iloc[-3]

    precio_actual = fila_actual['close']
    ema200 = fila_actual['ema200']
    adx_actual = fila_actual['adx']
    st_actual = fila_actual['st_direction']
    st_anterior = fila_anterior['st_direction']

    posicion = obtener_posicion_abierta(symbol)

    if posicion:
      side = posicion['side']
      print(
          f'📌 {symbol}: Posición abierta activa ({side.upper()}). Monitoreando'
          ' salida...'
      )

      salir = False
      if side == 'long' and not st_actual:
        print(f'🚨 Señal de salida LONG en {symbol}: SuperTrend viró a bajista.')
        salir = True
      elif side == 'short' and st_actual:
        print(f'🚨 Señal de salida SHORT en {symbol}: SuperTrend viró a alcista.')
        salir = True

      if salir:
        try:
          amount = float(posicion['contracts'])
          order_side = 'sell' if side == 'long' else 'buy'
          exchange.create_market_order(
              symbol, order_side, amount, params={'reduceOnly': True}
          )
          print(f'✓ Posición cerrada en {symbol} por cambio de tendencia.')
        except Exception as e:
          print(f'❌ Error al cerrar posición en {symbol}: {e}')
      continue

    # Evaluar entrada
    signal = None
    if adx_actual > ADX_THRESHOLD:
      if not st_anterior and st_actual and precio_actual > ema200:
        signal = 'long'
      elif st_anterior and not st_actual and precio_actual < ema200:
        signal = 'short'

    if signal:
      print(
          f'🎯 ¡Señal detectada para {symbol} ({signal.upper()})! ADX:'
          f' {adx_actual:.2f}'
      )
      try:
        ticker = exchange.fetch_ticker(symbol)
        precio_mercado = ticker['last']

        market = exchange.market(symbol)
        contract_size = market.get('contractSize', 1.0)

        notional = CAPITAL_RIESGO_USDT * LEVERAGE
        raw_amount = notional / (precio_mercado * contract_size)
        amount = float(exchange.amount_to_precision(symbol, raw_amount))

        if amount <= 0:
          print(f'⚠️ Monto muy chico para operar en {symbol}')
          continue

        if signal == 'long':
          order = exchange.create_market_order(symbol, 'buy', amount)
          sl_precio = precio_mercado * (1 - PORCENTAJE_SL)
          exchange.create_order(
              symbol,
              'market',
              'sell',
              amount,
              params={'stopPrice': sl_precio, 'triggerPrice': sl_precio},
          )
        else:
          order = exchange.create_market_order(symbol, 'sell', amount)
          sl_precio = precio_mercado * (1 + PORCENTAJE_SL)
          exchange.create_order(
              symbol,
              'market',
              'buy',
              amount,
              params={'stopPrice': sl_precio, 'triggerPrice': sl_precio},
          )

        print(
            f'✓ Orden ejecutada para {symbol} [{signal.upper()}] | Tamaño:'
            f' {amount} contratos | SL en {sl_precio:.4f}'
        )
      except Exception as e:
        print(f'❌ Error al abrir posición o SL en {symbol}: {e}')


def bot_loop():
  print('🤖 HILO DEL BOT INICIADO')
  for symbol in SIMBOLOS:
    configurar_mercado(symbol)

  while True:
    try:
      ejecutar_ciclo_bot()
    except Exception as e:
      print(f'⚠️ Error general en el loop del bot: {e}')
    time.sleep(60)


if __name__ == '__main__':
  # Arrancar el bot en segundo plano mediante Threads
  hilo_bot = threading.Thread(target=bot_loop, daemon=True)
  hilo_bot.start()

  # Iniciar servidor web para Render
  port = int(os.environ.get('PORT', 5000))
  app.run(host='0.0.0.0', port=port)