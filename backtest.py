import ccxt
import pandas as pd
import numpy as np
import os
import json
import time

exchange = ccxt.okx({
    'enableRateLimit': True,
    'timeout': 10000,
})

SIMBOLOS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
TIMEFRAME = '1H'  
DIAS_HISTORIAL = 180  

CAPITAL_INICIAL = 1000.0
LEVERAGE = 50
PORCENTAJE_SL = 0.015  # 1.5% Stop Loss (más margen para dejar respirar la tendencia)
# Nota: Eliminamos el Take Profit fijo para dejar correr los beneficios con el SuperTrend
CAPITAL_RIESGO_USDT = 50.0  
COMISION_TASA = 0.0005  

ST_PERIODO = 10
ST_MULTIPLIER = 3.0
PERIODO_EMA = 200
ADX_THRESHOLD = 22.0  


def descargar_datos_historicos(symbol, timeframe, dias):
    nombre_archivo = f"historial_{symbol.replace('/', '_').replace(':', '_')}_{timeframe}.csv"
    
    if os.path.exists(nombre_archivo):
        print(f"📂 Cargando datos locales desde {nombre_archivo}...")
        df = pd.read_csv(nombre_archivo)
        df['datetime'] = pd.to_datetime(df['datetime'])
        return df

    print(f"📊 Descargando {dias} días de datos ({timeframe}) para {symbol} desde OKX...")
    since = exchange.milliseconds() - (dias * 24 * 60 * 60 * 1000)
    all_ohlcv = []
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=100)
            if not ohlcv:
                break
            since = ohlcv[-1][0] + 1
            all_ohlcv.extend(ohlcv)
            if ohlcv[-1][0] >= exchange.milliseconds():
                break
            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            print(f"⚠️ Error descargando lote de datos: {e}")
            time.sleep(2)
            break

    if not all_ohlcv:
        return None

    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.drop_duplicates(subset=['timestamp'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    
    df.to_csv(nombre_archivo, index=False)
    print(f"✓ Datos guardados localmente: {len(df):,} velas")
    
    return df


def calcular_indicadores_con_adx(df, period=10, multiplier=3.0):
    high = df['high']
    low = df['low']
    close = df['close']
    hl2 = (high + low) / 2
    
    # --- CÁLCULO DE ADX ---
    df['up_move'] = high - high.shift(1)
    df['down_move'] = low.shift(1) - low
    
    df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0.0)
    df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0.0)
    
    df['tr0'] = abs(high - low)
    df['tr1'] = abs(high - close.shift(1))
    df['tr2'] = abs(low - close.shift(1))
    df['tr'] = pd.concat([df['tr0'], df['tr1'], df['tr2']], axis=1).max(axis=1)
    
    alpha = 1 / period
    df['tr_smoothed'] = df['tr'].ewm(alpha=alpha, adjust=False).mean()
    df['plus_dm_smoothed'] = df['plus_dm'].ewm(alpha=alpha, adjust=False).mean()
    df['minus_dm_smoothed'] = df['minus_dm'].ewm(alpha=alpha, adjust=False).mean()
    
    df['plus_di'] = 100 * (df['plus_dm_smoothed'] / df['tr_smoothed'])
    df['minus_di'] = 100 * (df['minus_dm_smoothed'] / df['tr_smoothed'])
    
    di_sum = df['plus_di'] + df['minus_di']
    df['dx'] = 100 * abs(df['plus_di'] - df['minus_di']) / di_sum.replace(0, 1)
    df['adx'] = df['dx'].ewm(alpha=alpha, adjust=False).mean()

    # --- CÁLCULO DE SUPERTREND ---
    df['upper_basic'] = hl2 + (multiplier * df['tr_smoothed'])
    df['lower_basic'] = hl2 - (multiplier * df['tr_smoothed'])

    upper_basic = df['upper_basic'].values
    lower_basic = df['lower_basic'].values
    close_vals = close.values

    upper_band = np.zeros(len(df))
    lower_band = np.zeros(len(df))
    st = np.ones(len(df), dtype=bool)

    for i in range(period, len(df)):
        if upper_basic[i] < upper_band[i - 1] or close_vals[i - 1] > upper_band[i - 1]:
            upper_band[i] = upper_basic[i]
        else:
            upper_band[i] = upper_band[i - 1]

        if lower_basic[i] > lower_band[i - 1] or close_vals[i - 1] < lower_band[i - 1]:
            lower_band[i] = lower_basic[i]
        else:
            lower_band[i] = lower_band[i - 1]

        if i == period:
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
    
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def ejecutar_backtest(symbol):
    df_raw = descargar_datos_historicos(symbol, TIMEFRAME, DIAS_HISTORIAL)
    if df_raw is None or len(df_raw) < 250:
        print(f"❌ Datos insuficientes para {symbol}")
        return None

    df = calcular_indicadores_con_adx(df_raw, period=ST_PERIODO, multiplier=ST_MULTIPLIER)
    
    capital = CAPITAL_INICIAL
    pico_capital = capital
    max_drawdown = 0.0
    trades = []
    
    en_posicion = False
    tipo_pos = None  
    precio_entrada = 0.0
    contracts = 0.0
    sl = 0.0

    print(f"🔄 Simulando operaciones con salida por tendencia en gráfico de 1h ({symbol})...")

    for i in range(1, len(df)):
        fila = df.iloc[i]
        fila_anterior = df.iloc[i - 1]
        
        precio_actual = fila['close']
        high = fila['high']
        low = fila['low']
        ema200 = fila['ema200']
        adx_actual = fila['adx']
        
        st_actual = fila['st_direction']
        st_anterior = fila_anterior['st_direction']

        if en_posicion:
            toco_sl = False
            cambio_tendencia = False
            precio_salida = 0.0

            if tipo_pos == 'long':
                if low <= sl:
                    toco_sl = True
                    precio_salida = sl
                elif not st_actual:  # Salida dinámica: el SuperTrend cambia a bajista
                    cambio_tendencia = True
                    precio_salida = precio_actual
                
                if toco_sl or cambio_tendencia:
                    ingreso_bruto = (precio_salida - precio_entrada) * contracts
                    comision = (precio_entrada * contracts * COMISION_TASA) + (precio_salida * contracts * COMISION_TASA)
                    pnl_neto = ingreso_bruto - comision
                    capital += pnl_neto
                    trades.append({'tipo': 'long', 'resultado': 'ganada' if cambio_tendencia else 'perdida', 'pnl': pnl_neto})
                    en_posicion = False

            elif tipo_pos == 'short':
                if high >= sl:
                    toco_sl = True
                    precio_salida = sl
                elif st_actual:  # Salida dinámica: el SuperTrend cambia a alcista
                    cambio_tendencia = True
                    precio_salida = precio_actual

                if toco_sl or cambio_tendencia:
                    ingreso_bruto = (precio_entrada - precio_salida) * contracts
                    comision = (precio_entrada * contracts * COMISION_TASA) + (precio_salida * contracts * COMISION_TASA)
                    pnl_neto = ingreso_bruto - comision
                    capital += pnl_neto
                    trades.append({'tipo': 'short', 'resultado': 'ganada' if cambio_tendencia else 'perdida', 'pnl': pnl_neto})
                    en_posicion = False

            if capital > pico_capital:
                pico_capital = capital
            drawdown = (capital - pico_capital) / pico_capital * 100
            if drawdown < max_drawdown:
                max_drawdown = drawdown

            continue

        signal = None
        if adx_actual > ADX_THRESHOLD:
            if not st_anterior and st_actual and precio_actual > ema200:
                signal = 'long'
            elif st_anterior and not st_actual and precio_actual < ema200:
                signal = 'short'

        if signal:
            tipo_pos = signal
            precio_entrada = precio_actual
            notional_target = CAPITAL_RIESGO_USDT * LEVERAGE
            contracts = notional_target / precio_entrada

            if tipo_pos == 'long':
                sl = precio_entrada * (1 - PORCENTAJE_SL)
            else:
                sl = precio_entrada * (1 + PORCENTAJE_SL)
            
            en_posicion = True

    total_trades = len(trades)
    if total_trades == 0:
        return None

    ganadores = [t for t in trades if t['pnl'] > 0]
    perdedores = [t for t in trades if t['pnl'] <= 0]
    
    win_rate = (len(ganadores) / total_trades) * 100
    ganancia_promedio = sum([t['pnl'] for t in ganadores]) / len(ganadores) if ganadores else 0
    perdida_promedio = abs(sum([t['pnl'] for t in perdedores]) / len(perdedores)) if perdedores else 0
    ratio_gp = ganancia_promedio / perdida_promedio if perdida_promedio > 0 else 0
    
    retorno_pct = ((capital - CAPITAL_INICIAL) / CAPITAL_INICIAL) * 100
    net_profit = capital - CAPITAL_INICIAL
    factor_recuperacion = abs(net_profit / (pico_capital * (max_drawdown / 100))) if max_drawdown != 0 else 0

    resultado_final = {
        "simbolo": symbol,
        "periodo_dias": DIAS_HISTORIAL,
        "capital_inicial": CAPITAL_INICIAL,
        "capital_final": round(capital, 2),
        "pnl_total": round(net_profit, 2),
        "retorno_pct": round(retorno_pct, 2),
        "total_trades": total_trades,
        "trades_ganadores": len(ganadores),
        "win_rate": round(win_rate, 2),
        "ganancia_promedio": round(ganancia_promedio, 2),
        "perdida_promedio": round(perdida_promedio, 2),
        "ratio_gp": round(ratio_gp, 2),
        "max_drawdown": round(max_drawdown, 2),
        "factor_recuperacion": round(factor_recuperacion, 2)
    }

    filename = f"backtest_resultado_{symbol.replace('/', '_').replace(':', '_')}.json"
    with open(filename, 'w') as f:
        json.dump(resultado_final, f, indent=4)
    
    print(f"\n=======================================================================")
    print(f"📊 REPORTE 1H DINÁMICO (MAJORS): {symbol}")
    print(f"=======================================================================")
    print(f"Capital Inicial:            ${CAPITAL_INICIAL:,.2f}")
    print(f"Capital Final:              ${capital:,.2f}")
    print(f"PnL Total:                  ${net_profit:,.2f} ({retorno_pct:+.2f}%)")
    print(f"Trades Totales:             {total_trades}")
    print(f"Win Rate:                   {win_rate:.2f}%")
    print(f"Ratio Ganancia/Pérdida:     {ratio_gp:.2f}x")
    print(f"Drawdown Máximo:            {max_drawdown:.2f}%")
    print(f"Factor de Recuperación:     {factor_recuperacion:.2f}x")
    print(f"✓ Guardado en {filename}")
    print(f"=======================================================================\n")

    return resultado_final


def mostrar_resumen_global(resultados):
    print("\n" + "=" * 80)
    print(f"{'RESUMEN GENERAL DE BACKTESTING (DINÁMICO MAJORS 1H)':^80}")
    print("=" * 80)
    print(f"{'Símbolo':<18} | {'Retorno':<10} | {'Win Rate':<10} | {'Trades':<8} | {'Max DD':<8} | {'Ratio G/P':<10}")
    print("-" * 80)
    
    for r in resultados:
        if r:
            print(f"{r['simbolo']:<18} | {r['retorno_pct']:>+8.2f}% | {r['win_rate']:>8.2f}% | {r['total_trades']:>8} | {r['max_drawdown']:>6.2f}% | {r['ratio_gp']:>8.2f}x")
    
    print("=" * 80 + "\n")


if __name__ == '__main__':
    print("🤖 INICIANDO MOTOR DE BACKTESTING (ESTRATEGIA DINÁMICA)\n")
    resultados_globales = []
    for symbol in SIMBOLOS:
        res = ejecutar_backtest(symbol)
        if res:
            resultados_globales.append(res)
    
    if resultados_globales:
        mostrar_resumen_global(resultados_globales)