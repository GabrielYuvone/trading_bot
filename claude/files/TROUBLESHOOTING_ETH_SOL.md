# 🔍 Troubleshooting: Solo BTC Opera, ETH/SOL No Operan

## 📋 Diagnóstico Paso a Paso

### Paso 1: Verificar Logs Recientes

En Render Dashboard → Logs:

```bash
# Busca líneas como:
2024-01-15 | INFO | Bot | ETH/USDT:USDT | Precio: 2345.23 | EMA200: 2300.45 | ADX: 18.2 | ST: ↗️

# Si NO ves linea para ETH o SOL:
# ❌ PROBLEMA: No se están analizando
```

---

## 🔧 Cause #1: Velas Insuficientes

### Síntoma
```
⚠️ ETH/USDT:USDT: Datos insuficientes (150/220)
```

### Solución
```python
# El bot necesita 220 velas de 1h = ~9 días de datos
# Soluciones:

# A) Reducir velas_minimas (TEMPORAL, para testing)
config.velas_minimas = 100  # ⚠️ Indicadores menos precisos

# B) Esperar a que acumule datos (MEJOR)
# Espera 9 días desde que el símbolo se agregó

# C) Aumentar timeframe (SI ES POSIBLE)
config.timeframe = '4h'  # Menos velas necesarias
```

---

## 🔧 Cause #2: ADX Muy Bajo

### Síntoma
```
INFO | Bot | ETH/USDT:USDT | ADX: 12.3 | (abajo del threshold de 22)
```

### Explicación
- ADX < 22 = Mercado sin tendencia fuerte
- Bot NO abre posiciones sin tendencia clara
- **Es intencional**, protege capital

### Soluciones

**A) Reducir ADX Threshold (RIESGOSO)**
```python
config.adx_threshold = 18.0  # En lugar de 22.0
# ⚠️ Abrirá más posiciones pero con tendencias débiles
# Riesgo de SL más frecuentes
```

**B) Esperar a que ADX suba (RECOMENDADO)**
```
Simplemente espera. ADX va y viene con la volatilidad.
Si la moneda entra en tendencia, ADX subirá.
```

**C) Cambiar estrategia**
```python
# En lugar de:
if adx > 22:
    # Aceptar señal

# Podrías usar:
if precio > ema200:  # Solo dirección
    # Aceptar señal (sin ADX)
# ⚠️ Pero es más arriesgado
```

---

## 🔧 Cause #3: Señales pero NO se Detectan

### Síntoma
```
INFO | Bot | ETH/USDT:USDT | ADX: 25.2 | ST: ↗️
# Pero no ves: "🎯 SEÑAL DETECTADA"
```

### Diagnóstico

**El bot busca CAMBIO de dirección:**
```python
# Para LONG:
if (NOT st_anterior AND st_actual AND precio > ema200 AND adx > 22):
    señal = LONG
    
# Para SHORT:
if (st_anterior AND NOT st_actual AND precio < ema200 AND adx > 22):
    señal = SHORT
```

**Esto significa:**
- El SuperTrend debe CAMBIAR en esta vela
- No es suficiente que esté en tendencia
- Necesita cruzarse

### Solución: Monitorear Cambios

```python
# En logs verás:
ST: ↗️  # Está arriba
# Siguiente ciclo:
ST: ↘️  # Cambió a abajo = SEÑAL PARA SHORT

# Si no ve cambios = No hay señales
# Es normal! Las señales son RARAS
```

---

## 🔧 Cause #4: Balance Insuficiente

### Síntoma
```
❌ Balance insuficiente para operar en ETH/USDT:USDT
```

### Solución

```bash
# 1. Ir a testnet OKX
# https://www.okx.com/account/deposits

# 2. Hacer depósito de testnet
# (No usa dinero real, es simulado)

# 3. Distribuir entre símbolos:
# Si tienes 1000 USDT de testnet:
# - 500 para BTC trades
# - 250 para ETH trades
# - 250 para SOL trades
```

---

## 🔧 Cause #5: Configuración de Mercado Falló

### Síntoma
```
❌ Símbolo inválido: ETH/USDT:USDT
# O no ves:
✓ ETH/USDT:USDT: Apalancamiento configurado a 50x
```

### Solución

**A) Verificar símbolo en OKX**
```python
# En Python local:
import ccxt

exchange = ccxt.okx({
    'apiKey': 'tu_key',
    'secret': 'tu_secret',
    'password': 'tu_password',
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})

# Buscar símbolo exacto:
markets = exchange.load_markets()
eth_symbols = [s for s in markets.keys() if 'ETH' in s and 'USDT' in s]
print(eth_symbols)
# Debería mostrar: ['ETH/USDT:USDT', ...]
```

