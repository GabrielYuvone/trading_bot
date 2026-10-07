# Estrategia + diagnóstico: por qué el bot dejó de entrar y qué corregimos

**Fecha:** 6 de octubre de 2026
**Datos usados:** 4.320 velas horarias por símbolo (12-mar-2026 → 08-sep-2026), 5 perpetuos
USDT de OKX: BTC, ETH, SOL, XRP, DOGE. Todo el análisis es reproducible y está en `research/`.

---

## 1. Resumen ejecutivo

| | Antes (v2) | Ahora (v3) |
|---|---|---|
| Señales ejecutadas en 6 meses (backtest, mismos datos) | ~70 | **124** |
| Expectativa por trade | ~0,07 R | **+0,32 R** |
| Resultado 6 meses (riesgo 0,75 %/trade) | **−0,5 %** | **+28 %** |
| Drawdown máximo | −8,8 % | −15,6 % |
| Frecuencia | 2,7 señales/semana | **4,8 señales/semana** |

Los tres cambios que explican la diferencia, en orden de impacto:

1. **Se eliminó el take-profit fijo de 2R.** Era el destructor de la estrategia:
   con TP 2R el sistema queda en −0,5 %; sin TP, +28 %. El sistema es de
   tendencia: necesita que las ganancias corran (ver §4).
2. **Reversión en la misma vela.** Cuando el SuperTrend giraba, el bot cerraba
   la posición pero no abría la contraria hasta la vela siguiente, y para
   entonces la señal ya no existía. En la muestra, eso solo se comía el mejor
   trade del semestre (+27,9 R).
3. **Parámetros recalibrados:** SuperTrend(10; 3,0) → **(24; 3,5)** y
   umbral ADX 22 → **20**. Elegido por estar en el centro de una meseta
   (33 de 36 combinaciones vecinas son rentables), no por ser el mejor número.

Además se arreglaron **5 fallas operativas** que podían dejar al bot "vivo pero
sin operar" durante semanas (§3).

---

## 2. Estrategia elegida (la que queda operando)

**Tendencia con stops ajustados, en velas de 1 hora, 5 perpetuos.**

```
LARGO   cuando el SuperTrend(24; 3,5) gira a alcista
        y el cierre de la vela > EMA200  y  ADX(14) > 20
CORTO   igual y simétrico (cierre < EMA200)

STOP    la banda contraria del SuperTrend en la vela de la señal (~2-3 %)
SALIDA  giro del SuperTrend en contra → se cierra y se revierte
        o salta el stop en el exchange
SIN TAKE PROFIT

TAMAÑO  riesgo fijo del 0,75 % del equity por operación
        contratos = riesgo / (distancia al stop + comisiones)
Tope    máximo 4 posiciones simultáneas y límite de pérdida diaria de 3R
```

* Por qué 1 hora y no 15 m/30 m: con los mismos parámetros, 15 m da **−44 %** y
  30 m **−12 %**; el ruido intradía se come la señal después de comisiones.
* Por qué no media móvil, Donchian, momentum o reversión a la media: se
  testearon las 8 alternativas con los mismos costos y **ninguna ganó dinero**
  en esta muestra (tabla en §4).
* Por qué se mantienen los cortos: en el semestre empataron (−0,03 R contra
  +0,65 R de los largos), pero el periodo fue alcista. Son la cobertura cuando
  el régimen cambie. Se pueden apagar con `PERMITIR_CORTOS=0`.

### Resultado medido (costos reales: 5 bp taker + 3 bp slippage + funding)

| Métrica | Valor |
|---|---|
| Retorno 6 meses | **+28,2 %** (CAGR 66 %) |
| Drawdown máximo | −15,6 % |
| Sharpe | 1,87 |
| Trades | 124 (4,8/semana) |
| Aciertos | 39,5 % |
| Expectativa | **+0,35 R** por trade |
| Profit factor | 1,82 |
| Duración media | 53 horas |

**Validación, no curve-fitting:**

* **Vecindario:** 33 de 36 combinaciones de (periodo × multiplicador × ADX)
  alrededor de la elegida son rentables; mediana +13,5 %. Es una meseta, no un pico.
