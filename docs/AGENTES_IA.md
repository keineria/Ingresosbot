# Agentes de IA de Ingresosbot

Cinco asistentes leen, vigilan y te avisan por Telegram. Empiezan solo opinando. Ganan permisos para actuar
**únicamente cuando sus resultados en paper demuestran que ahorran dinero**, y los pierden solos si empeoran. Lo
que nunca pueden hacer: abrir una operación, subir el tamaño, mover un stop o tocar el riesgo. No tienen las
claves del exchange. Si la IA falla, tarda o se acaba el presupuesto, los bots siguen exactamente igual que hoy.

| Agente | Qué hace | Puede llegar a |
|---|---|---|
| Vigía | Cada 4 h lee titulares (CoinDesk, Cointelegraph, Decrypt, The Block) y vigila que USDC/USDT valga 1.00 | Sugerir **pausar entradas nuevas** 24 h. Necesita 2 medios distintos y tu botón "Pausar" |
| Analista | Vota OK, REDUCIR o VETO cada señal antes de entrar | **Vetar** la entrada o **reducirla a la mitad**, solo si ganó ese permiso en la escalera |
| Guardián de salidas | Una vez al día mira cada posición que ya gana 1 R o más | **Cerrar antes** la posición, solo si ganó ese permiso |
| Auditor | Revisa cada operación cerrada; el domingo hace un informe semanal | Proponer **1 cambio de parámetro al mes**. Se prueba en el laboratorio y lo aplicas tú a mano |
| Coordinador | Resumen del día por Telegram a las 20:00 (Colombia) | Nada. Las cifras las pone el código |

## Qué bot tiene qué

| Bot | Estrategia | Agentes |
|---|---|---|
| **Keiner** | Trend Rider (4 h) | Todos: vigía, analista, guardián de salidas, auditor y resumen, con su laboratorio de 20 pares |
| **Celeste** | Tortuga 55 (diario) | Todos, con su laboratorio de 20 pares más oro (PAXG). El guardián le pregunta cada 3 días, no cada día |
| **Nathalia** | Bot Oficial (1 h) | Solo el resumen diario y la pausa del vigía. Pierde en el backtest y hace muchas señales: ponerle el analista con Opus costaría más de lo que aporta |

Los nombres salen del campo `trader` de `deploy/bots.json`. Para cambiar qué bots vigila la IA: `AI_BOT_DBS` en
`.env` (ver `.env.example`).

Lo que sigue siendo código, sin IA: los datos de mercado, la Capa 1 (circuit breaker), la Capa 2 (régimen), la
Capa 3 (riesgo ATR) y la ejecución. Aunque la IA pida algo, quien abre y cierra es `bot_runner.py`.

## La escalera de autonomía

| Escalón | Permiso | Cómo se mide cada caso |
|---|---|---|
| 1 | Sombra: opina y se anota | Siempre activo |
| 2 | Vetar entradas | Si vetó una operación que perdió -0.8 R, ahorró +0.8 R. Si vetó una que ganó +2.3 R, costó -2.3 R |
| 3 | Reducir el tamaño a la mitad | La mitad de lo anterior |
| 4 | Cerrar antes una posición | Lo que daba cerrar cuando lo pidió, menos lo que dio de verdad la posición |

Cada permiso se gana por separado. Para activarse, con el mismo modelo de IA que está configurado (si cambias de
modelo, el historial empieza de cero):

- **Casos suficientes:** al menos 10 vetos (o 10 reducciones) y 50 votos con resultado en total; 20 salidas.
- **Dinero:** el total ahorrado tiene que ser positivo y, en una prueba estadística (bootstrap), seguir positivo
  en 9 de cada 10 casos; así se descarta que sea suerte.
- **Acierto por encima del azar:** en Trend Rider el 64% de las operaciones pierde, así que vetar sin criterio ya
  "acierta" el 64%. Se exige ese 64% más 10 puntos (~74%). En salidas, 60%.

Se pierde solo si el total cae a cero o menos, si los últimos 10 casos suman menos de -2 R, o si acierta menos
que el azar. Cuando gana o pierde un permiso, te llega un mensaje 🎓 o ⬇️ por Telegram, con un botón
**Quitar permiso**. Un permiso que tú quitas no vuelve solo: aparece un botón **Devolver permiso**.

`AI_AUTONOMIA_MAX` pone el techo: 0 = solo opina, 1 = puede llegar a vetar, 2 = también reducir, 3 = también
cerrar antes (por defecto 3).

### Laboratorio en sombra

