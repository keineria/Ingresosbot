# SKILL 3 · postmortem-circuit-breaker

**Capa 1 de la cadena de ejecución: se evalúa SIEMPRE PRIMERO.** Audita la racha de resultados y el drawdown del día. Si la cuenta está en `HALTED`, el motor aborta **antes de mirar el gráfico**: no se calculan indicadores ni se genera orden.

Implementación de referencia: `circuit_breaker()`, `postmortem_update()` y `tick_cooldown()` en `quant_core_engine.py`.

---

## 1. Estado de la cuenta (`account_state.json`)

```json
{
  "balance": 10000.00,
  "consecutive_losses": 0,
  "daily_drawdown_pct": 0.0,
  "cooldown_candles_remaining": 0,
  "day_peak_equity": 10000.00
}
```

| Campo | Definición |
|---|---|
| `balance` | Equity actual en USD |
| `consecutive_losses` | Pérdidas seguidas desde la última ganancia |
| `daily_drawdown_pct` | `(day_peak_equity − balance) / day_peak_equity · 100` |
| `day_peak_equity` | Máximo equity del día; arranca en el balance de apertura (00:00 UTC) |
| `cooldown_candles_remaining` | Velas que faltan para salir de HALTED |

## 2. Máquina de estados

| Estado | Condición de entrada (cualquiera) | Riesgo por trade | Efecto |
|---|---|---|---|
| **ACTIVE** | `consecutive_losses ≤ 1` **y** `daily_drawdown_pct < 2.0%` | 1.5% | Operación normal |
| **THROTTLED** | `consecutive_losses == 2` **o** `2.0% ≤ daily_drawdown_pct < 3.0%` | 0.75% | `RISK_THROTTLE = True`, riesgo a la mitad |
| **HALTED** | `consecutive_losses ≥ 3` **o** `daily_drawdown_pct ≥ 3.0%` **o** `cooldown_candles_remaining > 0` | 0% | Análisis abortado, enfriamiento de **15 velas** |

Orden de evaluación: `cooldown en curso → HALTED → THROTTLED → ACTIVE`. La condición más severa gana siempre. Los límites son inclusivos (2 pérdidas exactas o 2.00% exacto ya es THROTTLED; 3 pérdidas o 3.00% exacto ya es HALTED).

```
          2 pérdidas o DD ≥ 2%            3 pérdidas o DD ≥ 3%
ACTIVE ─────────────────────► THROTTLED ─────────────────────► HALTED
  ▲                               │  ▲                            │
  │  1 ganancia y DD < 2%         │  │   fin de enfriamiento      │
  └───────────────────────────────┘  └── (si DD sigue ≥ 2%) ──────┤
  ▲                                                               │
  └──────────── fin de enfriamiento (si DD < 2%) ─────────────────┘
```

## 3. Auditoría post-trade (`postmortem_update`)

Se ejecuta al cerrar cada operación:

| Resultado del trade | Acción sobre `consecutive_losses` |
|---|---|
| `LOSS`: PnL < 0 | `+1` |
| `WIN`: PnL > 0 | reinicia a `0` |
| `BREAKEVEN`: cerrado por el stop de Break-Even (o PnL = 0) | sin cambio: ni suma pérdida ni reinicia la racha |

El resultado `BREAKEVEN` se decide por el motivo de salida, no por el signo del PnL: tras comisiones y deslizamiento puede quedar en unos centavos a favor o en contra, pero el trade cumplió su función de proteger el capital. El balance sí recoge el PnL real.

Implementación persistente: `state_manager.py` (SQLite, `data/trading_state.db`) guarda este estado en la tabla `account_state` y cada cierre en `trade_history`.

Después:
```
day_peak_equity     = max(day_peak_equity, balance_nuevo)
daily_drawdown_pct  = (day_peak_equity − balance_nuevo) / day_peak_equity · 100
si consecutive_losses ≥ 3  o  daily_drawdown_pct ≥ 3.0  →  cooldown_candles_remaining = 15
```

## 4. Enfriamiento (`tick_cooldown`)

Se llama **una vez por cada vela cerrada** del marco temporal operado:

```
si cooldown_candles_remaining > 0:
    cooldown_candles_remaining −= 1
    si llega a 0:
        consecutive_losses = 0                      ← la racha se da por auditada
        si daily_drawdown_pct ≥ 3.0:
            cooldown_candles_remaining = 15         ← sigue bloqueado hasta el reset diario
```

