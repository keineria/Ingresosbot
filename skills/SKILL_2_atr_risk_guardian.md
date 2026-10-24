# SKILL 2 · atr-risk-guardian

**Capa 3 de la cadena de ejecución.** Convierte una señal LONG/SHORT en niveles exactos de orden (SL, TP, Break-Even) y en un tamaño de posición que nunca arriesgue más del porcentaje permitido. Solo se ejecuta si la Capa 1 no es `HALTED` y la Capa 2 dio señal.

Todos los cálculos se hacen con código (`atr_risk_guardian()` en `quant_core_engine.py`), nunca a mano.

---

## 1. Entradas

| Variable | Origen |
|---|---|
| `Entry_Price` | Último cierre, o precio indicado con `--entry` |
| `ATR_14` | Capa 2 |
| `Balance` | Estado de la cuenta (USD) |
| `Risk_Pct` | Capa 1: **1.5%** si ACTIVE · **0.75%** si THROTTLED · 0% si HALTED |

## 2. Riesgo por operación

```
Risk_Pct = 1.50 %   (estado ACTIVE, riesgo base)
Risk_Pct = 0.75 %   (estado THROTTLED, RISK_THROTTLE = True)
Risk_USD_objetivo = Balance · Risk_Pct / 100
```

## 3. Niveles de precio

### LONG
```
Stop_Loss          = Entry_Price − 1.6 · ATR_14
Take_Profit        = Entry_Price + 3.6 · ATR_14
Break_Even_Trigger = Entry_Price + 1.5 · ATR_14
Break_Even_New_SL  = Entry_Price · (1 + 0.002)      ← cubre 0.2% de comisión ida y vuelta
```

### SHORT (espejo)
```
Stop_Loss          = Entry_Price + 1.6 · ATR_14
Take_Profit        = Entry_Price − 3.6 · ATR_14
Break_Even_Trigger = Entry_Price − 1.5 · ATR_14
Break_Even_New_SL  = Entry_Price · (1 − 0.002)
```

**Relación beneficio/riesgo fija:** `3.6 / 1.6 = 2.25 R`. Con este R:R el sistema es rentable (antes de comisiones) con una tasa de acierto superior al `1 / (1 + 2.25) = 30.8%`.

**Regla de Break-Even:** cuando el precio toca `Break_Even_Trigger`, el ejecutor mueve el SL a `Break_Even_New_SL`. Desde ese momento la operación ya no puede cerrar en pérdida (salvo deslizamiento).

## 4. Redondeo "al centavo" (tick del exchange)

Los precios se redondean a `price_tick` (por defecto `0.01`) con aritmética decimal, siempre en la dirección conservadora:

| Nivel | LONG | SHORT | Motivo |
|---|---|---|---|
| Stop_Loss | hacia abajo | hacia arriba | El stop nunca queda más ceñido que la fórmula |
| Take_Profit | hacia abajo | hacia arriba | El objetivo nunca es más ambicioso que la fórmula |
| Break_Even_Trigger | hacia arriba | hacia abajo | Nunca se activa antes de tiempo |
| Break_Even_New_SL | hacia arriba | hacia abajo | Siempre cubre la comisión completa |

Para activos con precio < 1 USD, ajustar `price_tick` al tick real del par (p. ej. `0.0001`).

## 5. Tamaño de posición

```
SL_Distance_Frac   = |Entry_Price − Stop_Loss| / Entry_Price        (con el SL ya redondeado)
Size_por_riesgo    = Risk_USD_objetivo / SL_Distance_Frac
Size_tope          = Balance · 0.85                                  ← tope del 85% del capital
Position_Size_USD  = min(Size_tope, Size_por_riesgo)                 (redondeo hacia abajo al centavo)
Quantity           = floor(Position_Size_USD / Entry_Price, qty_step)
Risk_USD_efectivo  = Quantity · |Entry_Price − Stop_Loss|
```

**Cuándo actúa el tope del 85%.** El tope se activa siempre que la distancia al SL sea menor que `Risk_Pct / 0.85`:
- Riesgo 1.5% → tope activo si el SL está a menos de **1.765%** del precio.
- Riesgo 0.75% → tope activo si el SL está a menos de **0.882%**.

Cuando el tope actúa, el riesgo real es **menor** que el objetivo (`capped_by_max_allocation = true`). Nunca es mayor.

**Orden mínima.** Si `Position_Size_USD < min_notional_usd` (10 USD por defecto), no se envía orden y la decisión pasa a `WAIT / NO_TRADE`.

## 6. Ejemplo verificado (prueba `test_long_formulas`)

Entrada 100.00, ATR 2.00, Balance 10 000 USD, ACTIVE:

| Concepto | Cálculo | Resultado |
|---|---|---|
| SL | 100 − 1.6·2 | **96.80** |
| TP | 100 + 3.6·2 | **107.20** |
| BE trigger | 100 + 1.5·2 | **103.00** |
| BE nuevo SL | 100 · 1.002 | **100.20** |
| Riesgo objetivo | 10 000 · 1.5% | 150.00 USD |
| Distancia SL | 3.2 / 100 | 3.2% |
| Tamaño por riesgo | 150 / 0.032 | 4 687.50 USD |
| Tope 85% | 10 000 · 0.85 | 8 500.00 USD |
| **Tamaño final** | min(8 500, 4 687.50) | **4 687.50 USD (46.875 uds)** |
| Riesgo efectivo | 46.875 · 3.2 | 150.00 USD |

## 7. Parámetros editables

| Parámetro | Valor | Significado |
|---|---|---|
| `risk_base_pct` | 1.5 | Riesgo en estado ACTIVE |
| `risk_throttled_pct` | 0.75 | Riesgo en estado THROTTLED |
| `sl_atr_mult` | 1.6 | Multiplicador ATR del stop |
| `tp_atr_mult` | 3.6 | Multiplicador ATR del objetivo |
| `be_atr_mult` | 1.5 | Multiplicador ATR del disparo de Break-Even |
| `be_fee_offset_pct` | 0.2 | Colchón de comisión del nuevo SL |
| `max_allocation_pct` | 85 | Tope de capital por posición |
| `price_tick` | 0.01 | Tick de precio |
| `qty_step` | 0.000001 | Paso mínimo de cantidad |
| `min_notional_usd` | 10 | Orden mínima |

## 8. Salida (bloque `layer_3_atr_risk_guardian` y `order`)

```json
"order": {
  "symbol": "BTC/USDT", "side": "buy", "type": "market",
  "amount": 0.117158, "notional_usd": 8499.94, "reference_price": 72551.12,
  "stop_loss":   {"type": "stop_market", "price": 71622.73},
  "take_profit": {"type": "limit",       "price": 74640.00},
  "break_even":  {"trigger_price": 73421.50, "new_stop_loss": 72696.23},
  "risk_usd": 108.77, "risk_mode": "ACTIVE", "client_tag": "qce-bull_expansion"
}
```

El bloque `order` está pensado para mapearse directamente a `exchange.create_order(symbol, type, side, amount)` de CCXT más órdenes de protección (SL/TP), o a una orden bracket de Alpaca. El movimiento a Break-Even lo debe vigilar el ejecutor (n8n/CCXT) en cada vela.
