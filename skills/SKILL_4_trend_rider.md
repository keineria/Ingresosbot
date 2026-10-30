# SKILL 4 · trend-rider (perfil paralelo)

**Perfil alternativo de las Capas 2 y 3, que se elige con `Params.strategy = "trend"`.** No reemplaza las reglas oficiales (SKILL_1..3), que siguen siendo el valor por defecto (`strategy = "official"`). La cadena y su orden no cambian:

1. **Capa 1, postmortem-circuit-breaker (SKILL_3): idéntica.** 3 pérdidas seguidas o DD diario ≥ 3% → `HALTED` y 15 velas de enfriamiento. 2 pérdidas o DD ≥ 2% → `THROTTLED` y riesgo 0.75%. Si está `HALTED`, no se mira el gráfico.
2. **Capa 2: entrada por ruptura de tendencia** (`trend_regime()`).
3. **Capa 3: mismo tamaño de posición, salida por trailing stop** (`trend_risk_guardian()` + `StateManager.apply_trailing()`).

Por qué existe: el backtest real del perfil oficial en el VPS (BTCUSDT 2024-2026) perdió -35% en 1h y -26.6% en 4h, con un win rate del 19.5%. El TP fijo de 3.6 ATR y el Break-Even a 1.5 ATR cortan las tendencias grandes, que son las que pagan en cripto, y el 1h genera tantas operaciones que las comisiones se comen el resultado. Trend Rider hace lo contrario: entra pocas veces, corta rápido lo que no funciona y deja correr lo que sí.

---

## 1. Capa 2: entrada

`Params.trend_entry` elige la regla:

| Modo | LONG (todas deben cumplirse) |
|---|---|
| `breakout` (por defecto) | `Close > EMA_200` **y** `Close > máximo de las 20 velas anteriores` (canal de Donchian, sin contar la vela actual) |
| `pullback` | La entrada oficial de SKILL_1 (EMA200, EMA20 > EMA50, pendiente > +0.2%, RSI 46-64, extensión ≤ 3.2 ATR) |
| `both` | Cualquiera de las dos |

- **SHORT** (solo si `trend_allow_short = true`, es decir, en Futuros): `Close < EMA_200` y `Close < mínimo de las 20 velas anteriores`.
- En la ruptura **no hay filtro de RSI ni de extensión**: una ruptura sana suele ir con RSI alto y el precio estirado, y es justo la que se quiere comprar.
- Si no hay señal: `DECISION: WAIT / NO_TRADE`, con el filtro que bloqueó, por ejemplo `TREND.LONG.breakout_close_gt_max20`.

## 2. Capa 3: riesgo y salida

```
Riesgo %          = el de la Capa 1 (1.5% ACTIVE · 0.75% THROTTLED)
Stop_Loss inicial = Entry_Price − 2.5 · ATR_14                    (trend_sl_atr_mult)
Position_Size_USD = min(Balance · 0.85 · max_leverage,
                        Balance · Risk_Pct / ((Entry_Price − Stop_Loss) / Entry_Price))
Take_Profit       = ninguno
Break-Even        = ninguno
Trailing stop     = al cierre de cada vela:
                    SL = max(SL actual, máximo desde la entrada − 4 · ATR_14)   (trail_atr_mult)
```

- El SL **solo se mueve a favor** y el nuevo nivel rige desde la vela siguiente (igual que el Break-Even oficial).
- SHORT es el espejo: SL inicial `Entry + 2.5 ATR`, y el trailing baja con el mínimo desde la entrada.
- Redondeo conservador al tick: el trailing de un LONG se redondea hacia abajo y el de un SHORT hacia arriba.
- Motivo de salida en el historial: `STOP_LOSS` si el SL nunca se movió y `TRAILING_STOP` si ya se había movido (puede ser `WIN`).
- La orden JSON lleva `take_profit: null`, `break_even: null` y un bloque `trailing_stop`. Cada vez que el SL sube, el bot emite el evento `trailing_stop` al webhook para que el ejecutor (n8n / CCXT) mueva la orden stop en el exchange.

