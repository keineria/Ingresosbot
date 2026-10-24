
## ESCENARIO 1 — Long válido (cuenta sana, tendencia alcista)

| Campo | Valor |
|---|---|
| Símbolo / TF | BTC/USDT / 1h |
| DECISIÓN | **LONG** |
| Capa 1 · Circuit Breaker | ACTIVE (riesgo 1.5%) — sin rachas ni drawdown relevantes |
| Capa 2 · Régimen | BULL_EXPANSION → LONG |
| Close / EMA20 / EMA50 / EMA200 | 72551.12 / 72092.30 / 71904.32 / 69126.24 |
| Pendiente EMA20 (3v) / RSI14 | +0.340% / 54.95 |
| ATR14 / Extensión | 580.25 / +0.79 ATR |
| Capa 3 · Entrada / SL / TP | 72551.12 / 71622.73 / 74640.00 |
| Break-Even (trigger → nuevo SL) | 73421.50 → 72696.23 |
| Tamaño / Cantidad | 8499.94 USD / 0.117158 (tope 85%) |
| Riesgo efectivo / R:R | 108.77 USD (1.088%) / 2.25 |

```json
{
  "engine": "quant_core_engine",
  "version": "1.0.0",
  "generated_at_utc": "2026-10-24T02:54:54+00:00",
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "candle_time": "2026-01-21T19:00:00+00:00",
  "input_mode": "ohlcv",
  "decision": "LONG",
  "warnings": [],
  "layer_1_circuit_breaker": {
    "status": "ACTIVE",
    "risk_throttle": false,
    "risk_pct": 1.5,
    "cooldown_candles_remaining": 0,
    "consecutive_losses": 0,
    "daily_drawdown_pct": 0.0,
    "reasons": [
      "sin rachas ni drawdown relevantes"
    ],
    "abort_analysis": false
  },
  "layer_2_market_regime": {
    "evaluated": true,
    "regime": "BULL_EXPANSION",
    "signal": "LONG",
    "indicators": {
      "close": 72551.12417355,
      "ema20": 72092.30038887,
      "ema50": 71904.32411167,
      "ema200": 69126.23576876,
      "ema20_slope_3c_pct": 0.3401,
      "rsi14": 54.9523,
      "atr14": 580.24583974,
      "extension_atr": 0.7907
    },
    "long_checks": {
      "macro_close_gt_ema200": true,
      "structure_ema20_gt_ema50": true,
      "slope_ema20_gt_+0.20%": true,
      "rsi_in_46_64": true,
      "extension_le_3.2ATR": true
    },
    "short_checks": {
      "macro_close_lt_ema200": false,
      "structure_ema20_lt_ema50": false,
      "slope_ema20_lt_-0.20%": false,
      "rsi_in_36_54": false,
      "extension_le_3.2ATR": true,
      "short_enabled": true
    },
    "blocking_filters": []
  },
  "layer_3_atr_risk_guardian": {
    "evaluated": true,
    "side": "LONG",
    "entry_price": 72551.12417355,
    "atr14": 580.24583974,
    "stop_loss": 71622.73,
    "take_profit": 74640.0,
    "break_even_trigger": 73421.5,
    "break_even_new_sl": 72696.23,
    "sl_distance_pct": 1.2796,
    "risk_pct_target": 1.5,
    "risk_usd_target": 150.0,
    "position_size_by_risk_usd": 11722.03,
    "position_cap_usd": 8500.0,
    "capped_by_max_allocation": true,
    "position_size_usd": 8499.94,
    "quantity": 0.117158,
    "risk_usd_effective": 108.77,
    "risk_pct_effective": 1.0877,
    "reward_usd": 244.73,
    "reward_risk_ratio": 2.25,
    "below_min_notional": false
  },
  "order": {
    "symbol": "BTC/USDT",
    "side": "buy",
    "type": "market",
    "amount": 0.117158,
    "notional_usd": 8499.94,
    "reference_price": 72551.12417355,
    "stop_loss": {
      "type": "stop_market",
      "price": 71622.73
    },
    "take_profit": {
      "type": "limit",
      "price": 74640.0
    },
    "break_even": {
      "trigger_price": 73421.5,
      "new_stop_loss": 72696.23
    },
    "risk_usd": 108.77,
    "risk_mode": "ACTIVE",
    "client_tag": "qce-bull_expansion"
  }
}
```

## ESCENARIO 2 — Mercado lateral bloqueado por régimen