* **Walk-forward (60 % in-sample / 40 % out-of-sample):** IS +4,4 %, OOS +21,4 %.
  Aviso: el periodo marzo–junio fue lateral y **todas** las variantes dieron ~0;
  la ganancia se concentra en julio–septiembre. El sistema depende del régimen.
* **Leave-one-out:** quitando cualquier símbolo el resultado sigue positivo
  (+10 % a +36 %). No es un solo activo el que sostiene la estrategia.
* **Costos:** incluso asumiendo 10 bp de comisión, 6 bp de slippage y el doble
  de funding, sigue en +10 %.
* **Bootstrap (20.000 remuestreos):** P(expectativa > 0) = 93 %,
  IC 95 % de la expectativa entre −0,09 R y +0,91 R.
* **Concentración — leer esto:** el mejor trade aporta el 48 % del resultado y
  los 3 mejores, el 109 %. La mediana de los trades es −0,23 R. Es normal en
  seguimiento de tendencia, pero significa que **perderse pocos trades grandes
  cambia mucho el año**. Por eso el bot no debe perder señales (§3) y por eso
  el tope de posiciones se subió a 4.

---

## 3. Por qué hacía 2 semanas que no entraba

No hay una sola causa: hay defectos que producen ese síntoma. Con los datos
históricos la estrategia anterior generaba ~2,7 señales/semana, así que dos
semanas en blanco tenían ~0,4 % de probabilidad por azar. Algo se estaba
perdiendo. Esto es lo que estaba roto:

1. **La señal se marcaba como "procesada" ANTES de intentar la entrada.**
   Cualquier fallo transitorio (red, sin fill, rechazo de la API) consumía la
   señal para siempre, y el giro del SuperTrend sólo existe en esa vela.
   *Arreglado:* si el fallo es transitorio la señal queda pendiente y se
   reintenta hasta 8 ciclos mientras la vela siga vigente; si se agota, avisa.
2. **Un fallo al arrancar dejaba el health-check verde con el bot muerto.**
   Si `TradingBot()` levantaba una excepción, el proceso se quedaba en un
   `while True: sleep(60)` sirviendo Flask: Render lo veía "vivo" y no operaba
   nunca. *Arreglado:* reintento cada 5 minutos con alerta y el error visible
   en `/status`.
3. **Fallo silencioso de la API.** Si `fetch_positions` o el balance fallaban,
   el ciclo se abortaba en silencio (sólo en el log). *Arreglado:* contador de
   fallos consecutivos, alerta a Telegram a los 3, 10 y 30 fallos, y el bot
   deja de operar en lugar de operar a ciegas.
4. **Capital insuficiente: el tamaño redondeaba a cero y el símbolo jamás
   operaba.** Con equity de 1.000 USDT y un stop lejano, XRP y BTC quedaban
   por debajo del mínimo de 1 contrato. *Arreglado:* el pre-flight de arranque
   calcula el tamaño sugerido por símbolo y avisa si redondea a cero.
5. **Reversión perdida** (§2, punto 2) y **señal comida por el tope de
   posiciones**.

Y lo más importante para el futuro: **ahora el bot te dice por qué no entra.**

```
GET /por_que_no_opero      → motivos acumulados, en texto
GET /status                → lo mismo en JSON + salud de la API
Telegram cada 6 h          → latido: ciclos, posiciones, señales, bloqueos
```

---

## 4. Evidencia: las alternativas que se descartaron

Mismos datos, mismos costos, mismo motor, portafolio de 5 símbolos, riesgo
0,75 %, máximo 3 posiciones, 180 días:

| Estrategia | Retorno | Max DD | Sharpe | Trades | Expectativa |
|---|---|---|---|---|---|
| **ST(24;3,5)+EMA200+ADX>20 sin TP (elegida)** | **+28,2 %** | −15,6 % | 1,87 | 124 | +0,35 R |
| ST(10;3)+EMA200+ADX>22 **con TP 2R** (la de producción) | −0,5 % | −8,8 % | −0,05 | 70 | +0,07 R |
| ST(10;3)+EMA200+ADX>22 sin TP | +13,5 % | −18,0 % | 1,27 | 91 | +0,30 R |
| SuperTrend solo (sin filtros) | −28,6 % | −37,3 % | −2,57 | 321 | −0,11 R |
| SuperTrend + ADX (sin EMA200) | −18,7 % | −26,5 % | −2,20 | 179 | −0,11 R |
| SuperTrend pullback (continuación) | −24,4 % | −31,0 % | −2,01 | 209 | −0,09 R |
| Breakout de Donchian 48 h | −23,8 % | −49,9 % | −1,14 | 287 | −0,03 R |
| Cruce de medias 50/200 | −11,2 % | −40,8 % | −0,43 | 321 | +0,02 R |
| Momentum 14 días | −1,0 % | −21,2 % | 0,03 | 98 | +0,03 R |
| Reversión a la media (Bollinger+RSI) | −55,6 % | −57,2 % | −7,06 | 268 | −0,33 R |

Conclusiones que se desprenden de la tabla:

* **Los filtros (ADX y EMA200) no son decorativos:** sacarlos convierte un
  sistema rentable en uno que pierde −19 % a −29 %.
* **El TP fijo es el error más caro:** recorta justo la cola derecha que paga
  las pérdidas. TP 2R → −0,5 %; sin TP → +28 %.
* **Más trades no es mejor:** las variantes sin filtros operan 3-4 veces más y
  pierden más. El edge está en la selectividad.

---

## 5. Configuración para producción

Variables que conviene fijar en Render (valores por defecto entre paréntesis):

```
OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSWORD     (obligatorias)
TELEGRAM_TOKEN / TELEGRAM_CHAT_ID                   (recomendadas)
OKX_SANDBOX=1            # 1 = demo. Pasar a REAL sólo con 0 y a conciencia
SIMBOLOS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,DOGE/USDT:USDT
FRACCION_EQUITY=0.0075   # 0,75 % de riesgo por trade
MAX_POSICIONES=4
LIMITE_DIARIO_R=3
OKX_LEVERAGE=10
PERMITIR_CORTOS=1
USAR_TP=0                # Dejar en 0: es lo que destruye la expectativa
HEARTBEAT_HORAS=6
REPORTES_HORAS=9,13,17,21
LOGS_TOKEN=...           # habilita /logs
```

Perfiles de riesgo (medidos en la muestra, cuenta de 1.000 USDT):

| Perfil | FRACCION_EQUITY | MAX_POSICIONES | Retorno 6 m | Max DD |
|---|---|---|---|---|
| Conservador | 0,005 | 3 | +19,7 % | −10,3 % |
| **Estándar (recomendado)** | **0,0075** | **4** | **+29,3 %** | **−17,0 %** |
| Agresivo | 0,015 | 4 | +51,9 % | −29,3 % |

Ojo: el drawdown real puede ser bastante peor que el de la muestra (el periodo
fue mayormente favorable). Con dinero real conviene arrancar en el perfil
conservador y subir el riesgo sólo después de algunas semanas de operaciones
reales que confirmen que la ejecución coincide con el backtest.

---

## 6. Cómo reproducir los números

```bash
pip install pandas numpy ccxt
python3 research/diagnostico.py      # por qué no entraba: filtro por filtro
python3 research/comparar.py         # las 9 estrategias candidatas
python3 research/barrido.py          # TP, SuperTrend, ADX, largos vs cortos
python3 research/robustez.py         # vecindario de parámetros y costos
python3 research/validacion.py       # bootstrap, leave-one-out, timeframes
python3 research/verificar_bot.py    # ¿el bot hace lo que dice el backtest?
python3 research/test_integracion.py # bot real contra un exchange simulado
```

Los CSV históricos están en la raíz del repo. Para revalidar con datos frescos:
bajar velas nuevas de OKX y reemplazar los `historial_*.csv`.

**Nota de honestidad:** todo esto se midió sobre 6 meses y un solo régimen de
mercado. La expectativa es positiva con un 93 % de confianza, no con certeza.
Los primeros 2-3 meses de operación real son la validación que falta.
