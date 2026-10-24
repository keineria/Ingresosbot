# SKILL 1 · market-regime-quant

**Capa 2 de la cadena de ejecución.** Clasifica el mercado en uno de cuatro regímenes y decide si hay señal LONG, SHORT o `WAIT / NO_TRADE`. Solo se ejecuta si la Capa 1 (postmortem-circuit-breaker) no devolvió `HALTED`.

Implementación de referencia: `market_regime()` en `quant_core_engine.py`.

---

## 1. Entradas

| Variable | Definición | Fuente |
|---|---|---|
| `Close` | Cierre de la última vela **cerrada** | OHLCV |
| `EMA_20`, `EMA_50`, `EMA_200` | Media móvil exponencial sobre `Close` | Calculado |
| `EMA_20[t-3]` | EMA_20 tres velas antes | Calculado |
| `RSI_14` | RSI de Wilder, 14 periodos | Calculado |
| `ATR_14` | Average True Range de Wilder, 14 periodos | Calculado |

Historial mínimo: **203 velas** (200 para la EMA_200 + 3 para la pendiente). Recomendado: **≥ 400 velas** para que la EMA_200 converja con la de TradingView.

## 2. Cálculo de indicadores (idéntico a TradingView)

**EMA(n)**
```
α = 2 / (n + 1)
EMA[n-1] = SMA(Close[0..n-1])                 (semilla)
EMA[t]   = α · Close[t] + (1 − α) · EMA[t−1]
```

**RMA de Wilder(n)** (usada por RSI y ATR)
```
RMA[primero] = SMA de los n primeros valores
RMA[t]       = (1/n) · x[t] + (1 − 1/n) · RMA[t−1]
```

**RSI(14)**
```
Δ[t]     = Close[t] − Close[t−1]
Gain[t]  = max(Δ, 0)      Loss[t] = max(−Δ, 0)
RS       = RMA14(Gain) / RMA14(Loss)
RSI      = 100 − 100 / (1 + RS)          (Loss = 0 → RSI = 100)
```

**ATR(14)**
```
TR[t] = max(High − Low, |High − Close[t−1]|, |Low − Close[t−1]|)
ATR   = RMA14(TR)
```

**Métricas derivadas** (redondeadas a 8 decimales antes de comparar, para que el ruido de coma flotante no mueva los límites):
```
Pendiente_EMA20_% = (EMA_20[t] / EMA_20[t−3] − 1) · 100
Extensión_ATR     = (Close − EMA_20) / ATR_14
```

## 3. Clasificación de régimen (se evalúa en este orden; gana la primera que se cumple)

| # | Régimen | Condición |
|---|---|---|
| 1 | **EXHAUSTION_CLIMAX** | `|Extensión_ATR| > 3.2` (precio a más de 3.2 ATR de la EMA_20, arriba o abajo) |
| 2 | **BULL_EXPANSION** | `Close > EMA_200` **y** `EMA_20 > EMA_50` **y** `Pendiente_EMA20_% > +0.2` |
| 3 | **BEAR_DISTRIBUTION** | `Close < EMA_200` **y** `EMA_20 < EMA_50` **y** `Pendiente_EMA20_% < −0.2` |
| 4 | **CHOP_RANGE** | Cualquier otro caso (estructura sin tendencia definida) |

El agotamiento tiene prioridad: una tendencia alcista perfecta pero sobreextendida es `EXHAUSTION_CLIMAX`, no `BULL_EXPANSION`.

## 4. Reglas de entrada

### LONG (todas obligatorias)
| Filtro | Regla | Clave en el JSON |
|---|---|---|
| a) Régimen macro | `Close > EMA_200` | `macro_close_gt_ema200` |
| b) Impulso estructural | `EMA_20 > EMA_50` | `structure_ema20_gt_ema50` |
| b) Pendiente | `Pendiente_EMA20_% > +0.2` (estricto: 0.2 exacto **no** pasa) | `slope_ema20_gt_+0.20%` |
| c) Zona de valor (sin FOMO) | `46 ≤ RSI_14 ≤ 64` (límites incluidos) | `rsi_in_46_64` |
| d) Sin sobreextensión | `|Extensión_ATR| ≤ 3.2` | `extension_le_3.2ATR` |

