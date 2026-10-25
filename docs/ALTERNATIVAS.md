# Estrategias y activos alternativos (octubre 2026)

Qué más se puede operar con el mismo bot, qué ya se descartó con datos y cómo poner en marcha el bot nuevo, **Tortuga 55**. Todo en paper trading: dinero simulado.

## 1. Respuesta corta

| Alternativa | Veredicto | Por qué |
|---|---|---|
| **Tortuga 55** (ruptura de 55 días en velas diarias) | **Bot nuevo, en paper** | 22 de sus 24 variantes ganaron en las dos mitades de la prueba. La elegida dio +51,5% con la menor caída (12,1%). |
| Rotación por momentum (comprar las monedas que más subieron) | Descartada | Perdió en el periodo de validación en todas las variantes probadas, con caídas de hasta el 40-67%. |
| Media móvil por moneda / cesta rebalanceada | Funciona, pero no aporta | Con las reglas de riesgo del bot (1,5% por operación) queda peor que Trend Rider. Sin ellas, cae un 30-60%. |
| Oro tokenizado (PAXG en Binance) | Incluido en Tortuga 55 | Es el único activo no cripto que se opera con la misma cuenta, la misma API y las mismas comisiones. Aún no está probado con datos: ver la sección 5. |
| ETFs o acciones por bróker (IBKR, Alpaca) | No por ahora | Con 500 USD, la comisión mínima de 1 USD por orden de IBKR se come entre el 10 y el 20% al año. |
| Forex / CFD | No | Apalancamiento alto, sin ventaja demostrada para cuentas pequeñas. |
| Grid bot (comprar y vender en un rango) | No | No lo probé con datos, pero por diseño gana solo en mercado lateral. En una tendencia bajista como la de 2026 va comprando la moneda mientras cae. |
| DCA (comprar un poco cada mes) | Hábito de ahorro, no estrategia | No da ventaja sobre el mercado. Si vas a aportar dinero cada mes, súmalo al balance del bot que mejor vaya. |
| Arbitraje entre exchanges | Ver su propio informe | Se evaluó aparte: solo el *funding* tiene sentido, y deja de 1 a 4 USD al mes con 500 USD. |

## 2. Cómo se probó

- **Datos reales de Binance**: velas de 4h de BTC, ETH, SOL, BNB y XRP, de enero de 2023 a octubre de 2026. Las diarias se arman con ellas. 2023 solo sirve para calentar los indicadores, así que se opera desde el 1 de enero de 2024.
- **500 USD**, comisión de 0,08% por orden más 0,02% de deslizamiento.
- **Corte 70/30**: entrenamiento del 1 de enero de 2024 al 19 de diciembre de 2025, y validación del 19 de diciembre de 2025 al 23 de octubre de 2026. La validación es un periodo bajista: la cesta de las 5 monedas perdió -17,6%, con caídas de hasta 47,5%. Una estrategia solo es creíble si gana en las dos partes.
- Se repite con `alt_lab.py` (sección 5).

### A. Ideas de cartera (sin las reglas de riesgo del bot)

| Variante | Retorno 2024-2026 | Caída máx. | Validación 2026 | Caída en validación |
|---|---|---|---|---|
| Comprar y mantener (cesta de 5, rebalanceo mensual) | +97,1% | 61,9% | -17,6% | 47,5% |
| Media móvil de 50 días por moneda | +152,9% | 32,7% | +5,0% | 24,1% |
| Media móvil de 200 días por moneda | +73,2% | 39,2% | +9,3% | 11,6% |
| Rotación 56 días, la mejor moneda | +184,1% | 51,8% | -10,2% | 35,1% |
| Rotación 28 días, las 2 mejores | +61,7% | 44,5% | -20,0% | 35,6% |

La rotación parece muy buena en el periodo de entrenamiento (hasta +216%) y se hunde en la validación: es el ejemplo clásico de una estrategia que solo funciona hacia atrás. Con solo 5 monedas, elegidas además porque hoy son las grandes, ese resultado ni siquiera es fiable. La media móvil sí protege en la bajada, pero invierte todo el capital, así que una caída del 33-39% sigue siendo demasiado para 500 USD.

### B. Con el motor real (Capa 1 → Capa 2 → Capa 3, una sola cuenta)

