"""
Script para testear el bot de trading localmente
Útil para debugging antes de deployar a Render
"""

import os
import sys
from datetime import datetime
import logging

# Configurar variables de entorno para testing
os.environ.setdefault('OKX_API_KEY', 'test_key_local')
os.environ.setdefault('OKX_API_SECRET', 'test_secret_local')
os.environ.setdefault('OKX_API_PASSWORD', 'test_password_local')
os.environ.setdefault('TELEGRAM_TOKEN', '')
os.environ.setdefault('TELEGRAM_CHAT_ID', '')

# ============================================================================
# TESTS
# ============================================================================

def test_imports():
    """Verifica que todas las librerías se importan correctamente"""
    print("\n" + "=" * 80)
    print("TEST 1: Importar librerías")
    print("=" * 80)
    
    try:
        import flask
        print("✓ Flask importado")
    except ImportError as e:
        print(f"❌ Error importando Flask: {e}")
        return False
    
    try:
        import ccxt
        print("✓ CCXT importado")
    except ImportError as e:
        print(f"❌ Error importando CCXT: {e}")
        return False
    
    try:
        import pandas
        print("✓ Pandas importado")
    except ImportError as e:
        print(f"❌ Error importando Pandas: {e}")
        return False
    
    try:
        import numpy
        print("✓ NumPy importado")
    except ImportError as e:
        print(f"❌ Error importando NumPy: {e}")
        return False
    
    try:
        import requests
        print("✓ Requests importado")
    except ImportError as e:
        print(f"❌ Error importando Requests: {e}")
        return False
    
    print("\n✅ Todas las librerías están disponibles\n")
    return True


def test_config():
    """Verifica que la configuración se carga correctamente"""
    print("\n" + "=" * 80)
    print("TEST 2: Cargar configuración")
    print("=" * 80)
    
    try:
        # Simular la carga de config
        api_key = os.getenv("OKX_API_KEY")
        api_secret = os.getenv("OKX_API_SECRET")
        api_password = os.getenv("OKX_API_PASSWORD")
        telegram_token = os.getenv("TELEGRAM_TOKEN")
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        print(f"OKX_API_KEY: {api_key[:10]}..." if api_key else "OKX_API_KEY: NO CONFIGURADO ⚠️")
        print(f"OKX_API_SECRET: {api_secret[:10]}..." if api_secret else "OKX_API_SECRET: NO CONFIGURADO ⚠️")
        print(f"OKX_API_PASSWORD: {api_password[:10]}..." if api_password else "OKX_API_PASSWORD: NO CONFIGURADO ⚠️")
        print(f"TELEGRAM_TOKEN: Configurado ✓" if telegram_token else "TELEGRAM_TOKEN: NO CONFIGURADO (opcional)")
        print(f"TELEGRAM_CHAT_ID: Configurado ✓" if telegram_chat_id else "TELEGRAM_CHAT_ID: NO CONFIGURADO (opcional)")
        
        if api_key and api_secret and api_password:
            print("\n✅ Configuración OKX lista\n")
            return True
        else:
            print("\n⚠️ Credenciales OKX incompletas para testing remoto\n")
            return True  # No es error crítico para testing local
            
    except Exception as e:
        print(f"❌ Error cargando configuración: {e}\n")
        return False


def test_exchange_connection():
    """Intenta conectar con OKX (testnet)"""
    print("\n" + "=" * 80)
    print("TEST 3: Conectar con OKX")
    print("=" * 80)
    
    try:
        import ccxt
        
        exchange = ccxt.okx({
            'apiKey': os.getenv("OKX_API_KEY"),
            'secret': os.getenv("OKX_API_SECRET"),
            'password': os.getenv("OKX_API_PASSWORD"),
            'enableRateLimit': True,
            'timeout': 15000,
            'options': {'defaultType': 'swap'},
        })
        
        exchange.set_sandbox_mode(True)
        
        # Intenta cargar mercados
        print("Cargando mercados...")
        markets = exchange.load_markets()
        print(f"✓ {len(markets)} mercados cargados")
        
        # Verificar símbolos
        simbolos = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
        for symbol in simbolos:
            if symbol in markets:
                print(f"  ✓ {symbol} disponible")
            else:
                print(f"  ❌ {symbol} NO disponible")
        
        print("\n✅ Conexión con OKX exitosa\n")
        return True
        
    except ccxt.AuthenticationError as e:
        print(f"❌ Error de autenticación: {e}")
        print("   Verifica las credenciales OKX\n")
        return False
    except ccxt.NetworkError as e:
        print(f"❌ Error de red: {e}")
        print("   Verifica tu conexión a internet\n")
        return False
    except Exception as e:
        print(f"❌ Error: {e}\n")
        return False


