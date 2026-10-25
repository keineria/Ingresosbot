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

Temporalidad recomendada: **4h**, porque da pocas operaciones, poca comisión y menos ruido. En 1h también funcionó en la investigación, pero paga unas 13 veces más comisiones.

## 4. Lo que hay que esperar

- **Win rate de 40-50%** con ganadores mucho más grandes que los perdedores (profit factor de 1.8 a 4.4 en la investigación).
- **Rachas de 5 a 9 pérdidas pequeñas** en mercados laterales. El Circuit Breaker las frena con `THROTTLED` y `HALTED`, como siempre.
- **En un mercado sin tendencia pierde poco** (comisiones y stops). En 12 series aleatorias sin tendencia perdió de media -2.8%, frente a -5.8% del oficial.
- **En un mercado muy alcista gana menos que comprar y mantener**, porque arriesga 1.5% por operación: en 4h usó de media el 33% del capital por operación (en 1h, el 60%). A cambio, su caída máxima es mucho menor.

Resultados y comandos de validación: [docs/TREND_RIDER.md](../docs/TREND_RIDER.md).
