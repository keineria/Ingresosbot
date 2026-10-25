# Panel con todos los bots en vivo

`https://bot.commonbird.com` abre en **Todos los bots**: una tarjeta por estrategia, todas a la vez, que se refrescan
solas cada 15 segundos (los contadores de "hace X" y "próxima vela en X" se mueven cada segundo).

## Qué muestra cada tarjeta

| Parte | Qué significa |
|---|---|
| **Funcionando / Apagado / Sin encender / Sin señal** | Si el proceso del bot está vivo. El bot deja un *latido* en su base cada 60 s; sin latido en 5 min el panel pregunta a systemd (`systemctl is-active`). |
| **Capa 1 · Activo / Riesgo reducido / Detenido / Kill switch** | El Circuit Breaker de ese bot (racha de pérdidas y drawdown del día). |
| **Valor de la cuenta** | Balance + lo que van ganando o perdiendo las posiciones abiertas, con su % sobre el capital inicial de ese bot. |
| **Pérdidas seguidas / Drawdown de hoy** | Lo que vigila la Capa 1: a las 3 pérdidas seguidas o 3% de caída en el día se detiene 15 velas. |
| **Última vela / Próxima vela / Latido** | Cuándo decidió por última vez y cuándo vuelve a decidir (1h, 4h o 1 día según el bot). |
| **Qué decidió en la última vela** | La decisión por par: Esperar, Comprar, Mantener… Pasa el ratón (o mantén el dedo) para ver qué filtro la bloqueó. |
| **Posiciones abiertas** | Entrada → precio actual, ganancia o pérdida sin cerrar, stop loss y si ya va con trailing o Break-Even. |
| **Últimas operaciones** | Las 3 últimas cerradas, con su resultado en $ y en R. |

Cada bot lleva un **nombre de persona** (campo `trader` en `deploy/bots.json`): Nathalia opera el Bot Oficial, Keiner
opera Trend Rider y Celeste opera Tortuga 55. Se cambian editando ese campo; el nombre de la estrategia (`name`) sigue
debajo para no perder la referencia.

Debajo de las tarjetas sigue la **Cadena de decisión** (Datos → Capa 1 → Capa 2 → Capa 3 → Ejecución), con un
selector para ver la de cada bot y el botón "Escanear ahora".

Arriba, **Rendimiento comparado** dibuja el % de cada bot sobre su propio capital inicial (el Bot Oficial empezó con
10.000 y los otros con 500, por eso se compara en %, no en dólares). Cada bot tiene siempre el mismo color.

Con **Ver detalle** (o las pestañas de arriba) se entra en un bot: la cadena de decisión de las 3 capas, su ficha
"Cómo opera", el Kill Switch de ese bot, el historial completo y el Plan de aportes.

## Añadir un cuarto bot

No hace falta tocar el panel ni crear otro archivo de servicio.

1. **En GitHub**, edita `deploy/bots.json` y añade una ficha al final de la lista (copia la de Tortuga 55 y cambia lo
   necesario). Lo mínimo:

   ```json
   {
     "id": "cuarto",
     "name": "Mi cuarto bot",
     "trader": "Ana",
     "service": "ingresosbot-bot@cuarto",
     "db": "data/cuarto_state.db",
     "interval": "4h",
     "symbols": ["BTCUSDT", "ETHUSDT"],
     "params": "deploy/cuarto_params.json",
     "balance": 500,
     "summary": "Una frase de qué hace.",
     "how": ["Cuándo entra.", "Cuándo sale.", "Cuánto arriesga."],
     "backtest": "Qué dio el backtest."
   }
   ```

   - `id`: sin espacios ni tildes (se usa en el nombre del servicio).
   - `trader`: el nombre de persona con el que aparece en el panel (opcional).
   - `db`: una base **nueva** para ese bot; nunca la de otro.
   - `params`: un JSON con los parámetros de la estrategia (como `deploy/tortuga_params.json`), o quítalo para usar las reglas oficiales.
   - `color` (opcional): del 1 al 8 para fijar su color en el panel; si no, toma el siguiente libre por orden.

2. **En la Terminal de hPanel (VPS de Francia)**, como root, trae el cambio y enciende el bot:

   ```
   sudo git -C /opt/Ingresosbot pull && sudo bash /opt/Ingresosbot/deploy/install_almalinux.sh
   sudo systemctl enable --now ingresosbot-bot@cuarto
   ```

   El panel lo muestra solo en cuanto cambia `bots.json` (no hay que reiniciar `ingresosbot-web`). Sus alertas de
   Telegram llegan con su nombre delante, y su log queda en `logs/bot-cuarto.log`.

3. Para apagarlo: `sudo systemctl disable --now ingresosbot-bot@cuarto`. Su ficha puede quedarse en `bots.json`
   (sale como "Apagado") o borrarse; la base `data/cuarto_state.db` no se borra sola.
