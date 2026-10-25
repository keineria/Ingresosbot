# Trend Rider: la versión paralela del bot

Trend Rider es un segundo perfil del mismo motor (`Params.strategy = "trend"`). La regla completa está en [SKILL_4](../skills/SKILL_4_trend_rider.md). Se puede probar y correr **al lado** del bot oficial sin tocarlo: el oficial sigue igual, con su base, su panel y sus reglas.

## 1. La idea en una frase

Comprar solo cuando el precio rompe el máximo de los últimos días estando por encima de la EMA200, cortar rápido si falla (SL a 2.5 ATR) y, si funciona, **no ponerle techo**: el stop va subiendo detrás del precio (máximo − 4 ATR) hasta que la tendencia se acaba.

Es la familia de estrategias con más evidencia en cripto (seguimiento de tendencia o *time-series momentum*, el método de las "Tortugas" con canal de Donchian). Gana pocas veces pero con ganancias grandes, opera poco (paga poca comisión) y se queda fuera en los mercados bajistas.

## 2. Qué cambia frente al oficial y por qué

| | Oficial (SKILL_1..3) | Trend Rider (SKILL_4) |
|---|---|---|
| Capa 1, Circuit Breaker | 3 pérdidas o DD ≥ 3% → HALTED 15 velas | **Igual** |
| Entrada | Retroceso en tendencia (RSI 46-64) | Ruptura del máximo de 20 velas sobre la EMA200 |
| Stop inicial | 1.6 ATR | 2.5 ATR (menos stops por ruido) |
| Salida con ganancia | TP fijo a 3.6 ATR y Break-Even a 1.5 ATR | Sin TP: trailing stop a 4 ATR del máximo |
| Riesgo por operación | 1.5% (0.75% en THROTTLED), tope 85% | **Igual** |
| Temporalidad sugerida | 1h | 4h |

El diagnóstico del backtest real (VPS, BTCUSDT 2024-2026) fue: win rate de 19.5%, profit factor de 0.69, 2.408 USD de comisiones en 1h y muchos trades que tocaban el Break-Even y luego habrían llegado al TP. El problema no era tanto la entrada como **la salida**: el TP fijo y el Break-Even cortan justo las tendencias que pagan.

## 3. Investigación (datos reales, honestidad incluida)

Desde este entorno no se puede descargar de Binance, así que la investigación usó **20.111 velas reales de 1h de BTC/USD (Coinbase, julio 2017 a octubre 2019**, reagrupadas a 4h y 1d). Ese periodo tiene de todo: la subida de 2017, la caída de -84% de 2018 y la recuperación de 2019. Se usó el mismo `backtester.py` del repo, con comisión de 0.08% por orden y deslizamiento de 0.02%.

**Periodo completo, solo largos (spot), 10.000 USD:**

| Serie | Perfil | Retorno | Buy & Hold | Caída máx. | Trades | Win rate | Profit factor | Comisiones |
|---|---|---|---|---|---|---|---|---|
| BTC 1h | Oficial | -4.8% | +217% | 36.3% | 318 | 25.2% | 0.98 | 4.096 USD |
| BTC 1h | **Trend Rider** | **+249.7%** | +217% | **17.6%** | 202 | 39.1% | 1.82 | 4.685 USD |
| BTC 4h | Oficial | +17.1% | +189% | 14.0% | 102 | 30.4% | 1.23 | 767 USD |
| BTC 4h | **Trend Rider** | **+123.7%** | +189% | **9.2%** | 40 | 52.5% | 4.39 | 352 USD |

Por año en 4h, Trend Rider ganó en los tres: 2017 +4.220 USD, **2018 (año de -73%) +1.549 USD** y 2019 +6.597 USD. El oficial perdió 1.047 USD en 2018.

**Validación 70/30** (se eligió con el primer 70% de cada serie y se midió en el último 30%, que nunca se miró para decidir):

| Serie | Oficial ent. → val. | Trend Rider ent. → val. |
|---|---|---|
| BTC 1h | +72.5% → **-25.6%** | +121.5% → **+57.9%** |
| BTC 4h | +6.8% → +10.5% | +55.6% → **+42.7%** |

**Robustez**: los vecinos de cada parámetro (Donchian 55, SL 2 o 3 ATR, trailing 3 o 5 ATR, entrada por retroceso o "ambas") también ganaron en entrenamiento y en validación en 1h y 4h. No depende de un número exacto. El vecino más flojo fue trailing 3 ATR, que sale demasiado pronto.