Ruptura de Donchian en velas **diarias**, con 24 combinaciones: 20 o 55 días, SL a 2 o 3 ATR, trailing a 4, 5 u 8 ATR, y con o sin filtro de la media de 200 días.

| | Retorno 2024-2026 | Caída máx. | Validación 2026 | Operaciones | Comisiones |
|---|---|---|---|---|---|
| Las 24 variantes (mediana) | +36,6% | 18,8% | +2,7% | — | — |
| Positivas en las dos partes | 22 de 24 | | | | |
| **Tortuga 55**: 55 días · SL 3 ATR · trailing 5 ATR · SMA200 diaria | **+51,5%** | **12,1%** | **+4,1%** | 30 | 3,77 USD |
| Trend Rider (4h, las mismas 5 monedas, como referencia) | +69,9% | 18,0% | -0,7% | 68 | 34,10 USD |

**Por qué Tortuga 55 y no otra**
- La ruptura de 55 días no salió de buscar en estos datos. Es la entrada del "Sistema 2" de los Turtle Traders (Richard Dennis, años 80), una de las reglas de seguimiento de tendencia más conocidas. La evidencia publicada sobre seguir tendencias es amplia: por ejemplo, Hurst, Ooi y Pedersen (2017) la encuentran en más de 100 años de datos de acciones, bonos, divisas y materias primas.
- Tiene la **menor caída** de las 24 variantes (12,1%), la mitad de operaciones que Trend Rider y una décima parte de sus comisiones.
- Sus vecinas (55 días con otros SL y trailing) también ganan en las dos partes. No depende de un ajuste fino.

**Lo que hay que saber**
- La combinación exacta (SL 3, trailing 5, filtro de 200 días) es la mejor de 24 en todo el periodo. Eso la favorece un poco, y es por eso que corre en paper antes de cualquier otra cosa.
- Gana menos que Trend Rider en años alcistas. Se parecen bastante: los dos siguen tendencias en las mismas monedas, y cuando cripto cae ninguno gana mucho. La diferencia de verdad la pone el **oro**.
- Opera poco, unas 10 entradas al año. Habrá semanas sin ninguna operación, y eso es normal.

## 3. Activos fuera de cripto

- **Oro (PAXG)**: en Binance, cada PAXG equivale a una onza de oro física. Se compra en spot igual que BTC (par `PAXGUSDT`), con la misma API, el mismo mínimo por orden (5-10 USD) y la misma comisión. El oro suele moverse distinto que cripto, y los Turtle Traders operaban justamente materias primas como el oro. Por eso Tortuga 55 lo incluye desde el primer día en paper. Ojo: este entorno no pudo descargar su historia, así que **no hay backtest del oro todavía** (se hace en el VPS, sección 5).
- **ETFs y acciones**: la evidencia de seguir tendencias con ETFs (Faber 2007, "timing" con media de 10 meses) es buena, pero con 500 USD las comisiones lo hacen inviable. IBKR cobra mínimo 1 USD por orden: rebalancear 4 ETFs cada mes cuesta de 4 a 8 USD, entre el 10 y el 20% del capital al año. Hay brókers sin comisión con API (como Alpaca), pero antes hay que comprobar que acepten residentes de Colombia. Para el bróker cuenta el **país de residencia**, no dónde está el VPS: desde la UE, por ejemplo, no se pueden comprar ETFs de EE. UU. Volver a mirarlo con un capital de 3.000-5.000 USD.
- **Forex y CFD**: no. Son brókers de apalancamiento alto y no hay una ventaja demostrada para cuentas pequeñas.

## 4. Encender Tortuga 55 en el VPS

Se hace en la **Terminal de hPanel** (Hostinger → VPS → Terminal), como root. Copia cada bloque y pulsa Enter.

```bash
# 1. Traer el código nuevo
cd /opt/Ingresosbot
git pull origin main

# 2. Reinstalar los servicios (copia ingresosbot-tortuga y el informe diario; no toca la .env)
sudo bash deploy/install_almalinux.sh

# 3. Encender Tortuga 55 y reiniciar el panel para que muestre los 3 bots
sudo systemctl enable --now ingresosbot-tortuga
sudo systemctl restart ingresosbot-web

# 4. Comprobar que arrancó (sal con Ctrl+C)
journalctl -u ingresosbot-tortuga -f
```