| Campo | Valor |
|---|---|
| Símbolo / TF | BTC/USDT / 1h |
| DECISIÓN | **WAIT / NO_TRADE** |
| Capa 1 · Circuit Breaker | ACTIVE (riesgo 1.5%) — sin rachas ni drawdown relevantes |
| Capa 2 · Régimen | CHOP_RANGE → WAIT / NO_TRADE |
| Close / EMA20 / EMA50 / EMA200 | 59317.77 / 59540.28 / 59811.11 / 59964.56 |
| Pendiente EMA20 (3v) / RSI14 | -0.090% / 41.76 |
| ATR14 / Extensión | 287.50 / -0.77 ATR |
| Filtros que bloquean | CHOP_RANGE: estructura sin tendencia; LONG.macro_close_gt_ema200; LONG.structure_ema20_gt_ema50; LONG.slope_ema20_gt_+0.20%; LONG.rsi_in_46_64; SHORT.slope_ema20_lt_-0.20% |
| Capa 3 · Riesgo | no evaluada (sin señal de entrada) |

```json
{
  "engine": "quant_core_engine",
  "version": "1.0.0",
  "generated_at_utc": "2026-10-24T02:54:54+00:00",
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "candle_time": "2026-01-21T19:00:00+00:00",
  "input_mode": "ohlcv",
  "decision": "WAIT / NO_TRADE",
  "warnings": [],
  "layer_1_circuit_breaker": {
    "status": "ACTIVE",
    "risk_throttle": false,
    "risk_pct": 1.5,
    "cooldown_candles_remaining": 0,
    "consecutive_losses": 0,
    "daily_drawdown_pct": 0.0,
    "reasons": [
      "sin rachas ni drawdown relevantes"
    ],
    "abort_analysis": false
  },
  "layer_2_market_regime": {
    "evaluated": true,
    "regime": "CHOP_RANGE",
    "signal": "WAIT / NO_TRADE",
    "indicators": {
      "close": 59317.77286773,
      "ema20": 59540.28182175,
      "ema50": 59811.11162773,
      "ema200": 59964.56006726,
      "ema20_slope_3c_pct": -0.0905,
      "rsi14": 41.7565,
      "atr14": 287.49902054,
      "extension_atr": -0.7739
    },
    "long_checks": {
      "macro_close_gt_ema200": false,
      "structure_ema20_gt_ema50": false,
      "slope_ema20_gt_+0.20%": false,
      "rsi_in_46_64": false,
      "extension_le_3.2ATR": true
    },
    "short_checks": {
      "macro_close_lt_ema200": true,
      "structure_ema20_lt_ema50": true,
      "slope_ema20_lt_-0.20%": false,
      "rsi_in_36_54": true,
      "extension_le_3.2ATR": true,
      "short_enabled": true
    },
    "blocking_filters": [
      "CHOP_RANGE: estructura sin tendencia",
      "LONG.macro_close_gt_ema200",
      "LONG.structure_ema20_gt_ema50",
      "LONG.slope_ema20_gt_+0.20%",
      "LONG.rsi_in_46_64",
      "SHORT.slope_ema20_lt_-0.20%"
    ]
  },
  "layer_3_atr_risk_guardian": {
    "evaluated": false,
    "reason": "sin señal de entrada"
  },
  "order": null
}
```

## ESCENARIO 3 — Cuenta bloqueada por Circuit Breaker (3 pérdidas seguidas, DD 3.4%)

| Campo | Valor |
|---|---|
| Símbolo / TF | BTC/USDT / 1h |
| DECISIÓN | **HALTED** |
| Capa 1 · Circuit Breaker | HALTED (riesgo 0.0%) — consecutive_losses=3 >= 3; daily_drawdown_pct=3.40% >= 3.0% |
| Capa 2 · Régimen | no evaluada (abortado por Circuit Breaker) |
| Capa 3 · Riesgo | no evaluada (abortado por Circuit Breaker) |

```json
{
  "engine": "quant_core_engine",
  "version": "1.0.0",
  "generated_at_utc": "2026-10-24T02:54:54+00:00",
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "candle_time": null,
  "input_mode": "ohlcv",
  "decision": "HALTED",
  "warnings": [],
  "layer_1_circuit_breaker": {
    "status": "HALTED",
    "risk_throttle": false,
    "risk_pct": 0.0,
    "cooldown_candles_remaining": 15,
    "consecutive_losses": 3,
    "daily_drawdown_pct": 3.4,
    "reasons": [
      "consecutive_losses=3 >= 3",
      "daily_drawdown_pct=3.40% >= 3.0%"
    ],
    "abort_analysis": true
  },
  "layer_2_market_regime": {
    "evaluated": false,
    "reason": "abortado por Circuit Breaker"
  },
  "layer_3_atr_risk_guardian": {
    "evaluated": false,
    "reason": "abortado por Circuit Breaker"
  },
  "order": null
}
```
