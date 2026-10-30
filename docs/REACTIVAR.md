# Reactivar a Keiner (Trend Rider) y Celeste (Tortuga 55)

30-oct-2026. Los dos bots llevaban cinco días en paper sin una sola operación. Keiner pidió que operen más y que funcionen en mercado alcista, bajista y lateral, sin arriesgar más el capital (500 USD, sin apalancamiento alto).

## 1. Por qué no operaban

No era una falla: los dos servicios estaban activos, leían cada vela y no tenían errores.

- **Trend Rider (4h):** en cada vela desde el 25-oct bloquearon `breakout_close_gt_max20` (ninguna moneda rompió su máximo de 20 velas) y casi siempre `macro_close_gt_ema200` (precio bajo la EMA 200, mercado `CHOP_RANGE` o `BEAR_DISTRIBUTION`).
- **Tortuga 55 (1d):** bloqueó `breakout_close_gt_max55` en las 6 monedas. El oro (PAXG) además estaba bajo su SMA de 200 días.
- En el último año, comprar y mantener esos pares perdió cerca de **-49%**. Quedarse fuera fue lo correcto para dos estrategias de tendencia que solo compran.

## 2. Cómo se probó

`revival_lab.py` usa el motor real (Capa 1 → 2 → 3, una cuenta compartida, 500 USD, comisión 0,08% y deslizamiento 0,02% por orden) con velas de Binance de 23 monedas y oro desde 2021 (las primeras 200 velas diarias solo calientan indicadores). Cada ventana empieza con 500 USD: 2022 (bajista), 2023, 2024, 2025, los últimos 12 meses, los últimos 3 meses y todo 2022 → 29-oct-2026. El resultado de cada operación también se agrupa según el régimen del mercado al entrar (alcista, bajista, lateral o clímax).

Regla para aceptar un cambio: no puede perder más en 2022 ni subir mucho la caída máxima, y tiene que ganar al menos lo mismo que la versión actual en el periodo completo.

Se corrieron 3 rondas en el VPS (`data/lab4/REACTIVAR*.md`, unos 9 minutos cada una con 2 procesos y `nice 19`).

## 3. Resultados

Retorno % · caída máxima %.

### Keiner · Trend Rider (4h)

| Variante | 2022 | 2023 | 2024 | 2025 | Últ. 12 m | Todo | Ops/mes |
|---|---|---|---|---|---|---|---|
| Actual (BTC ETH SOL) | 0 · 0 (sin operaciones) | +50 · 9.8 | +23.9 · 18.7 | +18.6 · 11.9 | +1.9 · 5.9 | **+125.5 · 18.7** | 0.6 |
| **12 monedas + oro, ADX ≥ 20 (elegida)** | -3.6 · 8.6 | +55.1 · 12.7 | +45.7 · 15.4 | +9.0 · 15.1 | +3.8 · 11.4 | **+185.3 · 19.6** | 3.6 |
| 12 monedas + ADX ≥ 25 | -0.7 · 6.8 | +41.4 · 13.3 | +17.6 · 24.7 | +21.8 · 14.1 | -3.6 · 15.1 | +89.2 · 24.7 | 3.3 |
| 12 monedas, sin ADX | -8.2 · 11.6 | +14.8 · 15.4 | +70.8 · 16.5 | +9.7 · 18.5 | -0.6 · 15.5 | +109.4 · 25.8 | 4.4 |
| 23 monedas | -17.2 · 22.4 | +87.6 · 21.1 | +71.7 · 21.1 | -4.8 · 20.1 | -7.2 · 24.7 | +156.8 · 25.8 | 7.4 |
| Filtro diario SMA100 | -14.7 · 21.1 | +36.9 · 13.2 | +24.6 · 18.9 | +18.9 · 11.9 | +12.6 · 8.2 | +96.5 · 22.6 | 1.2 |
| También en corto (Futuros 1x) | +38.0 · 18.4 | +31.2 · 17.4 | +24.3 · 16.3 | +4.9 · 21.6 | +4.3 · 20.6 | +152.5 · 23.9 | 4.2 |
| Corto + ADX ≥ 20 | +4.0 · 22.4 | +36.0 · 11.9 | +9.2 · 23.5 | -0.7 · 21.4 | -2.0 · 20.0 | +65.8 · 29.1 | 3.8 |
| 12 monedas + ADX + corto | +36.3 · 18.4 | +13.7 · 18.9 | +48.4 · 23.3 | -10.4 · 26.2 | -13.2 · 30.0 | +92.6 · 31.6 | 9.1 |
| Modo lateral (rebote en banda inferior) | -9.5 · 13.4 | +66.0 · 13.8 | +28.9 · 18.7 | -1.8 · 20.7 | -6.9 · 11.9 | +90.8 · 23.6 | 2.6 |