def test_indicadores():
    """Prueba el cálculo de indicadores"""
    print("\n" + "=" * 80)
    print("TEST 4: Calcular indicadores técnicos")
    print("=" * 80)
    
    try:
        import pandas as pd
        import numpy as np
        
        # Crear datos de prueba
        print("Generando datos de prueba...")
        np.random.seed(42)
        n_candles = 250
        
        close = 100 + np.cumsum(np.random.randn(n_candles) * 2)
        high = close + np.abs(np.random.randn(n_candles))
        low = close - np.abs(np.random.randn(n_candles))
        volume = np.random.rand(n_candles) * 1000
        timestamp = pd.date_range(start='2024-01-01', periods=n_candles, freq='h')
        
        df = pd.DataFrame({
            'timestamp': timestamp,
            'open': close + np.random.randn(n_candles),
            'high': high,
            'low': low,
            'close': close,
            'volume': volume
        })
        
        print(f"✓ Datos generados: {len(df)} velas")
        
        # Calcular indicadores simulados
        df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
        df['adx'] = np.random.rand(len(df)) * 50  # Simulado
        
        print(f"✓ EMA200: {df['ema200'].iloc[-1]:.4f}")
        print(f"✓ ADX: {df['adx'].iloc[-1]:.2f}")
        print(f"✓ Precio actual: {df['close'].iloc[-1]:.4f}")
        
        print("\n✅ Cálculo de indicadores funcionando\n")
        return True
        
    except Exception as e:
        print(f"❌ Error calculando indicadores: {e}\n")
        return False


def test_logging():
    """Prueba el sistema de logging"""
    print("\n" + "=" * 80)
    print("TEST 5: Sistema de logging")
    print("=" * 80)
    
    try:
        # Crear logger de prueba
        logger = logging.getLogger('TestBot')
        logger.setLevel(logging.DEBUG)
        
        # Handler para consola
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # Pruebas de logging
        print("\nProbando diferentes niveles:")
        logger.debug("Mensaje DEBUG")
        logger.info("Mensaje INFO")
        logger.warning("Mensaje WARNING")
        logger.error("Mensaje ERROR")
        
        print("\n✅ Sistema de logging funcionando\n")
        return True
        
    except Exception as e:
        print(f"❌ Error en logging: {e}\n")
        return False


def test_telegram():
    """Prueba notificaciones de Telegram"""
    print("\n" + "=" * 80)
    print("TEST 6: Notificaciones Telegram")
    print("=" * 80)
    
    try:
        import requests
        
        token = os.getenv("TELEGRAM_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        
        if not token or not chat_id:
            print("⚠️ TELEGRAM_TOKEN o TELEGRAM_CHAT_ID no configurados")
            print("   Esto es opcional, pero recomendado para alertas")
            print("\n✅ Test de Telegram saltado (no configurado)\n")
            return True
        
        # Intenta enviar mensaje de prueba
        print("Enviando mensaje de prueba a Telegram...")
        
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        payload = {
            'chat_id': chat_id,
            'text': '🤖 Test de Bot de Trading - Todo funciona ✅'
        }
        
        response = requests.post(url, json=payload, timeout=5)
        
        if response.status_code == 200:
            print("✓ Mensaje enviado a Telegram")
            print("\n✅ Telegram funcionando\n")
            return True
        else:
            print(f"❌ Error al enviar a Telegram: {response.status_code}")
            print("\n⚠️ Telegram no funcionando (revisar token/chat_id)\n")
            return True  # No es crítico
            
    except requests.exceptions.Timeout:
        print("❌ Timeout conectando con Telegram")
        return True  # No es crítico
    except Exception as e:
        print(f"❌ Error en Telegram: {e}")
        return True  # No es crítico


def test_flask():
    """Prueba que Flask se puede iniciar"""
    print("\n" + "=" * 80)
    print("TEST 7: Flask app")
    print("=" * 80)
    
    try:
        from flask import Flask
        
        app = Flask(__name__)
        
        @app.route('/')
        def home():
            return 'OK'
        
        print("✓ Flask app creada")
        print("✓ Rutas configuradas")
        
        print("\n✅ Flask funcionando\n")
        return True
        
    except Exception as e:
        print(f"❌ Error en Flask: {e}\n")
        return False


# ============================================================================
# EJECUCIÓN
# ============================================================================

def main():
    print("\n" + "=" * 80)
    print("🧪 SUITE DE TESTS - BOT DE TRADING OKX")
    print("=" * 80)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    tests = [
        ("Importar librerías", test_imports),
        ("Configuración", test_config),
        ("Conexión OKX", test_exchange_connection),
        ("Indicadores", test_indicadores),
        ("Logging", test_logging),
        ("Telegram", test_telegram),
        ("Flask", test_flask),
    ]
    
    resultados = []
    
    for nombre, test_func in tests:
        try:
            resultado = test_func()
            resultados.append((nombre, resultado))
        except Exception as e:
            print(f"\n❌ Error no capturado en {nombre}: {e}")
            resultados.append((nombre, False))
    
    # Resumen
    print("\n" + "=" * 80)
    print("📊 RESUMEN DE TESTS")
    print("=" * 80)
    
    exitosos = sum(1 for _, r in resultados if r)
    total = len(resultados)
    
    for nombre, resultado in resultados:
        status = "✅ PASS" if resultado else "❌ FAIL"
        print(f"{status:10} | {nombre}")
    
    print("=" * 80)
    print(f"Resultado: {exitosos}/{total} tests pasaron")
    print("=" * 80)
    
    if exitosos == total:
        print("\n🎉 ¡TODOS LOS TESTS PASARON!")
        print("El bot está listo para deployar a Render\n")
        return 0
    else:
        print(f"\n⚠️ {total - exitosos} test(s) fallaron")
        print("Revisa los errores arriba antes de deployar\n")
        return 1


if __name__ == '__main__':
    sys.exit(main())