- Trabaja con **velas diarias**: hace su análisis una vez al día, a las 00:00 UTC (las 19:00 en Colombia). El primer día puede que solo diga "Esperar".
- Su log está en `logs/bot-tortuga.log`. Las alertas de Telegram llegan con el prefijo **[Tortuga 55]**.
- En el panel (bot.commonbird.com) aparecen arriba **tres tarjetas**: Bot Oficial, Trend Rider y Tortuga 55. Pulsa una para ver su cuenta, sus operaciones y la ficha **"Cómo opera"**. El Kill Switch y "Escanear ahora" actúan solo sobre el bot que tengas seleccionado.
- Toda la configuración de los bots (nombre, pares, temporalidad, parámetros y textos del panel) está en `deploy/bots.json`. Los parámetros de Tortuga están en `deploy/tortuga_params.json`.

## 5. Validar el oro y la historia larga en el VPS

El VPS sí llega a Binance Vision, así que puede descargar desde 2019 y repetir las pruebas con PAXG. Tarda unos 5 minutos:

```bash
cd /opt/Ingresosbot
sudo -u ingresosbot .venv/bin/python alt_lab.py --fetch --limit 2500 \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,PAXGUSDT \
  --trade-from 2021-01-01 --out-md data/alt_lab_vps.md > /dev/null
cat data/alt_lab_vps.md
```

Copia lo que salga en el hilo. Si Tortuga 55 pierde con el oro o con la caída de 2022, se quita PAXG de `deploy/bots.json` o se ajusta antes de seguir.

## 6. Informe diario para seguir los bots desde Claude

Cada día a las 00:20 UTC, el VPS escribe un resumen de los tres bots: balance, operaciones, posiciones y última decisión de cada par. Lo sube a la rama **`bot-reports`** del repositorio en GitHub. No incluye claves ni datos de la cuenta real. Así Claude puede leer cómo van los bots sin entrar al VPS ni pedirte logs.

**Una sola vez** (unos 5 minutos):

1. En la **Terminal de hPanel**, crea una llave solo para esto:
   ```bash
   sudo mkdir -p /opt/ingresosbot-reports && cd /opt/ingresosbot-reports
   sudo ssh-keygen -t ed25519 -N "" -C "ingresosbot-reports" -f deploy_key
   sudo cat deploy_key.pub
   ```
   Copia la línea completa que empieza por `ssh-ed25519`.
2. En **GitHub**: entra en el repositorio **keineria/Ingresosbot** → **Settings** → **Deploy keys** → **Add deploy key**. En *Title* pon `VPS informes`, pega la línea en *Key*, marca **Allow write access** y pulsa **Add key**. Esta llave solo sirve para este repositorio.
3. De vuelta en la Terminal, prepara la rama y enciende el informe:
   ```bash
   cd /opt/Ingresosbot
   sudo chown -R ingresosbot:ingresosbot /opt/ingresosbot-reports
   sudo -u ingresosbot .venv/bin/python daily_report.py --init-repo /opt/ingresosbot-reports/repo --ssh-key /opt/ingresosbot-reports/deploy_key
   sudo systemctl start ingresosbot-report        # primer informe ahora mismo
   sudo systemctl enable --now ingresosbot-report.timer
   ```
4. Comprueba que en GitHub, en la rama `bot-reports`, aparece `reports/latest.md`.

> Si el repositorio es público, los informes también lo son. Solo muestran saldos simulados, pero si prefieres que nadie los vea, pon el repositorio en privado (GitHub → Settings → General → Danger Zone → Change visibility).

Para verlo a mano en cualquier momento: `sudo -u ingresosbot .venv/bin/python daily_report.py`.

## 7. Antes de usar dinero real

- Ningún bot pasa a dinero real hasta que gane en paper varias semanas y lo que haga en vivo se parezca a lo que dijo el backtest. Eso se revisa con el informe diario.
- Los resultados pasados no garantizan los futuros. Con 500 USD, un +50% en casi 3 años son unos 85 USD al año: sirve para hacer crecer el capital con el riesgo controlado, no como ingreso mensual.
- Si los tres bots corren a la vez con dinero real, cada uno necesita su propio capital: en paper, cada uno simula 500 USD por separado.
