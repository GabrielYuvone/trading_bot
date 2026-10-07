# research/ — análisis que respalda la estrategia

Todo lo que está en `ESTRATEGIA.md` sale de acá. No requiere conexión a
Internet: trabaja con los `historial_*.csv` de la raíz del repo
(4.320 velas horarias por símbolo, 12-mar-2026 → 08-sep-2026, 5 perpetuos).

```bash
pip install pandas numpy ccxt flask requests
```

| Script | Qué responde |
|---|---|
| `datos.py` | Carga de CSV + indicadores (SuperTrend, ADX/ATR Wilder, Donchian, Bollinger, RSI) y especificación de contratos OKX |
| `motor.py` | Motor de backtest event-driven: entrada en la apertura siguiente, stop intra-vela, comisiones + slippage + funding, tamaño por riesgo, portafolio multi-símbolo |
| `estrategias.py` | Las 9 estrategias candidatas |
| `diagnostico.py` | Por qué el bot de producción no entraba: réplica exacta de su lógica, contando en qué filtro muere cada señal |
| `comparar.py` | Tabla comparativa de candidatas |
| `barrido.py` | Sensibilidad a TP, parámetros del SuperTrend, ADX, trailing, largos vs cortos |
| `ajuste_fino.py` | Break-even, tiempo máximo, riesgo por trade, nº de posiciones |
| `robustez.py` | Vecindario de parámetros, sensibilidad a costos, contexto de mercado |
| `validacion.py` | Concentración, bootstrap, leave-one-out, otros timeframes |
| `verificar_bot.py` | Compara las clases REALES del bot (`TechnicalAnalyzer`) contra el backtest |
| `test_integracion.py` | Corre el bot completo contra un exchange simulado que reproduce las velas |

## Supuestos del motor (todos conservadores)

* Entrada en la **apertura de la vela siguiente** a la señal + 3 bp de slippage.
* Stop evaluado intra-vela (high/low); si una vela toca SL y TP, gana el SL.
* Comisión taker 5 bp por lado, funding 1 bp cada 8 h contra la posición.
* Tamaño = riesgo fijo / distancia al stop, redondeado al `lotSz` de OKX,
  mínimo 1 contrato, tope de apalancamiento 10x.
* Portafolio con equity común y tope de posiciones simultáneas.

## Resultado principal

`ST(24; 3,5) + EMA200 + ADX>20 · sin take-profit · riesgo 0,75 % · máx 4 posiciones`

```
Retorno 6 meses  +28,2 %   (CAGR 66 %)
Max drawdown     -15,6 %
Sharpe            1,87
Trades            124 (4,8/semana) · aciertos 39,5 % · expectativa +0,35 R
Profit factor     1,82
```

Validaciones: 33/36 combinaciones vecinas rentables · walk-forward IS +4,4 % /
OOS +21,4 % · leave-one-out todo positivo · P(expectativa>0) = 93 % (bootstrap)
· sigue en +10 % con costos duplicados.

## Limitaciones

* 180 días y un solo régimen de mercado.
* El resultado depende de pocos trades muy grandes (el mejor aporta el 48 %).
* Los símbolos son 5 perpetuos muy correlacionados: en la práctica es casi una
  sola apuesta direccional.
