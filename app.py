import os
from flask import Flask

app = Flask(__name__)


@app.route('/')
def home():
  return 'Bot de Trading OKX Testnet en ejecución 24/7 🚀'


if __name__ == '__main__':
  port = int(os.environ.get('PORT', 5000))
  app.run(host='0.0.0.0', port=port)