**Prueba de trampa**: en 12 series aleatorias sin tendencia, Trend Rider perdió de media -2.8% (el oficial, -5.8%). No "ve el futuro": gana cuando hay tendencias y pierde poco cuando no las hay.

**Lo que NO demuestra todavía:**
- Que funcione en 2024-2026: es otra época del mercado. Por eso el paso 4 se corre en tu VPS.
- Que gane más que comprar y mantener en un mercado muy alcista: no lo hace, porque arriesga 1.5% por operación (en 4h usó de media el 33% del capital). Su ventaja es caer mucho menos.
- ETH y SOL: solo había datos de BTC. La comparación del VPS incluye los tres pares.
- En 1d hubo muy pocas operaciones (2 a 4 por mitad) para sacar conclusiones.

## 4. Pruébalo con datos de 2024-2026 en tu VPS

**Dónde:** hPanel → VPS → botón **Terminal** (se abre en el navegador, ya como root). Copia **solo las líneas de comando**, sin el texto del prompt (`[root@... ~]#`).

1) Actualiza el código (no toca el bot que está corriendo hasta el `restart`):

```bash
cd /opt/Ingresosbot
git pull
bash deploy/install_almalinux.sh
systemctl restart ingresosbot ingresosbot-web
```

Si `git pull` responde *dubious ownership*, ejecuta una sola vez `git config --global --add safe.directory /opt/Ingresosbot` y repite.

2) Compara Trend Rider contra el oficial en BTC, ETH y SOL, en 1h, 4h y 1d (descarga unos 1-4 años y tarda unos minutos):

```bash
cd /opt/Ingresosbot
sudo -u ingresosbot .venv/bin/python backtester.py --sweep BTCUSDT,ETHUSDT,SOLUSDT --intervals 1h,4h,1d --grid trend --out-md data/trend_vs_oficial.md --out-csv data/trend_vs_oficial.csv
```

Al final imprime dos tablas (también quedan en `data/trend_vs_oficial.md`). Cópialas y pégalas en el chat.

**Cómo se decide, fijado antes de ver los resultados para no hacer trampa:** Trend Rider es creíble si en 4h **gana en entrenamiento y en validación en al menos 2 de los 3 pares**, supera al oficial en validación y sus vecinos (trailing 3/5, SL 2/3, Donchian 55) no se derrumban. Si no se cumple, se queda solo en paper y se ajusta. Nunca se elige mirando la columna de validación.

3) Backtest de un solo par con detalle por año y por motivo de salida:

```bash
sudo -u ingresosbot .venv/bin/python backtester.py --symbol BTCUSDT --interval 4h --limit 4380 --params deploy/trend_params.json
```

## 5. Encenderlo en paralelo (paper trading, sin dinero real)

Corre como un segundo servicio, `ingresosbot-trend`, con su propia base (`data/trend_state.db`), en 4h y con 10.000 USD de papel. Usa los mismos pares y el mismo Telegram del `.env`; sus mensajes llegan con el prefijo **[Trend Rider]** y además avisa cada vez que sube el stop (📈 TRAILING STOP).

```bash
systemctl enable --now ingresosbot-trend
journalctl -u ingresosbot-trend -n 30 --no-pager
```

Ver su estado (balance, posiciones y últimos trades):

```bash
cd /opt/Ingresosbot
sudo -u ingresosbot .venv/bin/python state_manager.py --db data/trend_state.db
```

Apagarlo: `systemctl disable --now ingresosbot-trend`. El bot oficial no se entera de nada de esto.

**Cambiar parámetros a mano:** edita `deploy/trend_params.json` con `nano deploy/trend_params.json` (guardar: `Ctrl+O` y luego `Enter`; salir: `Ctrl+X`) y reinicia con `systemctl restart ingresosbot-trend`. Ojo: `git pull` puede chocar si cambias ese archivo; si pasa, avísame.

El panel web sigue mostrando el bot oficial. Si Trend Rider convence en paper, el siguiente paso es un segundo panel o un selector en el mismo.

## 6. Para el ejecutor real (n8n / CCXT), cuando llegue el momento

- La orden de Trend Rider trae `take_profit: null`, `break_even: null` y `trailing_stop: {"atr_mult": 4, ...}`.
- El webhook recibe el evento `trailing_stop` con `new_stop_loss` cada vez que el stop sube: el ejecutor debe **cancelar el stop anterior y poner el nuevo** en el exchange.
- El campo `bot` del webhook dice `"Trend Rider"` u `"official"` para no mezclar los dos.