Al salir del enfriamiento el estado se recalcula con el drawdown del día: si sigue entre 2% y 3%, la cuenta reentra en **THROTTLED** (0.75%), no en ACTIVE.

**Reset diario (00:00 UTC):** `day_peak_equity = balance`, `daily_drawdown_pct = 0`. `consecutive_losses` **no** se reinicia con el cambio de día: una racha perdedora sigue contando.

## 5. Ejemplo de racha (prueba `test_postmortem_and_cooldown`)

| Evento | Balance | Pérdidas seguidas | DD diario | Estado |
|---|---|---|---|---|
| Inicio del día | 10 000 | 0 | 0.00% | ACTIVE |
| Pérdida −100 | 9 900 | 1 | 1.00% | ACTIVE |
| Pérdida −100 | 9 800 | 2 | 2.00% | THROTTLED (0.75%) |
| Pérdida −50 | 9 750 | 3 | 2.50% | **HALTED** (15 velas) |
| +15 velas | 9 750 | 0 | 2.50% | THROTTLED |
| Ganancia +30 | 9 780 | 0 | 2.20% | THROTTLED (por DD) |

## 6. Parámetros editables

| Parámetro | Valor | Significado |
|---|---|---|
| `throttle_losses` | 2 | Pérdidas seguidas para THROTTLED |
| `throttle_dd_pct` | 2.0 | DD diario para THROTTLED |
| `halt_losses` | 3 | Pérdidas seguidas para HALTED |
| `halt_dd_pct` | 3.0 | DD diario para HALTED |
| `cooldown_candles` | 15 | Velas de enfriamiento |

## 7. Salida (bloque `layer_1_circuit_breaker`)

```json
{
  "status": "HALTED",
  "risk_throttle": false,
  "risk_pct": 0.0,
  "cooldown_candles_remaining": 15,
  "consecutive_losses": 3,
  "daily_drawdown_pct": 3.4,
  "reasons": ["consecutive_losses=3 >= 3", "daily_drawdown_pct=3.40% >= 3.0%"],
  "abort_analysis": true
}
```

Con `abort_analysis = true`, el JSON final lleva `"decision": "HALTED"`, `"order": null` y las capas 2 y 3 con `"evaluated": false`.

## 8. Reglas operativas

- El estado de la cuenta debe venir del exchange (o del ejecutor), no de la memoria de la conversación.
- Nunca saltarse un HALTED "porque el setup es muy bueno". El escenario 3 de la demo lo prueba: el mismo gráfico que da un LONG válido queda bloqueado si la cuenta lleva 3 pérdidas.
- Tras cada HALTED, revisar los 3 trades perdedores (régimen en el que entraron, RSI, extensión) antes de tocar parámetros.

## 9. Filtro externo opcional (apagado por defecto)

El estado de la cuenta puede traer un campo `external_risk` con un filtro de fuera del gráfico (sentimiento, calendario macro, alertas). **Solo puede endurecer la Capa 1, nunca ablandarla**, y el orden de capas no cambia:

```json
{"balance": 500.0, "consecutive_losses": 0, "daily_drawdown_pct": 0.0,
 "external_risk": {"level": "halt", "reasons": ["día de FOMC/CPI"]}}
```

| `level` | Efecto |
|---|---|
| `none` (o sin campo) | Ninguno: la Capa 1 funciona como en las secciones 1-8 |
| `throttle` | Si la cuenta estaba ACTIVE pasa a THROTTLED (riesgo 0.75%) |
| `halt` | HALTED mientras el filtro siga activo, sin enfriamiento de 15 velas; no cierra posiciones abiertas |

Si a la vez hay 3 pérdidas o 3% de DD, mandan esas reglas (HALTED con enfriamiento). Kill switch y enfriamiento en curso también tienen prioridad. Cada motivo aparece en `reasons` como `"filtro externo: ..."`.

El bot 24/7 **no** usa este filtro: en 2024-2026 ningún filtro de sentimiento ni de días FOMC/CPI mejoró a Trend Rider de forma robusta (ver `docs/FILTRO_SENTIMIENTO.md` y `sentiment_lab.py`).