USD según el mercado al entrar (todo el periodo): actual alcista +383, bajista 0, lateral -13. Elegida: alcista +401, bajista 0, lateral **+312**.

- El ADX ≥ 20 es lo que convierte el lateral de pérdida a ganancia: deja fuera las rupturas sin fuerza.
- En bajista se queda en dólares. Los cortos en 4h no son robustos: solos ganan +38% en 2022, pero con ADX o con 12 monedas pierden en 2025 y en el último año. Por eso Keiner sigue solo comprando (Spot).
- El modo lateral en 4h tampoco sirvió (empeora 2022 y el último año). Queda en el motor, apagado.
- Ojo: con ADX ≥ 25 el resultado cae a +89%. El filtro funciona, pero el valor exacto importa; se revisa con los primeros meses de paper.

### Celeste · Tortuga (1d)

| Variante | 2022 | 2023 | 2024 | 2025 | Últ. 12 m | Todo | Ops/mes |
|---|---|---|---|---|---|---|---|
| Actual (55 días) | -1.3 · 4.3 | +23.1 · 14.0 | +32.6 · 11.4 | +15.9 · 9.6 | +3.7 · 9.1 | **+108.8 · 13.7** | 0.6 |
| **30 días + corto + ADX ≥ 20 (elegida)** | +7.6 · 10.5 | +17.0 · 17.2 | +55.4 · 13.3 | +20.6 · 9.2 | +16.9 · 8.3 | **+161.4 · 19.6** | 1.8 |
| 55 días + corto + ADX ≥ 20 | +4.6 · 10.2 | +17.0 · 16.0 | +45.0 · 12.2 | +18.3 · 9.2 | +6.8 · 8.3 | +119.7 · 21.2 | 1.8 |
| 30 días, solo compras | -1.5 · 6.2 | +25.7 · 17.0 | +47.4 · 12.6 | +22.4 · 9.9 | +5.8 · 9.1 | +145.0 · 17.2 | 0.6 |
| 30 días + ADX ≥ 20, solo compras | -2.6 · 5.7 | +21.4 · 14.0 | +60.2 · 13.3 | +17.8 · 8.9 | +3.5 · 8.4 | +123.6 · 13.6 | 0.6 |
| 55 días + modo lateral TP 2 ATR (solo compras) | +0.3 · 7.0 | +23.0 · 14.1 | +35.9 · 11.4 | +19.9 · 11.8 | +5.1 · 9.1 | +127.8 · 17.5 | 1.0 |
| 30 días + corto, sin ADX | +8.0 · 11.9 | +3.4 · 16.8 | +45.9 · 12.6 | -1.6 · 14.4 | +17.4 · 9.0 | +87.4 · 21.0 | 2.0 |

USD según el mercado al entrar: actual alcista +392, bajista 0, lateral -16. Elegida: alcista +346, bajista **+100**, lateral **+248**. Es la única que gana en los tres mercados y en cada año.

- Los cortos en velas diarias sí aguantan: con 55 o 30 días, con ADX, son positivos en todos los años.
- **Los cortos son solo para paper.** Con dinero real exigen Binance Futuros a 1x (sin apalancamiento), y hoy la llave prevista es solo Spot. Si se quiere Celeste en Spot, la alternativa es "55 días + modo lateral TP 2 ATR" (+128%, positiva en cada año y en los tres mercados, pero gana poco en bajista).

## 4. Qué cambió en los bots (paper)

| | Antes | Ahora |
|---|---|---|
| Keiner · Trend Rider | BTC ETH SOL, Donchian 20 | 12 monedas + oro (BTC ETH SOL BNB XRP PAXG DOGE ADA LINK TRX AVAX LTC), Donchian 20, `trend_min_adx: 20`, `max_position_pct: 30` |
| Celeste · Tortuga | Donchian 55, solo compras | Donchian 30, `trend_min_adx: 20`, `trend_allow_short: true` (mismas 6 monedas) |

`ingresosbot-trend.service` ahora arranca con `--bot trend`, igual que Tortuga: las monedas salen de `deploy/bots.json` y no del archivo del servicio. Las dos cuentas siguen en 500 USD; como no tenían operaciones, no hace falta reiniciar sus bases.

Repetir la prueba en el VPS:

```
cd /opt/Ingresosbot && sudo -u ingresosbot nice -n 19 .venv/bin/python revival_lab.py --csv-dir data/lab4/velas --workers 2 --only base,u12_adx,d30_short_adx --out-md data/lab4/REPETIR.md
```

Son simulaciones con datos pasados. El resultado real puede ser distinto; por eso los dos siguen en paper y se revisan tras 3-4 semanas.
