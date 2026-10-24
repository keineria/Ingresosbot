# Ingresosbot
Bots de Traiding

## Núcleo cuantitativo (3 capas)

Motor offline (solo Python + pandas + numpy, sin APIs externas) que evalúa cada vela en este orden:

| Capa | Skill | Spec | Qué decide |
|---|---|---|---|
| 1 | postmortem-circuit-breaker | [SKILL_3](skills/SKILL_3_postmortem_circuit_breaker.md) | ACTIVE / THROTTLED / HALTED (si HALTED, aborta) |
| 2 | market-regime-quant | [SKILL_1](skills/SKILL_1_market_regime_quant.md) | Régimen y señal LONG / SHORT / WAIT |
| 3 | atr-risk-guardian | [SKILL_2](skills/SKILL_2_atr_risk_guardian.md) | SL, TP, Break-Even y tamaño |

```bash
pip install pandas numpy

# Verificación con los 3 escenarios (Long válido, lateral, Circuit Breaker)
python quant_core_engine.py --demo

# CSV exportado de TradingView o Binance
python quant_core_engine.py --csv BTCUSDT_1h.csv --state examples/account_state.json --symbol BTC/USDT --timeframe 1h

# Indicadores leídos a mano del gráfico
python quant_core_engine.py --manual examples/manual_input.json --balance 10000 --losses 2

# Solo JSON (para webhook n8n / CCXT / Alpaca)
python quant_core_engine.py --csv velas.csv --balance 10000 --json-only

# Pruebas
python -m unittest discover -s tests -v
```

Todos los umbrales se pueden cambiar sin tocar el código con `--params '{"slope_min_pct": 0.15, "allow_short": false}'`.
