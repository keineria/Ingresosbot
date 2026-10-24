# Ingresosbot
Bots de Traiding

## Núcleo cuantitativo (3 capas)

Motor offline (solo Python + pandas + numpy, sin APIs externas) que evalúa cada vela en este orden:

| Capa | Skill | Spec | Qué decide |
|---|---|---|---|
| 1 | postmortem-circuit-breaker | [SKILL_3](skills/SKILL_3_postmortem_circuit_breaker.md) | ACTIVE / THROTTLED / HALTED (si HALTED, aborta) |
| 2 | market-regime-quant | [SKILL_1](skills/SKILL_1_market_regime_quant.md) | Régimen y señal LONG / SHORT / WAIT |
| 3 | atr-risk-guardian | [SKILL_2](skills/SKILL_2_atr_risk_guardian.md) | SL, TP, Break-Even y tamaño |

## Módulos

| Archivo | Qué hace |
|---|---|
| `quant_core_engine.py` | Motor de 3 capas: indicadores, régimen, riesgo y JSON de la orden |
| `state_manager.py` | Estado persistente en SQLite (`data/trading_state.db`): cuenta, posiciones abiertas, historial; Break-Even automático |
| `market_fetcher.py` | Velas públicas sin API key: Binance → data-api.binance.vision → Bybit → CSV local (`data/candles/`) |
| `backtester.py` | Backtest vela a vela con engine + state_manager, comisión 0.08% por orden |
| `bot_runner.py` | Bot 24/7 en paper trading: ciclo al cierre de cada vela + vigilancia de precio cada 15 s |
| `notifier.py` | Alertas a webhook (n8n) y Telegram: apertura, Break-Even, cierre, cambios del Circuit Breaker y kill switch |
| `web_server.py` + `static/index.html` | Panel dark mode y API de control (`/api/state`, `/api/scan-now`, `/api/kill-switch`) en 127.0.0.1:8050 |

```bash
pip install pandas numpy

# Verificación con los 3 escenarios (Long válido, lateral, Circuit Breaker)
python quant_core_engine.py --demo

# Velas reales públicas (se guardan en data/candles/BTCUSDT_1h.csv para usarlas sin red)
python market_fetcher.py --symbol BTCUSDT --interval 1h --limit 1000

# Decisión con velas descargadas y estado de la cuenta guardado en SQLite
python quant_core_engine.py --fetch BTCUSDT --interval 1h --db data/trading_state.db

# CSV exportado de TradingView o Binance
python quant_core_engine.py --csv BTCUSDT_1h.csv --state examples/account_state.json --symbol BTC/USDT --timeframe 1h

# Indicadores leídos a mano del gráfico
python quant_core_engine.py --manual examples/manual_input.json --balance 10000 --losses 2

# Solo JSON (para webhook n8n / CCXT / Alpaca)
python quant_core_engine.py --csv velas.csv --balance 10000 --json-only

# Backtest (desde CSV o descargando ~1 año de velas 1h) con reportes en Markdown y JSON
python backtester.py --csv data/candles/BTCUSDT_1h.csv --out-md reporte.md --out-json reporte.json
python backtester.py --symbol BTCUSDT --interval 1h --limit 8760 --trades-out trades.csv

# Futuros x2: el tope del 85% escala a 170% para llegar al 1.5% de riesgo exacto
python backtester.py --csv data/candles/BTCUSDT_1h.csv --params '{"max_leverage": 2.0}'

# Estado guardado del bot
python state_manager.py --db data/trading_state.db

# Bot 24/7 en paper trading (una pasada de prueba con --once)
cp .env.example .env            # rellena WEBHOOK_URL y/o TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
python notifier.py --test       # comprueba que llegan las alertas
python bot_runner.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --interval 1h --once
python bot_runner.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --interval 4h --log-file logs/bot.log

# Pruebas
python -m unittest discover -s tests -v
```

### Bot 24/7 en un VPS (AlmaLinux 9 + CyberPanel 3.0)

```bash
sudo git clone https://github.com/keineria/Ingresosbot.git /opt/Ingresosbot
cd /opt/Ingresosbot && sudo bash deploy/install_almalinux.sh
```

El instalador crea el usuario de servicio `ingresosbot`, el `.venv` con pandas y numpy, el `.env` (con una `DASHBOARD_ACCESS_KEY` aleatoria) y deja activos `ingresosbot.service` (bot) e `ingresosbot-web.service` (panel). Para publicar el panel con SSL sin abrir puertos sigue [deploy/CYBERPANEL_SETUP.md](deploy/CYBERPANEL_SETUP.md). Para actualizar: `sudo git -C /opt/Ingresosbot pull && sudo bash deploy/install_almalinux.sh`.

Toda la configuración vive en `.env` (`BOT_SYMBOLS`, `BOT_INTERVAL`, `BOT_PARAMS`, `WEB_PORT`, alertas...), y la leen tanto el bot como el panel.

**Kill switch**: desde el panel (o `POST /api/kill-switch`) pone la cuenta en `HALTED` al instante; el bot, que es otro proceso, lo ve en su siguiente ciclo porque el interruptor vive en SQLite. Las posiciones abiertas siguen protegidas por su SL/TP. Al desactivarlo, el estado vuelve a lo que digan las reglas del Circuit Breaker: si había 3 pérdidas o DD ≥ 3%, sigue en `HALTED` hasta terminar el enfriamiento.

Cómo se comporta:
- **Una posición por par** y nunca más capital comprometido que `85% × max_leverage` del balance entre todos los pares: si la primera señal ya usa el tope, las demás se omiten (`SKIPPED`).
- **Nunca opera con datos viejos**: si la última vela descargada no es la que acaba de cerrar (por ejemplo, sin red y con un CSV antiguo), el par queda en `STALE_DATA`.
- **El enfriamiento cuenta velas, no pares**: con 3 pares, cada cierre de vela descuenta 1. Si el VPS estuvo apagado, al volver descuenta las velas perdidas.
- **Precio de entrada** = precio en vivo al cerrar la vela; si no hay precio o se aleja más de 5% del cierre, se usa el cierre.
- El estado vive en `data/trading_state.db`: reiniciar el bot no pierde posiciones ni rachas.

Todos los umbrales se pueden cambiar sin tocar el código con `--params '{"slope_min_pct": 0.15, "allow_short": false}'`.

Nota: `api.binance.com` responde HTTP 451 desde algunos países (p. ej. EE. UU.); por eso el fetcher prueba después el espejo público `data-api.binance.vision` y Bybit.
