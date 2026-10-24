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

# Pruebas
python -m unittest discover -s tests -v
```

Todos los umbrales se pueden cambiar sin tocar el código con `--params '{"slope_min_pct": 0.15, "allow_short": false}'`.

Nota: `api.binance.com` responde HTTP 451 desde algunos países (p. ej. EE. UU.); por eso el fetcher prueba después el espejo público `data-api.binance.vision` y Bybit.