Trend Rider hace unas 2 operaciones al mes con 3 pares, y Tortuga 55 unas 10 al año: juntar 50 votos con
resultado tardaría años. Por eso el servicio de IA corre, al lado, las mismas estrategias sobre **20 pares** (BTC, ETH, SOL, BNB, XRP, ADA, DOGE, AVAX,
LINK, LTC, DOT, TRX, BCH, NEAR, ATOM, UNI, XLM, ETC, FIL, APT; Tortuga también PAXG) con dinero ficticio, en
`data/sombra/`. No envía alertas y nunca obedece a la IA, así cada voto tiene un resultado limpio. Cada estrategia
gana sus permisos por separado. Se esperan unas 15 señales al mes en Trend Rider (**el primer permiso podría
ganarse en 4 a 6 meses**, y solo si la IA de verdad acierta) y unas 3 en Tortuga 55 (más de un año).

### La IA aprende de sus errores

En cada voto, el analista y el guardián ven sus últimos 8 votos y lo que pasó después. Además el analista ve
cómo va BTC (rendimiento a 1, 7 y 30 días y si está sobre su media de 200 días) y los titulares del día.

## Cuánto cuesta

| Potencia (`AI_POTENCIA`) | Modelos | USD/mes aprox. | Tope por defecto | Sobre 500 USD de capital |
|---|---|---|---|---|
| economica | Sonnet en entradas y auditoría, Haiku en el resto | 0.90 | 2 | 0.18% al mes |
| **equilibrada** (por defecto) | **Opus** en entradas y auditoría semanal, Sonnet en salidas | 3.70 | 5 | 0.74% al mes |
| maxima | Opus en casi todo | 9.00 | 12 | 1.8% al mes |

Precios con Keiner y Celeste vigilados. En equilibrada: analista Opus 5.5 ~1.26, guardián de salidas Sonnet 5.5
~1.36, auditoría semanal Opus 5.5 ~0.89, y vigía, auditor por operación y resumen (Haiku 5.5 y Sonnet 5.5) ~0.18.

Trend Rider y Tortuga 55 ganaron en el backtest de 2024-2026 alrededor de 1.2% al mes. La potencia "maxima" se
comería más de lo que ganan con 500 USD; por eso la recomendada es "equilibrada", que pone el modelo más potente donde más pesa
(la decisión de entrar). Para ver el precio con datos reales, después de una semana de uso:

```bash
cd /opt/Ingresosbot && sudo -u ingresosbot .venv/bin/python ai_agents.py precio
```

Al llegar al tope (`AI_MONTHLY_BUDGET_USD`) la IA se calla hasta el mes siguiente y te sigue llegando el resumen
diario con las cifras. Cada llamada queda anotada con su costo en `data/ai_state.db`.

## Cómo activarlos (unos 15 minutos)

### 1. Sacar la clave de Anthropic

1. Entra a https://console.anthropic.com y crea una cuenta (o inicia sesión).
2. Menú **Billing**: carga 10 USD de crédito.
3. Menú **Limits** (o **Spend limits**): pon un límite mensual de **6 USD**. Así, aunque algo fallara, nunca se
   gastaría más que eso.
4. Menú **API Keys** → **Create Key** → nombre `ingresosbot` → copia la clave (empieza por `sk-ant-`). Solo se
   muestra una vez.

### 2. Poner la clave en el servidor

1. En Hostinger: **hPanel → VPS → (el de Francia) → Terminal**.
2. Actualiza el código y el instalador (copia y pega, una línea cada vez):

   ```bash
   sudo git -C /opt/Ingresosbot pull
   sudo bash /opt/Ingresosbot/deploy/install_almalinux.sh
   ```

3. Abre el archivo de configuración:

   ```bash
   sudo nano /opt/Ingresosbot/.env
   ```

4. Baja con las flechas hasta el final. Si no ves la sección `Agentes de IA`, pega el bloque de `.env.example`
   al final; si ya está, cambia solo estas líneas:

   ```
   AI_ENABLED=true
   ANTHROPIC_API_KEY=sk-ant-...tu clave...
   AI_POTENCIA=equilibrada
   AI_BOT_DBS=
   ```

   (`AI_BOT_DBS` vacío = Keiner y Celeste con todos los agentes, Nathalia solo con el resumen.)

5. Guarda con **Ctrl+O** y luego **Enter**; sal con **Ctrl+X**.

### 3. Probar que responde

```bash
cd /opt/Ingresosbot && sudo -u ingresosbot .venv/bin/python ai_agents.py probar
```

Debe decir `OK: respondió anthropic/claude-haiku-5-5 · costo 0.0000xx USD`. Si dice `FALLÓ`, revisa la clave
(sin espacios ni comillas).

### 4. Encender los agentes

