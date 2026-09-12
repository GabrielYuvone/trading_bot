# 🤖 Guía de Deployment - Bot Trading OKX Refactorizado

## 📋 Requisitos Previos

1. Cuenta OKX con API Keys (Testnet)
2. Bot de Telegram con Token y Chat ID
3. Render.com account
4. Git configurado localmente

---

## 🔐 Variables de Entorno

### En Render Dashboard

Debes establecer las siguientes variables de entorno en tu servicio:

```
OKX_API_KEY=tu_api_key_aqui
OKX_API_SECRET=tu_api_secret_aqui
OKX_API_PASSWORD=tu_api_password_aqui
TELEGRAM_TOKEN=tu_telegram_token_aqui
TELEGRAM_CHAT_ID=tu_chat_id_aqui
PORT=5000
```

**⚠️ IMPORTANTE:** 
- Usa **variables de entorno**, nunca hardcodees las credenciales
- En Render: Environment → Add Environment Variable
- Las credenciales del código anterior eran visibles a todos

### Cómo obtener credenciales:

#### OKX API:
1. Ir a: https://www.okx.com/account/api
2. Crear nueva API Key
3. Copiar: Key, Secret, Passphrase
4. ⚠️ IMPORTANTE: Revocar las credenciales antiguas que estaban en el código

#### Telegram:
1. Hablar con @BotFather en Telegram
2. Crear bot nuevo → copiar TOKEN
3. Hablar con @userinfobot → obtener CHAT_ID
4. O enviar mensaje al bot y obtener chat_id de logs

---

## 📦 Estructura de Archivos

```
tu-repo/
├── bot_trading_refactored.py   # Código principal del bot
├── requirements.txt             # Dependencias Python
├── Procfile                      # Configuración para Render
├── runtime.txt                   # Versión de Python
├── .gitignore                    # Archivos a ignorar
└── bot_trading.log              # Se genera automáticamente
```

---

## 📝 Archivos Necesarios

### 1. `requirements.txt`

```
Flask==3.0.0
ccxt==4.1.14
pandas==2.1.1
numpy==1.24.3
requests==2.31.0
python-dotenv==1.0.0
```

### 2. `Procfile`

```
web: python bot_trading_refactored.py
```

### 3. `runtime.txt`

```
python-3.11.6
```

### 4. `.gitignore`

```
*.log
*.pyc
__pycache__/
.env
.DS_Store
venv/
*.egg-info/
dist/
build/
```

---

## 🚀 Steps de Deployment en Render

### 1. Preparar el repositorio

```bash
# Crear nuevo repo o usar uno existente
cd tu-proyecto
git init
git add .
git commit -m "Bot trading refactorizado"
git push origin main
```

### 2. En Render Dashboard

1. **Crear nuevo Web Service**
   - Dashboard → New → Web Service
   - Conectar repositorio GitHub
   - Elegir rama (main)

2. **Configurar servicio**
   - **Name:** trading-bot-okx
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot_trading_refactored.py`
   - **Instance Type:** Starter (gratis)

3. **Variables de Entorno**
   - Environment → Add Environment Variable
   - Agregar todas las variables listadas arriba

4. **Deploy**
   - Click "Deploy"
   - Render compilará e iniciará el bot automáticamente

### 3. Verificar estado

```bash
# En el navegador
https://tu-servicio.onrender.com/
# Debería mostrar el estado del bot

# Endpoint de status (JSON)
https://tu-servicio.onrender.com/status
```

---

## 📊 Monitoreo

### Logs en Render

1. Dashboard → Seleccionar servicio
2. Logs → Ver logs en vivo
3. Buscar líneas con:
   - `✓` = Operación exitosa
   - `❌` = Error
   - `🎯` = Señal detectada
   - `🟢` = Posición abierta
   - `🔴` = Posición cerrada

### Logs Locales

El bot también crea `bot_trading.log`:
```bash
tail -f bot_trading.log
```

---

## 🔧 Troubleshooting

### "Solo BTC ejecuta, ETH/SOL no"

**Causas posibles:**

1. **Credenciales de API expiradas**
   ```bash
   # Solución: Revoca las credenciales antiguas en OKX
   # Crea nuevas y actualiza variables en Render
   ```

2. **Símbolos mal configurados**
   ```python
   # Verificar en Render logs si aparece:
   # "Error: Bad Symbol"
   # Solución: Cambia en el código a símbolos válidos
   ```

3. **ADX muy bajo**
   ```
   # Si ves en logs: "ADX: 15.2" (< 22)
   # El bot no abre posiciones sin tendencia fuerte
   # Es normal, espera a que suba ADX
   ```

4. **Balance insuficiente**
   ```
   # Logs: "Balance insuficiente para operar"
   # Solución: Agregar fondos al testnet de OKX
   ```

### Bot no se inicia

1. **Revisar Environment Variables**
   - ¿Todas están configuradas?
   - ¿Sin espacios extras?

2. **Ver logs de build**
   - Dashboard → Build Logs
   - Buscar errores de dependencias

3. **Probar localmente**
   ```bash
   python bot_trading_refactored.py
   # Debería mostrar logs y conectar
   ```

### Telegram no recibe mensajes

```
# Verificar en logs:
"Telegram deshabilitado, mensaje no enviado"

# Soluciones:
1. Verificar TELEGRAM_TOKEN y TELEGRAM_CHAT_ID
2. El bot debe tener permiso de escribir en el chat
3. No envía si está deshabilitado (solo debug)
```

---

## 📈 Mejoras Futuras

- [ ] Dashboard web con estadísticas en tiempo real
- [ ] Base de datos para historial de trades
- [ ] API REST para control remoto
- [ ] Backtesting de estrategia
- [ ] Múltiples estrategias paralelizadas
- [ ] Alertas avanzadas (Discord, Email)

---

## 🎯 Monitoreo Recomendado

### Daily Checklist

- [ ] Bot está activo en Render (`/status`)
- [ ] Revisó logs últimas 24 horas
- [ ] Telegram recibe notificaciones
- [ ] Balance OKX está disponible
- [ ] ADX de símbolos es monitoreado

### Alertas a Revisar

- `❌ Error de autenticación` → Credenciales expiradas
- `❌ Error de red` → Problema de conexión
- `⚠️ Balance insuficiente` → Agregar fondos
- `❌ Símbolo inválido` → Configuración incorrecta

---

## 📞 Soporte

Si el bot falla:

1. Revisa `bot_trading.log`
2. Busca líneas con `❌` o `ERROR`
3. Copias el traceback completo
4. Verifica variables de entorno

---

**Última actualización:** Septiembre 2026
**Versión:** 2.0 Refactorizada
**Estado:** Producción ✅
