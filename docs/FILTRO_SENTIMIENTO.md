# ¿Ayuda un filtro de noticias o sentimiento a Trend Rider? (ene-2024 a oct-2026, 500 USD)

Pregunta: ¿conviene que el bot mire noticias, el miedo/codicia del mercado o el calendario de la FED antes de entrar, usando fuentes gratis o IA barata?

**Respuesta corta: no, por ahora.** Ningún filtro probado mejora a Trend Rider de forma robusta. El que parece ayudar con BTC+ETH+SOL empeora la media de las 20 combinaciones de pares. Por eso el enganche queda en el motor, **apagado**, y el bot 24/7 sigue igual.

## 1. Cómo se probó

- Trend Rider recomendado (`deploy/trend_params.json`: Donchian 20, SL 2 ATR, trailing 5 ATR, filtro SMA200 diaria), 4h, velas reales de Binance.
- Una sola cuenta de **500 USD** por grupo, con comisión 0.08% y deslizamiento 0.02%, operando desde el 1-ene-2024. Las 20 combinaciones de 2 y 3 pares de BTC, ETH, SOL, BNB y XRP, igual que `LAB_2024_2026.md`.
- El filtro entra a la **Capa 1** como `external_risk` (ver SKILL_3, sección 9): `halt` bloquea entradas nuevas mientras dure y `throttle` baja el riesgo a 0.75%. Nunca cierra posiciones abiertas ni cambia las capas 2 y 3.
- **Fear & Greed** de alternative.me (diario, gratis). La vela que abre el día D usa el valor publicado a las 00:00 UTC de D. También se probó con el valor del día anterior.
- **Calendario macro**: días de decisión de la FED (FOMC) y de inflación de EE. UU. (CPI). Se bloquea todo el día UTC y la primera vela del día siguiente.

## 2. Resultado (20 grupos)

| Filtro | Retorno medio | vs sin filtro | Grupos mejor / peor | Peor grupo | Caída máx. media |
|---|---|---|---|---|---|
| **Sin filtro** | **+86.5%** | — | — | +28.2% | 18.9% |
| Pausa si F&G >= 70 | +38.8% | -47.7 pts | 5 / 15 | -4.6% | 14.4% |
| Pausa si F&G >= 75 | +45.4% | -41.0 pts | 8 / 12 | +6.2% | 15.7% |
| Pausa si F&G >= 80 | +83.7% | -2.8 pts | 7 / 13 | +25.6% | 18.4% |
| Pausa si F&G >= 85 | +86.5% | 0.0 pts | 5 / 2 | +29.7% | 18.6% |
| Pausa si F&G >= 75 (dato de ayer) | +87.6% | +1.1 pts | 11 / 9 | +31.3% | 16.4% |
| Riesgo 0.75% si F&G >= 70 | +73.2% | -13.3 pts | 9 / 11 | +39.4% | 15.8% |
| Riesgo 0.75% si F&G >= 75 | +69.9% | -16.6 pts | 7 / 13 | +29.2% | 17.0% |
| Pausa si F&G <= 15 o <= 25 (pánico) | +86.5% | 0.0 pts | 0 / 0 | +28.2% | 18.9% |
| Pausa si F&G salta >= 15 puntos en un día | +80.1% | -6.4 pts | 5 / 15 | +28.3% | 19.7% |
| Pausa en días FOMC | +56.7% | -29.8 pts | 6 / 13 | +19.3% | 18.9% |
| Pausa en días FOMC + CPI | +58.2% | -28.3 pts | 6 / 14 | +18.1% | 19.4% |

Lo que dicen los números:

1. **Filtrar por euforia (F&G alto) baja la caída máxima, pero se come las mejores subidas.** Trend Rider gana con pocas tendencias grandes, y esas empiezan justo con el mercado codicioso. En los grupos con XRP, la pausa con F&G >= 75 quitó en promedio 88 puntos, porque bloqueó la entrada al rally de XRP de noviembre de 2024. Sin XRP ganó +6 puntos de media: poca ganancia contra una pérdida rara pero enorme.
2. **No es estable.** Con 75 pierde 41 puntos, con 78-80 casi nada, y con el dato de ayer gana 1 punto. Un cambio de un día o de 3 puntos lo da vuelta. Eso es ruido, no ventaja.
3. **En BTC+ETH+SOL (lo que corre en el VPS) la pausa con 75 habría dado +65.7% en vez de +50.0%.** Elegir el filtro porque ganó justo en ese grupo sería ajustar a la historia. En 2024-2026 hubo solo 2-3 episodios de euforia, muy pocos para confiar.
4. **El filtro de pánico (F&G bajo) no cambia nada**, porque el filtro SMA200 diaria ya deja al bot fuera en los mercados bajistas.
5. **No operar los días de FED/CPI empeora el resultado.** En 4h el bot no compite en el minuto del dato, y lo que se pierde son rupturas buenas de esos días.

## 3. Noticias en tiempo real e IA: costo y por qué no se probaron

Las noticias sueltas (hackeos, delistings, titulares) no tienen un archivo gratis con hora exacta para hacer un backtest honesto. Además, el mercado descuenta una noticia en segundos o minutos, y un bot de 4h llega tarde por diseño. Por eso no se conectan a las órdenes.

| Fuente | Costo al mes | Sirve para backtest | Uso razonable |
|---|---|---|---|
| Fear & Greed (alternative.me) | 0 USD | Sí (probado arriba) | Ninguno por ahora |
| Calendario FOMC/CPI (lista fija) | 0 USD | Sí (probado arriba) | Ninguno por ahora |
| RSS de CoinDesk / Cointelegraph + palabras clave | 0 USD | No | Alerta a Telegram ante hackeo del exchange, retiros pausados o una stablecoin que pierde el 1:1 |
| CryptoPanic, plan gratis | 0 USD (pide clave, con límites) | Historial corto | Igual que RSS |
| Funding / open interest (APIs públicas) | 0 USD | Sí, en el VPS | Ya cubierto por el escáner de arbitraje (PR #9) |
| Claude Haiku 5.5 clasificando titulares | Unos 0.05 USD (4 llamadas al día con unos 50 titulares) y unos 0.30 USD aunque fuera cada hora | No | Resumir y priorizar alertas, nunca abrir órdenes |

Precios de Claude Haiku 5.5: 0.10 USD por millón de tokens de entrada y 0.50 USD por millón de salida. Cuatro lotes al día de unos 2,500 tokens de entrada y 300 de salida dan unos 0.3 M de entrada y 0.04 M de salida al mes, es decir, unos 0.05 USD. Con la API de lotes (Batch) cuesta la mitad. **Lo que no compensa no es el precio de la IA. Es que no hay ventaja medible que pagar.**

## 4. Qué sí suma con 500 USD (sin atajos)

- **Bajar comisiones.** En el laboratorio, Trend Rider pagó unos 34 USD de comisiones sobre 250 USD de ganancia. En Binance, pagar las comisiones con BNB las baja un 25%: con el mismo sistema son unos 8 USD más en el bolsillo.
- **Los aportes mensuales.** Con 500 USD, sumar capital de a poco pesa más que cualquier filtro.
- **Mantener lo probado:** Trend Rider en paper, revisar a mediados de noviembre y el carry de funding solo si pasa del 15% anual (PR #9).
- **Evitar:** grupos de señales pagos, bots que prometen un X% mensual, copy trading, apalancamiento alto y cualquier "pump" o manipulación. Además de ser ilegal, quien entra tarde es el que paga la salida de los demás.

## 5. Repetirlo en el VPS

```bash
cd /opt/Ingresosbot
python sentiment_lab.py --csv-dir data/candles --out-md data/filtro_sentimiento.md --out-csv data/filtro_sentimiento.csv
```

Descarga el Fear & Greed de alternative.me (o usa `--fng-csv`) y corre los 13 filtros en los 20 grupos. Un filtro nuevo es un objeto `Gate` o cualquier función `vela -> {"level", "reasons"}` que se pasa a `backtester.run_portfolio_backtest(..., risk_gate=...)`. Solo debería activarse en el bot si mejora la mayoría de los grupos y no solo uno.