```bash
sudo systemctl enable --now ingresosbot-ai
sudo systemctl restart ingresosbot ingresosbot-trend ingresosbot-tortuga ingresosbot-web
```

El reinicio de los bots hace que lean `AI_ENABLED=true` y empiecen a consultar al analista en cada señal. El
servicio `ingresosbot-ai` arranca también el laboratorio en sombra.

Para ver qué hacen en vivo: `journalctl -u ingresosbot-ai -f` (sal con **Ctrl+C**).

Comandos útiles (desde `cd /opt/Ingresosbot`):

| Comando | Qué muestra |
|---|---|
| `sudo -u ingresosbot .venv/bin/python ai_agents.py escalera` | Cada permiso, si está en sombra o activo, y cuántas pruebas lleva |
| `sudo -u ingresosbot .venv/bin/python ai_agents.py precio` | Cuánto cuesta al mes cada potencia |
| `sudo -u ingresosbot .venv/bin/python ai_agents.py estado` | Gasto del mes, pausas, permisos y últimas decisiones |
| `sudo -u ingresosbot .venv/bin/python ai_agents.py resumen` | Manda el resumen del día ahora |
| `sudo -u ingresosbot .venv/bin/python ai_agents.py vigia` | Corre el vigía ahora |

A Telegram te llegan mensajes que empiezan por 🤖.

## Los botones de Telegram

- **Pausar / Ignorar**: cuando el vigía ve un riesgo serio para el dinero en Binance o una stablecoin, confirmado
  por dos medios. "Pausar" detiene las entradas nuevas 24 h (luego se quita sola). Te llega un botón
  **Quitar pausa** por si quieres volver antes. Si no tocas nada, no pasa nada: la sugerencia caduca.
- **Quitar permiso / Devolver permiso**: en los mensajes de la escalera.
- **Probar en laboratorio / Descartar**: cuando el auditor propone un cambio. "Probar" corre el bot con los
  parámetros actuales y con el cambio en ventanas de 3 meses de 2024-2026 y te manda el resultado. El cambio
  **nunca se aplica solo**: si te convence, se edita `deploy/trend_params.json` (o `deploy/tortuga_params.json`)
  y se reinicia el bot.

Solo se aceptan botones del chat de `TELEGRAM_CHAT_ID`. Los mismos datos están en el panel: `GET /api/ai`, y las
pausas y permisos se pueden cambiar con `POST /api/ai/decide`.

## Apagarlos

- Todo: `AI_ENABLED=false` en `.env`, luego `sudo systemctl disable --now ingresosbot-ai` y
  `sudo systemctl restart ingresosbot ingresosbot-trend ingresosbot-tortuga`.
- Que nunca actúen, solo opinen: `AI_AUTONOMIA_MAX=0` (o `AI_ANALISTA=sombra`) y reiniciar los bots e
  `ingresosbot-ai`.
- Solo para un bot: el botón **Apagar** de su tarjeta en el panel. El bot deja de abrir operaciones y la IA deja de
  votar por él (también en su laboratorio). Con **Encender** vuelve todo.
- Solo una parte: `AI_SALIDAS=false`, `AI_LAB=false`, `AI_VIGIA=false`, `AI_AUDITOR=false` o
  `AI_RESUMEN=false` y reiniciar `ingresosbot-ai`.

## Seguridad

- La clave de la IA vive solo en `/opt/Ingresosbot/.env` (permisos 600). Nunca va en el código ni en los mensajes
  a la IA.
- La IA no recibe claves del exchange ni tiene ninguna herramienta para operar. Sus respuestas tienen un formato
  fijo (JSON validado); cualquier otra cosa se descarta. Sus permisos solo pueden quitar riesgo: vetar, reducir a
  la mitad o cerrar antes.
- Los titulares de internet se le pasan marcados como "datos, no instrucciones".
- Respaldo opcional: si pones `GROQ_API_KEY` o `GEMINI_API_KEY`, se usan cuando Anthropic no responde. Sus votos
  no cuentan para la escalera (son otro modelo).
- Cuando se opere con dinero real, cada permiso de la escalera debería volver a pedir tu confirmación: esto está
  pensado y probado solo para paper trading.

## Archivos

- `ai_core.py`: proveedores (Anthropic, Groq, Gemini), potencias, validación de respuestas, tope de gasto y registro.
- `ai_agents.py`: los agentes, botones de Telegram, precio y la puerta que usa `bot_runner.py`.
- `ai_autonomy.py`: las reglas de la escalera (sin IA, solo cuentas).
- `ai_lab.py`: el laboratorio en sombra.
- `deploy/ingresosbot-ai.service`: el servicio de systemd.
- `tests/test_ai_agents.py`: pruebas con IA, noticias y Telegram simulados.