**B) Cambiar símbolo en config (si es necesario)**
```python
config.simbolos = [
    'BTC/USDT:USDT',
    'ETH/USDT:USDT',    # Verifica este formato
    'SOL/USDT:USDT',
]
```

---

## 🔧 Cause #6: Credenciales Expiradas/Revocadas

### Síntoma
```
❌ Error de autenticación OKX
# O en logs:
❌ Conexión OKX fallida
```

### Solución

**PASO 1: Verificar en OKX**
```
1. Ir a: https://www.okx.com/account/api
2. ¿Las credenciales están activas?
3. ¿No fueron revocadas?
4. ¿Tienen permisos correctos?
```

**PASO 2: Crear nuevas credenciales**
```
1. Revocar las credenciales viejas
   (Las que estaban en el código antiguo: 91a74a90-c744...)
2. Crear NUEVAS credenciales
3. Copiar: API Key, API Secret, Passphrase
```

**PASO 3: Actualizar en Render**
```
Render Dashboard → Environment → Editar:
- OKX_API_KEY = nueva key
- OKX_API_SECRET = nuevo secret
- OKX_API_PASSWORD = nueva password
```

**PASO 4: Redeploy**
```
Render detectará cambios en Environment y redeploy automático
O manualmente: Dashboard → Manual Deploy
```

---

## 🔧 Cause #7: Red o Timeout

### Síntoma
```
❌ Error de red al obtener velas de ETH/USDT:USDT
# O:
❌ Timeout al conectar con OKX
```

### Solución

**Es temporal, el bot reintentar automáticamente.**

```python
# El bot continúa en el siguiente ciclo
# El logging lo registra
# No es necesario intervenir
```

Si persiste >1 hora:
```
1. Verificar conexión internet
2. Verificar que OKX esté operativo
3. Revisar logs para patrón
```

---

## 📊 Monitoreo en Tiempo Real

### Crear Script de Monitoreo Local

```python
# monitor.py - Ejecutar en local
import requests
import time
from datetime import datetime

URL = "https://tu-servicio.onrender.com/status"

while True:
    try:
        response = requests.get(URL, timeout=5)
        data = response.json()
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Bot Status:", data)
        time.sleep(60)
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(60)
```

```bash
# Ejecutar:
python monitor.py

# Verás:
# [14:32:15] Bot Status: {
#     'estado': 'activo',
#     'ciclos_ejecutados': 145,
#     'simbolos': ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT'],
#     'timestamp': '2024-01-15T14:32:15.123456'
# }
```

---

## 🎯 Checklist de Debugging

- [ ] **Verificar logs:** ¿Aparecen los 3 símbolos analizados?
- [ ] **Verificar ADX:** ¿ETH/SOL ADX > 22?
- [ ] **Verificar velas:** ¿Tienen >=220 velas?
- [ ] **Verificar balance:** ¿Suficiente USDT de testnet?
- [ ] **Verificar símbolo:** ¿Formato correcto en config?
- [ ] **Verificar credenciales:** ¿No fueron revocadas?
- [ ] **Verificar red:** ¿OKX está operativo?
- [ ] **Verificar cambios:** ¿SuperTrend cambió en esta vela?

---

## 📝 Cómo Compartir Logs para Ayuda

Si necesitas ayuda, proporciona:

```
1. Últimas 50 líneas de bot_trading.log
2. Captura de /status endpoint
3. Qué símbolo no opera (BTC, ETH, SOL)
4. Cuándo fue la última operación exitosa
5. Mensajes de error específicos
```

**Ejemplo:**
```
Bot corría bien desde: 2024-01-10
Última operación: BTC hace 1 hora
ETH/SOL: No operan desde hace 1 semana
Error en logs: "❌ Error al descargar velas para ETH"
```

---

## ✅ Señales de Que Todo Funciona Bien

```
✓ Ves líneas INFO para BTC, ETH, SOL cada ciclo
✓ ADX varía entre ciclos (subes/bajas)
✓ Ocasionalmente ves "🎯 SEÑAL DETECTADA"
✓ Ves "🟢 NUEVA OPERACIÓN" o "🔴 POSICIÓN CERRADA"
✓ Telegram recibe notificaciones
✓ /status endpoint responde correctamente
✓ Ciclos ejecutados incrementa cada minuto
```

---

## 🚨 Errores Críticos (Rara Vez Ocurren)

```
❌ CREDENCIALES OKX NO CONFIGURADAS
  → Falta variable de entorno

❌ Error de autenticación OKX
  → Credenciales incorrectas o revocadas

❌ Conexión OKX exitosa (... mercados cargados)
  → Este NO es error, es exitoso
```

---

**Última actualización:** Septiembre 2026
**Para problemas persistentes:** Revisa el archivo de log completo
