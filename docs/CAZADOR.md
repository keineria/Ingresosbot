# Cazador: laboratorio de rupturas cortas en ~40 monedas

El Cazador sería un cuarto bot, más atrevido que los demás: busca arranques fuertes en velas de 15 minutos y de 1 hora
en unas 40 monedas de Binance Spot, con verificaciones antes de cada compra y topes duros. Antes de construirlo como
bot en papel, `hunter_lab.py` comprueba con datos reales de 2024-2026 si esas reglas ganan después de comisiones.
Si ninguna variante pasa, el Cazador no se construye.

Diseño completo y por qué no copiamos el ejemplo de memecoins con GMGN:
[documento "Bot atrevido de oportunidades cortas"](https://claude.ai/artifact/G1NYcnFbfRGXF8nZhbMVCN).

## Qué simula

Una sola cuenta de 500 USD que comparte el dinero entre todas las monedas, como lo haría el bot de verdad:

| Paso | Regla |
| --- | --- |
| Capa 1 | 3 pérdidas seguidas → 15 velas sin operar; 2 seguidas → riesgo a la mitad; caída del día ≥ 3% → nada más ese día; caída total ≥ 20% → el Cazador se apaga |
| Escáner | Cierre por encima del máximo de las N velas anteriores, con volumen ≥ V veces la media de 20 velas |
| Anti-trampa | Volumen de 24 h ≥ 5 M USD, la vela no concentra más del 25% del volumen del día, ≥ 7 días de historia, cierre como mucho E ATR sobre el canal (sin FOMO), cierre en la mitad alta de la vela |
| Capa 2 | BTC con el cierre diario de ayer sobre su SMA 200 y la moneda sobre su EMA 200 |
| Capa 3 | SL a k ATR, riesgo 0,75% del saldo, máx. 25% del saldo por moneda, 3 posiciones y 6 entradas por día; salida por SL, trailing o tiempo |
| Costos | 0,08% de comisión por orden y 0,05% de deslizamiento en contra |

Spread y profundidad del libro no existen en el histórico: se verifican en el bot en papel, si llega a existir.

Las variantes se eligen **solo** con la primera mitad del periodo. Las 5 mejores se miden en la segunda mitad,
en el periodo completo y en 20 grupos aleatorios de 20 monedas. Una variante pasa si el completo y la validación
son positivos, al menos 15 de 20 grupos ganan, la caída máxima es menor de 20% y hay al menos 40 operaciones.

Cuidado: las 40 monedas son las que siguen listadas hoy, así que el resultado sale un poco mejor de lo real
(las que se hundieron y Binance retiró no están). Por eso el criterio es exigente y después viene el papel.

## Cómo correrlo en el VPS (Francia)

Todo se hace en la **Terminal de hPanel** del VPS de Francia (hPanel → VPS → botón *Terminal*), como `root`.
Copia solo las líneas de comando, sin el `[root@srv... ]#` del principio.

1. Después de unir el PR en GitHub, trae el código nuevo (no hace falta reiniciar los bots: no lo usan):

   ```
   sudo git -C /opt/Ingresosbot pull
   ```

2. Descarga las velas de 15 minutos de las 40 monedas desde junio de 2023. Tarda unos 30-40 minutos y sigue
   aunque cierres la terminal:

   ```
   sudo -u ingresosbot bash -c 'cd /opt/Ingresosbot && nohup nice -n 10 .venv/bin/python hunter_lab.py download > data/cazador_descarga.log 2>&1 &'
   ```

   Para ver cómo va: `tail -f /opt/Ingresosbot/data/cazador_descarga.log` (sales con `Ctrl + C`; la descarga sigue).
   Termina cuando aparece `LDOUSDT_15m.csv: ...`.

3. Corre el laboratorio (unos 5-10 minutos, prueba 160 variantes en 15 min y 1 h):

   ```
   sudo -u ingresosbot bash -c 'cd /opt/Ingresosbot && nice -n 10 .venv/bin/python hunter_lab.py run > data/cazador_run.log 2>&1'
   ```

4. Muestra el informe y pégalo en el chat:

   ```
   cat /opt/Ingresosbot/data/cazador/informes/informe_cazador.md
   ```

Archivos que deja en `data/cazador/informes/`: `informe_cazador.md` (resumen y veredicto), `entrenamiento_15m.csv`
y `entrenamiento_1h.csv` (todas las variantes), `operaciones_mejor_*.csv` (cada operación de la mejor variante con
su razón de entrada) y, solo si alguna pasa, `cazador_params_candidato.json` para el bot en papel.

Para repetirlo más adelante con velas nuevas, vuelve a correr los pasos 2 y 3: la descarga solo baja lo que falta.