### SHORT (espejo; todas obligatorias)
| Filtro | Regla | Clave en el JSON |
|---|---|---|
| a) Régimen macro | `Close < EMA_200` | `macro_close_lt_ema200` |
| b) Impulso estructural | `EMA_20 < EMA_50` | `structure_ema20_lt_ema50` |
| b) Pendiente | `Pendiente_EMA20_% < −0.2` | `slope_ema20_lt_-0.20%` |
| c) Zona de valor | `36 ≤ RSI_14 ≤ 54` | `rsi_in_36_54` |
| d) Sin sobreextensión | `|Extensión_ATR| ≤ 3.2` | `extension_le_3.2ATR` |
| e) Habilitado | `allow_short = true` (poner `false` en cuentas spot) | `short_enabled` |

### Matriz de decisión
| Régimen | Señal |
|---|---|
| BULL_EXPANSION + todos los filtros LONG | `LONG` |
| BEAR_DISTRIBUTION + todos los filtros SHORT | `SHORT` |
| BULL_EXPANSION con RSI fuera de 46–64 | `WAIT / NO_TRADE` (bloquea `LONG.rsi_in_46_64`) |
| BEAR_DISTRIBUTION con RSI fuera de 36–54 | `WAIT / NO_TRADE` (bloquea `SHORT.rsi_in_36_54`) |
| CHOP_RANGE | `WAIT / NO_TRADE` (se listan todos los filtros que fallan) |
| EXHAUSTION_CLIMAX | `WAIT / NO_TRADE` (nunca se persigue el precio) |

Toda salida `WAIT / NO_TRADE` incluye `blocking_filters` con el nombre exacto de cada filtro que bloqueó la entrada.

## 5. Parámetros editables (`Params` en el motor)

| Parámetro | Valor | Significado |
|---|---|---|
| `ema_fast` / `ema_mid` / `ema_slow` | 20 / 50 / 200 | Periodos de las EMAs |
| `rsi_len` / `atr_len` | 14 / 14 | Periodos de RSI y ATR |
| `slope_lookback` | 3 | Velas para la pendiente de EMA_20 |
| `slope_min_pct` | 0.2 | Pendiente mínima en % (estricta) |
| `rsi_long_min` / `rsi_long_max` | 46 / 64 | Zona de valor LONG |
| `rsi_short_min` / `rsi_short_max` | 36 / 54 | Zona de valor SHORT |
| `max_extension_atr` | 3.2 | Límite de sobreextensión |
| `allow_short` | true | Permitir cortos |

## 6. Salida (bloque `layer_2_market_regime`)

```json
{
  "evaluated": true,
  "regime": "BULL_EXPANSION",
  "signal": "LONG",
  "indicators": {"close": 0, "ema20": 0, "ema50": 0, "ema200": 0,
                 "ema20_slope_3c_pct": 0, "rsi14": 0, "atr14": 0, "extension_atr": 0},
  "long_checks":  {"macro_close_gt_ema200": true, "...": true},
  "short_checks": {"macro_close_lt_ema200": false, "...": false},
  "blocking_filters": []
}
```

## 7. Reglas operativas

- Evaluar **solo velas cerradas**. Si el CSV trae la vela en formación, usar `--drop-last`.
- La pendiente de 0.2% en 3 velas está pensada para marcos de 15m–4h. En 1D suele ser exigente; en 1m–5m es ruidosa. Si se cambia el marco temporal, recalibrar `slope_min_pct` con backtest, no a ojo.
- Nunca operar por intuición: si una condición falla, la respuesta es `WAIT / NO_TRADE` aunque el gráfico "se vea bien".
