# Agentes de IA de Ingresosbot

Cuatro asistentes que leen, vigilan y te avisan por Telegram. **Ninguno puede abrir, cerrar ni agrandar una
operación**: no tienen las claves del exchange y el código solo les deja *endurecer* las reglas. Si la IA falla,
tarda o se acaba el presupuesto, los bots siguen exactamente igual que hoy.

| Agente | Qué hace | Modelo | Puede cambiar algo |
|---|---|---|---|
| Vigía | Cada 4 h lee titulares (CoinDesk, Cointelegraph, Decrypt, The Block) y vigila que USDC/USDT valga 1.00 | Haiku 5.5 | Sugiere **pausar entradas nuevas** 24 h. Necesita 2 medios distintos y tu botón "Pausar" |
| Analista | Vota OK o VETO cada señal de Trend Rider antes de entrar | Sonnet 5.5 | En **sombra** (por defecto) no bloquea nada: solo se anota para medir si acierta |
| Auditor | Revisa cada operación cerrada; el domingo hace un informe semanal | Sonnet 5.5 | Propone como máximo **1 cambio de parámetro al mes**. Se prueba en el laboratorio y lo aplicas tú a mano |
| Coordinador | Resumen del día por Telegram a las 20:00 (Colombia) | Haiku 5.5 | Nada. Las cifras las pone el código; la IA solo las explica |

Lo que sigue siendo código, sin IA: los datos de mercado, la Capa 1 (circuit breaker), la Capa 2 (régimen), la
Capa 3 (riesgo ATR) y la ejecución. La pausa del vigía entra en la Capa 1 igual que el kill switch: con ella el
bot no abre posiciones nuevas, y las abiertas siguen con su stop.

## Cuánto cuesta

Con los dos bots en paper, menos de **1 USD al mes** (vigía ~0.06, resumen ~0.02, analista ~0.30, auditor ~0.45).
Hay un tope: `AI_MONTHLY_BUDGET_USD=2`. Al llegar a él, la IA se calla hasta el mes siguiente y te sigue llegando el
resumen diario con las cifras, sin texto de IA. Cada llamada queda anotada con su costo en `data/ai_state.db`.

## Cómo activarlos (unos 15 minutos)

### 1. Sacar la clave de Anthropic

1. Entra a https://console.anthropic.com y crea una cuenta (o inicia sesión).
2. Menú **Billing**: carga 5 USD de crédito. Dura muchos meses.
3. Menú **Limits** (o **Spend limits**): pon un límite mensual de **3 USD**. Así, aunque algo fallara, nunca se
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

4. Baja con las flechas hasta el final. Si no ves la sección `Agentes de IA`, pega esto al final; si ya está,
   cambia solo estas dos líneas:

   ```
   AI_ENABLED=true
   ANTHROPIC_API_KEY=sk-ant-...tu clave...
   ```

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
sudo systemctl restart ingresosbot ingresosbot-trend ingresosbot-web
```

El reinicio de los bots hace que lean `AI_ENABLED=true` y empiecen a consultar al analista en cada señal.

Para ver qué hacen en vivo: `journalctl -u ingresosbot-ai -f` (sal con **Ctrl+C**).
Para ver su estado y el gasto del mes: `cd /opt/Ingresosbot && sudo -u ingresosbot .venv/bin/python ai_agents.py estado`.

### 5. Probar el resumen y el vigía sin esperar

```bash
cd /opt/Ingresosbot
sudo -u ingresosbot .venv/bin/python ai_agents.py resumen
sudo -u ingresosbot .venv/bin/python ai_agents.py vigia
```

A Telegram te llegan mensajes que empiezan por 🤖.

## Los botones de Telegram

- **Pausar / Ignorar**: cuando el vigía ve un riesgo serio para el dinero en Binance o una stablecoin, confirmado
  por dos medios. "Pausar" detiene las entradas nuevas 24 h (luego se quita sola). Te llega un botón
  **Quitar pausa** por si quieres volver antes. Si no tocas nada, no pasa nada: la sugerencia caduca.
- **Probar en laboratorio / Descartar**: cuando el auditor propone un cambio. "Probar" corre el bot con los
  parámetros actuales y con el cambio en ventanas de 3 meses de 2024-2026 y te manda el resultado. El cambio
  **nunca se aplica solo**: si te convence, se edita `deploy/trend_params.json` y se reinicia el bot.

Solo se aceptan botones del chat de `TELEGRAM_CHAT_ID`. Los mismos datos están en el panel: `GET /api/ai`, y las
pausas se pueden aprobar o quitar con `POST /api/ai/decide`.

## Cuándo el analista podría pasar de "sombra" a "activo"

En sombra el analista vota y el voto se compara con el resultado real de cada operación (en R). Aunque pongas
`AI_ANALISTA=activo`, el código lo mantiene en sombra hasta que haya **30 votos con resultado, al menos 5 vetos, y
las operaciones vetadas hayan rendido peor que las aprobadas**. Con Trend Rider eso tarda varios meses. La
simulación con datos reales mostró que vetar al azar quita ~30% de la ganancia, porque pocas operaciones grandes
hacen casi todo el beneficio; por eso la barra es alta.

## Apagarlos

- Todo: `AI_ENABLED=false` en `.env`, luego `sudo systemctl disable --now ingresosbot-ai` y
  `sudo systemctl restart ingresosbot ingresosbot-trend`.
- Solo una parte: `AI_ANALISTA=off`, `AI_VIGIA=false`, `AI_AUDITOR=false` o `AI_RESUMEN=false` y reiniciar
  `ingresosbot-ai` (y los bots, si es el analista).

## Seguridad

- La clave de la IA vive solo en `/opt/Ingresosbot/.env` (permisos 600). Nunca va en el código ni en los mensajes
  a la IA.
- La IA no recibe claves del exchange ni tiene ninguna herramienta para operar. Sus respuestas tienen un formato
  fijo (JSON validado); cualquier otra cosa se descarta.
- Los titulares de internet se le pasan marcados como "datos, no instrucciones". Aun si un titular intentara
  engañarla, lo peor que puede pasar es una sugerencia de pausa, que tú decides.
- Respaldo opcional: si pones `GROQ_API_KEY` o `GEMINI_API_KEY`, se usan cuando Anthropic no responde.

## Archivos

- `ai_core.py`: proveedores (Anthropic, Groq, Gemini), validación de respuestas, tope de gasto y registro.
- `ai_agents.py`: los cuatro agentes, botones de Telegram y la puerta que usa `bot_runner.py`.
- `deploy/ingresosbot-ai.service`: el servicio de systemd.
- `tests/test_ai_agents.py`: pruebas con IA, noticias y Telegram simulados.