## 3. Parámetros (todos manuales, en `deploy/trend_params.json` o `--params`)

| Parámetro | Defecto | Qué hace |
|---|---|---|
| `strategy` | `"trend"` | `"official"` vuelve a las reglas de SKILL_1..3 |
| `trend_entry` | `"breakout"` | `breakout`, `pullback` o `both` |
| `donchian_len` | `20` | Velas del canal de ruptura (20 velas de 4h son unos 3 días) |
| `trend_sl_atr_mult` | `2.5` | SL inicial en ATR (también fija el tamaño de la posición) |
| `trail_atr_mult` | `4.0` | Distancia del trailing en ATR: más alto deja correr más y devuelve más ganancia al salir |
| `trend_allow_short` | `false` | `true` solo en Futuros |
| `daily_sma_len` | `0` (en `trend_params.json`: `200`) | Filtro diario: LONG solo si el cierre diario de AYER está sobre la SMA de N días |
| `trend_exit_on_filter` | `false` | `true`: cerrar a mercado cuando el filtro diario se da la vuelta |
| `trend_min_adx` | `0` (apagado) | Solo entra si el ADX14 es al menos X: evita las rupturas falsas del mercado lateral |
| `max_position_pct` | `0` (apagado) | Tope por posición en % del balance (más bajo que el 85% total) para tener varias monedas a la vez |
| `range_entry` | `false` | Modo lateral: sin ruptura, ADX14 < `range_max_adx` (20), cierre bajo la banda de Bollinger inferior (`range_bb_len` 20, `range_bb_std` 2) y RSI14 ≤ `range_rsi_long_max` (35): compra el rebote con SL `range_sl_atr_mult` y TP fijo `range_tp_atr_mult` (1,5 ATR), sin trailing. Con `trend_allow_short` también vende en la banda superior (RSI ≥ `range_rsi_short_min`) |

Hay más filtros y salidas opcionales (volumen, rango de volatilidad, empuje de la ruptura, salidas por canal, EMA o vela sin avance, trailing ajustado); todos vienen apagados y están descritos en `Params` de `quant_core_engine.py`.

`trend_entry = "daily_filter"` entra solo con el filtro diario (sin ruptura). El filtro usa siempre el día anterior ya cerrado, para no mirar el futuro, y el bot descarga las velas necesarias (205 días) cuando está activo.

**Configuración recomendada para paper** (`deploy/trend_params.json`, validada con datos de 2024-2026 en [docs/LAB_2024_2026.md](../docs/LAB_2024_2026.md)): 4h, ruptura de Donchian 20, SL inicial 2 ATR, trailing 5 ATR y filtro SMA200 diaria.

**Desde el 30-oct-2026** ([docs/REACTIVAR.md](../docs/REACTIVAR.md), datos 2022-2026): Keiner usa además `trend_min_adx: 20` y `max_position_pct: 30` sobre 12 monedas con oro; Celeste (Tortuga, velas diarias) usa Donchian 30, `trend_min_adx: 20` y `trend_allow_short: true` (cortos solo en paper; con dinero real exigen Futuros 1x).

Temporalidad recomendada: **4h**, porque da pocas operaciones, poca comisión y menos ruido. En 1h también funcionó en la investigación, pero paga unas 13 veces más comisiones.

## 4. Lo que hay que esperar

- **Win rate de 40-50%** con ganadores mucho más grandes que los perdedores (profit factor de 1.8 a 4.4 en la investigación).
- **Rachas de 5 a 9 pérdidas pequeñas** en mercados laterales. El Circuit Breaker las frena con `THROTTLED` y `HALTED`, como siempre.
- **En un mercado sin tendencia pierde poco** (comisiones y stops). En 12 series aleatorias sin tendencia perdió de media -2.8%, frente a -5.8% del oficial.
- **En un mercado muy alcista gana menos que comprar y mantener**, porque arriesga 1.5% por operación: en 4h usó de media el 33% del capital por operación (en 1h, el 60%). A cambio, su caída máxima es mucho menor.

Resultados y comandos de validación: [docs/TREND_RIDER.md](../docs/TREND_RIDER.md).
